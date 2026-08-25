"""Crash-conscious SQLite ledger for one-shot wake-up monitors."""

from __future__ import annotations

import json
import hashlib
import hmac
import math
import secrets
import sqlite3
import threading
import time
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping

from .models import (
    ConflictError,
    Evaluation,
    MonitorMode,
    MonitorState,
    TargetGuard,
    TriState,
    WakeReason,
    ValidationError,
    is_terminal,
)
from .delivery import DeliveryKind, ThreadDeliveryState
from .terminal_events import (
    EventKind,
    Heartbeat,
    TerminalEvent,
    normalize_heartbeat,
    normalize_terminal_event,
)


def canonical_json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"))


def _load(value: str | None, fallback: Any) -> Any:
    return json.loads(value) if value is not None else fallback


class EventReservationState(StrEnum):
    RESERVED = "reserved"
    BOUND = "bound"
    TERMINAL = "terminal"
    CANCELLED = "cancelled"
    EXPIRED = "expired"


def _publish_token_digest(token: str, salt: bytes) -> str:
    return hashlib.sha256(salt + token.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class EventReservationRecord:
    reservation_id: str
    idempotency_key: str | None
    kind: str
    producer_id: str
    semantic: Mapping[str, Any]
    state: EventReservationState
    token_salt: bytes
    token_digest: str
    expires_at: float
    bound_monitor_id: str | None
    heartbeat: Mapping[str, Any] | None
    heartbeat_sequence: int | None
    heartbeat_fingerprint: str | None
    terminal_event: Mapping[str, Any] | None
    terminal_fingerprint: str | None
    git_attestation: Mapping[str, Any] | None
    cancelled_at: float | None
    expired_at: float | None
    created_at: float
    updated_at: float

    @classmethod
    def from_row(cls, row: sqlite3.Row) -> "EventReservationRecord":
        try:
            kind = EventKind(str(row["kind"]))
            semantic = _load(row["semantic_json"], {})
            heartbeat = _load(row["heartbeat_json"], None)
            terminal_event = _load(row["terminal_json"], None)
            git_attestation = _load(row["attestation_json"], None)
        except (json.JSONDecodeError, KeyError, TypeError, ValueError) as exc:
            raise ValidationError("event reservation contains invalid stored JSON") from exc
        if not isinstance(semantic, Mapping):
            raise ValidationError("event reservation semantic payload is corrupt")
        reservation_id = row["reservation_id"]
        producer_id = row["producer_id"]
        idempotency_key = row["idempotency_key"]
        for value, label in (
            (reservation_id, "identity"),
            (producer_id, "producer identity"),
        ):
            if (
                not isinstance(value, str)
                or not value
                or "\0" in value
                or len(value.encode("utf-8")) > 4096
            ):
                raise ValidationError(f"event reservation {label} is corrupt")
        if idempotency_key is not None and (
            not isinstance(idempotency_key, str)
            or not idempotency_key
            or "\0" in idempotency_key
            or len(idempotency_key.encode("utf-8")) > 4096
        ):
            raise ValidationError("event reservation idempotency identity is corrupt")
        try:
            if len(canonical_json(semantic).encode("utf-8")) > 64 * 1024:
                raise ValidationError("event reservation semantic payload is oversized")
        except (TypeError, ValueError) as exc:
            raise ValidationError("event reservation semantic payload is corrupt") from exc
        token_salt = bytes(row["token_salt"])
        token_digest = str(row["token_digest"])
        if (
            len(token_salt) != 32
            or len(token_digest) != 64
            or any(character not in "0123456789abcdef" for character in token_digest)
        ):
            raise ValidationError("event reservation publish identity is corrupt")
        try:
            expires_at = float(row["expires_at"])
            created_at = float(row["created_at"])
            updated_at = float(row["updated_at"])
        except (TypeError, ValueError, OverflowError) as exc:
            raise ValidationError("event reservation timestamps are corrupt") from exc
        if not all(math.isfinite(item) for item in (expires_at, created_at, updated_at)):
            raise ValidationError("event reservation timestamps are corrupt")
        bound_monitor_id = row["bound_monitor_id"]
        if bound_monitor_id is not None and (
            not isinstance(bound_monitor_id, str) or not bound_monitor_id
        ):
            raise ValidationError("event reservation binding is corrupt")
        try:
            cancelled_at = (
                float(row["cancelled_at"])
                if row["cancelled_at"] is not None
                else None
            )
            expired_at = (
                float(row["expired_at"])
                if row["expired_at"] is not None
                else None
            )
        except (TypeError, ValueError, OverflowError) as exc:
            raise ValidationError(
                "event reservation terminal timestamps are corrupt"
            ) from exc
        if any(
            item is not None and not math.isfinite(item)
            for item in (cancelled_at, expired_at)
        ):
            raise ValidationError("event reservation terminal timestamps are corrupt")
        if cancelled_at is not None and expired_at is not None:
            raise ValidationError("event reservation has conflicting terminal facts")
        if (cancelled_at is not None or expired_at is not None) and (
            bound_monitor_id is not None or terminal_event is not None
        ):
            raise ValidationError("event reservation lifecycle facts conflict")

        try:
            heartbeat_sequence = (
                int(row["heartbeat_sequence"])
                if row["heartbeat_sequence"] is not None
                else None
            )
        except (TypeError, ValueError, OverflowError) as exc:
            raise ValidationError("event reservation heartbeat sequence is corrupt") from exc
        heartbeat_fingerprint = row["heartbeat_fingerprint"]
        if heartbeat is None:
            if heartbeat_sequence is not None or heartbeat_fingerprint is not None:
                raise ValidationError("event reservation heartbeat facts conflict")
        else:
            if not isinstance(heartbeat, Mapping):
                raise ValidationError("event reservation heartbeat is corrupt")
            heartbeat_input = dict(heartbeat)
            host_received_at = heartbeat_input.pop("host_received_at", None)
            try:
                normalized_heartbeat = normalize_heartbeat(
                    heartbeat_input, host_received_at=host_received_at
                )
            except (TypeError, ValueError, OverflowError) as exc:
                raise ValidationError("event reservation heartbeat is corrupt") from exc
            if (
                normalized_heartbeat.as_dict() != heartbeat
                or heartbeat_sequence != normalized_heartbeat.sequence
                or heartbeat_fingerprint != normalized_heartbeat.fingerprint
            ):
                raise ValidationError("event reservation heartbeat facts conflict")

        terminal_fingerprint = row["terminal_fingerprint"]
        normalized_terminal: TerminalEvent | None = None
        if terminal_event is None:
            if terminal_fingerprint is not None or git_attestation is not None:
                raise ValidationError("event reservation terminal facts conflict")
        else:
            if not isinstance(terminal_event, Mapping):
                raise ValidationError("event reservation terminal payload is corrupt")
            terminal_input = dict(terminal_event)
            host_received_at = terminal_input.pop("host_received_at", None)
            if terminal_input.pop("lead_accepted", False) is not False:
                raise ValidationError("event reservation terminal acceptance is corrupt")
            try:
                normalized_terminal = normalize_terminal_event(
                    terminal_input,
                    kind=kind,
                    host_received_at=host_received_at,
                )
            except (TypeError, ValueError, OverflowError) as exc:
                raise ValidationError("event reservation terminal payload is corrupt") from exc
            if (
                normalized_terminal.as_dict() != terminal_event
                or terminal_fingerprint != normalized_terminal.fingerprint
            ):
                raise ValidationError("event reservation terminal facts conflict")
        _validate_stored_attestation(git_attestation, normalized_terminal)

        if row["cancelled_at"] is not None:
            state = EventReservationState.CANCELLED
        elif row["expired_at"] is not None:
            state = EventReservationState.EXPIRED
        elif row["terminal_json"] is not None:
            state = EventReservationState.TERMINAL
        elif row["bound_monitor_id"] is not None:
            state = EventReservationState.BOUND
        else:
            state = EventReservationState.RESERVED
        return cls(
            reservation_id=reservation_id,
            idempotency_key=idempotency_key,
            kind=kind.value,
            producer_id=producer_id,
            semantic=semantic,
            # ``state`` remains a compatibility/index cache.  The public
            # lifecycle is derived from the orthogonal durable facts above so
            # one stale cache value cannot change authorization semantics.
            state=state,
            token_salt=token_salt,
            token_digest=token_digest,
            expires_at=expires_at,
            bound_monitor_id=bound_monitor_id,
            heartbeat=heartbeat,
            heartbeat_sequence=heartbeat_sequence,
            heartbeat_fingerprint=heartbeat_fingerprint,
            terminal_event=terminal_event,
            terminal_fingerprint=terminal_fingerprint,
            git_attestation=git_attestation,
            cancelled_at=cancelled_at,
            expired_at=expired_at,
            created_at=created_at,
            updated_at=updated_at,
        )

    def status_dict(self) -> dict[str, Any]:
        return {
            "reservation_id": self.reservation_id,
            "idempotency_key": self.idempotency_key,
            "kind": self.kind,
            "producer_id": self.producer_id,
            "semantic": dict(self.semantic),
            "state": self.state.value,
            "token_fingerprint": self.token_digest[:12],
            "expires_at": self.expires_at,
            "bound_monitor_id": self.bound_monitor_id,
            "last_heartbeat": self.heartbeat,
            "terminal_event": self.terminal_event,
            "git_attestation": self.git_attestation,
            "cancelled_at": self.cancelled_at,
            "expired_at": self.expired_at,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
        }


def _validate_stored_attestation(
    value: Any, terminal: TerminalEvent | None
) -> None:
    if value is None:
        return
    if (
        terminal is None
        or terminal.kind is not EventKind.WORKER_TERMINAL
        or getattr(getattr(terminal, "outcome", None), "value", None) != "delivered"
    ):
        raise ValidationError("event reservation attestation has no delivery")
    if not isinstance(value, Mapping):
        raise ValidationError("event reservation attestation is corrupt")
    allowed = {
        "status",
        "candidate_commit",
        "path_count",
        "paths",
        "paths_digest",
        "paths_truncated",
        "path_bytes_truncated",
        "error",
        "lead_accepted",
    }
    if set(value) != allowed or value.get("lead_accepted") is not False:
        raise ValidationError("event reservation attestation shape is corrupt")
    status = value.get("status")
    if status not in {
        "valid",
        "invalid_commit",
        "baseline_mismatch",
        "out_of_scope",
        "attestation_error",
    }:
        raise ValidationError("event reservation attestation status is corrupt")
    candidate = value.get("candidate_commit")
    if candidate != getattr(terminal, "candidate_oid", None):
        raise ValidationError("event reservation attestation candidate is corrupt")
    path_count = value.get("path_count")
    paths = value.get("paths")
    if (
        isinstance(path_count, bool)
        or not isinstance(path_count, int)
        or not 0 <= path_count <= 8 * 1024 * 1024
        or not isinstance(paths, list)
        or len(paths) > 256
        or len(paths) > path_count
    ):
        raise ValidationError("event reservation attestation path facts are corrupt")
    path_bytes = 0
    for path in paths:
        if not isinstance(path, str) or "\0" in path:
            raise ValidationError("event reservation attestation path is corrupt")
        path_bytes += len(path.encode("utf-8", errors="surrogatepass"))
    if path_bytes > 64 * 1024:
        raise ValidationError("event reservation attestation paths are oversized")
    for key in ("paths_truncated", "path_bytes_truncated"):
        if not isinstance(value.get(key), bool):
            raise ValidationError("event reservation attestation truncation is corrupt")
    digest = value.get("paths_digest")
    if digest is not None and (
        not isinstance(digest, str)
        or len(digest) != 64
        or any(character not in "0123456789abcdef" for character in digest)
    ):
        raise ValidationError("event reservation attestation digest is corrupt")
    if status in {"valid", "out_of_scope"} and digest is None:
        raise ValidationError("event reservation attestation digest is missing")
    error = value.get("error")
    if error is not None and (
        not isinstance(error, str) or len(error.encode("utf-8")) > 4096
    ):
        raise ValidationError("event reservation attestation error is corrupt")


@dataclass(frozen=True)
class MonitorRecord:
    """One durable monitor row, decoded at the storage boundary."""

    monitor_id: str
    idempotency_key: str | None
    semantic: Mapping[str, Any]
    target: TargetGuard | None
    condition: Mapping[str, Any]
    state: MonitorState
    mode: MonitorMode
    idle_barrier: bool
    allow_heuristic_continuation: bool
    expires_at: float
    evidence: Mapping[str, Any]
    witness: tuple[Mapping[str, Any], ...]
    outcome: Mapping[str, Any] | None
    activation_snapshot: Mapping[str, Any] | None
    created_at: float
    updated_at: float
    wake_reason: WakeReason | None = None
    rearm_of: str | None = None
    evaluation_count: int = 0
    armed_at: float | None = None
    delivery: Mapping[str, Any] | None = None
    delivery_state: str | None = None
    admission_attempted_at: float | None = None
    queue_receipt: Mapping[str, Any] | None = None
    reconciliation: Mapping[str, Any] | None = None
    delivery_outcome: Mapping[str, Any] | None = None

    @classmethod
    def from_row(cls, row: sqlite3.Row) -> "MonitorRecord":
        delivery = _load(row["delivery_json"], None)
        target_value = _load(row["target_json"], {})
        target = (
            None
            if isinstance(delivery, Mapping)
            and delivery.get("kind") == DeliveryKind.THREAD
            else TargetGuard.from_dict(target_value)
        )
        return cls(
            monitor_id=str(row["monitor_id"]),
            idempotency_key=row["idempotency_key"],
            semantic=_load(row["semantic_json"], {}),
            target=target,
            condition=_load(row["condition_json"], {}),
            state=MonitorState(row["state"]),
            mode=MonitorMode(row["mode"]),
            idle_barrier=bool(row["idle_barrier"]),
            allow_heuristic_continuation=bool(row["allow_heuristic_continuation"]),
            expires_at=float(row["expires_at"]),
            evidence=_load(row["evidence_json"], {}),
            witness=tuple(_load(row["witness_json"], [])),
            outcome=_load(row["outcome_json"], None),
            activation_snapshot=_load(row["activation_snapshot_json"], None),
            created_at=float(row["created_at"]),
            updated_at=float(row["updated_at"]),
            wake_reason=(
                WakeReason(row["wake_reason"])
                if row["wake_reason"] is not None
                else None
            ),
            rearm_of=row["rearm_of"],
            evaluation_count=int(row["evaluation_count"] or 0),
            armed_at=(
                float(row["armed_at"]) if row["armed_at"] is not None else None
            ),
            delivery=delivery,
            delivery_state=row["delivery_state"],
            admission_attempted_at=(
                float(row["admission_attempted_at"])
                if row["admission_attempted_at"] is not None
                else None
            ),
            queue_receipt=_load(row["queue_receipt_json"], None),
            reconciliation=_load(row["reconciliation_json"], None),
            delivery_outcome=_load(row["delivery_outcome_json"], None),
        )

    @property
    def delivery_kind(self) -> DeliveryKind:
        if isinstance(self.delivery, Mapping):
            return DeliveryKind(str(self.delivery.get("kind")))
        return DeliveryKind.GOAL

    @property
    def thread_delivery_state(self) -> ThreadDeliveryState | None:
        if self.delivery_kind != DeliveryKind.THREAD:
            return None
        return ThreadDeliveryState(
            self.delivery_state or ThreadDeliveryState.UNATTEMPTED
        )

    def status_dict(self) -> dict[str, Any]:
        return {
            "monitor_id": self.monitor_id,
            "idempotency_key": self.idempotency_key,
            "target": (
                self.target.as_dict()
                if self.target is not None
                else {"thread_id": str((self.delivery or {}).get("thread_id", ""))}
            ),
            "condition": self.condition,
            "state": self.state.value,
            "mode": self.mode.value,
            "idle_barrier": self.idle_barrier,
            "allow_heuristic_continuation": self.allow_heuristic_continuation,
            "expires_at": self.expires_at,
            "evidence": self.evidence,
            "witness": list(self.witness),
            "outcome": self.outcome,
            "activation_snapshot": self.activation_snapshot,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "wake_reason": self.wake_reason.value if self.wake_reason else None,
            "rearm_of": self.rearm_of,
            "evaluation_count": self.evaluation_count,
            "armed_at": self.armed_at,
            "delivery_kind": self.delivery_kind.value,
            "delivery": self.delivery,
            "delivery_state": self.delivery_state,
            "admission_attempted_at": self.admission_attempted_at,
            "queue_receipt": self.queue_receipt,
            "reconciliation": self.reconciliation,
            "delivery_outcome": self.delivery_outcome,
        }


class Ledger:
    """The sole durable authority for monitor state transitions.

    Every public transition uses a SQL compare-and-set in a short
    ``BEGIN IMMEDIATE`` transaction. In particular, the state changes to
    ``activating`` before any app-server packet is sent, so recovery consumes
    the attempt instead of guessing that another send is safe.
    """

    def __init__(self, root: Path):
        self.root = root
        self.path = root / "monitors.sqlite3"
        self._lock = threading.RLock()
        self._connection = sqlite3.connect(
            self.path,
            timeout=10.0,
            isolation_level=None,
            check_same_thread=False,
        )
        self._connection.row_factory = sqlite3.Row
        self._initialize()

    def close(self) -> None:
        with self._lock:
            self._connection.close()

    def _initialize(self) -> None:
        connection = self._connection
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("PRAGMA synchronous=FULL")
        connection.execute("PRAGMA foreign_keys=ON")
        connection.execute("PRAGMA busy_timeout=10000")
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS monitors (
                monitor_id TEXT PRIMARY KEY,
                idempotency_key TEXT UNIQUE,
                semantic_json TEXT NOT NULL,
                target_json TEXT NOT NULL,
                condition_json TEXT NOT NULL,
                state TEXT NOT NULL,
                mode TEXT NOT NULL DEFAULT 'legacy',
                idle_barrier INTEGER NOT NULL DEFAULT 0,
                allow_heuristic_continuation INTEGER NOT NULL,
                expires_at REAL NOT NULL,
                evidence_json TEXT NOT NULL DEFAULT '{}',
                witness_json TEXT NOT NULL DEFAULT '[]',
                outcome_json TEXT,
                activation_snapshot_json TEXT,
                created_at REAL NOT NULL,
                updated_at REAL NOT NULL,
                wake_reason TEXT,
                rearm_of TEXT,
                evaluation_count INTEGER NOT NULL DEFAULT 0,
                armed_at REAL
                ,delivery_json TEXT
                ,delivery_state TEXT
                ,admission_attempted_at REAL
                ,queue_receipt_json TEXT
                ,reconciliation_json TEXT
                ,delivery_outcome_json TEXT
            )
            """
        )
        columns = {
            str(row["name"])
            for row in connection.execute("PRAGMA table_info(monitors)").fetchall()
        }
        if "mode" not in columns:
            connection.execute(
                "ALTER TABLE monitors ADD COLUMN mode TEXT NOT NULL DEFAULT 'legacy'"
            )
        if "idle_barrier" not in columns:
            connection.execute(
                "ALTER TABLE monitors ADD COLUMN idle_barrier INTEGER NOT NULL DEFAULT 0"
            )
        # Wake-reason bookkeeping is additive: every column either defaults or
        # stays NULL, so rows written before this change load unchanged.
        if "wake_reason" not in columns:
            connection.execute("ALTER TABLE monitors ADD COLUMN wake_reason TEXT")
        if "rearm_of" not in columns:
            connection.execute("ALTER TABLE monitors ADD COLUMN rearm_of TEXT")
        if "evaluation_count" not in columns:
            connection.execute(
                "ALTER TABLE monitors ADD COLUMN evaluation_count INTEGER NOT NULL DEFAULT 0"
            )
        if "armed_at" not in columns:
            connection.execute("ALTER TABLE monitors ADD COLUMN armed_at REAL")
        for name, declaration in (
            ("delivery_json", "TEXT"),
            ("delivery_state", "TEXT"),
            ("admission_attempted_at", "REAL"),
            ("queue_receipt_json", "TEXT"),
            ("reconciliation_json", "TEXT"),
            ("delivery_outcome_json", "TEXT"),
        ):
            if name not in columns:
                connection.execute(
                    f"ALTER TABLE monitors ADD COLUMN {name} {declaration}"
                )
        connection.execute(
            "CREATE INDEX IF NOT EXISTS monitors_state_expiry ON monitors(state, expires_at)"
        )
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS event_reservations (
                reservation_id TEXT PRIMARY KEY,
                idempotency_key TEXT UNIQUE,
                kind TEXT NOT NULL,
                producer_id TEXT NOT NULL,
                semantic_json TEXT NOT NULL,
                state TEXT NOT NULL,
                token_salt BLOB NOT NULL,
                token_digest TEXT NOT NULL,
                expires_at REAL NOT NULL,
                bound_monitor_id TEXT UNIQUE,
                heartbeat_json TEXT,
                heartbeat_sequence INTEGER,
                heartbeat_fingerprint TEXT,
                terminal_json TEXT,
                terminal_fingerprint TEXT,
                attestation_json TEXT,
                cancelled_at REAL,
                expired_at REAL,
                created_at REAL NOT NULL,
                updated_at REAL NOT NULL
            )
            """
        )
        event_columns = {
            str(row["name"])
            for row in connection.execute(
                "PRAGMA table_info(event_reservations)"
            ).fetchall()
        }
        for name, declaration in (
            ("heartbeat_json", "TEXT"),
            ("heartbeat_sequence", "INTEGER"),
            ("heartbeat_fingerprint", "TEXT"),
            ("terminal_json", "TEXT"),
            ("terminal_fingerprint", "TEXT"),
            ("attestation_json", "TEXT"),
            ("cancelled_at", "REAL"),
            ("expired_at", "REAL"),
        ):
            if name not in event_columns:
                connection.execute(
                    f"ALTER TABLE event_reservations ADD COLUMN {name} {declaration}"
                )
        connection.execute(
            "CREATE TABLE IF NOT EXISTS metadata (key TEXT PRIMARY KEY, value TEXT NOT NULL)"
        )
        canonical_root = str(self.root.resolve())
        existing = connection.execute(
            "SELECT value FROM metadata WHERE key = 'runtime_root'"
        ).fetchone()
        if existing is None:
            connection.execute(
                "INSERT INTO metadata(key, value) VALUES ('runtime_root', ?)",
                (canonical_root,),
            )
        elif existing["value"] != canonical_root:
            raise RuntimeError(
                "runtime root mismatch in durable ledger: "
                f"{existing['value']} != {canonical_root}"
            )

    def _transaction(self) -> sqlite3.Connection:
        self._lock.acquire()
        try:
            self._connection.execute("BEGIN IMMEDIATE")
            return self._connection
        except BaseException:
            self._lock.release()
            raise

    def _commit(self) -> None:
        try:
            self._connection.execute("COMMIT")
        finally:
            self._lock.release()

    def _rollback(self) -> None:
        try:
            self._connection.execute("ROLLBACK")
        finally:
            self._lock.release()

    def _record(self, row: sqlite3.Row | None) -> MonitorRecord | None:
        return MonitorRecord.from_row(row) if row is not None else None

    def get(self, monitor_id: str) -> MonitorRecord | None:
        with self._lock:
            row = self._connection.execute(
                "SELECT * FROM monitors WHERE monitor_id = ?", (monitor_id,)
            ).fetchone()
        return self._record(row)

    def get_event(self, reservation_id: str) -> EventReservationRecord | None:
        with self._lock:
            row = self._connection.execute(
                "SELECT * FROM event_reservations WHERE reservation_id = ?",
                (reservation_id,),
            ).fetchone()
        return EventReservationRecord.from_row(row) if row is not None else None

    def event_for_monitor(self, monitor_id: str) -> EventReservationRecord | None:
        with self._lock:
            rows = self._connection.execute(
                "SELECT * FROM event_reservations WHERE bound_monitor_id = ?",
                (monitor_id,),
            ).fetchall()
        if len(rows) > 1:
            raise ConflictError("monitor is bound to multiple event reservations")
        return EventReservationRecord.from_row(rows[0]) if rows else None

    def event_capability_required(self) -> bool:
        from .conditions import contains_event_condition

        nonterminal = tuple(
            state.value for state in MonitorState if not is_terminal(state)
        )
        placeholders = ",".join("?" for _ in nonterminal)
        with self._lock:
            rows = self._connection.execute(
                f"""
                SELECT monitor.condition_json,
                       EXISTS(
                           SELECT 1
                           FROM event_reservations AS event
                           WHERE event.bound_monitor_id = monitor.monitor_id
                       ) AS has_bound_event
                FROM monitors AS monitor
                WHERE monitor.state IN ({placeholders})
                """,
                nonterminal,
            ).fetchall()
        for row in rows:
            if bool(row["has_bound_event"]):
                return True
            try:
                condition = json.loads(str(row["condition_json"]))
            except (json.JSONDecodeError, TypeError, ValueError):
                return True
            if not isinstance(condition, Mapping):
                return True
            if contains_event_condition(condition):
                return True
        return False

    def delivery_capability_required(self) -> bool:
        """Return whether an older daemon would misread live ThreadDelivery."""

        return any(
            record.delivery_kind == DeliveryKind.THREAD
            for record in self.list(include_terminal=False)
        )

    def reserve_event(
        self,
        *,
        reservation_id: str,
        idempotency_key: str | None,
        kind: str,
        producer_id: str,
        semantic: Mapping[str, Any],
        publish_token: str,
        expires_at: float,
        now: float | None = None,
    ) -> tuple[EventReservationRecord, bool]:
        observed_at = time.time() if now is None else float(now)
        salt = secrets.token_bytes(32)
        digest = _publish_token_digest(publish_token, salt)
        transaction = self._transaction()
        try:
            if idempotency_key is not None:
                existing_row = transaction.execute(
                    "SELECT * FROM event_reservations WHERE idempotency_key = ?",
                    (idempotency_key,),
                ).fetchone()
                if existing_row is not None:
                    existing = EventReservationRecord.from_row(existing_row)
                    if (
                        existing.kind != kind
                        or existing.producer_id != producer_id
                        or existing.semantic != semantic
                        or existing.expires_at != float(expires_at)
                    ):
                        raise ConflictError(
                            "idempotency key already belongs to an event reservation "
                            "with different semantics"
                        )
                    self._commit()
                    return existing, False
            transaction.execute(
                """
                INSERT INTO event_reservations(
                    reservation_id, idempotency_key, kind, producer_id,
                    semantic_json, state, token_salt, token_digest, expires_at,
                    created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    reservation_id,
                    idempotency_key,
                    kind,
                    producer_id,
                    canonical_json(semantic),
                    EventReservationState.RESERVED.value,
                    salt,
                    digest,
                    float(expires_at),
                    observed_at,
                    observed_at,
                ),
            )
            row = transaction.execute(
                "SELECT * FROM event_reservations WHERE reservation_id = ?",
                (reservation_id,),
            ).fetchone()
            self._commit()
            assert row is not None
            return EventReservationRecord.from_row(row), True
        except BaseException:
            self._rollback()
            raise

    def cancel_event(
        self, reservation_id: str, *, now: float | None = None
    ) -> EventReservationRecord:
        observed_at = time.time() if now is None else float(now)
        transaction = self._transaction()
        try:
            row = transaction.execute(
                "SELECT * FROM event_reservations WHERE reservation_id = ?",
                (reservation_id,),
            ).fetchone()
            if row is None:
                raise ConflictError(f"unknown event reservation: {reservation_id}")
            current = EventReservationRecord.from_row(row)
            if current.state == EventReservationState.CANCELLED:
                self._commit()
                return current
            if current.state == EventReservationState.EXPIRED:
                self._commit()
                return current
            if current.state != EventReservationState.RESERVED:
                raise ConflictError("only an unbound live reservation can be cancelled")
            terminal_fact = (
                EventReservationState.EXPIRED
                if observed_at >= current.expires_at
                else EventReservationState.CANCELLED
            )
            timestamp_column = (
                "expired_at"
                if terminal_fact is EventReservationState.EXPIRED
                else "cancelled_at"
            )
            cursor = transaction.execute(
                """
                UPDATE event_reservations
                SET state = ?, """
                + timestamp_column
                + """ = ?, updated_at = ?
                WHERE reservation_id = ?
                  AND bound_monitor_id IS NULL
                  AND terminal_json IS NULL
                  AND cancelled_at IS NULL
                  AND expired_at IS NULL
                """,
                (
                    terminal_fact.value,
                    observed_at,
                    observed_at,
                    reservation_id,
                ),
            )
            if cursor.rowcount != 1:
                raise ConflictError("event cancellation lost its lifecycle race")
            row = transaction.execute(
                "SELECT * FROM event_reservations WHERE reservation_id = ?",
                (reservation_id,),
            ).fetchone()
            self._commit()
            assert row is not None
            return EventReservationRecord.from_row(row)
        except BaseException:
            self._rollback()
            raise

    def expire_events(self, *, now: float | None = None) -> int:
        observed_at = time.time() if now is None else float(now)
        transaction = self._transaction()
        try:
            cursor = transaction.execute(
                """
                UPDATE event_reservations
                SET state = ?, expired_at = ?, updated_at = ?
                WHERE bound_monitor_id IS NULL
                  AND terminal_json IS NULL
                  AND cancelled_at IS NULL
                  AND expired_at IS NULL
                  AND expires_at <= ?
                """,
                (
                    EventReservationState.EXPIRED.value,
                    observed_at,
                    observed_at,
                    observed_at,
                ),
            )
            changed = int(cursor.rowcount)
            self._commit()
            return changed
        except BaseException:
            self._rollback()
            raise

    def _authenticated_event_row(
        self,
        transaction: sqlite3.Connection,
        reservation_id: str,
        publish_token: str,
    ) -> sqlite3.Row:
        row = transaction.execute(
            "SELECT * FROM event_reservations WHERE reservation_id = ?",
            (reservation_id,),
        ).fetchone()
        if row is None:
            raise ConflictError(f"unknown event reservation: {reservation_id}")
        expected = str(row["token_digest"])
        actual = _publish_token_digest(publish_token, bytes(row["token_salt"]))
        if not hmac.compare_digest(expected, actual):
            raise ConflictError("invalid event publish capability")
        return row

    def _ensure_event_writable(
        self,
        transaction: sqlite3.Connection,
        record: EventReservationRecord,
        *,
        now: float,
    ) -> None:
        if record.cancelled_at is not None or record.expired_at is not None:
            raise ConflictError("event reservation is no longer writable")
        if record.bound_monitor_id is None:
            if now >= record.expires_at:
                raise ConflictError("unbound event reservation has expired")
            return
        monitor_row = transaction.execute(
            "SELECT state FROM monitors WHERE monitor_id = ?",
            (record.bound_monitor_id,),
        ).fetchone()
        if monitor_row is None:
            raise ConflictError("bound event monitor is missing")
        writable_monitor_states = {
            MonitorState.REGISTERING,
            MonitorState.DEFER_INTENT,
            MonitorState.PAUSING,
            MonitorState.ARMED,
        }
        if MonitorState(str(monitor_row["state"])) not in writable_monitor_states:
            raise ConflictError("bound event monitor has consumed its event window")

    def publish_event_heartbeat(
        self,
        reservation_id: str,
        *,
        publish_token: str,
        heartbeat: Heartbeat | Mapping[str, Any],
        now: float | None = None,
    ) -> EventReservationRecord:
        observed_at = time.time() if now is None else float(now)
        incoming = normalize_heartbeat(heartbeat, host_received_at=observed_at)
        transaction = self._transaction()
        try:
            row = self._authenticated_event_row(
                transaction, reservation_id, publish_token
            )
            current = EventReservationRecord.from_row(row)
            self._ensure_event_writable(transaction, current, now=observed_at)
            if current.terminal_event is not None:
                raise ConflictError("terminal event already published")
            if current.heartbeat_sequence is not None:
                if incoming.sequence < current.heartbeat_sequence:
                    raise ConflictError("heartbeat sequence moved backwards")
                if incoming.sequence == current.heartbeat_sequence:
                    if incoming.fingerprint != current.heartbeat_fingerprint:
                        raise ConflictError(
                            "heartbeat sequence was reused with different evidence"
                        )
                    self._commit()
                    return current
            transaction.execute(
                """
                UPDATE event_reservations
                SET heartbeat_json = ?, heartbeat_sequence = ?,
                    heartbeat_fingerprint = ?, updated_at = ?
                WHERE reservation_id = ?
                """,
                (
                    canonical_json(incoming.as_dict()),
                    incoming.sequence,
                    incoming.fingerprint,
                    observed_at,
                    reservation_id,
                ),
            )
            updated = transaction.execute(
                "SELECT * FROM event_reservations WHERE reservation_id = ?",
                (reservation_id,),
            ).fetchone()
            self._commit()
            assert updated is not None
            return EventReservationRecord.from_row(updated)
        except BaseException:
            self._rollback()
            raise

    def publish_terminal_event(
        self,
        reservation_id: str,
        *,
        publish_token: str,
        terminal_event: TerminalEvent | Mapping[str, Any],
        git_attestation: Mapping[str, Any] | None = None,
        now: float | None = None,
    ) -> EventReservationRecord:
        observed_at = time.time() if now is None else float(now)
        transaction = self._transaction()
        try:
            row = self._authenticated_event_row(
                transaction, reservation_id, publish_token
            )
            current = EventReservationRecord.from_row(row)
            incoming = normalize_terminal_event(
                terminal_event,
                kind=EventKind(current.kind),
                host_received_at=observed_at,
            )
            if current.terminal_event is not None:
                if incoming.fingerprint != current.terminal_fingerprint:
                    raise ConflictError(
                        "terminal event is immutable after first publication"
                    )
                self._commit()
                return current
            self._ensure_event_writable(transaction, current, now=observed_at)
            transaction.execute(
                """
                UPDATE event_reservations
                SET state = ?, terminal_json = ?, terminal_fingerprint = ?,
                    attestation_json = ?, updated_at = ?
                WHERE reservation_id = ? AND terminal_json IS NULL
                """,
                (
                    EventReservationState.TERMINAL.value,
                    canonical_json(incoming.as_dict()),
                    incoming.fingerprint,
                    (
                        canonical_json(git_attestation)
                        if git_attestation is not None
                        else None
                    ),
                    observed_at,
                    reservation_id,
                ),
            )
            updated = transaction.execute(
                "SELECT * FROM event_reservations WHERE reservation_id = ?",
                (reservation_id,),
            ).fetchone()
            self._commit()
            assert updated is not None
            return EventReservationRecord.from_row(updated)
        except BaseException:
            self._rollback()
            raise

    def get_by_idempotency_key(self, idempotency_key: str) -> MonitorRecord | None:
        with self._lock:
            row = self._connection.execute(
                "SELECT * FROM monitors WHERE idempotency_key = ?", (idempotency_key,)
            ).fetchone()
        return self._record(row)

    def list(self, *, include_terminal: bool = True) -> list[MonitorRecord]:
        with self._lock:
            if include_terminal:
                rows = self._connection.execute(
                    "SELECT * FROM monitors ORDER BY created_at ASC"
                ).fetchall()
            else:
                placeholders = ",".join("?" for _ in MonitorState if is_terminal(_))
                rows = self._connection.execute(
                    f"SELECT * FROM monitors WHERE state NOT IN ({placeholders}) ORDER BY created_at ASC",
                    tuple(state.value for state in MonitorState if is_terminal(state)),
                ).fetchall()
        return [MonitorRecord.from_row(row) for row in rows]

    def create_or_get(
        self,
        *,
        monitor_id: str,
        idempotency_key: str | None,
        semantic: Mapping[str, Any],
        target: TargetGuard | None,
        condition: Mapping[str, Any],
        allow_heuristic_continuation: bool,
        expires_at: float,
        delivery: Mapping[str, Any] | None = None,
        mode: MonitorMode = MonitorMode.LEGACY,
        idle_barrier: bool = False,
        initial_state: MonitorState = MonitorState.REGISTERING,
        rearm_of: str | None = None,
        event_reservation_id: str | None = None,
        event_kind: str | None = None,
        now: float | None = None,
    ) -> tuple[MonitorRecord, bool]:
        """Create a monitor intent, or return its identical keyed ancestor."""

        observed_at = time.time() if now is None else float(now)
        if (event_reservation_id is None) != (event_kind is None):
            raise ConflictError(
                "event reservation ID and event kind must be provided together"
            )
        semantic_json = canonical_json(semantic)
        transaction = self._transaction()
        try:
            if idempotency_key is not None:
                row = transaction.execute(
                    "SELECT * FROM monitors WHERE idempotency_key = ?", (idempotency_key,)
                ).fetchone()
                if row is not None:
                    existing = MonitorRecord.from_row(row)
                    if existing.mode != mode or existing.semantic != semantic:
                        raise ConflictError(
                            "idempotency key already belongs to a monitor with different semantics"
                        )
                    if event_reservation_id is not None:
                        self._bind_event_in_transaction(
                            transaction,
                            reservation_id=event_reservation_id,
                            event_kind=str(event_kind),
                            monitor_id=existing.monitor_id,
                            now=observed_at,
                        )
                    self._commit()
                    return existing, False
            if target is None:
                if not isinstance(delivery, Mapping) or delivery.get("kind") != DeliveryKind.THREAD:
                    raise ConflictError("a monitor without a goal guard must be ThreadDelivery")
                target_json = canonical_json({"thread_id": delivery.get("thread_id")})
            else:
                if delivery is not None:
                    raise ConflictError(
                        "a monitor must select exactly one delivery kind"
                    )
                target_json = canonical_json(target.as_dict())
            transaction.execute(
                """
                INSERT INTO monitors(
                    monitor_id, idempotency_key, semantic_json, target_json,
                    condition_json, state, mode, idle_barrier,
                    allow_heuristic_continuation, expires_at, created_at,
                    updated_at, rearm_of, delivery_json, delivery_state
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    monitor_id,
                    idempotency_key,
                    semantic_json,
                    target_json,
                    canonical_json(condition),
                    initial_state.value,
                    mode.value,
                    int(idle_barrier),
                    int(allow_heuristic_continuation),
                    expires_at,
                    observed_at,
                    observed_at,
                    rearm_of,
                    canonical_json(delivery) if delivery is not None else None,
                    (
                        ThreadDeliveryState.UNATTEMPTED.value
                        if delivery is not None
                        and delivery.get("kind") == DeliveryKind.THREAD
                        else None
                    ),
                ),
            )
            if event_reservation_id is not None:
                self._bind_event_in_transaction(
                    transaction,
                    reservation_id=event_reservation_id,
                    event_kind=str(event_kind),
                    monitor_id=monitor_id,
                    now=observed_at,
                )
            row = transaction.execute(
                "SELECT * FROM monitors WHERE monitor_id = ?", (monitor_id,)
            ).fetchone()
            self._commit()
            assert row is not None
            return MonitorRecord.from_row(row), True
        except BaseException:
            self._rollback()
            raise

    def begin_thread_admission(
        self,
        monitor_id: str,
        *,
        capability: Mapping[str, Any],
        now: float | None = None,
    ) -> MonitorRecord | None:
        """Consume the sole queue-add attempt before any transport write."""

        observed_at = time.time() if now is None else float(now)
        transaction = self._transaction()
        try:
            row = transaction.execute(
                "SELECT * FROM monitors WHERE monitor_id = ?", (monitor_id,)
            ).fetchone()
            if row is None:
                self._commit()
                return None
            current = MonitorRecord.from_row(row)
            if (
                current.state != MonitorState.CLAIMED
                or current.delivery_kind != DeliveryKind.THREAD
                or current.thread_delivery_state != ThreadDeliveryState.UNATTEMPTED
                or current.admission_attempted_at is not None
            ):
                self._commit()
                return None
            delivery = dict(current.delivery or {})
            delivery["capability_at_admission"] = dict(capability)
            transaction.execute(
                """
                UPDATE monitors
                SET state = ?, delivery_json = ?, delivery_state = ?,
                    admission_attempted_at = ?, updated_at = ?
                WHERE monitor_id = ? AND state = ?
                  AND admission_attempted_at IS NULL
                """,
                (
                    MonitorState.ADMISSION_IN_PROGRESS.value,
                    canonical_json(delivery),
                    ThreadDeliveryState.ADMISSION_IN_PROGRESS.value,
                    observed_at,
                    observed_at,
                    monitor_id,
                    MonitorState.CLAIMED.value,
                ),
            )
            updated = transaction.execute(
                "SELECT * FROM monitors WHERE monitor_id = ?", (monitor_id,)
            ).fetchone()
            self._commit()
            assert updated is not None
            return MonitorRecord.from_row(updated)
        except BaseException:
            self._rollback()
            raise

    def update_thread_delivery(
        self,
        monitor_id: str,
        *,
        expected: Iterable[MonitorState],
        state: MonitorState,
        delivery_state: ThreadDeliveryState,
        queue_receipt: Mapping[str, Any] | None = None,
        reconciliation: Mapping[str, Any] | None = None,
        delivery_outcome: Mapping[str, Any] | None = None,
        now: float | None = None,
    ) -> MonitorRecord | None:
        """Atomically persist one typed thread-delivery fact."""

        allowed = tuple(item.value for item in expected)
        if not allowed:
            raise ValueError("thread delivery update requires an expected state")
        observed_at = time.time() if now is None else float(now)
        assignments = [
            "state = ?",
            "delivery_state = ?",
            "updated_at = ?",
        ]
        parameters: list[Any] = [state.value, delivery_state.value, observed_at]
        if queue_receipt is not None:
            assignments.append("queue_receipt_json = ?")
            parameters.append(canonical_json(queue_receipt))
        if reconciliation is not None:
            assignments.append("reconciliation_json = ?")
            parameters.append(canonical_json(reconciliation))
        if delivery_outcome is not None:
            assignments.append("delivery_outcome_json = ?")
            parameters.append(canonical_json(delivery_outcome))
            assignments.append("outcome_json = ?")
            parameters.append(canonical_json(delivery_outcome))
        placeholders = ",".join("?" for _ in allowed)
        parameters.extend([monitor_id, *allowed])
        transaction = self._transaction()
        try:
            cursor = transaction.execute(
                f"UPDATE monitors SET {', '.join(assignments)} "
                f"WHERE monitor_id = ? AND state IN ({placeholders}) "
                "AND delivery_json IS NOT NULL",
                tuple(parameters),
            )
            if cursor.rowcount != 1:
                self._commit()
                return None
            row = transaction.execute(
                "SELECT * FROM monitors WHERE monitor_id = ?", (monitor_id,)
            ).fetchone()
            self._commit()
            assert row is not None
            return MonitorRecord.from_row(row)
        except BaseException:
            self._rollback()
            raise

    def record_thread_queue_ack(
        self,
        monitor_id: str,
        *,
        receipt: Mapping[str, Any],
        now: float | None = None,
    ) -> MonitorRecord | None:
        return self.update_thread_delivery(
            monitor_id,
            expected=(MonitorState.ADMISSION_IN_PROGRESS,),
            state=MonitorState.QUEUE_ACCEPTED,
            delivery_state=ThreadDeliveryState.QUEUE_ACCEPTED,
            queue_receipt=receipt,
            now=now,
        )

    def begin_thread_cancellation(
        self,
        monitor_id: str,
        *,
        reconciliation: Mapping[str, Any],
        now: float | None = None,
    ) -> MonitorRecord | None:
        return self.update_thread_delivery(
            monitor_id,
            expected=(MonitorState.CANCEL_REQUESTED,),
            state=MonitorState.CANCELLATION_IN_PROGRESS,
            delivery_state=ThreadDeliveryState.CANCELLATION_IN_PROGRESS,
            reconciliation=reconciliation,
            now=now,
        )

    def _bind_event_in_transaction(
        self,
        transaction: sqlite3.Connection,
        *,
        reservation_id: str,
        event_kind: str,
        monitor_id: str,
        now: float,
    ) -> None:
        row = transaction.execute(
            "SELECT * FROM event_reservations WHERE reservation_id = ?",
            (reservation_id,),
        ).fetchone()
        if row is None:
            raise ConflictError(f"unknown event reservation: {reservation_id}")
        event = EventReservationRecord.from_row(row)
        if event.kind != event_kind:
            raise ConflictError("event reservation kind does not match condition")
        if event.bound_monitor_id is not None:
            if event.bound_monitor_id == monitor_id:
                return
            raise ConflictError("event reservation is already bound to another monitor")
        if event.cancelled_at is not None or event.expired_at is not None:
            raise ConflictError("event reservation cannot be bound in its terminal state")
        if now >= event.expires_at:
            raise ConflictError("event reservation bind deadline has expired")
        state = (
            EventReservationState.TERMINAL
            if event.terminal_event is not None
            else EventReservationState.BOUND
        )
        transaction.execute(
            """
            UPDATE event_reservations
            SET bound_monitor_id = ?, state = ?, updated_at = ?
            WHERE reservation_id = ? AND bound_monitor_id IS NULL
            """,
            (monitor_id, state.value, now, reservation_id),
        )

    def evaluate_and_claim_event_monitor(
        self,
        monitor_id: str,
        *,
        expected_evaluation_count: int,
        observed_condition: Mapping[str, Any],
        external_evaluations: Mapping[tuple[int, ...], Evaluation],
        contract_error: str | None = None,
        now_factory: Callable[[], float] = time.time,
    ) -> MonitorRecord | None:
        """Persist event evaluation and its optional claim in one transaction."""

        from .conditions import (
            evaluate_event_condition_tree,
            witness_authorizes_continuation,
        )

        transaction = self._transaction()
        try:
            row = transaction.execute(
                "SELECT * FROM monitors WHERE monitor_id = ?", (monitor_id,)
            ).fetchone()
            if row is None:
                self._commit()
                return None
            current = MonitorRecord.from_row(row)
            if (
                current.state != MonitorState.ARMED
                or current.evaluation_count != expected_evaluation_count
            ):
                self._commit()
                return None
            event_rows = transaction.execute(
                "SELECT * FROM event_reservations WHERE bound_monitor_id = ?",
                (monitor_id,),
            ).fetchall()
            snapshot_error: str | None = None
            snapshot_kind = "corrupt_bound_reservation"
            event_status: Mapping[str, Any] | None = None
            if not event_rows:
                snapshot_kind = "missing_bound_reservation"
                snapshot_error = "bound event reservation is missing"
            elif len(event_rows) != 1:
                snapshot_error = "bound event reservation is missing or duplicated"
            else:
                try:
                    event_status = EventReservationRecord.from_row(
                        event_rows[0]
                    ).status_dict()
                except Exception as exc:
                    snapshot_error = str(exc)
            observed_at = float(now_factory())
            condition = json.loads(canonical_json(observed_condition))
            if snapshot_error is not None:
                evaluation = Evaluation(
                    value=TriState.UNKNOWN,
                    evidence={
                        "type": "event_reservation",
                        "kind": snapshot_kind,
                        "error": snapshot_error,
                    },
                    fatal=True,
                )
            elif contract_error is not None:
                evaluation = Evaluation(
                    value=TriState.UNKNOWN,
                    evidence={
                        "type": "event_condition",
                        "kind": "invalid_event_condition_contract",
                        "error": contract_error,
                    },
                    fatal=True,
                )
            else:
                evaluation = evaluate_event_condition_tree(
                    condition,
                    event_status,
                    now=observed_at,
                    external_evaluations=external_evaluations,
                )

            state = MonitorState.ARMED
            wake_reason = current.wake_reason
            outcome = current.outcome
            thread_delivery = current.delivery_kind == DeliveryKind.THREAD
            if evaluation.value == TriState.TRUE:
                state = MonitorState.CLAIMED
                wake_reason = (
                    WakeReason.CONDITION
                    if thread_delivery
                    or witness_authorizes_continuation(
                        evaluation.witness,
                        allow_heuristic_continuation=(
                            current.allow_heuristic_continuation
                        ),
                    )
                    else WakeReason.UNAUTHORIZED_EVIDENCE
                )
                outcome = {
                    "kind": "trigger_claimed",
                    "wake_reason": wake_reason.value,
                    "at": observed_at,
                }
            elif evaluation.fatal:
                if current.mode == MonitorMode.DEFERRED or thread_delivery:
                    state = MonitorState.CLAIMED
                    wake_reason = WakeReason.OBSERVER_FAILED
                    outcome = {
                        "kind": "trigger_claimed",
                        "wake_reason": wake_reason.value,
                        "at": observed_at,
                    }
                else:
                    state = MonitorState.OBSERVER_FAILED
                    outcome = {
                        "kind": "observer_identity_failed",
                        "at": observed_at,
                    }
            elif observed_at >= current.expires_at:
                if current.mode == MonitorMode.DEFERRED or thread_delivery:
                    state = MonitorState.CLAIMED
                    wake_reason = WakeReason.EXPIRED
                    outcome = {
                        "kind": "trigger_claimed",
                        "wake_reason": wake_reason.value,
                        "at": observed_at,
                    }
                else:
                    state = MonitorState.EXPIRED
                    outcome = {"kind": "expired", "at": observed_at}

            transaction.execute(
                """
                UPDATE monitors
                SET condition_json = ?, evidence_json = ?, witness_json = ?,
                    state = ?, wake_reason = ?, outcome_json = ?,
                    evaluation_count = evaluation_count + 1, updated_at = ?
                WHERE monitor_id = ? AND state = ? AND evaluation_count = ?
                """,
                (
                    canonical_json(condition),
                    canonical_json(evaluation.evidence),
                    canonical_json(list(evaluation.witness)),
                    state.value,
                    wake_reason.value if wake_reason is not None else None,
                    canonical_json(outcome) if outcome is not None else None,
                    observed_at,
                    monitor_id,
                    MonitorState.ARMED.value,
                    expected_evaluation_count,
                ),
            )
            updated_row = transaction.execute(
                "SELECT * FROM monitors WHERE monitor_id = ?", (monitor_id,)
            ).fetchone()
            self._commit()
            if state == MonitorState.ARMED:
                return None
            assert updated_row is not None
            return MonitorRecord.from_row(updated_row)
        except BaseException:
            self._rollback()
            raise

    def transition(
        self,
        monitor_id: str,
        *,
        expected: Iterable[MonitorState],
        state: MonitorState,
        outcome: Mapping[str, Any] | None = None,
        evidence: Mapping[str, Any] | None = None,
        witness: Iterable[Mapping[str, Any]] | None = None,
        condition: Mapping[str, Any] | None = None,
        activation_snapshot: Mapping[str, Any] | None = None,
        wake_reason: WakeReason | None = None,
        stamp_armed_at: bool = False,
        increment_evaluation: bool = False,
    ) -> MonitorRecord | None:
        """Atomically update a monitor if it remains in an allowed state."""

        allowed = tuple(item.value for item in expected)
        if not allowed:
            raise ValueError("transition requires at least one expected state")
        now = time.time()
        assignments = ["state = ?", "updated_at = ?"]
        parameters: list[Any] = [state.value, now]
        if wake_reason is not None:
            assignments.append("wake_reason = ?")
            parameters.append(wake_reason.value)
        if stamp_armed_at:
            assignments.append("armed_at = ?")
            parameters.append(now)
        if increment_evaluation:
            assignments.append("evaluation_count = evaluation_count + 1")
        if outcome is not None:
            assignments.append("outcome_json = ?")
            parameters.append(canonical_json(outcome))
        if evidence is not None:
            assignments.append("evidence_json = ?")
            parameters.append(canonical_json(evidence))
        if witness is not None:
            assignments.append("witness_json = ?")
            parameters.append(canonical_json(list(witness)))
        if condition is not None:
            assignments.append("condition_json = ?")
            parameters.append(canonical_json(condition))
        if activation_snapshot is not None:
            assignments.append("activation_snapshot_json = ?")
            parameters.append(canonical_json(activation_snapshot))
        placeholders = ",".join("?" for _ in allowed)
        parameters.extend([monitor_id, *allowed])
        transaction = self._transaction()
        try:
            cursor = transaction.execute(
                f"UPDATE monitors SET {', '.join(assignments)} "
                f"WHERE monitor_id = ? AND state IN ({placeholders})",
                tuple(parameters),
            )
            if cursor.rowcount != 1:
                self._commit()
                return None
            row = transaction.execute(
                "SELECT * FROM monitors WHERE monitor_id = ?", (monitor_id,)
            ).fetchone()
            self._commit()
            assert row is not None
            return MonitorRecord.from_row(row)
        except BaseException:
            self._rollback()
            raise

    def update_evaluation(
        self,
        monitor_id: str,
        *,
        condition: Mapping[str, Any],
        evidence: Mapping[str, Any],
        witness: Iterable[Mapping[str, Any]],
    ) -> MonitorRecord | None:
        return self.transition(
            monitor_id,
            expected=(MonitorState.ARMED,),
            state=MonitorState.ARMED,
            condition=condition,
            evidence=evidence,
            witness=witness,
            increment_evaluation=True,
        )

    def arm(self, monitor_id: str) -> MonitorRecord | None:
        return self.transition(
            monitor_id,
            expected=(MonitorState.REGISTERING,),
            state=MonitorState.ARMED,
            stamp_armed_at=True,
        )

    def begin_pause(
        self, monitor_id: str, *, pre_pause: Mapping[str, Any]
    ) -> MonitorRecord | None:
        return self.transition(
            monitor_id,
            expected=(MonitorState.DEFER_INTENT,),
            state=MonitorState.PAUSING,
            outcome={
                "kind": "pause_attempt_recorded",
                "pre_pause": dict(pre_pause),
                "at": time.time(),
            },
        )

    def arm_deferred(
        self, monitor_id: str, *, confirmation: Mapping[str, Any]
    ) -> MonitorRecord | None:
        return self.transition(
            monitor_id,
            expected=(MonitorState.PAUSING,),
            state=MonitorState.ARMED,
            stamp_armed_at=True,
            outcome={
                "kind": "pause_confirmed",
                "confirmation": dict(confirmation),
                "at": time.time(),
            },
        )

    def arm_paused_deferred(
        self, monitor_id: str, *, confirmation: Mapping[str, Any]
    ) -> MonitorRecord | None:
        return self.transition(
            monitor_id,
            expected=(MonitorState.DEFER_INTENT,),
            state=MonitorState.ARMED,
            stamp_armed_at=True,
            outcome={
                "kind": "paused_guard_confirmed",
                "confirmation": dict(confirmation),
                "at": time.time(),
            },
        )

    def claim(
        self,
        monitor_id: str,
        *,
        condition: Mapping[str, Any],
        evidence: Mapping[str, Any],
        witness: Iterable[Mapping[str, Any]],
        wake_reason: WakeReason | None = None,
    ) -> MonitorRecord | None:
        return self.transition(
            monitor_id,
            expected=(MonitorState.ARMED,),
            state=MonitorState.CLAIMED,
            condition=condition,
            evidence=evidence,
            witness=witness,
            wake_reason=wake_reason,
            outcome={
                "kind": "trigger_claimed",
                "wake_reason": wake_reason.value if wake_reason else None,
                "at": time.time(),
            },
        )

    def cancel(self, monitor_id: str) -> MonitorRecord | None:
        current = self.get(monitor_id)
        if (
            current is not None
            and current.delivery_kind == DeliveryKind.THREAD
            and current.state
            in {MonitorState.ADMISSION_IN_PROGRESS, MonitorState.QUEUE_ACCEPTED}
        ):
            return self.update_thread_delivery(
                monitor_id,
                expected=(current.state,),
                state=MonitorState.CANCEL_REQUESTED,
                delivery_state=ThreadDeliveryState.CANCEL_REQUESTED,
                delivery_outcome={"kind": "cancellation_requested", "at": time.time()},
            )
        return self.transition(
            monitor_id,
            expected=(
                MonitorState.REGISTERING,
                MonitorState.DEFER_INTENT,
                MonitorState.ARMED,
                MonitorState.CLAIMED,
            ),
            state=MonitorState.CANCELLED,
            outcome={"kind": "cancelled", "at": time.time()},
        )

    def begin_activation(
        self, monitor_id: str, *, activation_snapshot: Mapping[str, Any]
    ) -> MonitorRecord | None:
        return self.transition(
            monitor_id,
            expected=(MonitorState.CLAIMED,),
            state=MonitorState.ACTIVATING,
            activation_snapshot=activation_snapshot,
            outcome={"kind": "activation_attempt_recorded", "at": time.time()},
        )

    def recover_activating(self) -> int:
        """Consume attempts that were durable before an interrupted send."""

        transaction = self._transaction()
        try:
            cursor = transaction.execute(
                """
                UPDATE monitors
                SET state = ?, outcome_json = ?, updated_at = ?
                WHERE state = ?
                """,
                (
                    MonitorState.ACTIVATION_UNCERTAIN.value,
                    canonical_json(
                        {
                            "kind": "activation_uncertain_after_restart",
                            "at": time.time(),
                        }
                    ),
                    time.time(),
                    MonitorState.ACTIVATING.value,
                ),
            )
            self._commit()
            return cursor.rowcount
        except BaseException:
            self._rollback()
            raise

    def recover_registering(self) -> int:
        """Close a registration interrupted before its second guard read."""

        transaction = self._transaction()
        try:
            cursor = transaction.execute(
                """
                UPDATE monitors
                SET state = ?, outcome_json = ?, updated_at = ?
                WHERE state = ?
                """,
                (
                    MonitorState.SUPERSEDED.value,
                    canonical_json(
                        {
                            "kind": "registration_interrupted_by_restart",
                            "at": time.time(),
                        }
                    ),
                    time.time(),
                    MonitorState.REGISTERING.value,
                ),
            )
            self._commit()
            return cursor.rowcount
        except BaseException:
            self._rollback()
            raise

    def recover_defer_intent(self) -> int:
        """Abandon a defer interrupted before its irreversible pause phase."""

        transaction = self._transaction()
        try:
            cursor = transaction.execute(
                """
                UPDATE monitors
                SET state = ?, outcome_json = ?, updated_at = ?
                WHERE state = ?
                """,
                (
                    MonitorState.DEFER_ABANDONED.value,
                    canonical_json(
                        {
                            "kind": "defer_intent_abandoned_after_restart",
                            "at": time.time(),
                        }
                    ),
                    time.time(),
                    MonitorState.DEFER_INTENT.value,
                ),
            )
            self._commit()
            return cursor.rowcount
        except BaseException:
            self._rollback()
            raise

    def recover_pausing(self) -> int:
        """Consume a pause attempt whose delivery cannot be established."""

        transaction = self._transaction()
        try:
            cursor = transaction.execute(
                """
                UPDATE monitors
                SET state = ?, outcome_json = ?, updated_at = ?
                WHERE state = ?
                """,
                (
                    MonitorState.PAUSE_UNCERTAIN.value,
                    canonical_json(
                        {
                            "kind": "pause_uncertain_after_restart",
                            "at": time.time(),
                        }
                    ),
                    time.time(),
                    MonitorState.PAUSING.value,
                ),
            )
            self._commit()
            return cursor.rowcount
        except BaseException:
            self._rollback()
            raise

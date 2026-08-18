"""Crash-conscious SQLite ledger for one-shot wake-up monitors."""

from __future__ import annotations

import json
import sqlite3
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Mapping

from .models import (
    ConflictError,
    MonitorMode,
    MonitorState,
    TargetGuard,
    WakeReason,
    is_terminal,
)


def canonical_json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"))


def _load(value: str | None, fallback: Any) -> Any:
    return json.loads(value) if value is not None else fallback


@dataclass(frozen=True)
class MonitorRecord:
    """One durable monitor row, decoded at the storage boundary."""

    monitor_id: str
    idempotency_key: str | None
    semantic: Mapping[str, Any]
    target: TargetGuard
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

    @classmethod
    def from_row(cls, row: sqlite3.Row) -> "MonitorRecord":
        return cls(
            monitor_id=str(row["monitor_id"]),
            idempotency_key=row["idempotency_key"],
            semantic=_load(row["semantic_json"], {}),
            target=TargetGuard.from_dict(_load(row["target_json"], {})),
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
        )

    def status_dict(self) -> dict[str, Any]:
        return {
            "monitor_id": self.monitor_id,
            "idempotency_key": self.idempotency_key,
            "target": self.target.as_dict(),
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
        connection.execute(
            "CREATE INDEX IF NOT EXISTS monitors_state_expiry ON monitors(state, expires_at)"
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
        target: TargetGuard,
        condition: Mapping[str, Any],
        allow_heuristic_continuation: bool,
        expires_at: float,
        mode: MonitorMode = MonitorMode.LEGACY,
        idle_barrier: bool = False,
        initial_state: MonitorState = MonitorState.REGISTERING,
        rearm_of: str | None = None,
    ) -> tuple[MonitorRecord, bool]:
        """Create a monitor intent, or return its identical keyed ancestor."""

        now = time.time()
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
                    self._commit()
                    return existing, False
            transaction.execute(
                """
                INSERT INTO monitors(
                    monitor_id, idempotency_key, semantic_json, target_json,
                    condition_json, state, mode, idle_barrier,
                    allow_heuristic_continuation, expires_at, created_at,
                    updated_at, rearm_of
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    monitor_id,
                    idempotency_key,
                    semantic_json,
                    canonical_json(target.as_dict()),
                    canonical_json(condition),
                    initial_state.value,
                    mode.value,
                    int(idle_barrier),
                    int(allow_heuristic_continuation),
                    expires_at,
                    now,
                    now,
                    rearm_of,
                ),
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

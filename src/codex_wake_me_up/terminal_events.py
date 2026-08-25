"""Pure validation and value objects for terminal producer events.

This module deliberately owns no persistence, process, Git, or monitor I/O.
It turns producer mappings into immutable, bounded values that a later ledger
layer can store and compare.  Host-observed receive times are carried for
inspection but are excluded from semantic fingerprints: producers cannot use
their clocks to determine event ordering or terminal idempotency.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field, replace
from enum import StrEnum
import copy
import hashlib
import hmac
import json
import math
import posixpath
import re
from numbers import Real
from types import MappingProxyType
from typing import Any, TypeAlias, cast

from .models import ConflictError, ValidationError


MAX_EVENT_BYTES = 64 * 1024
MAX_STRING_BYTES = 4 * 1024
# Names used by the event contract and by callers that want to make a budget
# assertion without importing an implementation-oriented alias.
EVENT_JSON_BUDGET = MAX_EVENT_BYTES
MAX_PATH_REFERENCES = 64
MAX_RESERVATION_TTL_SECONDS = 30 * 24 * 60 * 60
FULL_OID_LENGTHS = frozenset({40, 64})

_HEX_RE = re.compile(r"^[0-9a-fA-F]+$")
_RAW_COMMAND_KEYS = frozenset(
    {
        "args",
        "argv",
        "cmd",
        "command",
        "commandline",
        "commandlineargs",
        "rawcommand",
        "script",
        "shell",
        "shellcommand",
    }
)
_SECRET_KEYS = frozenset({"token", "publishtoken"})
_MISSING = object()


class EventKind(StrEnum):
    COMMAND_TERMINAL = "command_terminal"
    WORKER_TERMINAL = "worker_terminal"


class ReservationState(StrEnum):
    RESERVED = "reserved"
    BOUND = "bound"
    TERMINAL = "terminal"
    CANCELLED = "cancelled"
    EXPIRED = "expired"


class CommandStatus(StrEnum):
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLED = "cancelled"
    SIGNALED = "signaled"


class WorkerOutcome(StrEnum):
    DELIVERED = "delivered"
    BLOCKED = "blocked"
    FAILED = "failed"
    CANCELLED = "cancelled"


def _validation(message: str) -> ValidationError:
    return ValidationError(message)


def _mapping(value: Any, what: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise _validation(f"{what} must be an object")
    for key in value:
        if not isinstance(key, str):
            raise _validation(f"{what} keys must be strings")
    return value


def _bounded_text(value: Any, what: str, *, required: bool = True) -> str | None:
    if value is None and not required:
        return None
    if not isinstance(value, str):
        raise _validation(f"{what} must be a string")
    if not value and required:
        raise _validation(f"{what} must be non-empty")
    if len(value.encode("utf-8")) > MAX_STRING_BYTES:
        raise _validation(f"{what} exceeds the 4 KiB string budget")
    return value


def _finite_number(value: Any, what: str, *, integer: bool = False) -> int | float:
    # bool is a Real in Python, but accepting it for a sequence or timestamp
    # silently turns malformed producer data into valid evidence.
    if isinstance(value, bool) or not isinstance(value, Real):
        raise _validation(f"{what} must be a number")
    try:
        finite = float(value)
    except (OverflowError, TypeError, ValueError) as exc:
        raise _validation(f"{what} must be a bounded finite number") from exc
    if not math.isfinite(finite):
        raise _validation(f"{what} must be finite")
    if not integer:
        return cast(int | float, value)
    try:
        number = int(cast(Any, value))
    except (OverflowError, TypeError, ValueError) as exc:
        raise _validation(f"{what} must be an integer") from exc
    if number != value or abs(number) > (2**63 - 1):
        raise _validation(f"{what} must be a bounded integer")
    return number


def _positive_timestamp(value: Any, what: str) -> float:
    number = float(_finite_number(value, what))
    if number <= 0:
        raise _validation(f"{what} must be greater than zero")
    return number


def _host_time(value: Any, what: str = "host receive time") -> float:
    if value is None:
        raise _validation(f"{what} must be provided")
    return _positive_timestamp(value, what)


def _producer_time(value: Any, what: str = "producer timestamp") -> float | str:
    if isinstance(value, str):
        return _bounded_text(value, what)  # type: ignore[return-value]
    return _positive_timestamp(value, what)


def _enum(value: Any, enum_type: type[StrEnum], what: str) -> StrEnum:
    if isinstance(value, enum_type):
        return value
    if not isinstance(value, str):
        raise _validation(f"{what} must be one of {[item.value for item in enum_type]}")
    try:
        return enum_type(value)
    except ValueError as exc:
        raise _validation(f"unknown {what}: {value!r}") from exc


def _optional_alias(
    value: Mapping[str, Any], names: Sequence[str], what: str, *, default: Any = _MISSING
) -> Any:
    present = [name for name in names if name in value]
    if len(present) > 1:
        raise _validation(f"{what} must use exactly one field, not {present}")
    if present:
        return value[present[0]]
    if default is _MISSING:
        raise _validation(f"missing {what}")
    return default


def _reject_raw_commands(value: Any) -> None:
    if isinstance(value, Mapping):
        for key, nested in value.items():
            if isinstance(key, str):
                normalized = re.sub(r"[^a-z0-9]", "", key.lower())
                if normalized in _RAW_COMMAND_KEYS:
                    raise _validation("raw command fields are forbidden")
                if normalized in _SECRET_KEYS:
                    raise _validation("publish token fields are forbidden")
            _reject_raw_commands(nested)
    elif isinstance(value, (list, tuple)):
        for nested in value:
            _reject_raw_commands(nested)


def _canonical_value(value: Any) -> Any:
    if value is None or isinstance(value, (str, bool, int)):
        return value
    if isinstance(value, float):
        if not math.isfinite(value):
            raise _validation("payload numbers must be finite")
        return value
    if isinstance(value, Mapping):
        result: dict[str, Any] = {}
        for key, nested in value.items():
            if not isinstance(key, str):
                raise _validation("payload object keys must be strings")
            result[key] = _canonical_value(nested)
        return {key: result[key] for key in sorted(result)}
    if isinstance(value, (list, tuple)):
        return [_canonical_value(nested) for nested in value]
    raise _validation(f"payload contains unsupported value {type(value).__name__}")


def canonical_json(value: Any) -> str:
    """Return one compact, UTF-8-safe JSON representation for hashing."""

    return json.dumps(
        _canonical_value(value),
        ensure_ascii=False,
        allow_nan=False,
        separators=(",", ":"),
        sort_keys=True,
    )


def payload_fingerprint(value: Any) -> str:
    """Hash semantic payload content, independent of mapping insertion order."""

    if hasattr(value, "semantic_payload"):
        semantic = value.semantic_payload  # type: ignore[attr-defined]
        value = semantic() if callable(semantic) else semantic
    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


def _check_budget(value: Any, what: str = "event") -> None:
    size = len(canonical_json(value).encode("utf-8"))
    if size > MAX_EVENT_BYTES:
        raise _validation(f"{what} exceeds the 64 KiB JSON budget")


def _full_oid(value: Any, what: str = "commit object ID") -> str:
    text = _bounded_text(value, what)
    assert text is not None
    if len(text) not in FULL_OID_LENGTHS or _HEX_RE.fullmatch(text) is None:
        raise _validation(f"{what} must be one full commit object ID")
    return text.lower()


def _command_digest(value: Any) -> str:
    text = _bounded_text(value, "command digest")
    assert text is not None
    if len(text) != 64 or _HEX_RE.fullmatch(text) is None:
        raise _validation("command digest must be a 64-character hexadecimal SHA-256")
    return text.lower()


def _path_list(value: Any, what: str) -> tuple[str, ...]:
    if value is None:
        return ()
    if not isinstance(value, (list, tuple)):
        raise _validation(f"{what} must be a list")
    if len(value) > MAX_PATH_REFERENCES:
        raise _validation(f"{what} exceeds the {MAX_PATH_REFERENCES}-path budget")
    paths = tuple(_bounded_text(item, f"{what} entry") for item in value)
    if any(path is None for path in paths):
        raise _validation(f"{what} entries must be non-empty")
    return paths  # type: ignore[return-value]


def _absolute_path(value: Any, what: str) -> str:
    text = _bounded_text(value, what)
    assert text is not None
    if "\x00" in text or not text.startswith("/"):
        raise _validation(f"{what} must be an absolute path")
    parts = text.split("/")
    if ".." in parts:
        raise _validation(f"{what} must not contain '..'")
    return posixpath.normpath(text)


def _relative_prefixes(value: Any) -> tuple[str, ...]:
    if not isinstance(value, (list, tuple)) or not value:
        raise _validation("allowed path prefixes must be a non-empty list")
    if len(value) > MAX_PATH_REFERENCES:
        raise _validation("allowed path prefixes exceed the 64-path budget")
    normalized: set[str] = set()
    for item in value:
        text = _bounded_text(item, "allowed path prefix")
        assert text is not None
        if (
            text.startswith("/")
            or text.startswith("-")
            or "\x00" in text
            or "\\" in text
        ):
            raise _validation("allowed path prefix must be repository-relative")
        parts = text.split("/")
        if any(part in {".", "..", ".git"} for part in parts):
            raise _validation("allowed path prefix has unsafe components")
        # Redundant separators and a trailing slash carry no authority and
        # are normalized away, matching Git's repository-relative path form.
        canonical = "/".join(part for part in parts if part)
        if canonical in {"", ".", ".."}:
            raise _validation("allowed path prefix cannot be unrestricted")
        normalized.add(canonical)
    return tuple(sorted(normalized))


def _copy_json(value: Any) -> Any:
    """Copy JSON data without allowing producer-owned mappings to leak in."""

    if isinstance(value, Mapping):
        return {key: _copy_json(nested) for key, nested in value.items()}
    if isinstance(value, list):
        return [_copy_json(nested) for nested in value]
    if isinstance(value, tuple):
        return [_copy_json(nested) for nested in value]
    return copy.deepcopy(value)


def _freeze_mapping(value: Mapping[str, Any]) -> Mapping[str, Any]:
    return _freeze_json(value)


def _freeze_json(value: Any) -> Any:
    """Deep-freeze accepted JSON so evidence cannot mutate after validation."""

    if isinstance(value, Mapping):
        return MappingProxyType(
            {key: _freeze_json(nested) for key, nested in value.items()}
        )
    if isinstance(value, (list, tuple)):
        return tuple(_freeze_json(nested) for nested in value)
    return copy.deepcopy(value)


@dataclass(frozen=True, slots=True)
class CommandTerminalEvent:
    status: CommandStatus | str
    command_label: str
    command_digest: str
    exit_code: int | None = None
    signal: str | int | None = None
    reason: str | None = None
    producer_timestamp: float | str | None = None
    log_paths: tuple[str, ...] = ()
    artifact_paths: tuple[str, ...] = ()
    host_received_at: float | None = None
    kind: EventKind = field(default=EventKind.COMMAND_TERMINAL, init=False)

    def __post_init__(self) -> None:
        status = _enum(self.status, CommandStatus, "command status")
        object.__setattr__(self, "status", status)
        object.__setattr__(self, "command_label", _bounded_text(self.command_label, "command label"))
        object.__setattr__(self, "command_digest", _command_digest(self.command_digest))
        object.__setattr__(self, "log_paths", _path_list(self.log_paths, "log paths"))
        object.__setattr__(self, "artifact_paths", _path_list(self.artifact_paths, "artifact paths"))
        if self.exit_code is not None:
            code = _finite_number(self.exit_code, "exit code", integer=True)
            if not 0 <= int(code) <= 255:
                raise _validation("exit code must be between 0 and 255")
            object.__setattr__(self, "exit_code", int(code))
        if self.signal is not None:
            if isinstance(self.signal, bool):
                raise _validation("signal identity must not be boolean")
            if isinstance(self.signal, int):
                if not 1 <= self.signal <= 255:
                    raise _validation("signal identity must be between 1 and 255")
            else:
                signal = _bounded_text(self.signal, "signal identity")
                assert signal is not None
                object.__setattr__(self, "signal", signal)
        if self.reason is not None:
            object.__setattr__(self, "reason", _bounded_text(self.reason, "command reason"))
        if self.producer_timestamp is not None:
            object.__setattr__(
                self,
                "producer_timestamp",
                _producer_time(self.producer_timestamp),
            )
        if self.host_received_at is not None:
            object.__setattr__(self, "host_received_at", _host_time(self.host_received_at))

        if status in {CommandStatus.SUCCEEDED, CommandStatus.FAILED}:
            if self.exit_code is None or self.signal is not None:
                raise _validation("succeeded and failed statuses require exit evidence only")
            if status is CommandStatus.SUCCEEDED and self.exit_code != 0:
                raise _validation("succeeded status requires exit code zero")
            if status is CommandStatus.FAILED and self.exit_code == 0:
                raise _validation("failed status requires a nonzero exit code")
        elif status is CommandStatus.SIGNALED:
            if self.signal is None or self.exit_code is not None:
                raise _validation("signaled status requires signal evidence only")
        elif self.exit_code is not None or self.signal is not None:
            raise _validation("cancelled status must not carry exit or signal evidence")
        _check_budget(self.semantic_payload())

    def semantic_payload(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "kind": self.kind.value,
            "status": cast(CommandStatus, self.status).value,
            "command_label": self.command_label,
            "command_digest": self.command_digest,
            "lead_accepted": False,
        }
        if self.exit_code is not None:
            payload["exit_code"] = self.exit_code
        if self.signal is not None:
            payload["signal"] = self.signal
        if self.reason is not None:
            payload["reason"] = self.reason
        if self.producer_timestamp is not None:
            payload["producer_at"] = self.producer_timestamp
        if self.log_paths:
            payload["log_paths"] = list(self.log_paths)
        if self.artifact_paths:
            payload["artifact_paths"] = list(self.artifact_paths)
        return payload

    @property
    def fingerprint(self) -> str:
        return payload_fingerprint(self.semantic_payload())

    @property
    def semantic_fingerprint(self) -> str:
        return self.fingerprint

    @property
    def operator_label(self) -> str:
        return self.command_label

    def as_dict(self) -> dict[str, Any]:
        payload = self.semantic_payload()
        if self.host_received_at is not None:
            payload["host_received_at"] = self.host_received_at
        return payload

    as_status = as_dict


@dataclass(frozen=True, slots=True)
class WorkerTerminalEvent:
    outcome: WorkerOutcome | str
    producer_task_id: str
    candidate_oid: str | None = None
    reason: str | None = None
    producer_timestamp: float | str | None = None
    host_received_at: float | None = None
    kind: EventKind = field(default=EventKind.WORKER_TERMINAL, init=False)

    def __post_init__(self) -> None:
        outcome = _enum(self.outcome, WorkerOutcome, "worker outcome")
        object.__setattr__(self, "outcome", outcome)
        object.__setattr__(
            self,
            "producer_task_id",
            _bounded_text(self.producer_task_id, "producer task identity"),
        )
        if self.candidate_oid is not None:
            object.__setattr__(self, "candidate_oid", _full_oid(self.candidate_oid))
        if self.reason is not None:
            object.__setattr__(self, "reason", _bounded_text(self.reason, "worker reason"))
        if self.producer_timestamp is not None:
            object.__setattr__(
                self,
                "producer_timestamp",
                _producer_time(self.producer_timestamp),
            )
        if self.host_received_at is not None:
            object.__setattr__(self, "host_received_at", _host_time(self.host_received_at))
        if outcome is WorkerOutcome.DELIVERED:
            if self.candidate_oid is None:
                raise _validation("delivered outcome requires exactly one full commit object ID")
        else:
            if self.candidate_oid is not None:
                raise _validation("non-delivery outcome must not claim a candidate commit")
            if self.reason is None:
                raise _validation("non-delivery worker outcome requires a bounded reason")
        _check_budget(self.semantic_payload())

    def semantic_payload(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "kind": self.kind.value,
            "outcome": cast(WorkerOutcome, self.outcome).value,
            "producer_task_id": self.producer_task_id,
            "lead_accepted": False,
        }
        if self.candidate_oid is not None:
            payload["candidate_oid"] = self.candidate_oid
        if self.reason is not None:
            payload["reason"] = self.reason
        if self.producer_timestamp is not None:
            payload["producer_at"] = self.producer_timestamp
        return payload

    @property
    def fingerprint(self) -> str:
        return payload_fingerprint(self.semantic_payload())

    @property
    def semantic_fingerprint(self) -> str:
        return self.fingerprint

    @property
    def status(self) -> WorkerOutcome:
        return cast(WorkerOutcome, self.outcome)

    @property
    def candidate_commit(self) -> str | None:
        return self.candidate_oid

    def as_dict(self) -> dict[str, Any]:
        payload = self.semantic_payload()
        if self.host_received_at is not None:
            payload["host_received_at"] = self.host_received_at
        return payload

    as_status = as_dict


TerminalEvent: TypeAlias = CommandTerminalEvent | WorkerTerminalEvent


def _event_keys(value: Mapping[str, Any], allowed: set[str]) -> None:
    for key in value:
        if key == "host_received_at":
            raise _validation("host receive time is stamped by the host")
        if key not in allowed:
            raise _validation(f"unknown terminal event field: {key}")


def normalize_command_terminal(
    value: Mapping[str, Any] | CommandTerminalEvent,
    *,
    host_received_at: float | None = None,
) -> CommandTerminalEvent:
    if isinstance(value, CommandTerminalEvent):
        return replace(value, host_received_at=_host_time(host_received_at)) if host_received_at is not None else value
    if isinstance(value, WorkerTerminalEvent):
        raise _validation("worker terminal event has the wrong kind")
    mapping = _mapping(value, "command terminal event")
    _reject_raw_commands(mapping)
    allowed = {
        "kind",
        "status",
        "command_label",
        "label",
        "operator_label",
        "command_digest",
        "digest",
        "exit_code",
        "signal",
        "reason",
        "producer_at",
        "producer_timestamp",
        "log_paths",
        "artifact_paths",
    }
    _event_keys(mapping, allowed)
    if "kind" in mapping and _enum(mapping["kind"], EventKind, "event kind") is not EventKind.COMMAND_TERMINAL:
        raise _validation("command event has the wrong kind")
    label = _optional_alias(mapping, ("command_label", "label", "operator_label"), "command label")
    digest = _optional_alias(mapping, ("command_digest", "digest"), "command digest")
    producer_at = _optional_alias(
        mapping,
        ("producer_at", "producer_timestamp"),
        "producer timestamp",
        default=None,
    )
    return CommandTerminalEvent(
        status=mapping.get("status", _MISSING),
        command_label=label,
        command_digest=digest,
        exit_code=mapping.get("exit_code"),
        signal=mapping.get("signal"),
        reason=mapping.get("reason"),
        producer_timestamp=producer_at,
        log_paths=mapping.get("log_paths", ()),
        artifact_paths=mapping.get("artifact_paths", ()),
        host_received_at=_host_time(host_received_at) if host_received_at is not None else None,
    )


def normalize_worker_terminal(
    value: Mapping[str, Any] | WorkerTerminalEvent,
    *,
    host_received_at: float | None = None,
) -> WorkerTerminalEvent:
    if isinstance(value, WorkerTerminalEvent):
        return replace(value, host_received_at=_host_time(host_received_at)) if host_received_at is not None else value
    if isinstance(value, CommandTerminalEvent):
        raise _validation("command terminal event has the wrong kind")
    mapping = _mapping(value, "worker terminal event")
    _reject_raw_commands(mapping)
    allowed = {
        "kind",
        "outcome",
        "producer_task_id",
        "reason",
        "candidate_oid",
        "candidate_commit",
        "candidate_commit_oid",
        "producer_at",
        "producer_timestamp",
    }
    _event_keys(mapping, allowed)
    if "kind" in mapping and _enum(mapping["kind"], EventKind, "event kind") is not EventKind.WORKER_TERMINAL:
        raise _validation("worker event has the wrong kind")
    candidate = _optional_alias(
        mapping,
        ("candidate_oid", "candidate_commit", "candidate_commit_oid"),
        "candidate commit object ID",
        default=None,
    )
    producer_at = _optional_alias(
        mapping,
        ("producer_at", "producer_timestamp"),
        "producer timestamp",
        default=None,
    )
    return WorkerTerminalEvent(
        outcome=mapping.get("outcome", _MISSING),
        producer_task_id=mapping.get("producer_task_id", _MISSING),
        candidate_oid=candidate,
        reason=mapping.get("reason"),
        producer_timestamp=producer_at,
        host_received_at=_host_time(host_received_at) if host_received_at is not None else None,
    )


def normalize_terminal_event(
    value: Mapping[str, Any] | TerminalEvent,
    *,
    kind: EventKind | str | None = None,
    host_received_at: float | None = None,
) -> TerminalEvent:
    expected: EventKind | None = (
        cast(EventKind, _enum(kind, EventKind, "event kind"))
        if kind is not None
        else None
    )
    if isinstance(value, CommandTerminalEvent):
        actual: EventKind = EventKind.COMMAND_TERMINAL
        if expected is not None and expected is not actual:
            raise _validation("terminal event kind does not match payload")
        return normalize_command_terminal(value, host_received_at=host_received_at)
    if isinstance(value, WorkerTerminalEvent):
        actual = EventKind.WORKER_TERMINAL
        if expected is not None and expected is not actual:
            raise _validation("terminal event kind does not match payload")
        return normalize_worker_terminal(value, host_received_at=host_received_at)
    mapping = _mapping(value, "terminal event")
    supplied = mapping.get("kind")
    actual_kind = (
        cast(EventKind, _enum(supplied, EventKind, "event kind"))
        if supplied is not None
        else expected
    )
    if actual_kind is None:
        raise _validation("terminal event requires an event kind")
    if expected is not None and actual_kind is not expected:
        raise _validation("terminal event kind does not match payload")
    if actual_kind is EventKind.COMMAND_TERMINAL:
        return normalize_command_terminal(mapping, host_received_at=host_received_at)
    return normalize_worker_terminal(mapping, host_received_at=host_received_at)


def validate_terminal_rewrite(
    existing: Mapping[str, Any] | TerminalEvent,
    incoming: Mapping[str, Any] | TerminalEvent,
) -> TerminalEvent:
    """Return the original event for an identical retry; reject rewrites."""

    first = normalize_terminal_event(existing)
    second = normalize_terminal_event(incoming, kind=first.kind)
    if first.fingerprint == second.fingerprint:
        return first
    raise ConflictError("terminal event is immutable; conflicting rewrite rejected")


@dataclass(frozen=True, slots=True, kw_only=True)
class Heartbeat:
    sequence: int
    payload: Mapping[str, Any] = field(default_factory=dict)
    producer_timestamp: float | str | None = None
    host_received_at: float | None = None

    def __post_init__(self) -> None:
        sequence = _finite_number(self.sequence, "heartbeat sequence", integer=True)
        if int(sequence) < 0:
            raise _validation("heartbeat sequence must be nonnegative")
        object.__setattr__(self, "sequence", int(sequence))
        mapping = _mapping(self.payload, "heartbeat payload")
        _reject_raw_commands(mapping)
        copied = _copy_json(mapping)
        object.__setattr__(self, "payload", _freeze_mapping(copied))
        if self.producer_timestamp is not None:
            object.__setattr__(self, "producer_timestamp", _producer_time(self.producer_timestamp))
        if self.host_received_at is not None:
            object.__setattr__(self, "host_received_at", _host_time(self.host_received_at))
        semantic = self.semantic_payload()
        _validate_all_strings(semantic)
        _check_budget(semantic, "heartbeat payload")

    def semantic_payload(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "sequence": self.sequence,
            "payload": _copy_json(self.payload),
        }
        if self.producer_timestamp is not None:
            payload["producer_at"] = self.producer_timestamp
        return payload

    @property
    def fingerprint(self) -> str:
        return payload_fingerprint(self.semantic_payload())

    @property
    def semantic_fingerprint(self) -> str:
        return self.fingerprint

    def as_dict(self) -> dict[str, Any]:
        payload = self.semantic_payload()
        if self.host_received_at is not None:
            payload["host_received_at"] = self.host_received_at
        return payload

    as_status = as_dict


def _validate_all_strings(value: Any) -> None:
    if isinstance(value, str):
        if len(value.encode("utf-8")) > MAX_STRING_BYTES:
            raise _validation("heartbeat string exceeds the 4 KiB string budget")
    elif isinstance(value, Mapping):
        for key, nested in value.items():
            if len(str(key).encode("utf-8")) > MAX_STRING_BYTES:
                raise _validation("heartbeat object key exceeds the 4 KiB string budget")
            _validate_all_strings(nested)
    elif isinstance(value, (list, tuple)):
        for nested in value:
            _validate_all_strings(nested)


def normalize_heartbeat(
    value: Mapping[str, Any] | Heartbeat,
    *,
    host_received_at: float | None = None,
) -> Heartbeat:
    if isinstance(value, Heartbeat):
        return replace(value, host_received_at=_host_time(host_received_at)) if host_received_at is not None else value
    mapping = _mapping(value, "heartbeat")
    _event_keys(mapping, {"sequence", "payload", "producer_at", "producer_timestamp"})
    producer_at = _optional_alias(
        mapping,
        ("producer_at", "producer_timestamp"),
        "producer timestamp",
        default=None,
    )
    payload = mapping.get("payload", {})
    return Heartbeat(
        sequence=mapping.get("sequence", _MISSING),
        payload=payload,
        producer_timestamp=producer_at,
        host_received_at=_host_time(host_received_at) if host_received_at is not None else None,
    )


def validate_heartbeat(
    previous: Heartbeat | Mapping[str, Any] | None,
    incoming: Heartbeat | Mapping[str, Any],
) -> Heartbeat:
    candidate = normalize_heartbeat(incoming)
    if previous is None:
        return candidate
    prior = normalize_heartbeat(previous)
    if candidate.sequence > prior.sequence:
        return candidate
    if candidate.sequence == prior.sequence and candidate.fingerprint == prior.fingerprint:
        return prior
    raise ConflictError("heartbeat sequence must advance; conflicting retry rejected")


@dataclass(frozen=True, slots=True, kw_only=True)
class Reservation:
    kind: EventKind | str
    reserved_at: float
    expires_at: float
    reservation_id: str | None = None
    idempotency_key: str | None = None
    producer_identity: str | None = None
    producer_task_id: str | None = None
    repository: str | None = None
    worktree: str | None = None
    baseline_commit: str | None = None
    allowed_path_prefixes: tuple[str, ...] = ()
    state: ReservationState | str = ReservationState.RESERVED

    def __post_init__(self) -> None:
        object.__setattr__(self, "kind", _enum(self.kind, EventKind, "event kind"))
        object.__setattr__(self, "state", _enum(self.state, ReservationState, "reservation state"))
        reserved_at = _host_time(self.reserved_at)
        expiry = _positive_timestamp(self.expires_at, "reservation expiry")
        if expiry <= reserved_at:
            raise _validation("reservation expiry must be in the future")
        if expiry > reserved_at + MAX_RESERVATION_TTL_SECONDS:
            raise _validation("reservation expiry exceeds the bounded lifetime")
        object.__setattr__(self, "reserved_at", reserved_at)
        object.__setattr__(self, "expires_at", expiry)
        for field_name, label in (
            ("reservation_id", "reservation ID"),
            ("idempotency_key", "idempotency key"),
            ("producer_identity", "producer identity"),
            ("producer_task_id", "producer task identity"),
        ):
            value = getattr(self, field_name)
            if value is not None:
                object.__setattr__(self, field_name, _bounded_text(value, label))
        if self.kind is EventKind.WORKER_TERMINAL:
            if self.producer_task_id is None:
                raise _validation("worker reservation requires producer task identity")
            if self.repository is None or self.worktree is None or self.baseline_commit is None:
                raise _validation("worker reservation requires repository delivery scope")
            object.__setattr__(self, "repository", _absolute_path(self.repository, "repository"))
            object.__setattr__(self, "worktree", _absolute_path(self.worktree, "worktree"))
            object.__setattr__(self, "baseline_commit", _full_oid(self.baseline_commit, "baseline commit"))
            object.__setattr__(
                self,
                "allowed_path_prefixes",
                _relative_prefixes(self.allowed_path_prefixes),
            )
        else:
            if any(
                item is not None and item != ()
                for item in (
                    self.repository,
                    self.worktree,
                    self.baseline_commit,
                )
            ) or self.allowed_path_prefixes:
                raise _validation("command reservation must not carry worker delivery scope")
        _check_budget(self.semantic_payload(), "reservation payload")

    def semantic_payload(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "kind": cast(EventKind, self.kind).value,
            "expires_at": self.expires_at,
        }
        for name in ("idempotency_key", "producer_identity", "producer_task_id"):
            value = getattr(self, name)
            if value is not None:
                payload[name] = value
        if self.kind is EventKind.WORKER_TERMINAL:
            payload.update(
                {
                    "repository": self.repository,
                    "worktree": self.worktree,
                    "baseline_commit": self.baseline_commit,
                    "allowed_path_prefixes": list(self.allowed_path_prefixes),
                }
            )
        return payload

    @property
    def fingerprint(self) -> str:
        return payload_fingerprint(self.semantic_payload())

    def as_dict(self) -> dict[str, Any]:
        payload = dict(self.semantic_payload())
        if self.reservation_id is not None:
            payload["reservation_id"] = self.reservation_id
        payload["state"] = cast(ReservationState, self.state).value
        return payload

    def as_status(
        self,
        *,
        terminal_event: TerminalEvent | Mapping[str, Any] | None = None,
        heartbeat: Heartbeat | Mapping[str, Any] | None = None,
        token_fingerprint: str | None = None,
    ) -> dict[str, Any]:
        status = self.as_dict()
        if token_fingerprint is not None:
            fingerprint = _bounded_text(token_fingerprint, "publish token fingerprint")
            assert fingerprint is not None
            if len(fingerprint) not in {12, 64} or _HEX_RE.fullmatch(fingerprint) is None:
                raise _validation("publish token fingerprint must be hexadecimal")
            status["publish_token_fingerprint"] = fingerprint.lower()
        if heartbeat is not None:
            status["last_heartbeat"] = normalize_heartbeat(heartbeat).as_status()
        if terminal_event is not None:
            event = normalize_terminal_event(terminal_event)
            if event.kind is not self.kind:
                raise _validation("terminal event kind does not match reservation kind")
            status["terminal_event"] = event.as_status()
        return status


def normalize_reservation(
    value: Mapping[str, Any] | Reservation,
    *,
    now: float | None = None,
) -> Reservation:
    if isinstance(value, Reservation):
        if now is not None:
            current = float(_finite_number(now, "current time"))
            if value.expires_at <= current:
                raise _validation("reservation expiry must be in the future")
            if value.expires_at > current + MAX_RESERVATION_TTL_SECONDS:
                raise _validation("reservation expiry exceeds the bounded lifetime")
        return value
    if now is None:
        raise _validation("now is required to bound reservation expiry")
    current = float(_finite_number(now, "current time"))
    mapping = _mapping(value, "reservation")
    _reject_raw_commands(mapping)
    allowed = {
        "reservation_id",
        "kind",
        "event_kind",
        "expires_at",
        "expires_in_seconds",
        "idempotency_key",
        "producer_identity",
        "producer_id",
        "producer_task_id",
        "repository",
        "repository_root",
        "worktree",
        "worktree_root",
        "baseline_commit",
        "baseline_oid",
        "allowed_path_prefixes",
        "allowed_paths",
        "state",
    }
    for key in mapping:
        if key not in allowed:
            raise _validation(f"unknown reservation field: {key}")
    kind = _enum(_optional_alias(mapping, ("kind", "event_kind"), "event kind"), EventKind, "event kind")
    if "expires_at" in mapping and "expires_in_seconds" in mapping:
        raise _validation("reservation must use expires_at or expires_in_seconds, not both")
    if "expires_in_seconds" in mapping:
        duration = float(_finite_number(mapping["expires_in_seconds"], "reservation lifetime"))
        if duration <= 0 or duration > MAX_RESERVATION_TTL_SECONDS:
            raise _validation("reservation lifetime is outside the bounded range")
        expires_at = current + duration
    else:
        if "expires_at" not in mapping:
            raise _validation("missing reservation expiry")
        expires_at = float(_positive_timestamp(mapping["expires_at"], "reservation expiry"))
    if expires_at <= current:
        raise _validation("reservation expiry must be in the future")
    if expires_at > current + MAX_RESERVATION_TTL_SECONDS:
        raise _validation("reservation expiry exceeds the bounded lifetime")
    producer_task_id = mapping.get("producer_task_id")
    repository = _optional_alias(mapping, ("repository", "repository_root"), "repository", default=None)
    worktree = _optional_alias(mapping, ("worktree", "worktree_root"), "worktree", default=None)
    baseline = _optional_alias(mapping, ("baseline_commit", "baseline_oid"), "baseline commit", default=None)
    prefixes = _optional_alias(
        mapping,
        ("allowed_path_prefixes", "allowed_paths"),
        "allowed path prefixes",
        default=(),
    )
    return Reservation(
        kind=kind,
        reserved_at=current,
        expires_at=expires_at,
        reservation_id=mapping.get("reservation_id"),
        idempotency_key=mapping.get("idempotency_key"),
        producer_identity=_optional_alias(
            mapping,
            ("producer_identity", "producer_id"),
            "producer identity",
            default=None,
        ),
        producer_task_id=producer_task_id,
        repository=repository,
        worktree=worktree,
        baseline_commit=baseline,
        allowed_path_prefixes=prefixes,
        state=mapping.get("state", ReservationState.RESERVED),
    )


def hash_publish_token(token: str, salt: bytes | str) -> str:
    """Return a deterministic salted token digest without retaining the token."""

    token_text = _bounded_text(token, "publish token")
    assert token_text is not None
    salt_bytes = salt.encode("utf-8") if isinstance(salt, str) else salt
    if not isinstance(salt_bytes, bytes) or not salt_bytes:
        raise _validation("publish-token salt must be non-empty bytes")
    return hmac.new(salt_bytes, token_text.encode("utf-8"), hashlib.sha256).hexdigest()


def verify_publish_token(token: str, expected_digest: str, salt: bytes | str) -> bool:
    """Verify with constant-time comparison; return false for a wrong token."""

    actual = hash_publish_token(token, salt)
    if not isinstance(expected_digest, str):
        return False
    return hmac.compare_digest(actual, expected_digest)


__all__ = [
    "EVENT_JSON_BUDGET",
    "MAX_EVENT_BYTES",
    "MAX_PATH_REFERENCES",
    "MAX_RESERVATION_TTL_SECONDS",
    "MAX_STRING_BYTES",
    "FULL_OID_LENGTHS",
    "EventKind",
    "ReservationState",
    "CommandStatus",
    "WorkerOutcome",
    "CommandTerminalEvent",
    "WorkerTerminalEvent",
    "TerminalEvent",
    "Heartbeat",
    "Reservation",
    "canonical_json",
    "payload_fingerprint",
    "normalize_command_terminal",
    "normalize_worker_terminal",
    "normalize_terminal_event",
    "validate_terminal_rewrite",
    "normalize_heartbeat",
    "validate_heartbeat",
    "normalize_reservation",
    "hash_publish_token",
    "verify_publish_token",
]

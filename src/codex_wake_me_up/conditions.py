"""Typed monitor conditions and conservative host evidence observers."""

from __future__ import annotations

import copy
import os
import re
import stat
import subprocess
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Mapping, MutableMapping, Sequence

from .models import Evaluation, TriState, ValidationError
from .runtime import read_json


GpuQuery = Callable[[], Mapping[int, float]]
TmuxRun = Callable[[Sequence[str]], subprocess.CompletedProcess[str]]

# Reversible implementation budgets, not contract. They exist so one hostile
# log line or one enormous append cannot delay every other monitor's poll.
MAX_LOG_PATTERNS = 8
MAX_LOG_PATTERN_CHARS = 512
LOG_READ_BUDGET_BYTES = 4 * 1024 * 1024
LOG_LINE_MATCH_CHARS = 4096
LOG_EVALUATION_SECONDS = 1.0
JOURNAL_MAX_LINES = 50
JOURNAL_MAX_CHARS = 16 * 1024
JOURNAL_STORED_LINE_CHARS = 512
WITNESS_MATCHED_LINES = 5


def _default_gpu_query() -> Mapping[int, float]:
    result = subprocess.run(
        [
            "nvidia-smi",
            "--query-gpu=index,utilization.gpu",
            "--format=csv,noheader,nounits",
        ],
        check=False,
        capture_output=True,
        text=True,
        timeout=5,
    )
    if result.returncode != 0:
        raise RuntimeError(result.stderr.strip() or "nvidia-smi failed")
    values: dict[int, float] = {}
    for raw_line in result.stdout.splitlines():
        pieces = [piece.strip() for piece in raw_line.split(",")]
        if len(pieces) != 2:
            raise RuntimeError(f"unparseable nvidia-smi line: {raw_line!r}")
        values[int(pieces[0])] = float(pieces[1])
    return values


def _default_tmux_run(arguments: Sequence[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        list(arguments), check=False, capture_output=True, text=True, timeout=5
    )


@dataclass
class ObserverContext:
    """Injectable boundary for the few host observations v1 permits."""

    runtime_root: Path
    now: Callable[[], float] = time.time
    proc_root: Path = Path("/proc")
    gpu_query: GpuQuery = _default_gpu_query
    tmux_run: TmuxRun = _default_tmux_run
    # Child-thread summaries the service pre-reads through the app-server, so
    # condition evaluation itself stays synchronous and does no protocol I/O.
    thread_observations: dict[str, Mapping[str, Any] | None] = field(
        default_factory=dict
    )


def _expect_mapping(value: Any, what: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise ValidationError(f"{what} must be an object")
    return value


def _only_keys(raw: Mapping[str, Any], allowed: set[str], condition_type: str) -> None:
    unexpected = sorted(set(raw) - allowed)
    if unexpected:
        raise ValidationError(
            f"{condition_type} contains unsupported fields: {', '.join(unexpected)}"
        )


def _expect_number(value: Any, what: str, *, minimum: float = 0.0) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValidationError(f"{what} must be a number")
    numeric = float(value)
    if numeric < minimum:
        raise ValidationError(f"{what} must be at least {minimum}")
    return numeric


def _parse_deadline(value: Any) -> float:
    if not isinstance(value, str):
        raise ValidationError("time.at_utc must be an RFC3339 timestamp string")
    normalized = value.replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(normalized)
    except ValueError as exc:
        raise ValidationError("time.at_utc must be an RFC3339 timestamp") from exc
    if parsed.tzinfo is None:
        raise ValidationError("time.at_utc must include a UTC offset")
    return parsed.astimezone(timezone.utc).timestamp()


def _read_boot_id(proc_root: Path) -> str:
    try:
        return (proc_root / "sys/kernel/random/boot_id").read_text(encoding="utf-8").strip()
    except OSError as exc:
        raise ValidationError("cannot read the host boot identity") from exc


_TERMINAL_PROCESS_STATES = frozenset({"Z", "X", "x"})


def _read_pid_snapshot(proc_root: Path, pid: int) -> tuple[dict[str, Any], str]:
    process_dir = proc_root / str(pid)
    try:
        process_stat = (process_dir / "stat").read_text(encoding="utf-8")
        uid = process_dir.stat().st_uid
    except FileNotFoundError as exc:
        raise ValidationError(f"PID {pid} does not exist") from exc
    except OSError as exc:
        raise ValidationError(f"cannot inspect PID {pid}") from exc
    try:
        # The command name may contain spaces and parentheses. Fields after the
        # final ')' begin at proc stat field 3. State is index 0 and starttime is
        # index 19 in this suffix.
        fields_after_command = process_stat.rsplit(")", 1)[1].strip().split()
        state = fields_after_command[0]
        start_time = int(fields_after_command[19])
    except (IndexError, ValueError) as exc:
        raise ValidationError(f"cannot parse process identity for PID {pid}") from exc
    if len(state) != 1:
        raise ValidationError(f"cannot parse process identity for PID {pid}")
    identity = {
        "pid": pid,
        "boot_id": _read_boot_id(proc_root),
        "start_time": start_time,
        "uid": uid,
    }
    return identity, state


def _read_pid_identity(proc_root: Path, pid: int) -> dict[str, Any]:
    identity, _state = _read_pid_snapshot(proc_root, pid)
    return identity


def _tmux_command(socket: str, *arguments: str) -> list[str]:
    return ["tmux", "-S", socket, *arguments]


def _default_tmux_socket() -> str:
    raw = os.environ.get("TMUX")
    if not raw:
        raise ValidationError("tmux_exit requires socket when $TMUX is unavailable")
    socket = raw.split(",", 1)[0]
    if not socket:
        raise ValidationError("$TMUX does not contain a socket path")
    return socket


def _capture_tmux_identity(raw: Mapping[str, Any], context: ObserverContext) -> dict[str, Any]:
    target_kind = raw.get("target_kind")
    if target_kind not in {"session", "pane"}:
        raise ValidationError("tmux_exit.target_kind must be session or pane")
    target = raw.get("target")
    if not isinstance(target, str) or not target:
        raise ValidationError("tmux_exit.target must be a non-empty tmux target")
    socket_value = raw.get("socket") or _default_tmux_socket()
    if not isinstance(socket_value, str) or not socket_value.startswith("/"):
        raise ValidationError("tmux_exit.socket must be an absolute Unix socket path")
    result = context.tmux_run(
        _tmux_command(socket_value, "display-message", "-p", "-t", target, "#{pid}\t#{session_id}\t#{pane_id}")
    )
    if result.returncode != 0:
        raise ValidationError(result.stderr.strip() or "cannot resolve the tmux target")
    values = result.stdout.strip().split("\t")
    if len(values) != 3 or not values[0].isdigit():
        raise ValidationError("tmux returned an invalid identity")
    server_pid, session_id, pane_id = values
    return {
        "type": "tmux_exit",
        "target_kind": target_kind,
        "socket": socket_value,
        "server_pid": int(server_pid),
        "target_id": session_id if target_kind == "session" else pane_id,
        "session_id": session_id,
    }


def _compile_log_patterns(raw: Mapping[str, Any]) -> list[dict[str, str]]:
    """Validate the named alternation and prove every regex compiles at arm."""

    patterns = raw.get("patterns")
    if not isinstance(patterns, list) or not patterns:
        raise ValidationError("log_pattern.patterns must be a non-empty list")
    if len(patterns) > MAX_LOG_PATTERNS:
        raise ValidationError(
            f"log_pattern.patterns must contain at most {MAX_LOG_PATTERNS} entries"
        )
    compiled: list[dict[str, str]] = []
    seen: set[str] = set()
    for entry in patterns:
        item = _expect_mapping(entry, "log_pattern.patterns entry")
        _only_keys(item, {"name", "regex"}, "log_pattern.patterns entry")
        name = item.get("name")
        if not isinstance(name, str) or not name:
            raise ValidationError("log_pattern.patterns entry requires a non-empty name")
        if name in seen:
            raise ValidationError("log_pattern.patterns must not repeat a name")
        seen.add(name)
        regex = item.get("regex")
        if not isinstance(regex, str) or not regex:
            raise ValidationError("log_pattern.patterns entry requires a non-empty regex")
        if len(regex) > MAX_LOG_PATTERN_CHARS:
            raise ValidationError(
                f"log_pattern regex must be at most {MAX_LOG_PATTERN_CHARS} characters"
            )
        try:
            re.compile(regex)
        except re.error as exc:
            raise ValidationError(f"log_pattern regex {name} does not compile: {exc}") from exc
        compiled.append({"name": name, "regex": regex})
    return compiled


def _capture_log_identity(raw: Mapping[str, Any]) -> dict[str, Any]:
    """Capture device+inode and the EOF-at-arm baseline for one local log."""

    path = raw.get("path")
    if not isinstance(path, str) or not path.startswith("/"):
        raise ValidationError("log_pattern.path must be an absolute file path")
    patterns = _compile_log_patterns(raw)
    try:
        info = os.stat(path)
    except FileNotFoundError:
        # The job may not have created its log yet. Tolerated and recorded; the
        # leaf stays false until the file appears, then scans it from byte 0.
        identity: dict[str, int] | None = None
        offset = 0
    except OSError as exc:
        raise ValidationError(f"cannot inspect log file {path}") from exc
    else:
        if not stat.S_ISREG(info.st_mode):
            raise ValidationError("log_pattern.path must name a regular file")
        identity = {"device": int(info.st_dev), "inode": int(info.st_ino)}
        offset = int(info.st_size)
    return {
        "type": "log_pattern",
        "path": path,
        "patterns": patterns,
        "identity": identity,
        "offset": offset,
        "missing_at_arm": identity is None,
        "matched": False,
        "matched_names": [],
        "witness_lines": [],
        "journal": {"lines": [], "dropped": 0},
    }


def _capture_thread_idle(
    raw: Mapping[str, Any], context: ObserverContext
) -> dict[str, Any]:
    """Capture one locally loaded child thread that must still end a turn."""

    child_id = raw.get("thread_id")
    if not isinstance(child_id, str) or not child_id:
        raise ValidationError("thread_idle.thread_id must be a non-empty thread id")
    accept_already_idle = raw.get("accept_already_idle", False)
    if not isinstance(accept_already_idle, bool):
        raise ValidationError("thread_idle.accept_already_idle must be boolean")
    snapshot = context.thread_observations.get(child_id)
    if snapshot is None or not snapshot.get("is_loaded"):
        raise ValidationError(
            f"thread_idle child {child_id} is not locally loaded; only a loaded "
            "thread can be observed through the local app-server"
        )
    runtime_status = str(snapshot.get("runtime_status"))
    if runtime_status == "idle" and not accept_already_idle:
        raise ValidationError(
            f"thread_idle child {child_id} is already idle at registration; handle "
            "its result in the current turn, or set accept_already_idle to wait "
            "for a later idle observation"
        )
    return {
        "type": "thread_idle",
        "thread_id": child_id,
        "accept_already_idle": accept_already_idle,
        "armed_runtime_status": runtime_status,
        "armed_snapshot": dict(snapshot),
        "idle": False,
        "idle_snapshot": None,
    }


def thread_idle_targets(condition: Mapping[str, Any]) -> list[str]:
    """List every thread_idle child named by a raw or prepared condition."""

    if not isinstance(condition, Mapping):
        return []
    if condition.get("type") == "thread_idle":
        child_id = condition.get("thread_id")
        return [child_id] if isinstance(child_id, str) and child_id else []
    children = condition.get("children")
    if not isinstance(children, list):
        return []
    found: list[str] = []
    for child in children:
        for item in thread_idle_targets(child):
            if item not in found:
                found.append(item)
    return found


def _validate_children(raw: Mapping[str, Any], context: ObserverContext, *, monitor_id: str, receipt_token: str) -> list[dict[str, Any]]:
    children = raw.get("children")
    if not isinstance(children, list) or not children:
        raise ValidationError(f"{raw.get('type')}.children must be a non-empty list")
    return [
        prepare_condition(
            _expect_mapping(child, "condition child"),
            context,
            monitor_id=monitor_id,
            receipt_token=receipt_token,
        )
        for child in children
    ]


def prepare_condition(
    raw: Mapping[str, Any],
    context: ObserverContext,
    *,
    monitor_id: str,
    receipt_token: str,
) -> dict[str, Any]:
    """Validate an operator AST and capture every identity needed to observe it."""

    condition_type = raw.get("type")
    if condition_type in {"all", "any"}:
        _only_keys(raw, {"type", "children"}, str(condition_type))
        return {
            "type": condition_type,
            "children": _validate_children(
                raw, context, monitor_id=monitor_id, receipt_token=receipt_token
            ),
        }
    if condition_type in {"command_terminal", "worker_terminal"}:
        _only_keys(raw, {"type", "reservation_id"}, str(condition_type))
        return {
            "type": condition_type,
            "reservation_id": _event_reservation_id(raw.get("reservation_id")),
        }
    if condition_type == "heartbeat_stale":
        _only_keys(
            raw,
            {"type", "reservation_id", "stale_after_seconds"},
            "heartbeat_stale",
        )
        stale_after = _expect_number(
            raw.get("stale_after_seconds"),
            "heartbeat_stale.stale_after_seconds",
        )
        if stale_after <= 0:
            raise ValidationError(
                "heartbeat_stale.stale_after_seconds must be positive"
            )
        return {
            "type": "heartbeat_stale",
            "reservation_id": _event_reservation_id(raw.get("reservation_id")),
            "stale_after_seconds": stale_after,
        }
    if condition_type == "time":
        _only_keys(raw, {"type", "after_seconds", "at_utc"}, "time")
        has_relative = "after_seconds" in raw
        has_absolute = "at_utc" in raw
        if has_relative == has_absolute:
            raise ValidationError("time must contain exactly one of after_seconds or at_utc")
        deadline = (
            context.now() + _expect_number(raw["after_seconds"], "time.after_seconds")
            if has_relative
            else _parse_deadline(raw["at_utc"])
        )
        return {"type": "time", "deadline_utc": deadline}
    if condition_type == "gpu_stable":
        _only_keys(
            raw,
            {"type", "devices", "max_utilization_percent", "stable_for_seconds"},
            "gpu_stable",
        )
        devices = raw.get("devices")
        if not isinstance(devices, list) or not devices:
            raise ValidationError("gpu_stable.devices must be a non-empty list")
        if any(isinstance(device, bool) or not isinstance(device, int) or device < 0 for device in devices):
            raise ValidationError("gpu_stable.devices must contain non-negative integer indices")
        if len(set(devices)) != len(devices):
            raise ValidationError("gpu_stable.devices must not repeat an index")
        threshold = _expect_number(
            raw.get("max_utilization_percent"), "gpu_stable.max_utilization_percent"
        )
        if threshold > 100:
            raise ValidationError("gpu_stable.max_utilization_percent must be at most 100")
        return {
            "type": "gpu_stable",
            "devices": sorted(devices),
            "max_utilization_percent": threshold,
            "stable_for_seconds": _expect_number(
                raw.get("stable_for_seconds"), "gpu_stable.stable_for_seconds"
            ),
            "stable_since": None,
        }
    if condition_type == "pid_exit":
        _only_keys(raw, {"type", "pid"}, "pid_exit")
        pid = raw.get("pid")
        if isinstance(pid, bool) or not isinstance(pid, int) or pid <= 0:
            raise ValidationError("pid_exit.pid must be a positive integer")
        identity, state = _read_pid_snapshot(context.proc_root, pid)
        if state in _TERMINAL_PROCESS_STATES:
            raise ValidationError(f"PID {pid} has already terminated")
        return {"type": "pid_exit", "identity": identity}
    if condition_type == "tmux_exit":
        _only_keys(raw, {"type", "target_kind", "target", "socket"}, "tmux_exit")
        return _capture_tmux_identity(raw, context)
    if condition_type == "receipt_success":
        _only_keys(raw, {"type"}, "receipt_success")
        return {
            "type": "receipt_success",
            "monitor_id": monitor_id,
            "token": receipt_token,
            "path": str(context.runtime_root / "receipts" / f"{monitor_id}.json"),
        }
    if condition_type == "log_pattern":
        _only_keys(raw, {"type", "path", "patterns"}, "log_pattern")
        return _capture_log_identity(raw)
    if condition_type == "thread_idle":
        _only_keys(raw, {"type", "thread_id", "accept_already_idle"}, "thread_idle")
        return _capture_thread_idle(raw, context)
    raise ValidationError(
        "unsupported condition type; use time, gpu_stable, pid_exit, tmux_exit, "
        "log_pattern, thread_idle, receipt_success, command_terminal, "
        "worker_terminal, heartbeat_stale, all, or any"
    )


def _event_reservation_id(value: Any) -> str:
    if (
        not isinstance(value, str)
        or not value
        or len(value.encode("utf-8")) > 256
        or "\0" in value
    ):
        raise ValidationError("event reservation_id must be a bounded string")
    return value


def event_condition_binding(
    condition: Mapping[str, Any],
) -> tuple[str, str] | None:
    """Validate one-monitor/one-reservation event-leaf cardinality."""

    reservations: set[str] = set()
    terminal_leaves: list[tuple[str, str]] = []

    def visit(node: Mapping[str, Any]) -> None:
        condition_type = node.get("type")
        if condition_type in {"all", "any"}:
            children = node.get("children")
            if not isinstance(children, list):
                raise ValidationError("event condition children must be a list")
            for child in children:
                if not isinstance(child, Mapping):
                    raise ValidationError("event condition child must be an object")
                visit(child)
            return
        if condition_type in {
            "command_terminal",
            "worker_terminal",
            "heartbeat_stale",
        }:
            reservation_id = _event_reservation_id(node.get("reservation_id"))
            reservations.add(reservation_id)
            if condition_type in {"command_terminal", "worker_terminal"}:
                terminal_leaves.append((reservation_id, str(condition_type)))

    visit(condition)
    if not reservations:
        return None
    if len(reservations) != 1:
        raise ValidationError("one monitor may name only one distinct reservation")
    if len(terminal_leaves) != 1:
        if len(terminal_leaves) > 1:
            raise ValidationError("event monitor contains a duplicate terminal leaf")
        raise ValidationError("event monitor requires exactly one terminal leaf")
    reservation_id, event_kind = terminal_leaves[0]
    if reservation_id not in reservations:
        raise ValidationError("terminal leaf reservation is inconsistent")
    return reservation_id, event_kind


def contains_event_condition(condition: Mapping[str, Any]) -> bool:
    """Detect event leaves without trusting the stored AST's cardinality."""

    condition_type = condition.get("type")
    if condition_type in {
        "command_terminal",
        "worker_terminal",
        "heartbeat_stale",
    }:
        return True
    children = condition.get("children")
    if not isinstance(children, list):
        return False
    return any(
        contains_event_condition(child)
        for child in children
        if isinstance(child, Mapping)
    )


def observe_external_condition_leaves(
    condition: Mapping[str, Any], context: ObserverContext
) -> tuple[dict[str, Any], dict[tuple[int, ...], Evaluation]]:
    """Observe non-event leaves before entering the event claim transaction."""

    observed = copy.deepcopy(dict(condition))
    evaluations: dict[tuple[int, ...], Evaluation] = {}

    def visit(node: MutableMapping[str, Any], path: tuple[int, ...]) -> None:
        condition_type = node.get("type")
        if condition_type in {"all", "any"}:
            children = node.get("children")
            if not isinstance(children, list):
                evaluations[path] = _event_unknown(
                    str(condition_type), "invalid_stored_children"
                )
                return
            for index, child in enumerate(children):
                if isinstance(child, MutableMapping):
                    visit(child, (*path, index))
                else:
                    evaluations[(*path, index)] = _event_unknown(
                        str(condition_type), "invalid_stored_child"
                    )
            return
        if condition_type not in {
            "command_terminal",
            "worker_terminal",
            "heartbeat_stale",
        }:
            try:
                evaluations[path] = evaluate_condition(node, context)
            except (KeyError, TypeError, ValueError, ValidationError):
                evaluations[path] = _event_unknown(
                    str(condition_type), "invalid_stored_external_leaf"
                )

    visit(observed, ())
    return observed, evaluations


def public_condition_semantics(raw: Mapping[str, Any]) -> Mapping[str, Any]:
    """Copy request semantics before capture adds a deadline or host identity."""

    return copy.deepcopy(dict(raw))


def _leaf(value: TriState, condition_type: str, evidence: Mapping[str, Any], *, fatal: bool = False) -> Evaluation:
    classifications = {
        "time": "heuristic_time",
        "gpu_stable": "heuristic_resource_state",
        "pid_exit": "liveness",
        "tmux_exit": "liveness",
        "receipt_success": "task_success",
        "log_pattern": "heuristic_log_content",
        "thread_idle": "heuristic_thread_lifecycle",
    }
    rendered_evidence = dict(evidence)
    rendered_evidence.setdefault("classification", classifications.get(condition_type, "derived"))
    witness: tuple[Mapping[str, Any], ...] = ()
    if value == TriState.TRUE:
        witness = ({"type": condition_type, "evidence": rendered_evidence},)
    return Evaluation(value=value, evidence=rendered_evidence, witness=witness, fatal=fatal)


def _evaluate_gpu(condition: MutableMapping[str, Any], context: ObserverContext) -> Evaluation:
    try:
        samples = dict(context.gpu_query())
        selected = {device: float(samples[device]) for device in condition["devices"]}
    except (KeyError, OSError, RuntimeError, ValueError, subprocess.SubprocessError) as exc:
        condition["stable_since"] = None
        return _leaf(
            TriState.UNKNOWN,
            "gpu_stable",
            {"kind": "gpu_sample_unavailable", "error": str(exc)},
        )
    now = context.now()
    threshold = float(condition["max_utilization_percent"])
    below_threshold = all(value <= threshold for value in selected.values())
    if not below_threshold:
        condition["stable_since"] = None
        return _leaf(
            TriState.FALSE,
            "gpu_stable",
            {"samples": selected, "threshold": threshold, "stable_since": None},
        )
    stable_since = condition.get("stable_since")
    if stable_since is None:
        stable_since = now
        condition["stable_since"] = stable_since
    stable_for = now - float(stable_since)
    value = (
        TriState.TRUE
        if stable_for >= float(condition["stable_for_seconds"])
        else TriState.FALSE
    )
    return _leaf(
        value,
        "gpu_stable",
        {
            "samples": selected,
            "threshold": threshold,
            "stable_since": stable_since,
            "stable_for_seconds": stable_for,
        },
    )


def _evaluate_pid(condition: Mapping[str, Any], context: ObserverContext) -> Evaluation:
    identity = _expect_mapping(condition.get("identity"), "stored pid identity")
    try:
        current_boot = _read_boot_id(context.proc_root)
    except ValidationError as exc:
        return _leaf(TriState.UNKNOWN, "pid_exit", {"error": str(exc)})
    if current_boot != identity.get("boot_id"):
        return _leaf(
            TriState.UNKNOWN,
            "pid_exit",
            {"kind": "boot_identity_changed"},
            fatal=True,
        )
    pid = int(identity["pid"])
    process_dir = context.proc_root / str(pid)
    if not process_dir.exists():
        return _leaf(TriState.TRUE, "pid_exit", {"pid": pid, "kind": "process_absent"})
    try:
        current, state = _read_pid_snapshot(context.proc_root, pid)
    except ValidationError as exc:
        return _leaf(TriState.UNKNOWN, "pid_exit", {"error": str(exc)})
    if current != dict(identity):
        return _leaf(
            TriState.UNKNOWN,
            "pid_exit",
            {"kind": "pid_identity_changed", "current": current},
            fatal=True,
        )
    if state in _TERMINAL_PROCESS_STATES:
        return _leaf(
            TriState.TRUE,
            "pid_exit",
            {"pid": pid, "state": state, "kind": "process_terminated"},
        )
    return _leaf(
        TriState.FALSE,
        "pid_exit",
        {"pid": pid, "state": state, "kind": "process_alive"},
    )


def _tmux_server_pid(condition: Mapping[str, Any], context: ObserverContext) -> tuple[int | None, str | None]:
    try:
        result = context.tmux_run(
            _tmux_command(str(condition["socket"]), "display-message", "-p", "#{pid}")
        )
    except (OSError, subprocess.SubprocessError) as exc:
        return None, str(exc)
    if result.returncode != 0 or not result.stdout.strip().isdigit():
        return None, None
    return int(result.stdout.strip()), None


def _evaluate_tmux(condition: Mapping[str, Any], context: ObserverContext) -> Evaluation:
    server_pid, server_error = _tmux_server_pid(condition, context)
    if server_error is not None:
        return _leaf(
            TriState.UNKNOWN,
            "tmux_exit",
            {"kind": "tmux_query_failed", "error": server_error},
        )
    if server_pid is None:
        return _leaf(
            TriState.UNKNOWN,
            "tmux_exit",
            {"kind": "tmux_server_unavailable"},
            fatal=True,
        )
    if server_pid != int(condition["server_pid"]):
        return _leaf(
            TriState.UNKNOWN,
            "tmux_exit",
            {"kind": "tmux_server_restarted", "current_server_pid": server_pid},
            fatal=True,
        )
    target_id = str(condition["target_id"])
    try:
        result = context.tmux_run(
            _tmux_command(str(condition["socket"]), "display-message", "-p", "-t", target_id, "#{pane_id}")
        )
    except (OSError, subprocess.SubprocessError) as exc:
        return _leaf(
            TriState.UNKNOWN,
            "tmux_exit",
            {"kind": "tmux_query_failed", "error": str(exc)},
        )
    if result.returncode == 0:
        return _leaf(
            TriState.FALSE,
            "tmux_exit",
            {"target_id": target_id, "kind": "target_alive"},
        )
    if result.returncode == 1:
        return _leaf(
            TriState.TRUE,
            "tmux_exit",
            {"target_id": target_id, "kind": "target_absent"},
        )
    return _leaf(
        TriState.UNKNOWN,
        "tmux_exit",
        {"kind": "tmux_target_error", "error": result.stderr.strip()},
    )


def _journal_counters(condition: Mapping[str, Any]) -> dict[str, int]:
    journal = condition.get("journal") or {}
    lines = journal.get("lines") or []
    return {"lines_stored": len(lines), "dropped": int(journal.get("dropped") or 0)}


def _append_journal(condition: MutableMapping[str, Any], lines: Sequence[str]) -> None:
    """Keep the newest matched lines within hard caps, counting what is dropped."""

    journal = dict(condition.get("journal") or {})
    stored = [str(item) for item in (journal.get("lines") or [])]
    dropped = int(journal.get("dropped") or 0)
    stored.extend(line[:JOURNAL_STORED_LINE_CHARS] for line in lines)
    while stored and (
        len(stored) > JOURNAL_MAX_LINES
        or sum(len(item) for item in stored) > JOURNAL_MAX_CHARS
    ):
        stored.pop(0)
        dropped += 1
    condition["journal"] = {"lines": stored, "dropped": dropped}


def journal_tail(condition: Mapping[str, Any], *, limit: int) -> list[str]:
    """Collect the newest matched lines from every log_pattern leaf."""

    if not isinstance(condition, Mapping):
        return []
    if condition.get("type") == "log_pattern":
        journal = condition.get("journal") or {}
        return [str(item) for item in (journal.get("lines") or [])][-limit:]
    children = condition.get("children")
    if not isinstance(children, list):
        return []
    collected: list[str] = []
    for child in children:
        collected.extend(journal_tail(child, limit=limit))
    return collected[-limit:]


def elide_condition_journals(condition: Any) -> Any:
    """Return a copy whose journal lines collapse to counters.

    The stored condition embeds the journal, but the wake report's
    ``journal_tail`` is the sole line carrier: returning the lines from status
    and registration responses too would spend the post-wake context this
    change exists to save.
    """

    if not isinstance(condition, Mapping):
        return condition
    value = dict(condition)
    if value.get("type") == "log_pattern":
        value["journal"] = _journal_counters(value)
    children = value.get("children")
    if isinstance(children, list):
        value["children"] = [elide_condition_journals(child) for child in children]
    return value


def _evaluate_log_pattern(
    condition: MutableMapping[str, Any], context: ObserverContext
) -> Evaluation:
    """Scan bytes appended since arming against the named alternation.

    Truth latches: consumed bytes cannot be re-observed, and a leaf that
    flapped back to false would make ``all(...)`` composition unsatisfiable.
    """

    if condition.get("matched"):
        return _leaf(
            TriState.TRUE,
            "log_pattern",
            {
                "kind": "pattern_matched",
                "matched": list(condition.get("matched_names") or []),
                "matched_lines": list(condition.get("witness_lines") or []),
                "journal": _journal_counters(condition),
            },
        )
    path = Path(str(condition["path"]))
    captured = condition.get("identity")
    try:
        info = path.stat()
    except FileNotFoundError:
        if captured is None:
            return _leaf(
                TriState.FALSE,
                "log_pattern",
                {"kind": "log_absent_since_arm", "path": str(path)},
            )
        # The captured inode existed and is gone: a replacement file is a
        # different observation target and can never satisfy this leaf.
        return _leaf(
            TriState.UNKNOWN,
            "log_pattern",
            {"kind": "log_identity_lost", "path": str(path), "captured": dict(captured)},
            fatal=True,
        )
    except OSError as exc:
        return _leaf(
            TriState.UNKNOWN, "log_pattern", {"kind": "log_stat_failed", "error": str(exc)}
        )
    current = {"device": int(info.st_dev), "inode": int(info.st_ino)}
    if captured is None:
        condition["identity"] = current
        condition["offset"] = 0
    elif dict(captured) != current:
        return _leaf(
            TriState.UNKNOWN,
            "log_pattern",
            {"kind": "log_identity_changed", "captured": dict(captured), "current": current},
            fatal=True,
        )
    offset = int(condition.get("offset") or 0)
    if int(info.st_size) < offset:
        return _leaf(
            TriState.UNKNOWN,
            "log_pattern",
            {"kind": "log_truncated", "offset": offset, "size": int(info.st_size)},
            fatal=True,
        )
    pending = int(info.st_size) - offset
    if pending <= 0:
        return _leaf(
            TriState.FALSE, "log_pattern", {"kind": "no_new_bytes", "offset": offset}
        )
    over_budget = pending > LOG_READ_BUDGET_BYTES
    try:
        with open(path, "rb") as handle:
            handle.seek(offset)
            chunk = handle.read(min(pending, LOG_READ_BUDGET_BYTES))
    except OSError as exc:
        return _leaf(
            TriState.UNKNOWN, "log_pattern", {"kind": "log_read_failed", "error": str(exc)}
        )
    last_newline = chunk.rfind(b"\n")
    if last_newline < 0:
        if not over_budget:
            # A partial trailing line is re-read whole on the next poll.
            return _leaf(
                TriState.FALSE,
                "log_pattern",
                {"kind": "partial_line_pending", "offset": offset},
            )
        condition["offset"] = offset + len(chunk)
        return _leaf(
            TriState.UNKNOWN,
            "log_pattern",
            {
                "kind": "pattern_budget_exceeded",
                "reason": "no_line_end_within_read_budget",
                "read_budget_bytes": LOG_READ_BUDGET_BYTES,
                "offset": condition["offset"],
            },
        )
    compiled = [
        (str(entry["name"]), re.compile(str(entry["regex"])))
        for entry in condition["patterns"]
    ]
    deadline = context.now() + LOG_EVALUATION_SECONDS
    matched_names: list[str] = []
    matched_lines: list[str] = []
    line_truncated = False
    wall_clock_exceeded = False
    consumed = 0
    for raw_line in chunk[: last_newline + 1].splitlines(keepends=True):
        if context.now() > deadline:
            wall_clock_exceeded = True
            break
        consumed += len(raw_line)
        text = raw_line.decode("utf-8", errors="replace").rstrip("\r\n")
        if len(text) > LOG_LINE_MATCH_CHARS:
            # `re` cannot be interrupted mid-match, so bounding the line — not
            # the wall clock — is what keeps one hostile line cheap.
            text = text[:LOG_LINE_MATCH_CHARS]
            line_truncated = True
        hits = [name for name, pattern in compiled if pattern.search(text)]
        if hits:
            for name in hits:
                if name not in matched_names:
                    matched_names.append(name)
            matched_lines.append(text)
    condition["offset"] = offset + consumed
    if matched_names:
        _append_journal(condition, matched_lines)
        witness_lines = [
            line[:JOURNAL_STORED_LINE_CHARS]
            for line in matched_lines[:WITNESS_MATCHED_LINES]
        ]
        condition["matched"] = True
        condition["matched_names"] = matched_names
        condition["witness_lines"] = witness_lines
        evidence: dict[str, Any] = {
            "kind": "pattern_matched",
            "matched": matched_names,
            "matched_lines": witness_lines,
            "journal": _journal_counters(condition),
        }
        if line_truncated:
            evidence["line_truncated"] = True
        return _leaf(TriState.TRUE, "log_pattern", evidence)
    if wall_clock_exceeded or over_budget:
        return _leaf(
            TriState.UNKNOWN,
            "log_pattern",
            {
                "kind": "pattern_budget_exceeded",
                "wall_clock_exceeded": wall_clock_exceeded,
                "read_budget_exceeded": over_budget,
                "read_budget_bytes": LOG_READ_BUDGET_BYTES,
                "offset": condition["offset"],
                "line_truncated": line_truncated,
            },
        )
    return _leaf(
        TriState.FALSE,
        "log_pattern",
        {
            "kind": "no_pattern_match",
            "offset": condition["offset"],
            "line_truncated": line_truncated,
        },
    )


def _evaluate_thread_idle(
    condition: MutableMapping[str, Any], context: ObserverContext
) -> Evaluation:
    """Report the captured child's observed turn completion, never its success."""

    child_id = str(condition["thread_id"])
    if condition.get("idle"):
        return _leaf(
            TriState.TRUE,
            "thread_idle",
            {
                "kind": "thread_idle",
                "thread_id": child_id,
                "child": dict(condition.get("idle_snapshot") or {}),
            },
        )
    if child_id not in context.thread_observations:
        return _leaf(
            TriState.UNKNOWN,
            "thread_idle",
            {"kind": "thread_observation_missing", "thread_id": child_id},
        )
    snapshot = context.thread_observations[child_id]
    if snapshot is None or not snapshot.get("is_loaded"):
        # Disappearance is not completion, exactly as for a vanished tmux target.
        return _leaf(
            TriState.UNKNOWN,
            "thread_idle",
            {
                "kind": "thread_unloaded",
                "thread_id": child_id,
                "child": dict(snapshot) if snapshot is not None else None,
            },
        )
    if str(snapshot.get("runtime_status")) == "idle":
        condition["idle"] = True
        condition["idle_snapshot"] = dict(snapshot)
        return _leaf(
            TriState.TRUE,
            "thread_idle",
            {"kind": "thread_idle", "thread_id": child_id, "child": dict(snapshot)},
        )
    return _leaf(
        TriState.FALSE,
        "thread_idle",
        {"kind": "thread_active", "thread_id": child_id, "child": dict(snapshot)},
    )


def _evaluate_receipt(condition: Mapping[str, Any]) -> Evaluation:
    path = Path(str(condition["path"]))
    receipt = read_json(path)
    if receipt is None:
        if path.exists():
            return _leaf(
                TriState.UNKNOWN,
                "receipt_success",
                {"kind": "receipt_malformed_or_unreadable"},
            )
        return _leaf(TriState.FALSE, "receipt_success", {"kind": "receipt_absent"})
    if receipt.get("monitor_id") != condition.get("monitor_id") or receipt.get("token") != condition.get("token"):
        return _leaf(TriState.UNKNOWN, "receipt_success", {"kind": "receipt_identity_mismatch"})
    if receipt.get("status") == "success":
        return _leaf(
            TriState.TRUE,
            "receipt_success",
            {"kind": "receipt_success", "published_at": receipt.get("published_at")},
        )
    return _leaf(
        TriState.FALSE,
        "receipt_success",
        {"kind": "receipt_not_success", "status": receipt.get("status")},
    )


def _combine(condition_type: str, children: list[Evaluation]) -> Evaluation:
    if condition_type == "all":
        if any(child.value == TriState.FALSE for child in children):
            return Evaluation(
                value=TriState.FALSE,
                evidence={"type": "all", "children": [child.evidence for child in children]},
                fatal=any(child.fatal for child in children),
            )
        if any(child.value == TriState.UNKNOWN for child in children):
            return Evaluation(
                value=TriState.UNKNOWN,
                evidence={"type": "all", "children": [child.evidence for child in children]},
                fatal=any(child.fatal for child in children),
            )
        return Evaluation(
            value=TriState.TRUE,
            evidence={"type": "all", "children": [child.evidence for child in children]},
            witness=tuple(item for child in children for item in child.witness),
        )
    if any(child.value == TriState.TRUE for child in children):
        true_children = [child for child in children if child.value == TriState.TRUE]
        return Evaluation(
            value=TriState.TRUE,
            evidence={"type": "any", "children": [child.evidence for child in children]},
            witness=tuple(item for child in true_children for item in child.witness),
        )
    if any(child.value == TriState.UNKNOWN for child in children):
        return Evaluation(
            value=TriState.UNKNOWN,
            evidence={"type": "any", "children": [child.evidence for child in children]},
            fatal=all(child.fatal for child in children),
        )
    return Evaluation(
        value=TriState.FALSE,
        evidence={"type": "any", "children": [child.evidence for child in children]},
        fatal=all(child.fatal for child in children),
    )


def evaluate_event_condition_tree(
    condition: Mapping[str, Any],
    event_status: Mapping[str, Any] | None,
    *,
    now: float,
    external_evaluations: Mapping[tuple[int, ...], Evaluation] | None = None,
    _path: tuple[int, ...] = (),
) -> Evaluation:
    """Evaluate event leaves from one transaction-current reservation snapshot."""

    condition_type = condition.get("type")
    if condition_type in {"all", "any"}:
        children = condition.get("children")
        if not isinstance(children, list) or any(
            not isinstance(child, Mapping) for child in children
        ):
            return _event_unknown(str(condition_type), "invalid_stored_children")
        return _combine(
            str(condition_type),
            [
                evaluate_event_condition_tree(
                    child,
                    event_status,
                    now=now,
                    external_evaluations=external_evaluations,
                    _path=(*_path, index),
                )
                for index, child in enumerate(children)
            ],
        )
    if condition_type not in {
        "command_terminal",
        "worker_terminal",
        "heartbeat_stale",
    }:
        if external_evaluations is not None and _path in external_evaluations:
            return external_evaluations[_path]
        return _event_unknown(str(condition_type), "external_leaf_not_observed")
    if event_status is None:
        return _event_unknown(str(condition_type), "missing_bound_reservation")
    reservation_id = condition.get("reservation_id")
    if (
        event_status.get("reservation_id") != reservation_id
        or event_status.get("bound_monitor_id") in {None, ""}
    ):
        return _event_unknown(str(condition_type), "reservation_identity_mismatch")
    expected_kind = (
        str(condition_type)
        if condition_type in {"command_terminal", "worker_terminal"}
        else event_status.get("kind")
    )
    if event_status.get("kind") != expected_kind:
        return _event_unknown(str(condition_type), "reservation_kind_mismatch")
    if event_status.get("cancelled_at") is not None or event_status.get("expired_at") is not None:
        return _event_unknown(str(condition_type), "bound_reservation_terminality_corrupt")

    terminal = event_status.get("terminal_event")
    heartbeat = event_status.get("last_heartbeat")
    if condition_type == "heartbeat_stale":
        if terminal is not None:
            return Evaluation(
                value=TriState.FALSE,
                evidence={
                    "type": "heartbeat_stale",
                    "reservation_id": reservation_id,
                    "kind": "terminal_event_present",
                },
            )
        if not isinstance(heartbeat, Mapping):
            return Evaluation(
                value=TriState.UNKNOWN,
                evidence={
                    "type": "heartbeat_stale",
                    "reservation_id": reservation_id,
                    "kind": "heartbeat_unavailable",
                },
            )
        try:
            received_at = float(heartbeat["host_received_at"])
            sequence = int(heartbeat["sequence"])
            stale_after = float(condition["stale_after_seconds"])
        except (KeyError, TypeError, ValueError, OverflowError):
            return _event_unknown("heartbeat_stale", "corrupt_heartbeat")
        age = max(0.0, float(now) - received_at)
        evidence = {
            "type": "heartbeat_stale",
            "reservation_id": reservation_id,
            "producer_id": event_status.get("producer_id"),
            "last_sequence": sequence,
            "host_received_at": received_at,
            "age_seconds": age,
            "stale_after_seconds": stale_after,
            "classification": "heuristic_stall",
            "task_success": False,
            "lead_accepted": False,
        }
        value = TriState.TRUE if age >= stale_after else TriState.FALSE
        return Evaluation(
            value=value,
            evidence=evidence,
            witness=(dict(evidence),) if value is TriState.TRUE else (),
        )

    if terminal is None:
        if event_status.get("state") == "terminal":
            return _event_unknown(str(condition_type), "terminal_payload_missing")
        return Evaluation(
            value=TriState.FALSE,
            evidence={
                "type": condition_type,
                "reservation_id": reservation_id,
                "kind": "awaiting_terminal_event",
            },
        )
    if not isinstance(terminal, Mapping) or terminal.get("kind") != condition_type:
        return _event_unknown(str(condition_type), "terminal_payload_kind_mismatch")

    classification = "command_termination"
    if condition_type == "worker_terminal":
        if terminal.get("outcome") != "delivered":
            classification = "worker_terminal_failure"
        elif isinstance(event_status.get("git_attestation"), Mapping) and event_status[
            "git_attestation"
        ].get("status") == "valid":
            classification = "valid_delivery_candidate"
        else:
            classification = "invalid_delivery"
    witness = {
        "type": condition_type,
        "reservation_id": reservation_id,
        "producer_id": event_status.get("producer_id"),
        "classification": classification,
        "task_success": False,
        "lead_accepted": False,
        "terminal_event": copy.deepcopy(dict(terminal)),
        "last_heartbeat": copy.deepcopy(heartbeat),
        "git_attestation": copy.deepcopy(event_status.get("git_attestation")),
    }
    return Evaluation(
        value=TriState.TRUE,
        evidence=dict(witness),
        witness=(witness,),
    )


def _event_unknown(condition_type: str, kind: str) -> Evaluation:
    return Evaluation(
        value=TriState.UNKNOWN,
        evidence={"type": condition_type, "kind": kind},
        fatal=True,
    )


def evaluate_condition(condition: MutableMapping[str, Any], context: ObserverContext) -> Evaluation:
    """Evaluate and update the condition's small durable observation state.

    Latching leaves (``gpu_stable`` intervals, ``log_pattern`` offsets and
    journals, ``thread_idle`` edges) persist only through in-place mutation of
    this mapping, which the caller must then store. Passing a copy that is not
    persisted silently disables latching and re-reads consumed evidence.
    """

    condition_type = condition.get("type")
    if condition_type == "time":
        deadline = float(condition["deadline_utc"])
        value = TriState.TRUE if context.now() >= deadline else TriState.FALSE
        return _leaf(value, "time", {"deadline_utc": deadline, "now": context.now()})
    if condition_type == "gpu_stable":
        return _evaluate_gpu(condition, context)
    if condition_type == "pid_exit":
        return _evaluate_pid(condition, context)
    if condition_type == "tmux_exit":
        return _evaluate_tmux(condition, context)
    if condition_type == "log_pattern":
        return _evaluate_log_pattern(condition, context)
    if condition_type == "thread_idle":
        return _evaluate_thread_idle(condition, context)
    if condition_type == "receipt_success":
        return _evaluate_receipt(condition)
    if condition_type in {"all", "any"}:
        raw_children = condition.get("children")
        # Children must be mutable: latch state persists only through in-place
        # mutation, so an immutable child is corrupted stored state, never a
        # silently unlatched evaluation.
        if not isinstance(raw_children, list):
            return _leaf(
                TriState.UNKNOWN,
                str(condition_type),
                {"kind": "invalid_stored_children"},
                fatal=True,
            )
        mutable_children = [
            child for child in raw_children if isinstance(child, MutableMapping)
        ]
        if len(mutable_children) != len(raw_children):
            return _leaf(
                TriState.UNKNOWN,
                str(condition_type),
                {"kind": "invalid_stored_children"},
                fatal=True,
            )
        children = [
            evaluate_condition(child, context) for child in mutable_children
        ]
        return _combine(str(condition_type), children)
    return _leaf(
        TriState.UNKNOWN,
        str(condition_type),
        {"kind": "unsupported_stored_condition"},
        fatal=True,
    )


def witness_authorizes_continuation(
    witness: Sequence[Mapping[str, Any]], *, allow_heuristic_continuation: bool
) -> bool:
    """Receipt truth or an explicit opt-in is required for a goal mutation."""

    return allow_heuristic_continuation or any(
        item.get("type")
        in {"receipt_success", "command_terminal", "worker_terminal"}
        for item in witness
    )


def receipt_payload(
    *, monitor_id: str, token: str, status: str, published_at: float | None = None
) -> dict[str, Any]:
    if status not in {"success", "failed"}:
        raise ValidationError("receipt status must be success or failed")
    return {
        "monitor_id": monitor_id,
        "token": token,
        "status": status,
        "published_at": time.time() if published_at is None else published_at,
    }

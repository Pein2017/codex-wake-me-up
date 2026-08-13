"""Typed monitor conditions and conservative host evidence observers."""

from __future__ import annotations

import copy
import os
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


def _read_pid_identity(proc_root: Path, pid: int) -> dict[str, Any]:
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
        # final ')' begin at proc stat field 3; starttime is therefore index 19.
        fields_after_command = process_stat.rsplit(")", 1)[1].strip().split()
        start_time = int(fields_after_command[19])
    except (IndexError, ValueError) as exc:
        raise ValidationError(f"cannot parse process identity for PID {pid}") from exc
    return {"pid": pid, "boot_id": _read_boot_id(proc_root), "start_time": start_time, "uid": uid}


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
        return {"type": "pid_exit", "identity": _read_pid_identity(context.proc_root, pid)}
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
    raise ValidationError(
        "unsupported condition type; use time, gpu_stable, pid_exit, tmux_exit, "
        "receipt_success, all, or any"
    )


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
        current = _read_pid_identity(context.proc_root, pid)
    except ValidationError as exc:
        return _leaf(TriState.UNKNOWN, "pid_exit", {"error": str(exc)})
    if current != dict(identity):
        return _leaf(
            TriState.UNKNOWN,
            "pid_exit",
            {"kind": "pid_identity_changed", "current": current},
            fatal=True,
        )
    return _leaf(TriState.FALSE, "pid_exit", {"pid": pid, "kind": "process_alive"})


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


def evaluate_condition(condition: MutableMapping[str, Any], context: ObserverContext) -> Evaluation:
    """Evaluate and update the condition's small durable observation state."""

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
    if condition_type == "receipt_success":
        return _evaluate_receipt(condition)
    if condition_type in {"all", "any"}:
        raw_children = condition.get("children")
        if not isinstance(raw_children, list):
            return _leaf(
                TriState.UNKNOWN,
                str(condition_type),
                {"kind": "invalid_stored_children"},
                fatal=True,
            )
        children = [
            evaluate_condition(_expect_mapping(child, "stored condition child"), context)
            for child in raw_children
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
        item.get("type") == "receipt_success" for item in witness
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

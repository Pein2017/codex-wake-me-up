from __future__ import annotations

import json
from pathlib import Path
import subprocess

import pytest

from codex_wake_me_up.conditions import (
    ObserverContext,
    evaluate_condition,
    prepare_condition,
    witness_authorizes_continuation,
)
from codex_wake_me_up.models import TriState, ValidationError

from .helpers import Clock


def context(tmp_path: Path, clock: Clock) -> ObserverContext:
    return ObserverContext(runtime_root=tmp_path, now=clock.now, gpu_query=lambda: {0: 0.0})


def test_any_receipt_or_time_requires_heuristic_opt_in_when_only_time_is_true(tmp_path: Path) -> None:
    clock = Clock(100.0)
    prepared = prepare_condition(
        {
            "type": "any",
            "children": [
                {"type": "receipt_success"},
                {"type": "time", "after_seconds": 0},
            ],
        },
        context(tmp_path, clock),
        monitor_id="monitor",
        receipt_token="token",
    )

    result = evaluate_condition(prepared, context(tmp_path, clock))

    assert result.value == TriState.TRUE
    assert [item["type"] for item in result.witness] == ["time"]
    assert not witness_authorizes_continuation(
        result.witness, allow_heuristic_continuation=False
    )


def test_receipt_success_authorizes_without_heuristic_opt_in(tmp_path: Path) -> None:
    clock = Clock(100.0)
    prepared = prepare_condition(
        {"type": "receipt_success"},
        context(tmp_path, clock),
        monitor_id="monitor",
        receipt_token="token",
    )
    receipt_path = Path(prepared["path"])
    receipt_path.parent.mkdir(parents=True)
    receipt_path.write_text(
        json.dumps({"monitor_id": "monitor", "token": "token", "status": "success"}),
        encoding="utf-8",
    )

    result = evaluate_condition(prepared, context(tmp_path, clock))

    assert result.value == TriState.TRUE
    assert witness_authorizes_continuation(
        result.witness, allow_heuristic_continuation=False
    )


def test_gpu_stability_resets_when_a_sample_is_missing(tmp_path: Path) -> None:
    clock = Clock(100.0)
    samples = [{0: 0.0}, {}, {0: 0.0}]
    prepared = prepare_condition(
        {
            "type": "gpu_stable",
            "devices": [0],
            "max_utilization_percent": 10,
            "stable_for_seconds": 5,
        },
        context(tmp_path, clock),
        monitor_id="monitor",
        receipt_token="token",
    )

    first = evaluate_condition(
        prepared, ObserverContext(runtime_root=tmp_path, now=clock.now, gpu_query=lambda: samples.pop(0))
    )
    clock.value = 103
    missing = evaluate_condition(
        prepared, ObserverContext(runtime_root=tmp_path, now=clock.now, gpu_query=lambda: samples.pop(0))
    )
    clock.value = 106
    final = evaluate_condition(
        prepared, ObserverContext(runtime_root=tmp_path, now=clock.now, gpu_query=lambda: samples.pop(0))
    )

    assert first.value == TriState.FALSE
    assert missing.value == TriState.UNKNOWN
    assert final.value == TriState.FALSE
    assert prepared["stable_since"] == 106


@pytest.mark.parametrize(
    "raw",
    [
        {"type": "shell", "command": "echo unsafe"},
        {"type": "time", "after_seconds": 1, "command": "echo unsafe"},
    ],
)
def test_raw_shell_or_untyped_expression_is_rejected(tmp_path: Path, raw: dict[str, object]) -> None:
    with pytest.raises(ValidationError):
        prepare_condition(
            raw,
            context(tmp_path, Clock()),
            monitor_id="monitor",
            receipt_token="token",
        )


def test_pid_absent_at_registration_is_rejected(tmp_path: Path) -> None:
    proc = tmp_path / "proc"
    (proc / "sys/kernel/random").mkdir(parents=True)
    (proc / "sys/kernel/random/boot_id").write_text("boot", encoding="utf-8")
    with pytest.raises(ValidationError):
        prepare_condition(
            {"type": "pid_exit", "pid": 999999},
            ObserverContext(runtime_root=tmp_path, proc_root=proc),
            monitor_id="monitor",
            receipt_token="token",
        )


def test_pid_exit_is_true_only_after_the_captured_process_disappears(tmp_path: Path) -> None:
    proc = tmp_path / "proc"
    (proc / "sys/kernel/random").mkdir(parents=True)
    (proc / "sys/kernel/random/boot_id").write_text("boot", encoding="utf-8")
    process = proc / "73"
    process.mkdir()
    # proc fields after the command begin at field 3; index 19 is starttime.
    (process / "stat").write_text(
        "73 (worker) S " + " ".join(["0"] * 18 + ["1234", "0"]),
        encoding="utf-8",
    )
    observer = ObserverContext(runtime_root=tmp_path, proc_root=proc)
    prepared = prepare_condition(
        {"type": "pid_exit", "pid": 73},
        observer,
        monitor_id="monitor",
        receipt_token="token",
    )

    assert evaluate_condition(prepared, observer).value == TriState.FALSE
    (process / "stat").unlink()
    process.rmdir()
    assert evaluate_condition(prepared, observer).value == TriState.TRUE


def test_tmux_exit_requires_the_captured_server_and_absent_target(tmp_path: Path) -> None:
    def fake_tmux(arguments):
        if arguments[-1] == "#{pid}":
            return subprocess.CompletedProcess(arguments, 0, "42\n", "")
        if arguments[-1] == "#{pid}\t#{session_id}\t#{pane_id}":
            return subprocess.CompletedProcess(arguments, 0, "42\t$1\t%1\n", "")
        return subprocess.CompletedProcess(arguments, 1, "", "can't find pane")

    observer = ObserverContext(runtime_root=tmp_path, tmux_run=fake_tmux)
    prepared = prepare_condition(
        {
            "type": "tmux_exit",
            "target_kind": "pane",
            "target": "%1",
            "socket": "/tmp/tmux-test/default",
        },
        observer,
        monitor_id="monitor",
        receipt_token="token",
    )

    assert evaluate_condition(prepared, observer).value == TriState.TRUE


def test_tmux_timeout_is_unknown_and_does_not_escape_the_observer(tmp_path: Path) -> None:
    prepared = {
        "type": "tmux_exit",
        "socket": "/tmp/tmux-test/default",
        "server_pid": 42,
        "target_id": "%1",
        "session_id": "$1",
    }

    def timeout_tmux(arguments):
        raise subprocess.TimeoutExpired(arguments, timeout=5)

    result = evaluate_condition(
        prepared,
        ObserverContext(runtime_root=tmp_path, tmux_run=timeout_tmux),
    )

    assert result.value == TriState.UNKNOWN
    assert result.evidence["kind"] == "tmux_query_failed"
    assert result.evidence["classification"] == "liveness"


def test_malformed_receipt_is_unknown_not_success(tmp_path: Path) -> None:
    prepared = prepare_condition(
        {"type": "receipt_success"},
        context(tmp_path, Clock()),
        monitor_id="monitor",
        receipt_token="token",
    )
    receipt = Path(prepared["path"])
    receipt.parent.mkdir(parents=True)
    receipt.write_text("not-json", encoding="utf-8")

    assert evaluate_condition(prepared, context(tmp_path, Clock())).value == TriState.UNKNOWN

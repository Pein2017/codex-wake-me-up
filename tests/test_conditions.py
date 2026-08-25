from __future__ import annotations

import json
from pathlib import Path
import subprocess

import pytest

from codex_wake_me_up import conditions as conditions_module
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


def _write_pid_stat(
    proc: Path,
    *,
    pid: int = 73,
    state: str = "S",
    start_time: int = 1234,
    command: str = "worker",
) -> Path:
    process = proc / str(pid)
    process.mkdir(exist_ok=True)
    # proc fields after the command begin at field 3; index 19 is starttime.
    (process / "stat").write_text(
        f"{pid} ({command}) {state} "
        + " ".join(["0"] * 18 + [str(start_time), "0"]),
        encoding="utf-8",
    )
    return process


def _pid_observer(tmp_path: Path) -> tuple[Path, ObserverContext]:
    proc = tmp_path / "proc"
    (proc / "sys/kernel/random").mkdir(parents=True)
    (proc / "sys/kernel/random/boot_id").write_text("boot", encoding="utf-8")
    return proc, ObserverContext(runtime_root=tmp_path, proc_root=proc)


def test_pid_exit_is_true_after_the_captured_process_disappears(tmp_path: Path) -> None:
    proc, observer = _pid_observer(tmp_path)
    process = _write_pid_stat(proc)
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


@pytest.mark.parametrize("state", ["Z", "X", "x"])
def test_pid_exit_treats_same_identity_terminal_state_as_terminated(
    tmp_path: Path, state: str
) -> None:
    proc, observer = _pid_observer(tmp_path)
    _write_pid_stat(proc, state="S", command="worker with ) parens")
    prepared = prepare_condition(
        {"type": "pid_exit", "pid": 73},
        observer,
        monitor_id="monitor",
        receipt_token="token",
    )

    _write_pid_stat(proc, state=state, command="worker with ) parens")
    result = evaluate_condition(prepared, observer)

    assert result.value == TriState.TRUE
    assert result.evidence == {
        "classification": "liveness",
        "pid": 73,
        "state": state,
        "kind": "process_terminated",
    }


@pytest.mark.parametrize("state", ["R", "S", "D", "T", "t", "I", "?"])
def test_pid_exit_treats_nonterminal_state_as_alive(tmp_path: Path, state: str) -> None:
    proc, observer = _pid_observer(tmp_path)
    _write_pid_stat(proc, state="S")
    prepared = prepare_condition(
        {"type": "pid_exit", "pid": 73},
        observer,
        monitor_id="monitor",
        receipt_token="token",
    )

    _write_pid_stat(proc, state=state)
    result = evaluate_condition(prepared, observer)

    assert result.value == TriState.FALSE
    assert result.evidence["kind"] == "process_alive"
    assert result.evidence["state"] == state


def test_pid_exit_checks_identity_before_terminal_state(tmp_path: Path) -> None:
    proc, observer = _pid_observer(tmp_path)
    _write_pid_stat(proc, state="S", start_time=1234)
    prepared = prepare_condition(
        {"type": "pid_exit", "pid": 73},
        observer,
        monitor_id="monitor",
        receipt_token="token",
    )

    _write_pid_stat(proc, state="Z", start_time=9999)
    result = evaluate_condition(prepared, observer)

    assert result.value == TriState.UNKNOWN
    assert result.fatal
    assert result.evidence["kind"] == "pid_identity_changed"


def test_pid_exit_keeps_volatile_state_out_of_persisted_identity(tmp_path: Path) -> None:
    proc, observer = _pid_observer(tmp_path)
    process = _write_pid_stat(proc, state="S")

    prepared = prepare_condition(
        {"type": "pid_exit", "pid": 73},
        observer,
        monitor_id="monitor",
        receipt_token="token",
    )

    assert prepared == {
        "type": "pid_exit",
        "identity": {
            "pid": 73,
            "boot_id": "boot",
            "start_time": 1234,
            "uid": process.stat().st_uid,
        },
    }


@pytest.mark.parametrize("state", ["Z", "X", "x"])
def test_pid_exit_rejects_process_already_terminated_at_registration(
    tmp_path: Path, state: str
) -> None:
    proc, observer = _pid_observer(tmp_path)
    _write_pid_stat(proc, state=state)

    with pytest.raises(ValidationError, match="already terminated"):
        prepare_condition(
            {"type": "pid_exit", "pid": 73},
            observer,
            monitor_id="monitor",
            receipt_token="token",
        )


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


def _log_condition(path: Path, *, patterns: list[dict[str, str]] | None = None) -> dict:
    return {
        "type": "log_pattern",
        "path": str(path),
        "patterns": patterns
        or [
            {"name": "ready", "regex": r"Ready in [0-9.]+s"},
            {"name": "crash", "regex": r"Traceback|CUDA out of memory|Killed"},
        ],
    }


def test_log_pattern_ignores_content_written_before_arming(tmp_path: Path) -> None:
    log = tmp_path / "train.log"
    log.write_text("Ready in 1.0s\nTraceback (most recent call last)\n", encoding="utf-8")
    observer = context(tmp_path, Clock())
    prepared = prepare_condition(
        _log_condition(log), observer, monitor_id="monitor", receipt_token="token"
    )

    result = evaluate_condition(prepared, observer)

    assert result.value == TriState.FALSE
    assert result.evidence["kind"] == "no_new_bytes"


def test_log_pattern_matches_an_appended_line_and_names_the_pattern(tmp_path: Path) -> None:
    log = tmp_path / "train.log"
    log.write_text("starting\n", encoding="utf-8")
    observer = context(tmp_path, Clock())
    prepared = prepare_condition(
        _log_condition(log), observer, monitor_id="monitor", receipt_token="token"
    )
    with log.open("a", encoding="utf-8") as handle:
        handle.write("epoch 1\nReady in 12.5s\n")

    result = evaluate_condition(prepared, observer)

    assert result.value == TriState.TRUE
    assert result.evidence["matched"] == ["ready"]
    assert result.evidence["matched_lines"] == ["Ready in 12.5s"]
    assert result.evidence["classification"] == "heuristic_log_content"
    assert not witness_authorizes_continuation(
        result.witness, allow_heuristic_continuation=False
    )


def test_log_pattern_matches_a_failure_signature(tmp_path: Path) -> None:
    log = tmp_path / "train.log"
    log.write_text("starting\n", encoding="utf-8")
    observer = context(tmp_path, Clock())
    prepared = prepare_condition(
        _log_condition(log), observer, monitor_id="monitor", receipt_token="token"
    )
    with log.open("a", encoding="utf-8") as handle:
        handle.write("CUDA out of memory. Tried to allocate 2.00 GiB\n")

    result = evaluate_condition(prepared, observer)

    assert result.value == TriState.TRUE
    assert result.evidence["matched"] == ["crash"]


def test_log_pattern_tolerates_a_missing_file_then_scans_it_from_byte_zero(tmp_path: Path) -> None:
    log = tmp_path / "later.log"
    observer = context(tmp_path, Clock())
    prepared = prepare_condition(
        _log_condition(log), observer, monitor_id="monitor", receipt_token="token"
    )
    assert prepared["missing_at_arm"]

    absent = evaluate_condition(prepared, observer)
    assert absent.value == TriState.FALSE
    assert absent.evidence["kind"] == "log_absent_since_arm"

    log.write_text("Ready in 3.0s\n", encoding="utf-8")
    appeared = evaluate_condition(prepared, observer)

    assert appeared.value == TriState.TRUE
    assert appeared.evidence["matched"] == ["ready"]


def test_log_pattern_rotation_is_unknown_and_fatal_never_true(tmp_path: Path) -> None:
    log = tmp_path / "train.log"
    log.write_text("starting\n", encoding="utf-8")
    observer = context(tmp_path, Clock())
    prepared = prepare_condition(
        _log_condition(log), observer, monitor_id="monitor", receipt_token="token"
    )
    log.rename(tmp_path / "train.log.1")
    log.write_text("Ready in 1.0s\n", encoding="utf-8")

    result = evaluate_condition(prepared, observer)

    assert result.value == TriState.UNKNOWN
    assert result.fatal
    assert result.evidence["kind"] == "log_identity_changed"


def test_log_pattern_truncation_is_unknown_and_fatal(tmp_path: Path) -> None:
    log = tmp_path / "train.log"
    log.write_text("a very long first line of output\n", encoding="utf-8")
    observer = context(tmp_path, Clock())
    prepared = prepare_condition(
        _log_condition(log), observer, monitor_id="monitor", receipt_token="token"
    )
    with log.open("r+", encoding="utf-8") as handle:
        handle.truncate(2)

    result = evaluate_condition(prepared, observer)

    assert result.value == TriState.UNKNOWN
    assert result.fatal
    assert result.evidence["kind"] == "log_truncated"


def test_log_pattern_deleted_file_is_unknown_not_completion(tmp_path: Path) -> None:
    log = tmp_path / "train.log"
    log.write_text("starting\n", encoding="utf-8")
    observer = context(tmp_path, Clock())
    prepared = prepare_condition(
        _log_condition(log), observer, monitor_id="monitor", receipt_token="token"
    )
    log.unlink()

    result = evaluate_condition(prepared, observer)

    assert result.value == TriState.UNKNOWN
    assert result.fatal
    assert result.evidence["kind"] == "log_identity_lost"


@pytest.mark.parametrize(
    "patterns",
    [
        [{"name": f"p{index}", "regex": "x"} for index in range(9)],
        [{"name": "too-long", "regex": "y" * 513}],
        [{"name": "bad", "regex": "unbalanced("}],
        [{"name": "", "regex": "x"}],
        [{"name": "dup", "regex": "x"}, {"name": "dup", "regex": "z"}],
    ],
)
def test_log_pattern_bounds_are_enforced_at_arm(tmp_path: Path, patterns) -> None:
    log = tmp_path / "train.log"
    log.write_text("", encoding="utf-8")
    with pytest.raises(ValidationError):
        prepare_condition(
            _log_condition(log, patterns=patterns),
            context(tmp_path, Clock()),
            monitor_id="monitor",
            receipt_token="token",
        )


def test_log_pattern_requires_an_absolute_regular_file(tmp_path: Path) -> None:
    with pytest.raises(ValidationError):
        prepare_condition(
            {"type": "log_pattern", "path": "relative.log", "patterns": [{"name": "n", "regex": "x"}]},
            context(tmp_path, Clock()),
            monitor_id="monitor",
            receipt_token="token",
        )
    with pytest.raises(ValidationError):
        prepare_condition(
            _log_condition(tmp_path),
            context(tmp_path, Clock()),
            monitor_id="monitor",
            receipt_token="token",
        )


def test_log_pattern_bounds_one_hostile_line_and_records_the_truncation(tmp_path: Path) -> None:
    log = tmp_path / "train.log"
    log.write_text("", encoding="utf-8")
    observer = context(tmp_path, Clock())
    prepared = prepare_condition(
        _log_condition(log, patterns=[{"name": "tail", "regex": "NEEDLE"}]),
        observer,
        monitor_id="monitor",
        receipt_token="token",
    )
    with log.open("a", encoding="utf-8") as handle:
        handle.write("z" * 5000 + "NEEDLE\n")

    result = evaluate_condition(prepared, observer)

    # The needle sits past the per-line match bound, so it is not observed and
    # the truncation is named in evidence rather than silently dropped.
    assert result.value == TriState.FALSE
    assert result.evidence["line_truncated"] is True


def test_log_pattern_journal_caps_lines_and_counts_drops(tmp_path: Path) -> None:
    log = tmp_path / "train.log"
    log.write_text("", encoding="utf-8")
    observer = context(tmp_path, Clock())
    prepared = prepare_condition(
        _log_condition(log, patterns=[{"name": "hit", "regex": "hit"}]),
        observer,
        monitor_id="monitor",
        receipt_token="token",
    )
    with log.open("a", encoding="utf-8") as handle:
        handle.write("".join(f"hit {index}\n" for index in range(60)))

    result = evaluate_condition(prepared, observer)

    assert result.value == TriState.TRUE
    assert prepared["journal"]["dropped"] == 10
    assert len(prepared["journal"]["lines"]) == 50
    assert prepared["journal"]["lines"][-1] == "hit 59"
    assert result.evidence["journal"] == {"lines_stored": 50, "dropped": 10}
    # Only a bounded witness sample travels with the evidence.
    assert len(result.evidence["matched_lines"]) == 5


def test_log_pattern_truth_latches_so_all_composition_stays_satisfiable(tmp_path: Path) -> None:
    log = tmp_path / "train.log"
    log.write_text("", encoding="utf-8")
    observer = context(tmp_path, Clock())
    prepared = prepare_condition(
        {
            "type": "all",
            "children": [
                _log_condition(log, patterns=[{"name": "ready", "regex": "Ready"}]),
                {"type": "time", "after_seconds": 0},
            ],
        },
        observer,
        monitor_id="monitor",
        receipt_token="token",
    )
    with log.open("a", encoding="utf-8") as handle:
        handle.write("Ready\n")

    first = evaluate_condition(prepared, observer)
    # A later poll consumes no new bytes; a non-latching leaf would flap false
    # here and the `all` would never be satisfiable again.
    second = evaluate_condition(prepared, observer)

    assert first.value == TriState.TRUE
    assert second.value == TriState.TRUE
    assert sorted(item["type"] for item in second.witness) == ["log_pattern", "time"]


def _child(runtime_status: str = "active", *, is_loaded: bool = True) -> dict:
    return {
        "thread_id": "child-thread",
        "runtime_status": runtime_status,
        "is_loaded": is_loaded,
        "goal_status": "active",
        "usage": {"tokens_used": 1200, "time_used_seconds": 42, "token_budget": 8000},
    }


def _thread_context(tmp_path: Path, observations: dict) -> ObserverContext:
    return ObserverContext(
        runtime_root=tmp_path, now=Clock().now, thread_observations=observations
    )


def test_thread_idle_rejects_an_unloaded_child_at_arm(tmp_path: Path) -> None:
    for observations in ({}, {"child-thread": None}, {"child-thread": _child(is_loaded=False)}):
        with pytest.raises(ValidationError):
            prepare_condition(
                {"type": "thread_idle", "thread_id": "child-thread"},
                _thread_context(tmp_path, observations),
                monitor_id="monitor",
                receipt_token="token",
            )


def test_thread_idle_rejects_an_already_idle_child_without_the_opt_in(tmp_path: Path) -> None:
    observations = {"child-thread": _child("idle")}
    with pytest.raises(ValidationError) as caught:
        prepare_condition(
            {"type": "thread_idle", "thread_id": "child-thread"},
            _thread_context(tmp_path, observations),
            monitor_id="monitor",
            receipt_token="token",
        )
    assert "already idle" in str(caught.value)

    accepted = prepare_condition(
        {"type": "thread_idle", "thread_id": "child-thread", "accept_already_idle": True},
        _thread_context(tmp_path, observations),
        monitor_id="monitor",
        receipt_token="token",
    )
    assert accepted["accept_already_idle"]


def test_thread_idle_is_true_once_the_child_is_observed_idle(tmp_path: Path) -> None:
    prepared = prepare_condition(
        {"type": "thread_idle", "thread_id": "child-thread"},
        _thread_context(tmp_path, {"child-thread": _child("active")}),
        monitor_id="monitor",
        receipt_token="token",
    )

    running = evaluate_condition(prepared, _thread_context(tmp_path, {"child-thread": _child("active")}))
    finished = evaluate_condition(prepared, _thread_context(tmp_path, {"child-thread": _child("idle")}))
    # Latching: a child that starts another turn must not un-satisfy the leaf.
    later = evaluate_condition(prepared, _thread_context(tmp_path, {"child-thread": _child("active")}))

    assert running.value == TriState.FALSE
    assert finished.value == TriState.TRUE
    assert later.value == TriState.TRUE
    assert finished.evidence["child"]["usage"]["tokens_used"] == 1200
    assert finished.evidence["classification"] == "heuristic_thread_lifecycle"
    assert not witness_authorizes_continuation(
        finished.witness, allow_heuristic_continuation=False
    )


@pytest.mark.parametrize(
    ("observations", "kind"),
    [
        ({}, "thread_observation_missing"),
        ({"child-thread": None}, "thread_unloaded"),
        ({"child-thread": _child("notLoaded", is_loaded=False)}, "thread_unloaded"),
    ],
)
def test_thread_idle_disappearance_is_unknown_never_completion(
    tmp_path: Path, observations, kind
) -> None:
    prepared = prepare_condition(
        {"type": "thread_idle", "thread_id": "child-thread"},
        _thread_context(tmp_path, {"child-thread": _child("active")}),
        monitor_id="monitor",
        receipt_token="token",
    )

    result = evaluate_condition(prepared, _thread_context(tmp_path, observations))

    assert result.value == TriState.UNKNOWN
    assert not result.fatal
    assert result.evidence["kind"] == kind


def test_thread_idle_composes_under_all_and_any(tmp_path: Path) -> None:
    arming = _thread_context(tmp_path, {"a": {**_child(), "thread_id": "a"}, "b": {**_child(), "thread_id": "b"}})
    prepared = prepare_condition(
        {
            "type": "all",
            "children": [
                {"type": "thread_idle", "thread_id": "a"},
                {"type": "thread_idle", "thread_id": "b"},
            ],
        },
        arming,
        monitor_id="monitor",
        receipt_token="token",
    )

    half = evaluate_condition(
        prepared,
        _thread_context(tmp_path, {"a": {**_child("idle"), "thread_id": "a"}, "b": {**_child("active"), "thread_id": "b"}}),
    )
    both = evaluate_condition(
        prepared,
        _thread_context(tmp_path, {"a": {**_child("idle"), "thread_id": "a"}, "b": {**_child("idle"), "thread_id": "b"}}),
    )

    assert half.value == TriState.FALSE
    assert both.value == TriState.TRUE
    assert len(both.witness) == 2


def test_log_pattern_read_budget_reports_unknown_but_still_makes_progress(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setattr(conditions_module, "LOG_READ_BUDGET_BYTES", 64)
    log = tmp_path / "train.log"
    log.write_text("", encoding="utf-8")
    observer = context(tmp_path, Clock())
    prepared = prepare_condition(
        _log_condition(log, patterns=[{"name": "ready", "regex": "Ready"}]),
        observer,
        monitor_id="monitor",
        receipt_token="token",
    )
    with log.open("a", encoding="utf-8") as handle:
        handle.write("".join(f"noise {index:02d}\n" for index in range(20)))
        handle.write("Ready to serve\n")

    first = evaluate_condition(prepared, observer)

    # More bytes are pending than one poll may read, so the leaf is honest
    # about the budget instead of silently claiming "no match".
    assert first.value == TriState.UNKNOWN
    assert not first.fatal
    assert first.evidence["kind"] == "pattern_budget_exceeded"
    assert first.evidence["read_budget_exceeded"] is True
    offset = prepared["offset"]
    assert 0 < offset <= 64

    # A bounded read is progress, not a stall: later polls resume from the
    # durable offset and the eventual match still latches true.
    results = []
    for _ in range(10):
        results.append(evaluate_condition(prepared, observer))
        assert prepared["offset"] >= offset
        offset = prepared["offset"]
        if results[-1].value == TriState.TRUE:
            break

    assert results[-1].value == TriState.TRUE
    assert results[-1].evidence["matched"] == ["ready"]


def test_log_pattern_match_inside_the_budgeted_window_wins_over_the_budget(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setattr(conditions_module, "LOG_READ_BUDGET_BYTES", 64)
    log = tmp_path / "train.log"
    log.write_text("", encoding="utf-8")
    observer = context(tmp_path, Clock())
    prepared = prepare_condition(
        _log_condition(log, patterns=[{"name": "crash", "regex": "Killed"}]),
        observer,
        monitor_id="monitor",
        receipt_token="token",
    )
    with log.open("a", encoding="utf-8") as handle:
        handle.write("Killed\n")
        handle.write("".join(f"trailing {index:02d}\n" for index in range(20)))

    result = evaluate_condition(prepared, observer)

    assert result.value == TriState.TRUE
    assert result.evidence["matched"] == ["crash"]


def test_log_pattern_wall_clock_guard_yields_unknown_not_a_stalled_daemon(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setattr(conditions_module, "LOG_EVALUATION_SECONDS", 0.0)
    log = tmp_path / "train.log"
    log.write_text("", encoding="utf-8")
    clock = Clock()
    observer = context(tmp_path, clock)
    prepared = prepare_condition(
        _log_condition(log, patterns=[{"name": "ready", "regex": "Ready"}]),
        observer,
        monitor_id="monitor",
        receipt_token="token",
    )
    with log.open("a", encoding="utf-8") as handle:
        handle.write("Ready\n")

    def ticking_now() -> float:
        clock.value += 1.0
        return clock.value

    result = evaluate_condition(
        prepared, ObserverContext(runtime_root=tmp_path, now=ticking_now)
    )

    assert result.value == TriState.UNKNOWN
    assert result.evidence["kind"] == "pattern_budget_exceeded"
    assert result.evidence["wall_clock_exceeded"] is True


def test_latch_state_persists_only_through_the_passed_in_mapping(tmp_path: Path) -> None:
    # The service persists exactly the mapping it passed in; latching leaves
    # (offsets, journals, edges) depend on in-place mutation of that object,
    # including nested composite children. A copy-then-evaluate refactor would
    # break this contract, so pin it structurally, not just behaviorally.
    clock = Clock()
    observer = ObserverContext(runtime_root=tmp_path, now=clock.now)
    log = tmp_path / "train.log"
    log.write_text("")
    prepared = prepare_condition(
        {
            "type": "all",
            "children": [
                _log_condition(log, patterns=[{"name": "ready", "regex": "Ready"}]),
                {"type": "time", "after_seconds": 3600},
            ],
        },
        observer,
        monitor_id="monitor",
        receipt_token="token",
    )
    child_before = prepared["children"][0]
    with log.open("a", encoding="utf-8") as handle:
        handle.write("Ready\n")

    evaluate_condition(prepared, observer)

    child_after = prepared["children"][0]
    assert child_after is child_before
    assert child_after["matched"] is True
    assert child_after["matched_names"] == ["ready"]
    assert child_after["offset"] > 0
    assert child_after["journal"] == {"lines": ["Ready"], "dropped": 0}


def test_evaluating_an_immutable_child_is_rejected_not_silently_unlatched(
    tmp_path: Path,
) -> None:
    from types import MappingProxyType

    clock = Clock()
    observer = ObserverContext(runtime_root=tmp_path, now=clock.now)
    frozen_child = MappingProxyType({"type": "time", "deadline_utc": 0.0})
    result = evaluate_condition(
        {"type": "any", "children": [frozen_child]}, observer
    )
    assert result.value == TriState.UNKNOWN
    assert result.fatal is True
    assert result.evidence["kind"] == "invalid_stored_children"

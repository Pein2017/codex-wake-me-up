from __future__ import annotations

import asyncio
import os
from pathlib import Path
import subprocess
import sys
import threading

from codex_wake_me_up import daemon
from codex_wake_me_up.runtime import (
    _daemon_lock_is_held,
    daemon_is_healthy,
    ensure_daemon_ready,
)


class StopLoop(Exception):
    pass


class FakeLedger:
    def __init__(self, records) -> None:
        self.records = records

    def list(self, *, include_terminal: bool = False):
        assert not include_terminal
        return self.records()


class FakeService:
    def __init__(self, records) -> None:
        self.ledger = FakeLedger(records)
        self.reconciled = 0

    def recover_after_daemon_start(self) -> None:
        pass

    async def reconcile_once(self) -> None:
        self.reconciled += 1


def test_empty_daemon_exits_only_after_the_fixed_idle_grace(monkeypatch, tmp_path) -> None:
    clock = {"now": 0.0}
    service = FakeService(lambda: [])
    heartbeats: list[bool] = []

    async def sleep(seconds: float) -> None:
        clock["now"] += seconds
        if clock["now"] > 60:
            raise StopLoop

    monkeypatch.setattr(daemon, "MonitorService", lambda _root: service)
    monkeypatch.setattr(
        daemon,
        "write_heartbeat",
        lambda _root, *, accepting_work=True: heartbeats.append(accepting_work),
    )

    assert asyncio.run(
        daemon.run_daemon(
            tmp_path,
            interval_seconds=20,
            monotonic=lambda: clock["now"],
            sleep=sleep,
        )
    ) == 0
    assert clock["now"] == 60
    assert heartbeats[-1] is False


def test_nonterminal_work_resets_idle_grace(monkeypatch, tmp_path) -> None:
    clock = {"now": 0.0}
    service = FakeService(lambda: [object()] if 40 <= clock["now"] < 80 else [])
    heartbeats: list[bool] = []

    async def sleep(seconds: float) -> None:
        clock["now"] += seconds
        if clock["now"] > 140:
            raise StopLoop

    monkeypatch.setattr(daemon, "MonitorService", lambda _root: service)
    monkeypatch.setattr(
        daemon,
        "write_heartbeat",
        lambda _root, *, accepting_work=True: heartbeats.append(accepting_work),
    )

    assert asyncio.run(
        daemon.run_daemon(
            tmp_path,
            interval_seconds=20,
            monotonic=lambda: clock["now"],
            sleep=sleep,
        )
    ) == 0
    assert clock["now"] == 140
    assert heartbeats.count(False) == 1


def test_work_after_retirement_marker_restores_acceptance_without_terminal_write(
    monkeypatch, tmp_path
) -> None:
    clock = {"now": 0.0, "queries": 0}

    def records():
        clock["queries"] += 1
        return [object()] if clock["queries"] == 5 else []

    service = FakeService(records)
    heartbeats: list[bool] = []

    async def sleep(seconds: float) -> None:
        clock["now"] += seconds
        if clock["now"] >= 80:
            raise StopLoop

    monkeypatch.setattr(daemon, "MonitorService", lambda _root: service)
    monkeypatch.setattr(
        daemon,
        "write_heartbeat",
        lambda _root, *, accepting_work=True: heartbeats.append(accepting_work),
    )

    try:
        asyncio.run(
            daemon.run_daemon(
                tmp_path,
                interval_seconds=20,
                monotonic=lambda: clock["now"],
                sleep=sleep,
            )
        )
    except StopLoop:
        pass
    else:
        raise AssertionError("new work after retirement marker must keep the daemon alive")
    assert heartbeats[-2:] == [False, True]
    assert service.reconciled == 4


def test_once_keeps_its_single_pass_behavior(monkeypatch, tmp_path) -> None:
    service = FakeService(
        lambda: (_ for _ in ()).throw(AssertionError("--once must not enter idle"))
    )

    monkeypatch.setattr(daemon, "MonitorService", lambda _root: service)
    assert asyncio.run(daemon.run_daemon(tmp_path, interval_seconds=1, once=True)) == 0
    assert service.reconciled == 1


def test_retiring_lock_allows_exactly_one_replacement_for_concurrent_callers(
    tmp_path,
) -> None:
    source_root = Path(__file__).resolve().parents[1] / "src"
    environment = os.environ.copy()
    previous = environment.get("PYTHONPATH")
    environment["PYTHONPATH"] = (
        str(source_root) if not previous else f"{source_root}{os.pathsep}{previous}"
    )
    retiring_script = """
import sys
from pathlib import Path
from codex_wake_me_up.daemon import DaemonLock
from codex_wake_me_up.runtime import write_heartbeat
root = Path(sys.argv[1])
with DaemonLock(root):
    write_heartbeat(root, accepting_work=False)
    print('retiring', flush=True)
    sys.stdin.readline()
"""
    replacement_script = """
import sys
from pathlib import Path
from codex_wake_me_up.daemon import DaemonLock
from codex_wake_me_up.runtime import write_heartbeat
root = Path(sys.argv[1])
with DaemonLock(root):
    write_heartbeat(root)
    print('ready', flush=True)
    sys.stdin.readline()
"""
    retiring = subprocess.Popen(
        [sys.executable, "-c", retiring_script, str(tmp_path)],
        env=environment,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        text=True,
    )
    assert retiring.stdin is not None and retiring.stdout is not None
    children: list[subprocess.Popen[str]] = []
    seen_retiring_lock = threading.Event()
    results: list[bool] = []
    errors: list[BaseException] = []

    def starter(root: Path) -> bool:
        child = subprocess.Popen(
            [sys.executable, "-c", replacement_script, str(root)],
            env=environment,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            text=True,
        )
        assert child.stdout is not None
        assert child.stdout.readline() == "ready\n"
        children.append(child)
        return True

    def lock_held(root: Path) -> bool:
        held = _daemon_lock_is_held(root)
        if held:
            seen_retiring_lock.set()
        return held

    def caller() -> None:
        try:
            results.append(
                ensure_daemon_ready(
                    tmp_path,
                    timeout_seconds=2,
                    poll_interval_seconds=0.01,
                    starter=starter,
                    health_check=daemon_is_healthy,
                    lock_held=lock_held,
                )
            )
        except BaseException as exc:  # surfaced after both callers are joined
            errors.append(exc)

    try:
        assert retiring.stdout.readline() == "retiring\n"
        callers = [threading.Thread(target=caller) for _ in range(2)]
        for thread in callers:
            thread.start()
        assert seen_retiring_lock.wait(timeout=1)
        retiring.stdin.write("\n")
        retiring.stdin.flush()
        retiring.wait(timeout=5)
        for thread in callers:
            thread.join(timeout=5)
        assert not errors
        assert results == [True, True]
        assert len(children) == 1
    finally:
        if retiring.poll() is None:
            retiring.stdin.write("\n")
            retiring.stdin.flush()
            retiring.wait(timeout=5)
        for child in children:
            if child.poll() is None:
                assert child.stdin is not None
                child.stdin.write("\n")
                child.stdin.flush()
                child.wait(timeout=5)

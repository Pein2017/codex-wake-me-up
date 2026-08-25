from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys
import time

from codex_wake_me_up.daemon import DaemonLock
from codex_wake_me_up.runtime import (
    DELIVERY_CAPABILITY_EPOCH,
    EVENT_CAPABILITY_EPOCH,
    atomic_write_json,
    daemon_delivery_capable,
    daemon_event_capable,
    daemon_is_healthy,
    ensure_daemon_ready,
    heartbeat_path,
    loaded_source_identity,
    write_heartbeat,
)


def test_daemon_readiness_waits_for_positive_health_without_real_sleep(tmp_path) -> None:
    clock = {"now": 0.0, "healthy": False, "starts": 0}

    def starter(_root) -> bool:
        clock["starts"] += 1
        return True

    def sleep(seconds: float) -> None:
        clock["now"] += seconds
        clock["healthy"] = True

    assert ensure_daemon_ready(
        tmp_path,
        timeout_seconds=1.0,
        poll_interval_seconds=0.1,
        starter=starter,
        health_check=lambda _root: bool(clock["healthy"]),
        monotonic=lambda: float(clock["now"]),
        sleep=sleep,
    )
    assert clock["starts"] == 1


def test_daemon_readiness_is_bounded_when_health_never_appears(tmp_path) -> None:
    clock = {"now": 0.0}

    assert not ensure_daemon_ready(
        tmp_path,
        timeout_seconds=0.2,
        poll_interval_seconds=0.05,
        starter=lambda _root: True,
        health_check=lambda _root: False,
        monotonic=lambda: float(clock["now"]),
        sleep=lambda seconds: clock.__setitem__("now", clock["now"] + seconds),
    )
    assert clock["now"] <= 0.2


def test_legacy_heartbeat_is_healthy_but_not_event_capable(tmp_path) -> None:
    atomic_write_json(
        heartbeat_path(tmp_path), {"pid": os.getpid(), "at": time.time()}
    )

    assert daemon_is_healthy(tmp_path)
    assert not daemon_event_capable(tmp_path)


def test_daemon_heartbeat_advertises_exact_event_epoch_and_source(tmp_path) -> None:
    with DaemonLock(tmp_path):
        write_heartbeat(tmp_path)

        assert daemon_event_capable(tmp_path)
        payload = json.loads(heartbeat_path(tmp_path).read_text())
        assert payload["event_capability_epoch"] == EVENT_CAPABILITY_EPOCH
        assert payload["delivery_capability_epoch"] == DELIVERY_CAPABILITY_EPOCH
        assert daemon_delivery_capable(tmp_path)
        assert payload["loaded_source_identity"] == loaded_source_identity()

    assert not daemon_event_capable(tmp_path)
    assert not daemon_delivery_capable(tmp_path)


def test_event_capability_requires_the_advertising_pid_to_own_daemon_lock(
    tmp_path,
) -> None:
    write_heartbeat(tmp_path)
    assert not daemon_event_capable(tmp_path)

    with DaemonLock(tmp_path) as lock:
        assert daemon_event_capable(tmp_path)
        assert lock.handle is not None
        lock.handle.seek(0)
        lock.handle.truncate()
        lock.handle.write("pid=999999\n")
        lock.handle.flush()
        os.fsync(lock.handle.fileno())
        assert not daemon_event_capable(tmp_path)


def test_event_capability_rejects_pid_different_from_kernel_lock_owner(
    tmp_path,
) -> None:
    """Dropping the kernel-owner PID check must make this test fail."""

    source_root = Path(__file__).resolve().parents[1] / "src"
    environment = os.environ.copy()
    previous = environment.get("PYTHONPATH")
    environment["PYTHONPATH"] = (
        str(source_root)
        if not previous
        else f"{source_root}{os.pathsep}{previous}"
    )
    script = """
import sys
from pathlib import Path
from codex_wake_me_up.daemon import DaemonLock
from codex_wake_me_up.runtime import write_heartbeat

root = Path(sys.argv[1])
with DaemonLock(root):
    write_heartbeat(root)
    print("ready", flush=True)
    sys.stdin.readline()
"""
    process = subprocess.Popen(
        [sys.executable, "-c", script, str(tmp_path)],
        env=environment,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        text=True,
    )
    assert process.stdout is not None
    assert process.stdin is not None
    try:
        assert process.stdout.readline() == "ready\n"
        assert daemon_event_capable(tmp_path)

        (tmp_path / "daemon.lock").write_text(
            f"pid={os.getpid()}\n", encoding="ascii"
        )
        write_heartbeat(tmp_path)

        assert not daemon_event_capable(tmp_path)
    finally:
        process.stdin.write("\n")
        process.stdin.flush()
        process.wait(timeout=5)

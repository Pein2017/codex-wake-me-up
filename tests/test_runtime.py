from __future__ import annotations

from codex_wake_me_up.runtime import ensure_daemon_ready


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

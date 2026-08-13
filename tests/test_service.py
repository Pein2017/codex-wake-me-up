from __future__ import annotations

import asyncio

import pytest

from codex_wake_me_up.conditions import ObserverContext
from codex_wake_me_up.ledger import Ledger
from codex_wake_me_up.models import AppServerError, MonitorState, ValidationError
from codex_wake_me_up.service import MonitorService

from .helpers import Clock, FakeAppServer, goal, observation


def _service(
    tmp_path,
    app_server,
    clock: Clock,
    *,
    daemon_readiness=lambda _root: True,
) -> MonitorService:
    return MonitorService(
        tmp_path,
        ledger=Ledger(tmp_path),
        app_server_factory=lambda: app_server,
        observer_context_factory=lambda: ObserverContext(runtime_root=tmp_path, now=clock.now),
        daemon_starter=lambda _root: False,
        daemon_readiness=daemon_readiness,
    )


def test_active_main_thread_can_register_its_own_paused_goal(tmp_path) -> None:
    clock = Clock()
    fake = FakeAppServer([observation(runtime_status="active"), observation(runtime_status="active")])
    service = _service(tmp_path, fake, clock)

    result = asyncio.run(
        service.register(
            thread_id="test-thread",
            condition={"type": "time", "after_seconds": 10},
            expires_in_seconds=100,
            start_daemon=False,
        )
    )

    assert result["state"] == MonitorState.ARMED
    assert result["mode"] == "legacy"
    assert not result["idle_barrier"]


def test_active_defer_orders_intent_readiness_pause_and_confirmation(tmp_path) -> None:
    clock = Clock()
    marker = goal()
    events: list[tuple[str, MonitorState]] = []
    service = None

    def readiness(_root) -> bool:
        assert service is not None
        events.append(("readiness", service.ledger.list()[0].state))
        return True

    def on_pause() -> None:
        assert service is not None
        events.append(("pause", service.ledger.list()[0].state))

    fake = FakeAppServer(
        [
            observation(runtime_status="active", goal_status="active", marker=marker),
            observation(runtime_status="active", goal_status="paused", marker=marker),
        ],
        pause=observation(
            runtime_status="active", goal_status="paused", marker=marker
        ),
        on_pause=on_pause,
    )
    service = _service(
        tmp_path,
        fake,
        clock,
        daemon_readiness=readiness,
    )

    result = asyncio.run(
        service.defer(
            thread_id="test-thread",
            condition={"type": "time", "after_seconds": 10},
            expires_in_seconds=100,
            idempotency_key="defer",
        )
    )

    assert events == [
        ("readiness", MonitorState.DEFER_INTENT),
        ("pause", MonitorState.PAUSING),
    ]
    assert fake.pause_calls == ["test-thread"]
    assert result["state"] == MonitorState.ARMED
    assert result["mode"] == "deferred"
    assert result["idle_barrier"]
    assert result["targeting"]["authenticated_current_task"] is False
    assert result["next_action"] == "end_current_turn"


def test_defer_readiness_failure_is_terminal_and_does_not_pause(tmp_path) -> None:
    clock = Clock()
    fake = FakeAppServer(
        [observation(runtime_status="active", goal_status="active")]
    )
    service = _service(
        tmp_path,
        fake,
        clock,
        daemon_readiness=lambda _root: False,
    )

    result = asyncio.run(
        service.defer(
            thread_id="test-thread",
            condition={"type": "time", "after_seconds": 10},
            expires_in_seconds=100,
            idempotency_key="defer",
        )
    )

    assert result["state"] == MonitorState.DAEMON_UNAVAILABLE
    assert result["outcome"]["kind"] == "daemon_unavailable_before_pause"
    assert fake.pause_calls == []


def test_defer_expiry_after_readiness_does_not_pause(tmp_path) -> None:
    clock = Clock()
    fake = FakeAppServer(
        [observation(runtime_status="active", goal_status="active")]
    )

    def readiness(_root) -> bool:
        clock.value += 100
        return True

    service = _service(
        tmp_path,
        fake,
        clock,
        daemon_readiness=readiness,
    )

    result = asyncio.run(
        service.defer(
            thread_id="test-thread",
            condition={"type": "time", "after_seconds": 10},
            expires_in_seconds=50,
            idempotency_key="defer",
        )
    )

    assert result["state"] == MonitorState.EXPIRED
    assert result["outcome"]["kind"] == "expired_before_pause"
    assert fake.pause_calls == []


@pytest.mark.parametrize("readiness_succeeds", [False, True])
def test_defer_cancellation_wins_readiness_and_expiry_cas(
    tmp_path, readiness_succeeds
) -> None:
    clock = Clock()
    fake = FakeAppServer(
        [observation(runtime_status="active", goal_status="active")]
    )
    service = None

    def readiness(_root) -> bool:
        assert service is not None
        record = service.ledger.list()[0]
        assert service.cancel(record.monitor_id)["state"] == MonitorState.CANCELLED
        if readiness_succeeds:
            clock.value += 100
        return readiness_succeeds

    service = _service(
        tmp_path,
        fake,
        clock,
        daemon_readiness=readiness,
    )
    result = asyncio.run(
        service.defer(
            thread_id="test-thread",
            condition={"type": "time", "after_seconds": 10},
            expires_in_seconds=50,
            idempotency_key="defer",
        )
    )

    assert result["state"] == MonitorState.CANCELLED
    assert fake.pause_calls == []


@pytest.mark.parametrize(
    ("pause_result", "expected_state"),
    [
        (AppServerError("lost response"), MonitorState.PAUSE_UNCERTAIN),
        (
            observation(runtime_status="active", goal_status="active"),
            MonitorState.PAUSE_REJECTED,
        ),
        (
            observation(
                runtime_status="active",
                goal_status="paused",
                marker=goal(created_at=2),
            ),
            MonitorState.MIS_TARGETED_PAUSE,
        ),
    ],
)
def test_defer_pause_terminal_paths_are_never_retried(
    tmp_path, pause_result, expected_state
) -> None:
    clock = Clock()
    fake = FakeAppServer(
        [observation(runtime_status="active", goal_status="active")],
        pause=pause_result,
    )
    service = _service(tmp_path, fake, clock)

    result = asyncio.run(
        service.defer(
            thread_id="test-thread",
            condition={"type": "time", "after_seconds": 10},
            expires_in_seconds=100,
            idempotency_key="defer",
        )
    )
    asyncio.run(service.reconcile_once())

    assert result["state"] == expected_state
    assert fake.pause_calls == ["test-thread"]
    assert fake.activation_calls == []


def test_defer_reobservation_guard_mismatch_is_mis_targeted(tmp_path) -> None:
    clock = Clock()
    captured = goal(created_at=1)
    fake = FakeAppServer(
        [
            observation(
                runtime_status="active", goal_status="active", marker=captured
            ),
            observation(
                runtime_status="active",
                goal_status="paused",
                marker=goal(created_at=2),
            ),
        ],
        pause=observation(
            runtime_status="active", goal_status="paused", marker=captured
        ),
    )
    service = _service(tmp_path, fake, clock)

    result = asyncio.run(
        service.defer(
            thread_id="test-thread",
            condition={"type": "time", "after_seconds": 10},
            expires_in_seconds=100,
            idempotency_key="defer",
        )
    )

    assert result["state"] == MonitorState.MIS_TARGETED_PAUSE
    assert result["outcome"]["source"] == "confirmation"
    assert fake.pause_calls == ["test-thread"]


def test_defer_pause_response_thread_mismatch_is_mis_targeted(tmp_path) -> None:
    clock = Clock()
    marker = goal()
    fake = FakeAppServer(
        [observation(runtime_status="active", goal_status="active", marker=marker)],
        pause=observation(
            thread_id="other-thread",
            runtime_status="active",
            goal_status="paused",
            marker=marker,
        ),
    )
    service = _service(tmp_path, fake, clock)

    result = asyncio.run(
        service.defer(
            thread_id="test-thread",
            condition={"type": "time", "after_seconds": 10},
            expires_in_seconds=100,
            idempotency_key="defer",
        )
    )

    assert result["state"] == MonitorState.MIS_TARGETED_PAUSE
    assert result["outcome"]["response"]["thread_id"] == "other-thread"


def test_defer_idempotency_is_separate_from_legacy_mode(tmp_path) -> None:
    clock = Clock()
    fake = FakeAppServer(
        [
            observation(runtime_status="active", goal_status="paused"),
            observation(runtime_status="active", goal_status="paused"),
            observation(runtime_status="active", goal_status="active"),
        ]
    )
    service = _service(tmp_path, fake, clock)
    asyncio.run(
        service.register(
            thread_id="test-thread",
            condition={"type": "time", "after_seconds": 1},
            expires_in_seconds=100,
            idempotency_key="same",
            start_daemon=False,
        )
    )

    with pytest.raises(ValidationError):
        asyncio.run(
            service.defer(
                thread_id="test-thread",
                condition={"type": "time", "after_seconds": 1},
                expires_in_seconds=100,
                idempotency_key="same",
            )
        )


def test_identical_defer_retry_returns_armed_monitor_without_second_pause(tmp_path) -> None:
    clock = Clock()
    marker = goal()
    fake = FakeAppServer(
        [
            observation(runtime_status="active", goal_status="active", marker=marker),
            observation(runtime_status="active", goal_status="paused", marker=marker),
            observation(runtime_status="idle", goal_status="paused", marker=marker),
        ],
        pause=observation(
            runtime_status="active", goal_status="paused", marker=marker
        ),
    )
    service = _service(tmp_path, fake, clock)
    request = {
        "thread_id": "test-thread",
        "condition": {"type": "time", "after_seconds": 10},
        "expires_in_seconds": 100,
        "idempotency_key": "same",
    }

    first = asyncio.run(service.defer(**request))
    second = asyncio.run(service.defer(**request))

    assert second["monitor_id"] == first["monitor_id"]
    assert second["next_action"] == "end_current_turn"
    assert fake.pause_calls == ["test-thread"]


def test_deferred_true_condition_waits_for_idle_before_one_activation(tmp_path) -> None:
    clock = Clock()
    marker = goal()
    fake = FakeAppServer(
        [
            observation(runtime_status="active", goal_status="active", marker=marker),
            observation(runtime_status="active", goal_status="paused", marker=marker),
            observation(runtime_status="active", goal_status="paused", marker=marker),
            observation(runtime_status="idle", goal_status="paused", marker=marker),
            observation(runtime_status="idle", goal_status="paused", marker=marker),
        ],
        pause=observation(
            runtime_status="active", goal_status="paused", marker=marker
        ),
        activation=observation(
            runtime_status="idle", goal_status="active", marker=marker
        ),
    )
    service = _service(tmp_path, fake, clock)
    registered = asyncio.run(
        service.defer(
            thread_id="test-thread",
            condition={"type": "time", "after_seconds": 0},
            expires_in_seconds=100,
            allow_heuristic_continuation=True,
            idempotency_key="defer",
        )
    )

    asyncio.run(service.reconcile_once())
    assert service.status(registered["monitor_id"])["state"] == MonitorState.ARMED
    assert fake.activation_calls == []

    asyncio.run(service.reconcile_once())
    asyncio.run(service.reconcile_once())
    assert service.status(registered["monitor_id"])["state"] == MonitorState.FIRED
    assert fake.activation_calls == ["test-thread"]


def test_time_witness_without_opt_in_terminates_without_activation(tmp_path) -> None:
    clock = Clock()
    fake = FakeAppServer(
        [
            observation(runtime_status="active"),
            observation(runtime_status="active"),
        ]
    )
    service = _service(tmp_path, fake, clock)
    registered = asyncio.run(
        service.register(
            thread_id="test-thread",
            condition={"type": "time", "after_seconds": 0},
            expires_in_seconds=100,
            allow_heuristic_continuation=False,
            start_daemon=False,
        )
    )

    asyncio.run(service.reconcile_once())

    assert service.status(registered["monitor_id"])["state"] == MonitorState.SATISFIED_REQUIRES_AUTHORIZATION
    assert fake.activation_calls == []


def test_guarded_activation_is_once_and_confirms_same_goal(tmp_path) -> None:
    clock = Clock()
    marker = goal()
    fake = FakeAppServer(
        [
            observation(runtime_status="active", marker=marker),
            observation(runtime_status="active", marker=marker),
            observation(runtime_status="idle", marker=marker),
        ],
        activation=observation(runtime_status="idle", goal_status="active", marker=marker),
    )
    service = _service(tmp_path, fake, clock)
    registered = asyncio.run(
        service.register(
            thread_id="test-thread",
            condition={"type": "time", "after_seconds": 0},
            expires_in_seconds=100,
            allow_heuristic_continuation=True,
            start_daemon=False,
        )
    )

    asyncio.run(service.reconcile_once())
    asyncio.run(service.reconcile_once())

    assert service.status(registered["monitor_id"])["state"] == MonitorState.FIRED
    assert fake.activation_calls == ["test-thread"]


def test_claim_expiry_immediately_before_activation_does_not_send(tmp_path) -> None:
    clock = Clock()
    marker = goal()

    class ExpiringFakeAppServer(FakeAppServer):
        def __init__(self) -> None:
            super().__init__(
                [
                    observation(runtime_status="active", marker=marker),
                    observation(runtime_status="active", marker=marker),
                    observation(runtime_status="idle", marker=marker),
                ]
            )
            self.read_count = 0

        async def read_observation(self, thread_id):
            result = await super().read_observation(thread_id)
            self.read_count += 1
            if self.read_count == 3:
                clock.value += 100
            return result

    fake = ExpiringFakeAppServer()
    service = _service(tmp_path, fake, clock)
    registered = asyncio.run(
        service.register(
            thread_id="test-thread",
            condition={"type": "time", "after_seconds": 0},
            expires_in_seconds=50,
            allow_heuristic_continuation=True,
            start_daemon=False,
        )
    )

    asyncio.run(service.reconcile_once())

    status = service.status(registered["monitor_id"])
    assert status["state"] == MonitorState.EXPIRED
    assert status["outcome"]["kind"] == "expired_before_activation"
    assert fake.activation_calls == []


def test_changed_goal_response_is_mis_targeted_and_never_retried(tmp_path) -> None:
    clock = Clock()
    captured = goal(created_at=1)
    changed = goal(created_at=2, objective="new goal")
    fake = FakeAppServer(
        [
            observation(runtime_status="active", marker=captured),
            observation(runtime_status="active", marker=captured),
            observation(runtime_status="idle", marker=captured),
        ],
        activation=observation(runtime_status="idle", goal_status="active", marker=changed),
    )
    service = _service(tmp_path, fake, clock)
    registered = asyncio.run(
        service.register(
            thread_id="test-thread",
            condition={"type": "time", "after_seconds": 0},
            expires_in_seconds=100,
            allow_heuristic_continuation=True,
            start_daemon=False,
        )
    )

    asyncio.run(service.reconcile_once())
    asyncio.run(service.reconcile_once())

    status = service.status(registered["monitor_id"])
    assert status["state"] == MonitorState.MIS_TARGETED_ACTIVATION
    assert status["outcome"]["captured_goal"]["created_at"] == 1
    assert status["outcome"]["returned_goal"]["created_at"] == 2
    assert fake.activation_calls == ["test-thread"]


def test_changed_thread_response_is_mis_targeted_activation(tmp_path) -> None:
    clock = Clock()
    marker = goal()
    fake = FakeAppServer(
        [
            observation(runtime_status="active", marker=marker),
            observation(runtime_status="active", marker=marker),
            observation(runtime_status="idle", marker=marker),
        ],
        activation=observation(
            thread_id="other-thread",
            runtime_status="idle",
            goal_status="active",
            marker=marker,
        ),
    )
    service = _service(tmp_path, fake, clock)
    registered = asyncio.run(
        service.register(
            thread_id="test-thread",
            condition={"type": "time", "after_seconds": 0},
            expires_in_seconds=100,
            allow_heuristic_continuation=True,
            start_daemon=False,
        )
    )

    asyncio.run(service.reconcile_once())

    status = service.status(registered["monitor_id"])
    assert status["state"] == MonitorState.MIS_TARGETED_ACTIVATION
    assert status["outcome"]["returned_thread_id"] == "other-thread"
    assert fake.activation_calls == ["test-thread"]


def test_idempotency_conflict_is_rejected(tmp_path) -> None:
    clock = Clock()
    fake = FakeAppServer(
        [
            observation(runtime_status="active"),
            observation(runtime_status="active"),
            observation(runtime_status="active"),
        ]
    )
    service = _service(tmp_path, fake, clock)
    asyncio.run(
        service.register(
            thread_id="test-thread",
            condition={"type": "time", "after_seconds": 1},
            expires_in_seconds=100,
            idempotency_key="same",
            start_daemon=False,
        )
    )
    with pytest.raises(ValidationError):
        asyncio.run(
            service.register(
                thread_id="test-thread",
                condition={"type": "time", "after_seconds": 2},
                expires_in_seconds=100,
                idempotency_key="same",
                start_daemon=False,
            )
        )


def test_identical_idempotency_key_returns_original_monitor(tmp_path) -> None:
    clock = Clock()
    fake = FakeAppServer(
        [
            observation(runtime_status="active"),
            observation(runtime_status="active"),
            observation(runtime_status="active"),
        ]
    )
    service = _service(tmp_path, fake, clock)
    first = asyncio.run(
        service.register(
            thread_id="test-thread",
            condition={"type": "time", "after_seconds": 1},
            expires_in_seconds=100,
            idempotency_key="same",
            start_daemon=False,
        )
    )
    second = asyncio.run(
        service.register(
            thread_id="test-thread",
            condition={"type": "time", "after_seconds": 1},
            expires_in_seconds=100,
            idempotency_key="same",
            start_daemon=False,
        )
    )

    assert second["monitor_id"] == first["monitor_id"]


def test_unloaded_target_never_sends_an_activation(tmp_path) -> None:
    clock = Clock()
    fake = FakeAppServer(
        [
            observation(runtime_status="active"),
            observation(runtime_status="active"),
            observation(
                runtime_status="notLoaded",
                is_loaded=False,
                goal_status=None,
            ),
        ]
    )
    service = _service(tmp_path, fake, clock)
    registered = asyncio.run(
        service.register(
            thread_id="test-thread",
            condition={"type": "time", "after_seconds": 0},
            expires_in_seconds=100,
            allow_heuristic_continuation=True,
            start_daemon=False,
        )
    )

    asyncio.run(service.reconcile_once())

    assert service.status(registered["monitor_id"])["state"] == MonitorState.UNLOADED_TARGET
    assert fake.activation_calls == []


def test_unloaded_target_is_rejected_at_registration(tmp_path) -> None:
    clock = Clock()
    fake = FakeAppServer(
        [observation(runtime_status="notLoaded", is_loaded=False, goal_status=None)]
    )
    service = _service(tmp_path, fake, clock)

    with pytest.raises(ValidationError):
        asyncio.run(
            service.register(
                thread_id="test-thread",
                condition={"type": "time", "after_seconds": 1},
                expires_in_seconds=100,
                start_daemon=False,
            )
        )


def test_raw_preflight_exception_becomes_a_non_firing_terminal_outcome(tmp_path) -> None:
    class BrokenAppServer:
        async def __aenter__(self):
            raise OSError("socket stopped accepting")

        async def __aexit__(self, *_args):
            return None

    clock = Clock()
    registration_server = FakeAppServer(
        [observation(runtime_status="active"), observation(runtime_status="active")]
    )
    factories = iter([registration_server, BrokenAppServer()])
    service = MonitorService(
        tmp_path,
        ledger=Ledger(tmp_path),
        app_server_factory=lambda: next(factories),
        observer_context_factory=lambda: ObserverContext(runtime_root=tmp_path, now=clock.now),
        daemon_starter=lambda _root: False,
    )
    registered = asyncio.run(
        service.register(
            thread_id="test-thread",
            condition={"type": "time", "after_seconds": 0},
            expires_in_seconds=100,
            allow_heuristic_continuation=True,
            start_daemon=False,
        )
    )

    asyncio.run(service.reconcile_once())

    status = service.status(registered["monitor_id"])
    assert status["state"] == MonitorState.ACTIVATION_FAILED
    assert status["outcome"]["kind"] == "unexpected_monitor_failure"


def test_receipt_publication_uses_the_monitor_specific_token(tmp_path) -> None:
    clock = Clock()
    fake = FakeAppServer([observation(runtime_status="active"), observation(runtime_status="active")])
    service = _service(tmp_path, fake, clock)
    registered = asyncio.run(
        service.register(
            thread_id="test-thread",
            condition={"type": "receipt_success"},
            expires_in_seconds=100,
            start_daemon=False,
        )
    )
    token = registered["receipt_instructions"][0]["token"]

    published = service.publish_receipt(
        monitor_id=registered["monitor_id"], token=token, status="success"
    )

    assert published["status"] == "success"
    with pytest.raises(ValidationError):
        service.publish_receipt(
            monitor_id=registered["monitor_id"], token="wrong", status="success"
        )

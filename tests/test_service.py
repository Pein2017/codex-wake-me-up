from __future__ import annotations

import asyncio
from dataclasses import replace

import pytest

from codex_wake_me_up.conditions import ObserverContext
from codex_wake_me_up.ledger import Ledger
from codex_wake_me_up.models import (
    AppServerError,
    MonitorState,
    ValidationError,
    WakeReason,
)
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


def test_paused_defer_uses_stable_capture_without_another_pause(tmp_path) -> None:
    clock = Clock()
    marker = goal()
    fake = FakeAppServer(
        [
            observation(runtime_status="idle", goal_status="paused", marker=marker),
            observation(runtime_status="idle", goal_status="paused", marker=marker),
        ]
    )
    service = _service(tmp_path, fake, clock)

    result = asyncio.run(
        service.defer(
            thread_id="test-thread",
            condition={"type": "time", "after_seconds": 10},
            expires_in_seconds=100,
            idempotency_key="defer-paused",
        )
    )

    assert fake.pause_calls == []
    assert not fake.observations
    assert result["state"] == MonitorState.ARMED
    assert result["mode"] == "deferred"
    assert result["idle_barrier"]
    assert result["outcome"]["kind"] == "paused_guard_confirmed"
    assert result["next_action"] == "end_current_turn"


def test_defer_without_goal_rejects_without_creating_or_pausing(tmp_path) -> None:
    clock = Clock()
    fake = FakeAppServer([observation(runtime_status="active", goal_status=None)])
    service = _service(tmp_path, fake, clock)

    with pytest.raises(
        ValidationError,
        match="Do not create a goal to satisfy this precondition",
    ):
        asyncio.run(
            service.defer(
                thread_id="test-thread",
                condition={"type": "time", "after_seconds": 10},
                expires_in_seconds=100,
                idempotency_key="defer-without-goal",
            )
        )

    assert fake.pause_calls == []
    assert service.ledger.list() == []


def test_register_without_goal_rejects_without_creating_monitor(tmp_path) -> None:
    clock = Clock()
    fake = FakeAppServer([observation(runtime_status="active", goal_status=None)])
    service = _service(tmp_path, fake, clock)

    with pytest.raises(
        ValidationError,
        match="Do not create a goal to satisfy this precondition",
    ):
        asyncio.run(
            service.register(
                thread_id="test-thread",
                condition={"type": "time", "after_seconds": 10},
                expires_in_seconds=100,
                start_daemon=False,
            )
        )

    assert service.ledger.list() == []


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


# --- Deferred terminal-wake policy -------------------------------------------


def _armed_deferred(
    tmp_path,
    clock: Clock,
    *,
    condition,
    expires_in_seconds: float = 100,
    allow_heuristic_continuation: bool = False,
    idle_after_arm: bool = True,
    marker=None,
    thread_observations=None,
    observer_context_factory=None,
    app_server_factory=None,
):
    """Arm one deferred monitor and hand back its service, fake, and receipt."""

    selected = marker if marker is not None else goal()
    observations = [
        observation(runtime_status="active", goal_status="active", marker=selected),
        observation(runtime_status="active", goal_status="paused", marker=selected),
    ]
    if idle_after_arm:
        observations.append(
            observation(runtime_status="idle", goal_status="paused", marker=selected)
        )
    fake = FakeAppServer(
        observations,
        pause=observation(runtime_status="active", goal_status="paused", marker=selected),
        activation=observation(runtime_status="idle", goal_status="active", marker=selected),
        thread_observations=thread_observations,
    )
    service = MonitorService(
        tmp_path,
        ledger=Ledger(tmp_path),
        app_server_factory=app_server_factory or (lambda: fake),
        observer_context_factory=observer_context_factory
        or (lambda: ObserverContext(runtime_root=tmp_path, now=clock.now)),
        daemon_starter=lambda _root: False,
        daemon_readiness=lambda _root: True,
    )
    registered = asyncio.run(
        service.defer(
            thread_id="test-thread",
            condition=condition,
            expires_in_seconds=expires_in_seconds,
            allow_heuristic_continuation=allow_heuristic_continuation,
            idempotency_key="defer",
        )
    )
    assert registered["state"] == MonitorState.ARMED
    return service, fake, registered


def test_deferred_expiry_wakes_the_goal_instead_of_stranding_it(tmp_path) -> None:
    clock = Clock()
    service, fake, registered = _armed_deferred(
        tmp_path,
        clock,
        condition={"type": "time", "after_seconds": 100_000},
        expires_in_seconds=50,
    )

    clock.value += 100
    asyncio.run(service.reconcile_once())

    status = service.status(registered["monitor_id"])
    assert status["state"] == MonitorState.FIRED
    assert status["wake_reason"] == "expired"
    assert status["outcome"]["wake_reason"] == "expired"
    assert fake.activation_calls == ["test-thread"]


def test_deferred_expiry_while_target_is_busy_stays_armed_then_fires_on_idle(tmp_path) -> None:
    clock = Clock()
    marker = goal()
    service, fake, registered = _armed_deferred(
        tmp_path,
        clock,
        condition={"type": "time", "after_seconds": 100_000},
        expires_in_seconds=50,
        idle_after_arm=False,
        marker=marker,
    )

    clock.value += 100
    asyncio.run(service.reconcile_once())

    assert service.status(registered["monitor_id"])["state"] == MonitorState.ARMED
    assert fake.activation_calls == []

    fake.observations.append(
        observation(runtime_status="idle", goal_status="paused", marker=marker)
    )
    asyncio.run(service.reconcile_once())

    status = service.status(registered["monitor_id"])
    assert status["state"] == MonitorState.FIRED
    assert status["outcome"]["wake_reason"] == "expired"
    assert fake.activation_calls == ["test-thread"]


def test_deferred_unauthorized_heuristic_evidence_wakes_with_an_honest_label(tmp_path) -> None:
    clock = Clock()
    service, fake, registered = _armed_deferred(
        tmp_path,
        clock,
        condition={"type": "time", "after_seconds": 0},
        allow_heuristic_continuation=False,
    )

    asyncio.run(service.reconcile_once())

    status = service.status(registered["monitor_id"])
    assert status["state"] == MonitorState.FIRED
    assert status["wake_reason"] == "unauthorized_evidence"
    assert status["outcome"]["wake_reason"] == "unauthorized_evidence"
    # The witness is retained and makes no success claim.
    assert [item["type"] for item in status["outcome"]["witness"]] == ["time"]
    assert fake.activation_calls == ["test-thread"]


def test_deferred_typed_fatal_observer_wakes_with_the_failure_detail(tmp_path) -> None:
    clock = Clock()
    log = tmp_path / "job.log"
    log.write_text("starting\n", encoding="utf-8")
    service, fake, registered = _armed_deferred(
        tmp_path,
        clock,
        condition={
            "type": "log_pattern",
            "path": str(log),
            "patterns": [{"name": "ready", "regex": "Ready"}],
        },
    )
    log.unlink()

    asyncio.run(service.reconcile_once())

    status = service.status(registered["monitor_id"])
    assert status["state"] == MonitorState.FIRED
    assert status["outcome"]["wake_reason"] == "observer_failed"
    assert status["outcome"]["failure_detail"]["kind"] == "log_identity_lost"
    assert fake.activation_calls == ["test-thread"]


def test_deferred_cancellation_wins_over_a_deadline_wake(tmp_path) -> None:
    clock = Clock()
    service, fake, registered = _armed_deferred(
        tmp_path,
        clock,
        condition={"type": "time", "after_seconds": 100_000},
        expires_in_seconds=50,
    )
    assert service.cancel(registered["monitor_id"])["state"] == MonitorState.CANCELLED

    clock.value += 100
    asyncio.run(service.reconcile_once())

    assert service.status(registered["monitor_id"])["state"] == MonitorState.CANCELLED
    assert fake.activation_calls == []


def test_deferred_guard_change_at_the_deadline_supersedes_without_activation(tmp_path) -> None:
    clock = Clock()
    captured = goal(created_at=1)
    fake = FakeAppServer(
        [
            observation(runtime_status="active", goal_status="active", marker=captured),
            observation(runtime_status="active", goal_status="paused", marker=captured),
            observation(
                runtime_status="idle", goal_status="paused", marker=goal(created_at=2)
            ),
        ],
        pause=observation(runtime_status="active", goal_status="paused", marker=captured),
    )
    service = _service(tmp_path, fake, clock)
    registered = asyncio.run(
        service.defer(
            thread_id="test-thread",
            condition={"type": "time", "after_seconds": 100_000},
            expires_in_seconds=50,
            idempotency_key="defer",
        )
    )

    clock.value += 100
    asyncio.run(service.reconcile_once())

    status = service.status(registered["monitor_id"])
    assert status["state"] == MonitorState.SUPERSEDED
    assert status["outcome"]["kind"] == "deferred_evaluation_guard_changed"
    assert fake.activation_calls == []


def test_deferred_expiry_wakes_even_while_the_observer_reports_unknown(tmp_path) -> None:
    """The deadline guarantee must hold under a degraded, non-fatal observer."""

    clock = Clock()

    def broken_gpu():
        raise RuntimeError("nvidia-smi unavailable")

    service, fake, registered = _armed_deferred(
        tmp_path,
        clock,
        condition={
            "type": "gpu_stable",
            "devices": [0],
            "max_utilization_percent": 5,
            "stable_for_seconds": 10,
        },
        expires_in_seconds=50,
        observer_context_factory=lambda: ObserverContext(
            runtime_root=tmp_path, now=clock.now, gpu_query=broken_gpu
        ),
    )

    asyncio.run(service.reconcile_once())
    assert service.status(registered["monitor_id"])["state"] == MonitorState.ARMED

    clock.value += 100
    asyncio.run(service.reconcile_once())

    status = service.status(registered["monitor_id"])
    assert status["state"] == MonitorState.FIRED
    assert status["outcome"]["wake_reason"] == "expired"


# --- The three deferred error paths that must not re-strand -------------------


def test_restart_past_expiry_while_claimed_continues_the_wake(tmp_path) -> None:
    clock = Clock()
    service, fake, registered = _armed_deferred(
        tmp_path,
        clock,
        condition={"type": "time", "after_seconds": 100_000},
        expires_in_seconds=50,
    )
    monitor_id = registered["monitor_id"]
    armed = service.ledger.get(monitor_id)
    assert armed is not None
    assert (
        service.ledger.claim(
            monitor_id,
            condition=armed.condition,
            evidence={"kind": "claimed_before_restart"},
            witness=[],
            wake_reason=WakeReason.EXPIRED,
        )
        is not None
    )

    # The daemon dies here and restarts after the expiry has already passed.
    clock.value += 100
    restarted = MonitorService(
        tmp_path,
        ledger=Ledger(tmp_path),
        app_server_factory=lambda: fake,
        observer_context_factory=lambda: ObserverContext(
            runtime_root=tmp_path, now=clock.now
        ),
        daemon_starter=lambda _root: False,
    )
    restarted.recover_after_daemon_start()
    asyncio.run(restarted.reconcile_once())

    status = restarted.status(monitor_id)
    assert status["state"] == MonitorState.FIRED
    assert status["outcome"]["wake_reason"] == "expired"
    assert fake.activation_calls == ["test-thread"]


def test_transient_observation_failure_leaves_a_deferred_monitor_armed(tmp_path) -> None:
    clock = Clock()

    class BrokenAppServer:
        async def __aenter__(self):
            raise OSError("socket stopped accepting")

        async def __aexit__(self, *_args):
            return None

    marker = goal()
    working = FakeAppServer(
        [
            observation(runtime_status="active", goal_status="active", marker=marker),
            observation(runtime_status="active", goal_status="paused", marker=marker),
            observation(runtime_status="idle", goal_status="paused", marker=marker),
        ],
        pause=observation(runtime_status="active", goal_status="paused", marker=marker),
        activation=observation(runtime_status="idle", goal_status="active", marker=marker),
    )
    current = {"server": working}
    service = MonitorService(
        tmp_path,
        ledger=Ledger(tmp_path),
        app_server_factory=lambda: current["server"],
        observer_context_factory=lambda: ObserverContext(
            runtime_root=tmp_path, now=clock.now
        ),
        daemon_starter=lambda _root: False,
        daemon_readiness=lambda _root: True,
    )
    registered = asyncio.run(
        service.defer(
            thread_id="test-thread",
            condition={"type": "time", "after_seconds": 100_000},
            expires_in_seconds=50,
            idempotency_key="defer",
        )
    )

    current["server"] = BrokenAppServer()
    asyncio.run(service.reconcile_once())

    status = service.status(registered["monitor_id"])
    assert status["state"] == MonitorState.ARMED
    assert status["outcome"]["kind"] == "transient_observation_failure_recorded"
    assert working.activation_calls == []

    # Expiry remains the backstop once the transport recovers.
    current["server"] = working
    clock.value += 100
    asyncio.run(service.reconcile_once())

    assert service.status(registered["monitor_id"])["state"] == MonitorState.FIRED
    assert working.activation_calls == ["test-thread"]


def test_preflight_failure_leaves_a_deferred_monitor_claimed_and_retryable(tmp_path) -> None:
    clock = Clock()

    class FailingPreflight:
        async def __aenter__(self):
            raise AppServerError("preflight read failed")

        async def __aexit__(self, *_args):
            return None

    marker = goal()
    working = FakeAppServer(
        [
            observation(runtime_status="active", goal_status="active", marker=marker),
            observation(runtime_status="active", goal_status="paused", marker=marker),
            observation(runtime_status="idle", goal_status="paused", marker=marker),
        ],
        pause=observation(runtime_status="active", goal_status="paused", marker=marker),
        activation=observation(runtime_status="idle", goal_status="active", marker=marker),
    )
    current = {"server": working}
    service = MonitorService(
        tmp_path,
        ledger=Ledger(tmp_path),
        app_server_factory=lambda: current["server"],
        observer_context_factory=lambda: ObserverContext(
            runtime_root=tmp_path, now=clock.now
        ),
        daemon_starter=lambda _root: False,
        daemon_readiness=lambda _root: True,
    )
    registered = asyncio.run(
        service.defer(
            thread_id="test-thread",
            condition={"type": "time", "after_seconds": 0},
            expires_in_seconds=100,
            allow_heuristic_continuation=True,
            idempotency_key="defer",
        )
    )
    monitor_id = registered["monitor_id"]
    armed = service.ledger.get(monitor_id)
    assert armed is not None
    service.ledger.claim(
        monitor_id,
        condition=armed.condition,
        evidence={},
        witness=[{"type": "time", "evidence": {}}],
        wake_reason=WakeReason.CONDITION,
    )

    current["server"] = FailingPreflight()
    asyncio.run(service.reconcile_once())

    status = service.status(monitor_id)
    assert status["state"] == MonitorState.CLAIMED
    assert status["outcome"]["kind"] == "deferred_preflight_retryable"
    assert working.activation_calls == []

    current["server"] = working
    asyncio.run(service.reconcile_once())

    assert service.status(monitor_id)["state"] == MonitorState.FIRED
    assert working.activation_calls == ["test-thread"]


def test_recovery_then_first_pass_completes_an_expired_armed_deferred_monitor(tmp_path) -> None:
    clock = Clock()
    service, fake, registered = _armed_deferred(
        tmp_path,
        clock,
        condition={"type": "time", "after_seconds": 100_000},
        expires_in_seconds=50,
    )
    monitor_id = registered["monitor_id"]

    clock.value += 100
    restarted = MonitorService(
        tmp_path,
        ledger=Ledger(tmp_path),
        app_server_factory=lambda: fake,
        observer_context_factory=lambda: ObserverContext(
            runtime_root=tmp_path, now=clock.now
        ),
        daemon_starter=lambda _root: False,
    )
    assert restarted.recover_after_daemon_start() == 0
    asyncio.run(restarted.reconcile_once())

    status = restarted.status(monitor_id)
    assert status["state"] == MonitorState.FIRED
    assert status["outcome"]["wake_reason"] == "expired"


def test_a_deferred_row_claimed_before_this_change_still_wakes(tmp_path) -> None:
    """A migrated claim stores no reason; mode, not NULL-ness, must decide."""

    clock = Clock()
    service, fake, registered = _armed_deferred(
        tmp_path,
        clock,
        condition={"type": "time", "after_seconds": 0},
        allow_heuristic_continuation=False,
    )
    monitor_id = registered["monitor_id"]
    armed = service.ledger.get(monitor_id)
    assert armed is not None
    # Exactly what pre-change code wrote: a claim with no wake reason at all.
    service.ledger.transition(
        monitor_id,
        expected=(MonitorState.ARMED,),
        state=MonitorState.CLAIMED,
        condition=armed.condition,
        evidence={"kind": "time"},
        witness=[{"type": "time", "evidence": {}}],
        outcome={"kind": "trigger_claimed"},
    )
    assert service.ledger.get(monitor_id).wake_reason is None

    asyncio.run(service.reconcile_once())

    status = service.status(monitor_id)
    assert status["state"] == MonitorState.FIRED
    assert status["wake_reason"] is None
    assert status["outcome"]["wake_reason"] == "unauthorized_evidence"
    assert fake.activation_calls == ["test-thread"]


def test_legacy_expiry_and_authorization_outcomes_are_unchanged(tmp_path) -> None:
    clock = Clock()
    fake = FakeAppServer(
        [observation(runtime_status="active"), observation(runtime_status="active")]
    )
    service = _service(tmp_path, fake, clock)
    registered = asyncio.run(
        service.register(
            thread_id="test-thread",
            condition={"type": "time", "after_seconds": 100_000},
            expires_in_seconds=50,
            start_daemon=False,
        )
    )

    clock.value += 100
    asyncio.run(service.reconcile_once())

    status = service.status(registered["monitor_id"])
    assert status["state"] == MonitorState.EXPIRED
    assert status["outcome"]["kind"] == "expired"
    assert status["wake_reason"] is None
    assert fake.activation_calls == []


# --- Wake report, journal elision, and re-arm lineage -------------------------


def test_wake_report_carries_the_journal_tail_while_status_elides_the_journal(tmp_path) -> None:
    clock = Clock()
    log = tmp_path / "train.log"
    log.write_text("booting\n", encoding="utf-8")
    service, fake, registered = _armed_deferred(
        tmp_path,
        clock,
        condition={
            "type": "log_pattern",
            "path": str(log),
            "patterns": [
                {"name": "ready", "regex": r"Ready in [0-9.]+s"},
                {"name": "crash", "regex": "Traceback|Killed"},
            ],
        },
        allow_heuristic_continuation=True,
    )
    # Registration responses already carry counters, never stored lines.
    assert registered["condition"]["journal"] == {"lines_stored": 0, "dropped": 0}

    with log.open("a", encoding="utf-8") as handle:
        handle.write("step 1\nReady in 9.5s\n")
    asyncio.run(service.reconcile_once())

    status = service.status(registered["monitor_id"])
    outcome = status["outcome"]
    assert status["state"] == MonitorState.FIRED
    assert outcome["wake_reason"] == "condition"
    # The stored journal collapses to counters; journal_tail is the only carrier.
    assert status["condition"]["journal"] == {"lines_stored": 1, "dropped": 0}
    assert "lines" not in status["condition"]["journal"]
    assert outcome["journal_tail"] == ["Ready in 9.5s"]
    assert [item["type"] for item in outcome["witness"]] == ["log_pattern"]
    assert outcome["armed_at"] is not None
    assert outcome["fired_at"] >= outcome["armed_at"]
    assert outcome["waited_seconds"] >= 0
    assert outcome["evaluation_count"] >= 1
    assert outcome["avoided_poll_turns"] == 0


def test_wake_report_estimates_avoided_poll_turns_at_the_yield_floor(tmp_path) -> None:
    clock = Clock()
    service, _fake, registered = _armed_deferred(
        tmp_path,
        clock,
        condition={"type": "time", "after_seconds": 100_000},
        expires_in_seconds=50,
    )
    record = service.ledger.get(registered["monitor_id"])
    assert record is not None

    report = service._wake_report(
        replace(record, armed_at=1_000.0, evaluation_count=7),
        fired_at=1_000.0 + 3_600.0,
    )

    assert report["waited_seconds"] == 3_600.0
    assert report["avoided_poll_turns"] == 20
    assert report["evaluation_count"] == 7


def test_rearm_lineage_validates_stores_and_surfaces_the_chain(tmp_path) -> None:
    clock = Clock()
    fake = FakeAppServer([observation(runtime_status="active")])
    service = _service(tmp_path, fake, clock)
    request = {
        "thread_id": "test-thread",
        "condition": {"type": "time", "after_seconds": 1},
        "expires_in_seconds": 100,
        "start_daemon": False,
    }
    first = asyncio.run(service.register(**request))

    with pytest.raises(ValidationError):
        asyncio.run(service.register(**request, rearm_of="no-such-monitor"))
    with pytest.raises(ValidationError):
        # An armed monitor is not terminal and cannot be a lineage parent.
        asyncio.run(service.register(**request, rearm_of=first["monitor_id"]))

    service.cancel(first["monitor_id"])
    second = asyncio.run(service.register(**request, rearm_of=first["monitor_id"]))
    service.cancel(second["monitor_id"])
    third = asyncio.run(service.register(**request, rearm_of=second["monitor_id"]))

    assert second["rearm_of"] == first["monitor_id"]
    assert second["rearm_chain"] == [first["monitor_id"]]
    assert third["rearm_chain"] == [second["monitor_id"], first["monitor_id"]]
    assert third["state"] == MonitorState.ARMED
    listed = {item["monitor_id"]: item for item in service.list()}
    assert listed[third["monitor_id"]]["rearm_chain"] == [
        second["monitor_id"],
        first["monitor_id"],
    ]


def test_rearm_lineage_is_part_of_idempotent_defer_semantics(tmp_path) -> None:
    clock = Clock()
    marker = goal()
    fake = FakeAppServer(
        [
            observation(runtime_status="active", goal_status="paused", marker=marker),
            observation(runtime_status="active", goal_status="paused", marker=marker),
            observation(runtime_status="active", goal_status="active", marker=marker),
            observation(runtime_status="active", goal_status="paused", marker=marker),
        ],
        pause=observation(runtime_status="active", goal_status="paused", marker=marker),
    )
    service = _service(tmp_path, fake, clock)
    parent = asyncio.run(
        service.register(
            thread_id="test-thread",
            condition={"type": "time", "after_seconds": 1},
            expires_in_seconds=100,
            start_daemon=False,
        )
    )
    service.cancel(parent["monitor_id"])
    request = {
        "thread_id": "test-thread",
        "condition": {"type": "time", "after_seconds": 10},
        "expires_in_seconds": 100,
        "idempotency_key": "rearmed",
    }
    armed = asyncio.run(service.defer(**request, rearm_of=parent["monitor_id"]))
    assert armed["rearm_of"] == parent["monitor_id"]

    with pytest.raises(ValidationError):
        # A replay that drops the lineage reference is different semantics.
        asyncio.run(service.defer(**request))

    replayed = asyncio.run(service.defer(**request, rearm_of=parent["monitor_id"]))
    assert replayed["monitor_id"] == armed["monitor_id"]
    assert fake.pause_calls == ["test-thread"]


# --- thread_idle integration --------------------------------------------------


def _child_observation(runtime_status: str) -> object:
    return observation(
        thread_id="child-thread",
        runtime_status=runtime_status,
        goal_status="active",
        marker=goal(created_at=99, objective="child work"),
    )


def test_thread_idle_defers_until_the_child_thread_ends_its_turn(tmp_path) -> None:
    clock = Clock()
    service, fake, registered = _armed_deferred(
        tmp_path,
        clock,
        condition={"type": "thread_idle", "thread_id": "child-thread"},
        allow_heuristic_continuation=True,
        thread_observations={
            "child-thread": [
                _child_observation("active"),
                _child_observation("active"),
                _child_observation("idle"),
            ]
        },
    )

    asyncio.run(service.reconcile_once())
    assert service.status(registered["monitor_id"])["state"] == MonitorState.ARMED
    assert fake.activation_calls == []

    asyncio.run(service.reconcile_once())

    status = service.status(registered["monitor_id"])
    witness = status["outcome"]["witness"]
    assert status["state"] == MonitorState.FIRED
    assert status["outcome"]["wake_reason"] == "condition"
    assert [item["type"] for item in witness] == ["thread_idle"]
    assert witness[0]["evidence"]["child"]["runtime_status"] == "idle"
    assert witness[0]["evidence"]["child"]["usage"]["token_budget"] == 32
    assert fake.child_reads.count("child-thread") == 3


def test_thread_idle_is_rejected_when_it_names_the_monitors_own_target(tmp_path) -> None:
    clock = Clock()
    condition = {"type": "thread_idle", "thread_id": "test-thread"}
    (tmp_path / "legacy").mkdir()
    (tmp_path / "deferred").mkdir()
    paused = FakeAppServer([observation(runtime_status="active", goal_status="paused")])
    with pytest.raises(ValidationError):
        asyncio.run(
            _service(tmp_path / "legacy", paused, clock).register(
                thread_id="test-thread",
                condition=condition,
                expires_in_seconds=100,
                start_daemon=False,
            )
        )

    active = FakeAppServer([observation(runtime_status="active", goal_status="active")])
    with pytest.raises(ValidationError):
        asyncio.run(
            _service(tmp_path / "deferred", active, clock).defer(
                thread_id="test-thread",
                condition=condition,
                expires_in_seconds=100,
                idempotency_key="defer",
            )
        )
    assert active.pause_calls == []


def test_thread_idle_rejects_an_already_idle_child_at_registration(tmp_path) -> None:
    clock = Clock()
    fake = FakeAppServer(
        [observation(runtime_status="active", goal_status="paused")],
        thread_observations={"child-thread": _child_observation("idle")},
    )
    service = _service(tmp_path, fake, clock)

    with pytest.raises(ValidationError) as caught:
        asyncio.run(
            service.register(
                thread_id="test-thread",
                condition={"type": "thread_idle", "thread_id": "child-thread"},
                expires_in_seconds=100,
                start_daemon=False,
            )
        )

    assert "already idle" in str(caught.value)


def test_legacy_monitor_also_observes_its_thread_idle_child(tmp_path) -> None:
    clock = Clock()
    fake = FakeAppServer(
        [
            observation(runtime_status="active"),
            observation(runtime_status="active"),
            observation(runtime_status="idle"),
        ],
        thread_observations={
            "child-thread": [_child_observation("active"), _child_observation("idle")]
        },
    )
    service = _service(tmp_path, fake, clock)
    registered = asyncio.run(
        service.register(
            thread_id="test-thread",
            condition={"type": "thread_idle", "thread_id": "child-thread"},
            expires_in_seconds=100,
            allow_heuristic_continuation=True,
            start_daemon=False,
        )
    )

    asyncio.run(service.reconcile_once())

    assert service.status(registered["monitor_id"])["state"] == MonitorState.FIRED
    assert fake.activation_calls == ["test-thread"]


def test_a_missing_child_observation_never_satisfies_a_deferred_wait(tmp_path) -> None:
    clock = Clock()
    service, fake, registered = _armed_deferred(
        tmp_path,
        clock,
        condition={"type": "thread_idle", "thread_id": "child-thread"},
        allow_heuristic_continuation=True,
        expires_in_seconds=50,
        thread_observations={
            "child-thread": [
                _child_observation("active"),
                AppServerError("child thread is gone"),
            ]
        },
    )

    asyncio.run(service.reconcile_once())
    status = service.status(registered["monitor_id"])
    assert status["state"] == MonitorState.ARMED
    assert status["evidence"]["kind"] == "thread_unloaded"
    assert fake.activation_calls == []

    # Disappearance is not completion; only the deadline may wake the goal.
    clock.value += 100
    asyncio.run(service.reconcile_once())

    assert service.status(registered["monitor_id"])["outcome"]["wake_reason"] == "expired"

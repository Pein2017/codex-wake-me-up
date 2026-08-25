from __future__ import annotations

import asyncio
import json
import sqlite3
import subprocess
from typing import cast

import pytest

from codex_wake_me_up.conditions import ObserverContext
from codex_wake_me_up.daemon import (
    assert_delivery_runtime_compatible,
    assert_delivery_schema_compatible,
)
from codex_wake_me_up.delivery import DeliveryKind, ThreadDeliveryState, build_thread_delivery
from codex_wake_me_up.ledger import Ledger
from codex_wake_me_up.models import (
    AppServerError,
    AppServerRejectedError,
    GoalMarker,
    MonitorMode,
    MonitorState,
    TargetGuard,
    ValidationError,
    WakeReason,
)
from codex_wake_me_up.service import AppServerFactory, MonitorService

from .helpers import Clock, FakeAppServer
from .test_thread_delivery import FakeThreadDelivery


def _factory(value) -> AppServerFactory:
    return cast(AppServerFactory, lambda: value)


PRE_DELIVERY_STATES = (
    "registering",
    "defer_intent",
    "pausing",
    "armed",
    "claimed",
    "activating",
    "fired",
    "cancelled",
    "expired",
    "superseded",
    "unloaded_target",
    "observer_failed",
    "satisfied_requires_authorization",
    "activation_uncertain",
    "activation_failed",
    "mis_targeted_activation",
    "daemon_unavailable",
    "defer_abandoned",
    "pause_uncertain",
    "pause_rejected",
    "mis_targeted_pause",
)


def _create_pre_delivery_ledger(path) -> None:
    connection = sqlite3.connect(path / "monitors.sqlite3")
    connection.execute(
        """
        CREATE TABLE monitors (
            monitor_id TEXT PRIMARY KEY,
            idempotency_key TEXT UNIQUE,
            semantic_json TEXT NOT NULL,
            target_json TEXT NOT NULL,
            condition_json TEXT NOT NULL,
            state TEXT NOT NULL,
            mode TEXT NOT NULL DEFAULT 'legacy',
            idle_barrier INTEGER NOT NULL DEFAULT 0,
            allow_heuristic_continuation INTEGER NOT NULL,
            expires_at REAL NOT NULL,
            evidence_json TEXT NOT NULL DEFAULT '{}',
            witness_json TEXT NOT NULL DEFAULT '[]',
            outcome_json TEXT,
            activation_snapshot_json TEXT,
            created_at REAL NOT NULL,
            updated_at REAL NOT NULL,
            wake_reason TEXT,
            rearm_of TEXT,
            evaluation_count INTEGER NOT NULL DEFAULT 0,
            armed_at REAL
        )
        """
    )
    target = {
        "thread_id": "legacy-thread",
        "goal": {"created_at": 7, "objective": "legacy", "token_budget": 32},
    }
    for mode in ("legacy", "deferred"):
        for index, state in enumerate(PRE_DELIVERY_STATES):
            monitor_id = f"{mode}-{state}"
            connection.execute(
                """
                INSERT INTO monitors VALUES (
                    ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?
                )
                """,
                (
                    monitor_id,
                    f"key-{monitor_id}",
                    json.dumps({"legacy": monitor_id}),
                    json.dumps(target),
                    json.dumps({"type": "time", "deadline_utc": 999}),
                    state,
                    mode,
                    int(mode == "deferred"),
                    1,
                    999.0,
                    json.dumps({"fact": state}),
                    json.dumps([{"witness": index}]),
                    json.dumps({"kind": f"outcome-{state}"}),
                    json.dumps({"runtime_status": "idle"}),
                    10.0,
                    20.0,
                    "condition",
                    "ancestor",
                    index,
                    11.0,
                ),
            )
    connection.commit()
    connection.close()


def test_every_pre_delivery_mode_and_state_decodes_as_goal_without_fact_loss(
    tmp_path,
) -> None:
    """Catches migration that rewrites or drops any historical goal fact."""

    _create_pre_delivery_ledger(tmp_path)
    ledger = Ledger(tmp_path)

    records = ledger.list(include_terminal=True)
    assert len(records) == 2 * len(PRE_DELIVERY_STATES)
    for record in records:
        state = record.monitor_id.split("-", 1)[1]
        assert record.delivery_kind == DeliveryKind.GOAL
        assert record.delivery is None
        assert record.target == TargetGuard(
            thread_id="legacy-thread",
            goal=GoalMarker(created_at=7, objective="legacy", token_budget=32),
        )
        assert record.state.value == state
        assert record.semantic == {"legacy": record.monitor_id}
        assert record.evidence == {"fact": state}
        assert record.outcome == {"kind": f"outcome-{state}"}
        assert record.activation_snapshot == {"runtime_status": "idle"}
        assert record.wake_reason == WakeReason.CONDITION
        assert record.rearm_of == "ancestor"
        assert record.armed_at == 11.0

    stored_tags = ledger._connection.execute(
        "SELECT DISTINCT delivery_json FROM monitors"
    ).fetchall()
    assert [row[0] for row in stored_tags] == [None]


def _thread_record(ledger: Ledger, monitor_id: str = "monitor-1"):
    delivery = build_thread_delivery(
        monitor_id=monitor_id,
        thread_id="thread-1",
        capability={"codex_version": "0.148.0", "queue_capacity": 100},
    )
    record, _ = ledger.create_or_get(
        monitor_id=monitor_id,
        idempotency_key=f"key-{monitor_id}",
        semantic={"delivery": delivery},
        target=None,
        delivery=delivery,
        condition={"type": "time", "deadline_utc": 200},
        allow_heuristic_continuation=False,
        expires_at=300,
    )
    return record, delivery


def test_delivery_downgrade_allows_terminal_absence_and_legacy_goal_rows(
    tmp_path,
) -> None:
    """Catches refusing harmless redacted history or pre-delivery goal rows."""

    ledger = Ledger(tmp_path)
    thread, _ = _thread_record(ledger)
    ledger.transition(
        thread.monitor_id,
        expected=(MonitorState.REGISTERING,),
        state=MonitorState.RECORDED,
        outcome={"kind": "recorded", "redacted": True},
    )
    ledger.create_or_get(
        monitor_id="legacy-goal",
        idempotency_key="legacy-key",
        semantic={"legacy": True},
        target=TargetGuard(
            thread_id="goal-thread",
            goal=GoalMarker(created_at=1, objective="goal", token_budget=None),
        ),
        condition={"type": "time", "deadline_utc": 200},
        allow_heuristic_continuation=False,
        expires_at=300,
        initial_state=MonitorState.FIRED,
    )
    ledger.close()

    absent = FakeThreadDelivery()
    absent.last_inspection = {
        "classification": "absent",
        "runtime_status": "idle",
        "interrupted": False,
        "history_matches": 0,
        "history_modified": 0,
        "queue_matches": 0,
        "queue_modified": 0,
        "observed_pointer_count": 0,
        "history": [],
        "queue": [],
        "queue_count": 0,
    }

    assert_delivery_schema_compatible(tmp_path, supported_delivery_epoch=0)
    asyncio.run(
        assert_delivery_runtime_compatible(
            tmp_path,
            supported_delivery_epoch=0,
            app_server_factory=lambda: absent,
        )
    )


@pytest.mark.parametrize(
    ("case", "expected_state", "receipt_key"),
    (
        ("condition", MonitorState.CLAIMED, "wake_reason"),
        ("expiry", MonitorState.CLAIMED, "wake_reason"),
        ("observer_failed", MonitorState.CLAIMED, "wake_reason"),
        ("transient", MonitorState.ARMED, "outcome"),
        ("cancel_before", MonitorState.CANCELLED, "outcome"),
        ("queue_ack", MonitorState.QUEUE_ACCEPTED, "queue_receipt"),
        ("queue_rejected", MonitorState.DELIVERY_REJECTED, "delivery_outcome"),
        ("admission_uncertain", MonitorState.ADMISSION_IN_PROGRESS, "reconciliation"),
        ("queue_present", MonitorState.QUEUE_ACCEPTED, "reconciliation"),
        ("recorded", MonitorState.RECORDED, "delivery_outcome"),
        ("modified", MonitorState.DELIVERY_MODIFIED, "delivery_outcome"),
        ("absence", MonitorState.DELIVERY_UNCERTAIN, "delivery_outcome"),
        ("interrupted", MonitorState.QUEUE_ACCEPTED, "reconciliation"),
        ("too_late", MonitorState.CANCELLATION_TOO_LATE, "delivery_outcome"),
        ("archive", MonitorState.DELIVERY_UNCERTAIN, "delivery_outcome"),
    ),
)
def test_terminal_write_table_persists_each_thread_disposition(
    tmp_path, case: str, expected_state: MonitorState, receipt_key: str
) -> None:
    """Catches a terminal-write disposition without its durable receipt."""

    ledger = Ledger(tmp_path)
    record, _ = _thread_record(ledger)
    armed = ledger.arm(record.monitor_id)
    assert armed is not None
    if case in {"condition", "expiry", "observer_failed"}:
        reasons = {
            "condition": WakeReason.CONDITION,
            "expiry": WakeReason.EXPIRED,
            "observer_failed": WakeReason.OBSERVER_FAILED,
        }
        ledger.claim(
            record.monitor_id,
            condition=record.condition,
            evidence={"case": case},
            witness=[{"case": case}],
            wake_reason=reasons[case],
        )
    elif case == "transient":
        ledger.transition(
            record.monitor_id,
            expected=(MonitorState.ARMED,),
            state=MonitorState.ARMED,
            outcome={"kind": "transient_observation_failure_recorded"},
        )
    elif case == "cancel_before":
        ledger.cancel(record.monitor_id)
    else:
        ledger.claim(
            record.monitor_id,
            condition=record.condition,
            evidence={"case": case},
            witness=[{"case": case}],
            wake_reason=WakeReason.CONDITION,
        )
        ledger.begin_thread_admission(record.monitor_id, capability={"epoch": 1})
        if case == "admission_uncertain":
            ledger.update_thread_delivery(
                record.monitor_id,
                expected=(MonitorState.ADMISSION_IN_PROGRESS,),
                state=MonitorState.ADMISSION_IN_PROGRESS,
                delivery_state=ThreadDeliveryState.ADMISSION_IN_PROGRESS,
                reconciliation={"classification": "admission_transport_uncertain"},
            )
        elif case == "queue_rejected":
            ledger.update_thread_delivery(
                record.monitor_id,
                expected=(MonitorState.ADMISSION_IN_PROGRESS,),
                state=MonitorState.DELIVERY_REJECTED,
                delivery_state=ThreadDeliveryState.DELIVERY_REJECTED,
                delivery_outcome={"kind": "queue_admission_rejected"},
            )
        else:
            ledger.record_thread_queue_ack(
                record.monitor_id,
                receipt={"item_id": "queue-1", "client_user_message_id": "delivery-1"},
            )
            state_map = {
                "queue_ack": (MonitorState.QUEUE_ACCEPTED, ThreadDeliveryState.QUEUE_ACCEPTED),
                "queue_present": (MonitorState.QUEUE_ACCEPTED, ThreadDeliveryState.QUEUE_ACCEPTED),
                "recorded": (MonitorState.RECORDED, ThreadDeliveryState.RECORDED),
                "modified": (MonitorState.DELIVERY_MODIFIED, ThreadDeliveryState.DELIVERY_MODIFIED),
                "absence": (MonitorState.DELIVERY_UNCERTAIN, ThreadDeliveryState.DELIVERY_UNCERTAIN),
                "interrupted": (MonitorState.QUEUE_ACCEPTED, ThreadDeliveryState.QUEUE_ACCEPTED),
                "too_late": (MonitorState.CANCELLATION_TOO_LATE, ThreadDeliveryState.CANCELLATION_TOO_LATE),
                "archive": (MonitorState.DELIVERY_UNCERTAIN, ThreadDeliveryState.DELIVERY_UNCERTAIN),
            }
            if case != "queue_ack":
                state, delivery_state = state_map[case]
                reconciliation = {
                    "classification": "queued",
                    "stall": "stalled_interrupted" if case == "interrupted" else None,
                }
                outcome = None
                if case in {"recorded", "modified", "absence", "too_late", "archive"}:
                    outcome = {
                        "kind": case,
                        "absence_kind": (
                            "unresolved_absence" if case in {"absence", "archive"} else None
                        ),
                    }
                ledger.update_thread_delivery(
                    record.monitor_id,
                    expected=(MonitorState.QUEUE_ACCEPTED,),
                    state=state,
                    delivery_state=delivery_state,
                    reconciliation=reconciliation,
                    delivery_outcome=outcome,
                )

    ledger.close()
    persisted = Ledger(tmp_path).get(record.monitor_id)
    assert persisted is not None
    assert persisted.state == expected_state
    value = persisted.status_dict()[receipt_key]
    assert value is not None
    if case == "expiry":
        assert value == WakeReason.EXPIRED
    if case == "observer_failed":
        assert value == WakeReason.OBSERVER_FAILED


@pytest.mark.parametrize(
    "case",
    ["invalid_registration", "capability_absent", "goal_fail_closed", "legacy_decode", "terminal_event"],
)
def test_terminal_write_table_covers_non_thread_dispositions(tmp_path, case: str) -> None:
    """Catches the non-queue rows omitted by a thread-only receipt matrix."""

    ledger = Ledger(tmp_path)
    if case == "invalid_registration":
        service = MonitorService(
            tmp_path,
            ledger=ledger,
            app_server_factory=_factory(FakeAppServer([])),
            thread_delivery_factory=_factory(FakeThreadDelivery()),
            observer_context_factory=lambda: ObserverContext(runtime_root=tmp_path, now=Clock().now),
            daemon_starter=lambda _root: False,
            thread_delivery_readiness=lambda _root: True,
        )
        with pytest.raises(ValidationError, match="idempotency_key"):
            asyncio.run(
                service.wait_for_event(
                    thread_id="thread-1",
                    condition={"type": "time", "after_seconds": 10},
                    expires_in_seconds=100,
                    idempotency_key="",
                )
            )
        assert ledger.list() == []
        return
    if case == "capability_absent":
        service = MonitorService(
            tmp_path,
            ledger=ledger,
            app_server_factory=_factory(FakeAppServer([])),
            thread_delivery_factory=_factory(FakeThreadDelivery()),
            observer_context_factory=lambda: ObserverContext(runtime_root=tmp_path, now=Clock().now),
            daemon_starter=lambda _root: False,
            thread_delivery_readiness=lambda _root: False,
        )
        with pytest.raises(ValidationError, match="delivery-capable daemon"):
            asyncio.run(
                service.wait_for_event(
                    thread_id="thread-1",
                    condition={"type": "time", "after_seconds": 10},
                    expires_in_seconds=100,
                    idempotency_key="capability-absent",
                )
            )
        assert ledger.list() == []
        return
    if case == "terminal_event":
        service = MonitorService(
            tmp_path,
            ledger=ledger,
            app_server_factory=_factory(FakeAppServer([])),
            observer_context_factory=lambda: ObserverContext(runtime_root=tmp_path, now=Clock().now),
            daemon_starter=lambda _root: False,
        )
        reservation = service.reserve_terminal_event(
            {
                "kind": "command_terminal",
                "expires_in_seconds": 100,
                "idempotency_key": "terminal-table",
                "producer_identity": "source-test",
            }
        )
        service.publish_terminal_event(
            reservation["reservation_id"],
            publish_token=reservation["publish_token"],
            terminal_event={
                "kind": "command_terminal",
                "status": "failed",
                "command_label": "source-test",
                "command_digest": "a" * 64,
                "exit_code": 2,
            },
        )
        persisted = service.event_status(reservation["reservation_id"])
        assert persisted["state"] == "terminal"
        assert persisted["terminal_event"]["status"] == "failed"
        assert "publish_token" not in repr(persisted)
        return

    guard = TargetGuard(
        thread_id="goal-thread",
        goal=GoalMarker(created_at=1, objective="goal", token_budget=32),
    )
    record, _ = ledger.create_or_get(
        monitor_id="goal-monitor",
        idempotency_key="goal-key",
        semantic={"goal": True},
        target=guard,
        condition={"type": "time", "deadline_utc": 200},
        allow_heuristic_continuation=False,
        expires_at=300,
        initial_state=(
            MonitorState.CLAIMED if case == "goal_fail_closed" else MonitorState.FIRED
        ),
    )
    if case == "goal_fail_closed":
        activating = ledger.begin_activation(
            record.monitor_id, activation_snapshot={"runtime_status": "idle"}
        )
        assert activating is not None
        ledger.transition(
            record.monitor_id,
            expected=(MonitorState.ACTIVATING,),
            state=MonitorState.ACTIVATION_FAILED,
            outcome={"kind": "goal_activation_rejected"},
        )
        ledger.close()
        persisted = Ledger(tmp_path).get(record.monitor_id)
        assert persisted is not None
        assert persisted.state == MonitorState.ACTIVATION_FAILED
        assert persisted.outcome == {"kind": "goal_activation_rejected"}
    else:
        assert record.delivery is None
        assert record.delivery_kind == DeliveryKind.GOAL
        assert record.target == guard


def test_source_prompt_return_detaches_observation_and_cancels_before_delivery(
    tmp_path,
) -> None:
    """Source smoke: prompt return survives observer ownership without any delivery I/O."""

    clock = Clock()
    adapter = FakeThreadDelivery()
    daemon_starts: list[str] = []
    registration = MonitorService(
        tmp_path,
        ledger=Ledger(tmp_path),
        app_server_factory=_factory(FakeAppServer([])),
        thread_delivery_factory=_factory(adapter),
        observer_context_factory=lambda: ObserverContext(runtime_root=tmp_path, now=clock.now),
        daemon_starter=lambda root: daemon_starts.append(str(root)) or True,
        daemon_readiness=lambda _root: True,
        event_daemon_readiness=lambda _root: True,
        thread_delivery_readiness=lambda _root: True,
    )
    prompt_receipt = asyncio.run(
        registration.wait_for_event(
            thread_id="thread-1",
            condition={"type": "time", "after_seconds": 3600},
            expires_in_seconds=7200,
            idempotency_key="source-smoke",
            start_daemon=True,
        )
    )
    assert prompt_receipt["state"] == MonitorState.ARMED
    assert prompt_receipt["next_action"] == "end_current_turn"
    assert daemon_starts == [str(tmp_path)]

    detached = MonitorService(
        tmp_path,
        ledger=Ledger(tmp_path),
        app_server_factory=_factory(FakeAppServer([])),
        thread_delivery_factory=_factory(adapter),
        observer_context_factory=lambda: ObserverContext(runtime_root=tmp_path, now=clock.now),
        daemon_starter=lambda _root: False,
        daemon_readiness=lambda _root: True,
        event_daemon_readiness=lambda _root: True,
        thread_delivery_readiness=lambda _root: True,
    )
    asyncio.run(detached.reconcile_once())
    observed = detached.status(prompt_receipt["monitor_id"])
    assert observed["state"] == MonitorState.ARMED
    assert observed["evaluation_count"] == 1

    cancelled = detached.cancel(prompt_receipt["monitor_id"])
    assert cancelled["state"] == MonitorState.CANCELLED
    assert adapter.add_calls == []
    assert adapter.resume_calls == []
    assert adapter.delete_calls == []


@pytest.mark.parametrize("goal_state", [None, "active", "paused", "blocked"])
def test_thread_registration_is_independent_of_every_goal_state(
    tmp_path, goal_state: str | None
) -> None:
    """Catches an accidental goal read or guard requirement in ThreadDelivery."""

    adapter = FakeThreadDelivery()
    original_preflight = adapter.preflight_thread_delivery

    async def preflight(thread_id: str):
        capability = await original_preflight(thread_id)
        capability["observed_goal_state"] = goal_state
        return capability

    adapter.preflight_thread_delivery = preflight
    service = MonitorService(
        tmp_path,
        ledger=Ledger(tmp_path),
        app_server_factory=_factory(FakeAppServer([])),
        thread_delivery_factory=_factory(adapter),
        observer_context_factory=lambda: ObserverContext(runtime_root=tmp_path, now=Clock().now),
        daemon_starter=lambda _root: False,
        daemon_readiness=lambda _root: True,
        event_daemon_readiness=lambda _root: True,
        thread_delivery_readiness=lambda _root: True,
    )

    result = asyncio.run(
        service.wait_for_event(
            thread_id="thread-1",
            condition={"type": "time", "after_seconds": 10},
            expires_in_seconds=100,
            idempotency_key=f"goal-state-{goal_state}",
            start_daemon=False,
        )
    )

    assert result["delivery_kind"] == DeliveryKind.THREAD
    assert result["target"] == {"thread_id": "thread-1"}
    assert adapter.add_calls == []


def test_mcp_disconnect_before_arm_leaves_no_row_but_after_arm_keeps_ownership(
    tmp_path,
) -> None:
    """Catches a cancelled prompt orphaning an unsafe partial registration."""

    before_root = tmp_path / "before"
    before_root.mkdir()

    class CancelBeforeArm(FakeThreadDelivery):
        async def preflight_thread_delivery(self, thread_id: str) -> dict:
            raise asyncio.CancelledError

    before_adapter = CancelBeforeArm()
    before = MonitorService(
        before_root,
        ledger=Ledger(before_root),
        app_server_factory=_factory(FakeAppServer([])),
        thread_delivery_factory=_factory(before_adapter),
        observer_context_factory=lambda: ObserverContext(runtime_root=before_root, now=Clock().now),
        daemon_starter=lambda _root: False,
        daemon_readiness=lambda _root: True,
        event_daemon_readiness=lambda _root: True,
        thread_delivery_readiness=lambda _root: True,
    )
    with pytest.raises(asyncio.CancelledError):
        asyncio.run(
            before.wait_for_event(
                thread_id="thread-1",
                condition={"type": "time", "after_seconds": 10},
                expires_in_seconds=100,
                idempotency_key="before-arm",
            )
        )
    assert before.ledger.list() == []

    after_root = tmp_path / "after"
    after_root.mkdir()
    after_adapter = FakeThreadDelivery()

    def cancel_after_arm(_root):
        raise asyncio.CancelledError

    after = MonitorService(
        after_root,
        ledger=Ledger(after_root),
        app_server_factory=_factory(FakeAppServer([])),
        thread_delivery_factory=_factory(after_adapter),
        observer_context_factory=lambda: ObserverContext(runtime_root=after_root, now=Clock().now),
        daemon_starter=cancel_after_arm,
        daemon_readiness=lambda _root: True,
        event_daemon_readiness=lambda _root: True,
        thread_delivery_readiness=lambda _root: True,
    )
    with pytest.raises(asyncio.CancelledError):
        asyncio.run(
            after.wait_for_event(
                thread_id="thread-1",
                condition={"type": "time", "after_seconds": 10},
                expires_in_seconds=100,
                idempotency_key="after-arm",
            )
        )
    rows = after.ledger.list()
    assert len(rows) == 1
    assert rows[0].state == MonitorState.ARMED
    assert after_adapter.add_calls == []


def test_thread_expiry_and_irrecoverable_observer_failure_share_the_claim_path(
    tmp_path,
) -> None:
    """Catches expiry/fatal observation bypassing the single durable claim."""

    expiry_root = tmp_path / "expiry"
    expiry_root.mkdir()
    expiry_clock = Clock()
    expiry_adapter = FakeThreadDelivery()
    expiry = MonitorService(
        expiry_root,
        ledger=Ledger(expiry_root),
        app_server_factory=_factory(FakeAppServer([])),
        thread_delivery_factory=_factory(expiry_adapter),
        observer_context_factory=lambda: ObserverContext(runtime_root=expiry_root, now=expiry_clock.now),
        daemon_starter=lambda _root: False,
        daemon_readiness=lambda _root: True,
        event_daemon_readiness=lambda _root: True,
        thread_delivery_readiness=lambda _root: True,
    )
    expiring = asyncio.run(
        expiry.wait_for_event(
            thread_id="thread-1",
            condition={"type": "time", "after_seconds": 100},
            expires_in_seconds=10,
            idempotency_key="expiry",
            start_daemon=False,
        )
    )
    expiry_clock.value = 111.0
    asyncio.run(expiry.reconcile_once())
    expired = expiry.status(expiring["monitor_id"])
    assert expired["wake_reason"] == WakeReason.EXPIRED
    assert expired["state"] == MonitorState.QUEUE_ACCEPTED
    assert len(expiry_adapter.add_calls) == 1

    failure_root = tmp_path / "failure"
    failure_root.mkdir()
    failure_clock = Clock()
    failure_adapter = FakeThreadDelivery()
    failure = MonitorService(
        failure_root,
        ledger=Ledger(failure_root),
        app_server_factory=_factory(FakeAppServer([])),
        thread_delivery_factory=_factory(failure_adapter),
        observer_context_factory=lambda: ObserverContext(runtime_root=failure_root, now=failure_clock.now),
        daemon_starter=lambda _root: False,
        daemon_readiness=lambda _root: True,
        event_daemon_readiness=lambda _root: True,
        thread_delivery_readiness=lambda _root: True,
    )
    reservation = failure.reserve_terminal_event(
        {
            "kind": "command_terminal",
            "expires_in_seconds": 100,
            "idempotency_key": "fatal-reservation",
            "producer_identity": "source-test",
        }
    )
    fatal_monitor = asyncio.run(
        failure.wait_for_event(
            thread_id="thread-1",
            condition={
                "type": "command_terminal",
                "reservation_id": reservation["reservation_id"],
            },
            expires_in_seconds=100,
            idempotency_key="fatal-monitor",
            start_daemon=False,
        )
    )
    failure.ledger._connection.execute(
        """
        UPDATE event_reservations
        SET terminal_json = '{not-json', terminal_fingerprint = ?, state = 'terminal'
        WHERE reservation_id = ?
        """,
        ("0" * 64, reservation["reservation_id"]),
    )
    failure.ledger._connection.commit()
    asyncio.run(failure.reconcile_once())
    failed = failure.status(fatal_monitor["monitor_id"])
    assert failed["wake_reason"] == WakeReason.OBSERVER_FAILED
    assert failed["state"] == MonitorState.QUEUE_ACCEPTED
    assert len(failure_adapter.add_calls) == 1


def test_queue_rejection_and_interrupted_reorder_have_typed_status(tmp_path) -> None:
    """Catches queue-full fallback or hiding a reordered interrupted FIFO stall."""

    rejected_root = tmp_path / "rejected"
    rejected_root.mkdir()
    rejected_clock = Clock()
    rejected_adapter = FakeThreadDelivery(
        add_result=AppServerRejectedError("queue capacity reached")
    )
    rejected = MonitorService(
        rejected_root,
        ledger=Ledger(rejected_root),
        app_server_factory=_factory(FakeAppServer([])),
        thread_delivery_factory=_factory(rejected_adapter),
        observer_context_factory=lambda: ObserverContext(runtime_root=rejected_root, now=rejected_clock.now),
        daemon_starter=lambda _root: False,
        daemon_readiness=lambda _root: True,
        event_daemon_readiness=lambda _root: True,
        thread_delivery_readiness=lambda _root: True,
    )
    monitor = asyncio.run(
        rejected.wait_for_event(
            thread_id="thread-1",
            condition={"type": "time", "after_seconds": 1},
            expires_in_seconds=100,
            idempotency_key="rejected",
            start_daemon=False,
        )
    )
    rejected_clock.value = 102.0
    asyncio.run(rejected.reconcile_once())
    status = rejected.status(monitor["monitor_id"])
    assert status["state"] == MonitorState.DELIVERY_REJECTED
    assert status["delivery_outcome"]["kind"] == "queue_admission_rejected"
    assert len(rejected_adapter.add_calls) == 1
    assert rejected_adapter.resume_calls == []

    stalled_root = tmp_path / "stalled"
    stalled_root.mkdir()
    stalled_clock = Clock()
    stalled_adapter = FakeThreadDelivery(
        inspections=[
            {
                "classification": "queued",
                "runtime_status": "idle",
                "interrupted": True,
                "history_matches": 0,
                "history_modified": 0,
                "queue_matches": 1,
                "queue_modified": 0,
                "observed_pointer_count": 1,
                "history": [],
                "queue": [{"item_id": "queue-1", "position": 3}],
                "queue_count": 4,
                "prior_item_count": 3,
                "prior_item_ids": ["user-1", "user-2", "user-3"],
            }
        ]
    )
    stalled = MonitorService(
        stalled_root,
        ledger=Ledger(stalled_root),
        app_server_factory=_factory(FakeAppServer([])),
        thread_delivery_factory=_factory(stalled_adapter),
        observer_context_factory=lambda: ObserverContext(runtime_root=stalled_root, now=stalled_clock.now),
        daemon_starter=lambda _root: False,
        daemon_readiness=lambda _root: True,
        event_daemon_readiness=lambda _root: True,
        thread_delivery_readiness=lambda _root: True,
    )
    stalled_monitor = asyncio.run(
        stalled.wait_for_event(
            thread_id="thread-1",
            condition={"type": "time", "after_seconds": 1},
            expires_in_seconds=100,
            idempotency_key="stalled",
            start_daemon=False,
        )
    )
    stalled_clock.value = 102.0
    asyncio.run(stalled.reconcile_once())
    stalled_status = stalled.status(stalled_monitor["monitor_id"])
    assert stalled_status["reconciliation"]["stall"] == "stalled_interrupted"
    assert stalled_status["reconciliation"]["queue"][0]["position"] == 3
    assert stalled_status["reconciliation"]["prior_item_ids"] == [
        "user-1",
        "user-2",
        "user-3",
    ]


def test_worker_event_survives_restart_into_thread_delivery_without_acceptance(
    tmp_path,
) -> None:
    """Catches worker identity/evidence loss or acceptance promotion on restart."""

    repository = tmp_path / "repo"
    subprocess.run(("git", "init", str(repository)), check=True, capture_output=True)
    subprocess.run(
        ("git", "-C", str(repository), "config", "user.name", "Test User"),
        check=True,
    )
    subprocess.run(
        (
            "git",
            "-C",
            str(repository),
            "config",
            "user.email",
            "test@example.invalid",
        ),
        check=True,
    )
    allowed = repository / "allowed"
    allowed.mkdir()
    (allowed / "base.txt").write_text("base\n", encoding="utf-8")
    subprocess.run(("git", "-C", str(repository), "add", "allowed/base.txt"), check=True)
    subprocess.run(("git", "-C", str(repository), "commit", "-m", "base"), check=True)
    baseline = subprocess.run(
        ("git", "-C", str(repository), "rev-parse", "HEAD"),
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()

    runtime = tmp_path / "runtime"
    runtime.mkdir()
    adapter = FakeThreadDelivery()
    producer = MonitorService(
        runtime,
        ledger=Ledger(runtime),
        app_server_factory=_factory(FakeAppServer([])),
        thread_delivery_factory=_factory(adapter),
        observer_context_factory=lambda: ObserverContext(runtime_root=runtime, now=Clock().now),
        daemon_starter=lambda _root: False,
        daemon_readiness=lambda _root: True,
        event_daemon_readiness=lambda _root: True,
        thread_delivery_readiness=lambda _root: True,
    )
    reserved = producer.reserve_terminal_event(
        {
            "kind": "worker_terminal",
            "expires_in_seconds": 100,
            "producer_task_id": "worker-1",
            "repository": str(repository / ".git"),
            "worktree": str(repository),
            "baseline_commit": baseline,
            "allowed_path_prefixes": ["allowed"],
        }
    )
    registered = asyncio.run(
        producer.wait_for_event(
            thread_id="thread-1",
            condition={
                "type": "worker_terminal",
                "reservation_id": reserved["reservation_id"],
            },
            expires_in_seconds=100,
            idempotency_key="worker-thread-delivery",
            start_daemon=False,
        )
    )
    (allowed / "candidate.txt").write_text("candidate\n", encoding="utf-8")
    subprocess.run(
        ("git", "-C", str(repository), "add", "allowed/candidate.txt"), check=True
    )
    subprocess.run(
        ("git", "-C", str(repository), "commit", "-m", "candidate"), check=True
    )
    candidate = subprocess.run(
        ("git", "-C", str(repository), "rev-parse", "HEAD"),
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    published = producer.publish_terminal_event(
        reserved["reservation_id"],
        publish_token=reserved["publish_token"],
        terminal_event={
            "kind": "worker_terminal",
            "outcome": "delivered",
            "producer_task_id": "worker-1",
            "candidate_oid": candidate,
        },
    )
    replay = producer.publish_terminal_event(
        reserved["reservation_id"],
        publish_token=reserved["publish_token"],
        terminal_event={
            "kind": "worker_terminal",
            "outcome": "delivered",
            "producer_task_id": "worker-1",
            "candidate_oid": candidate,
        },
    )
    assert replay == published
    producer.ledger.close()

    recovered = MonitorService(
        runtime,
        ledger=Ledger(runtime),
        app_server_factory=_factory(FakeAppServer([])),
        thread_delivery_factory=_factory(adapter),
        observer_context_factory=lambda: ObserverContext(runtime_root=runtime, now=Clock().now),
        daemon_starter=lambda _root: False,
        daemon_readiness=lambda _root: True,
        event_daemon_readiness=lambda _root: True,
        thread_delivery_readiness=lambda _root: True,
    )
    asyncio.run(recovered.reconcile_once())
    status = recovered.status(registered["monitor_id"])

    assert status["state"] == MonitorState.QUEUE_ACCEPTED
    assert status["witness"][0]["classification"] == "valid_delivery_candidate"
    assert status["witness"][0]["producer_id"] == "worker-1"
    assert status["witness"][0]["terminal_event"]["producer_task_id"] == "worker-1"
    assert status["witness"][0]["lead_accepted"] is False
    assert status["terminal_event"]["git_attestation"]["status"] == "valid"
    assert reserved["publish_token"] not in repr(status)
    assert reserved["publish_token"].encode() not in (runtime / "monitors.sqlite3").read_bytes()
    assert len(adapter.add_calls) == 1

from __future__ import annotations

import threading
from concurrent.futures import ThreadPoolExecutor

from codex_wake_me_up.delivery import DeliveryKind
from codex_wake_me_up.ledger import Ledger
from codex_wake_me_up.models import ConflictError, TargetGuard
from codex_wake_me_up.terminal_events import (
    normalize_command_terminal,
    normalize_heartbeat,
)

from .helpers import goal


def terminal_event():
    return normalize_command_terminal(
        {
            "kind": "command_terminal",
            "status": "succeeded",
            "command_label": "race-test",
            "command_digest": "a" * 64,
            "exit_code": 0,
        }
    )


def bound_monitor(
    tmp_path,
    *,
    condition: dict,
    monitor_expires_at: float,
    reservation_ids: tuple[str, ...] = ("event-1",),
):
    first = Ledger(tmp_path)
    second = Ledger(tmp_path)
    for reservation_id in reservation_ids:
        first.reserve_event(
            reservation_id=reservation_id,
            idempotency_key=None,
            kind="command_terminal",
            producer_id=f"shell-{reservation_id}",
            semantic={},
            publish_token=f"secret-{reservation_id}",
            expires_at=200.0,
            now=100.0,
        )
    monitor, _ = first.create_or_get(
        monitor_id="monitor-1",
        idempotency_key=None,
        semantic={"race": True},
        target=TargetGuard("test-thread", goal()),
        condition=condition,
        allow_heuristic_continuation=False,
        expires_at=monitor_expires_at,
        event_bindings=tuple(
            (reservation_id, "command_terminal")
            for reservation_id in reservation_ids
        ),
        now=100.0,
    )
    first.arm(monitor.monitor_id)
    return first, second


def test_multi_any_simultaneous_publications_claim_only_once(tmp_path) -> None:
    condition = {
        "type": "any",
        "children": [
            {"type": "command_terminal", "reservation_id": "event-1"},
            {"type": "command_terminal", "reservation_id": "event-2"},
        ],
    }
    first, second = bound_monitor(
        tmp_path,
        condition=condition,
        monitor_expires_at=200.0,
        reservation_ids=("event-1", "event-2"),
    )
    barrier = threading.Barrier(3)

    def publish(ledger: Ledger, reservation_id: str) -> bool:
        barrier.wait()
        try:
            ledger.publish_terminal_event(
                reservation_id,
                publish_token=f"secret-{reservation_id}",
                terminal_event=terminal_event(),
                now=102.0,
            )
        except ConflictError:
            return False
        return True

    def claim():
        barrier.wait()
        return first.evaluate_and_claim_event_monitor(
            "monitor-1",
            expected_evaluation_count=0,
            observed_condition=condition,
            external_evaluations={},
            now_factory=lambda: 102.0,
        )

    with ThreadPoolExecutor(max_workers=3) as pool:
        publications = [
            pool.submit(publish, ledger, reservation_id)
            for ledger, reservation_id in (
                (first, "event-1"),
                (second, "event-2"),
            )
        ]
        claimed = pool.submit(claim).result()
        for publication in publications:
            publication.result()

    if claimed is None:
        claimed = second.evaluate_and_claim_event_monitor(
            "monitor-1",
            expected_evaluation_count=1,
            observed_condition=condition,
            external_evaluations={},
            now_factory=lambda: 103.0,
        )
    assert claimed is not None
    assert claimed.state.value == "claimed"
    assert second.evaluate_and_claim_event_monitor(
        "monitor-1",
        expected_evaluation_count=claimed.evaluation_count,
        observed_condition=condition,
        external_evaluations={},
        now_factory=lambda: 104.0,
    ) is None
    assert [
        first.get_event(reservation_id).bound_monitor_id
        for reservation_id in ("event-1", "event-2")
    ] == ["monitor-1", "monitor-1"]


def test_multi_event_claim_permits_at_most_one_thread_queue_admission(tmp_path) -> None:
    first = Ledger(tmp_path)
    second = Ledger(tmp_path)
    for reservation_id in ("event-1", "event-2"):
        first.reserve_event(
            reservation_id=reservation_id,
            idempotency_key=None,
            kind="command_terminal",
            producer_id=reservation_id,
            semantic={},
            publish_token=reservation_id,
            expires_at=200.0,
            now=100.0,
        )
    condition = {
        "type": "any",
        "children": [
            {"type": "command_terminal", "reservation_id": "event-1"},
            {"type": "command_terminal", "reservation_id": "event-2"},
        ],
    }
    monitor, _ = first.create_or_get(
        monitor_id="monitor-1",
        idempotency_key=None,
        semantic={"race": "queue"},
        target=None,
        delivery={"kind": DeliveryKind.THREAD, "thread_id": "test-thread"},
        condition=condition,
        allow_heuristic_continuation=False,
        expires_at=200.0,
        event_bindings=(
            ("event-1", "command_terminal"),
            ("event-2", "command_terminal"),
        ),
        now=100.0,
    )
    first.arm(monitor.monitor_id)
    first.publish_terminal_event(
        "event-1",
        publish_token="event-1",
        terminal_event=terminal_event(),
        now=101.0,
    )
    barrier = threading.Barrier(2)

    def claim(ledger: Ledger):
        barrier.wait()
        return ledger.evaluate_and_claim_event_monitor(
            monitor.monitor_id,
            expected_evaluation_count=0,
            observed_condition=condition,
            external_evaluations={},
            now_factory=lambda: 102.0,
        )

    with ThreadPoolExecutor(max_workers=2) as pool:
        claims = [pool.submit(claim, ledger) for ledger in (first, second)]
        assert sum(claim.result() is not None for claim in claims) == 1

    admission_barrier = threading.Barrier(2)

    def admit(ledger: Ledger):
        admission_barrier.wait()
        return ledger.begin_thread_admission(
            monitor.monitor_id,
            capability={"delivery_thread_id": "test-thread"},
            now=103.0,
        )

    with ThreadPoolExecutor(max_workers=2) as pool:
        admissions = [pool.submit(admit, ledger) for ledger in (first, second)]
        assert sum(admission.result() is not None for admission in admissions) == 1


def test_terminal_vs_stale_claim_linearizes_across_separate_connections(
    tmp_path,
) -> None:
    condition = {
        "type": "any",
        "children": [
            {"type": "command_terminal", "reservation_id": "event-1"},
            {
                "type": "heartbeat_stale",
                "reservation_id": "event-1",
                "stale_after_seconds": 1,
            },
        ],
    }
    publisher, evaluator = bound_monitor(
        tmp_path, condition=condition, monitor_expires_at=200.0
    )
    publisher.publish_event_heartbeat(
        "event-1",
        publish_token="secret-event-1",
        heartbeat=normalize_heartbeat({"sequence": 1, "payload": {}}),
        now=100.0,
    )
    barrier = threading.Barrier(2)

    def publish() -> bool:
        barrier.wait()
        try:
            publisher.publish_terminal_event(
                "event-1",
                publish_token="secret-event-1",
                terminal_event=terminal_event(),
                now=102.0,
            )
        except ConflictError:
            return False
        return True

    def claim():
        barrier.wait()
        return evaluator.evaluate_and_claim_event_monitor(
            "monitor-1",
            expected_evaluation_count=0,
            observed_condition=condition,
            external_evaluations={},
            now_factory=lambda: 102.0,
        )

    with ThreadPoolExecutor(max_workers=2) as pool:
        publish_result = pool.submit(publish)
        claim_result = pool.submit(claim)
        published = publish_result.result()
        claimed = claim_result.result()

    assert claimed is not None
    assert claimed.state.value == "claimed"
    assert len(claimed.witness) == 1
    assert claimed.witness[0]["type"] == (
        "command_terminal" if published else "heartbeat_stale"
    )
    assert evaluator.evaluate_and_claim_event_monitor(
        "monitor-1",
        expected_evaluation_count=1,
        observed_condition=condition,
        external_evaluations={},
        now_factory=lambda: 103.0,
    ) is None


def test_terminal_vs_monitor_expiry_first_committer_decides_one_outcome(
    tmp_path,
) -> None:
    condition = {"type": "command_terminal", "reservation_id": "event-1"}
    publisher, evaluator = bound_monitor(
        tmp_path, condition=condition, monitor_expires_at=101.0
    )
    barrier = threading.Barrier(2)

    def publish() -> bool:
        barrier.wait()
        try:
            publisher.publish_terminal_event(
                "event-1",
                publish_token="secret-event-1",
                terminal_event=terminal_event(),
                now=102.0,
            )
        except ConflictError:
            return False
        return True

    def expire_or_claim():
        barrier.wait()
        return evaluator.evaluate_and_claim_event_monitor(
            "monitor-1",
            expected_evaluation_count=0,
            observed_condition=condition,
            external_evaluations={},
            now_factory=lambda: 102.0,
        )

    with ThreadPoolExecutor(max_workers=2) as pool:
        publish_result = pool.submit(publish)
        monitor_result = pool.submit(expire_or_claim)
        published = publish_result.result()
        outcome = monitor_result.result()

    assert outcome is not None
    assert outcome.state.value == ("claimed" if published else "expired")
    if published:
        assert outcome.witness[0]["type"] == "command_terminal"
    else:
        assert outcome.witness == ()

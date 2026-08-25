from __future__ import annotations

import sqlite3

import pytest

from codex_wake_me_up.daemon import assert_event_schema_compatible
from codex_wake_me_up.ledger import Ledger
from codex_wake_me_up.models import ConflictError, MonitorState, TargetGuard
from codex_wake_me_up.terminal_events import (
    normalize_command_terminal,
    normalize_heartbeat,
)

from .helpers import goal


def command_terminal(*, status: str = "succeeded", exit_code: int = 0):
    return normalize_command_terminal(
        {
            "kind": "command_terminal",
            "status": status,
            "command_label": "focused-test",
            "command_digest": "a" * 64,
            "exit_code": exit_code,
        }
    )


def test_event_reservation_persists_only_a_redacted_publish_identity(tmp_path) -> None:
    """Removing token hashing or exposing the raw token must break this test."""

    ledger = Ledger(tmp_path)
    record, created = ledger.reserve_event(
        reservation_id="event-1",
        idempotency_key="reserve-1",
        kind="command_terminal",
        producer_id="shell-test",
        semantic={"operator_label": "focused-test"},
        publish_token="secret-publish-token",
        expires_at=200.0,
        now=100.0,
    )

    assert created is True
    assert record.reservation_id == "event-1"
    assert record.state.value == "reserved"
    assert record.status_dict()["token_fingerprint"]
    assert "secret-publish-token" not in str(record.status_dict())

    ledger.close()
    reopened = Ledger(tmp_path)
    persisted = reopened.get_event("event-1")
    assert persisted is not None
    assert persisted.status_dict() == record.status_dict()
    assert "secret-publish-token" not in (tmp_path / "monitors.sqlite3").read_bytes().decode(
        "utf-8", errors="ignore"
    )


def test_cancelling_an_unbound_reservation_is_durable_and_idempotent(tmp_path) -> None:
    """Removing the terminal cancel state must make later inspection disagree."""

    ledger = Ledger(tmp_path)
    ledger.reserve_event(
        reservation_id="event-1",
        idempotency_key=None,
        kind="command_terminal",
        producer_id="shell-test",
        semantic={},
        publish_token="secret-publish-token",
        expires_at=200.0,
        now=100.0,
    )

    cancelled = ledger.cancel_event("event-1", now=101.0)
    repeated = ledger.cancel_event("event-1", now=102.0)

    assert cancelled.state.value == "cancelled"
    assert cancelled.updated_at == 101.0
    assert repeated.status_dict() == cancelled.status_dict()


def test_expiry_sweeps_only_unbound_live_reservations(tmp_path) -> None:
    """Removing the expiry CAS must leave a stale publish capability usable."""

    ledger = Ledger(tmp_path)
    ledger.reserve_event(
        reservation_id="expired",
        idempotency_key=None,
        kind="command_terminal",
        producer_id="shell-test",
        semantic={},
        publish_token="expired-token",
        expires_at=101.0,
        now=100.0,
    )
    ledger.reserve_event(
        reservation_id="live",
        idempotency_key=None,
        kind="command_terminal",
        producer_id="shell-test",
        semantic={},
        publish_token="live-token",
        expires_at=103.0,
        now=100.0,
    )

    assert ledger.expire_events(now=102.0) == 1
    assert ledger.get_event("expired").state.value == "expired"
    assert ledger.get_event("live").state.value == "reserved"
    assert ledger.expire_events(now=102.0) == 0


def test_public_lifecycle_is_derived_from_orthogonal_facts_not_cached_state(
    tmp_path,
) -> None:
    """A stale compatibility cache must not change the externally visible state."""

    ledger = Ledger(tmp_path)
    ledger.reserve_event(
        reservation_id="event-1",
        idempotency_key=None,
        kind="command_terminal",
        producer_id="shell-test",
        semantic={},
        publish_token="secret-token",
        expires_at=200.0,
        now=100.0,
    )
    ledger.close()
    connection = sqlite3.connect(tmp_path / "monitors.sqlite3")
    connection.execute(
        "UPDATE event_reservations SET state = 'expired' WHERE reservation_id = 'event-1'"
    )
    connection.commit()
    connection.close()

    reopened = Ledger(tmp_path)

    assert reopened.get_event("event-1").state.value == "reserved"


def test_cancel_uses_orthogonal_facts_when_cached_state_is_stale(tmp_path) -> None:
    ledger = Ledger(tmp_path)
    ledger.reserve_event(
        reservation_id="event-1",
        idempotency_key=None,
        kind="command_terminal",
        producer_id="shell-test",
        semantic={},
        publish_token="secret-token",
        expires_at=200.0,
        now=100.0,
    )
    ledger._connection.execute(
        "UPDATE event_reservations SET state = 'terminal' WHERE reservation_id = 'event-1'"
    )
    ledger._connection.commit()

    cancelled = ledger.cancel_event("event-1", now=101.0)

    assert cancelled.state.value == "cancelled"
    assert cancelled.cancelled_at == 101.0
    with pytest.raises(ConflictError, match="no longer writable"):
        ledger.publish_terminal_event(
            "event-1",
            publish_token="secret-token",
            terminal_event=command_terminal(),
            now=102.0,
        )


def test_cancel_at_or_after_deadline_persists_expiry_instead(tmp_path) -> None:
    ledger = Ledger(tmp_path)
    ledger.reserve_event(
        reservation_id="event-1",
        idempotency_key=None,
        kind="command_terminal",
        producer_id="shell-test",
        semantic={},
        publish_token="secret-token",
        expires_at=101.0,
        now=100.0,
    )

    expired = ledger.cancel_event("event-1", now=101.0)

    assert expired.state.value == "expired"
    assert expired.expired_at == 101.0
    assert expired.cancelled_at is None


def test_reservation_idempotency_reuses_identical_semantics_without_reissuing_token(tmp_path) -> None:
    """Weakening semantic comparison must permit a capability-confusion bug."""

    ledger = Ledger(tmp_path)
    original, created = ledger.reserve_event(
        reservation_id="event-1",
        idempotency_key="same-request",
        kind="worker_terminal",
        producer_id="worker-1",
        semantic={"allowed_prefixes": ["src/"]},
        publish_token="stable-token",
        expires_at=200.0,
        now=100.0,
    )
    replay, replay_created = ledger.reserve_event(
        reservation_id="discarded-new-id",
        idempotency_key="same-request",
        kind="worker_terminal",
        producer_id="worker-1",
        semantic={"allowed_prefixes": ["src/"]},
        publish_token="stable-token",
        expires_at=200.0,
        now=101.0,
    )

    assert created is True
    assert replay_created is False
    assert replay.status_dict() == original.status_dict()

    with pytest.raises(ConflictError):
        ledger.reserve_event(
            reservation_id="event-2",
            idempotency_key="same-request",
            kind="command_terminal",
            producer_id="worker-1",
            semantic={"allowed_prefixes": ["src/"]},
            publish_token="stable-token",
            expires_at=200.0,
            now=102.0,
        )
    tokenless_replay, replay_created = ledger.reserve_event(
        reservation_id="event-3",
        idempotency_key="same-request",
        kind="worker_terminal",
        producer_id="worker-1",
        semantic={"allowed_prefixes": ["src/"]},
        publish_token="discarded-new-token",
        expires_at=200.0,
        now=102.0,
    )
    assert not replay_created
    assert tokenless_replay.status_dict() == original.status_dict()
    assert ledger.get_event("event-1").status_dict() == original.status_dict()


def test_authenticated_heartbeats_are_monotonic_durable_and_redacted(tmp_path) -> None:
    ledger = Ledger(tmp_path)
    ledger.reserve_event(
        reservation_id="event-1",
        idempotency_key=None,
        kind="command_terminal",
        producer_id="shell-test",
        semantic={},
        publish_token="secret-token",
        expires_at=200.0,
        now=100.0,
    )
    first = ledger.publish_event_heartbeat(
        "event-1",
        publish_token="secret-token",
        heartbeat=normalize_heartbeat({"sequence": 1, "payload": {"phase": "test"}}),
        now=101.0,
    )
    replay = ledger.publish_event_heartbeat(
        "event-1",
        publish_token="secret-token",
        heartbeat=normalize_heartbeat({"sequence": 1, "payload": {"phase": "test"}}),
        now=102.0,
    )
    assert replay.heartbeat == first.heartbeat
    assert replay.heartbeat["sequence"] == 1
    with pytest.raises(ConflictError):
        ledger.publish_event_heartbeat(
            "event-1",
            publish_token="wrong-token",
            heartbeat=normalize_heartbeat({"sequence": 2, "payload": {}}),
            now=103.0,
        )
    with pytest.raises(ConflictError):
        ledger.publish_event_heartbeat(
            "event-1",
            publish_token="secret-token",
            heartbeat=normalize_heartbeat({"sequence": 0, "payload": {}}),
            now=103.0,
        )

    ledger.close()
    reopened = Ledger(tmp_path)
    assert reopened.get_event("event-1").heartbeat == first.heartbeat
    assert "secret-token" not in str(reopened.get_event("event-1").status_dict())


def test_terminal_publication_is_immutable_and_stops_heartbeats(tmp_path) -> None:
    ledger = Ledger(tmp_path)
    ledger.reserve_event(
        reservation_id="event-1",
        idempotency_key=None,
        kind="command_terminal",
        producer_id="shell-test",
        semantic={},
        publish_token="secret-token",
        expires_at=200.0,
        now=100.0,
    )
    terminal = ledger.publish_terminal_event(
        "event-1",
        publish_token="secret-token",
        terminal_event=command_terminal(),
        now=101.0,
    )
    replay = ledger.publish_terminal_event(
        "event-1",
        publish_token="secret-token",
        terminal_event=command_terminal(),
        now=102.0,
    )
    assert terminal.status_dict() == replay.status_dict()
    assert terminal.state.value == "terminal"
    with pytest.raises(ConflictError):
        ledger.publish_terminal_event(
            "event-1",
            publish_token="secret-token",
            terminal_event=command_terminal(status="failed", exit_code=2),
            now=103.0,
        )
    with pytest.raises(ConflictError):
        ledger.publish_event_heartbeat(
            "event-1",
            publish_token="secret-token",
            heartbeat=normalize_heartbeat({"sequence": 2, "payload": {}}),
            now=103.0,
        )


def test_monitor_creation_and_event_binding_are_one_transaction(tmp_path) -> None:
    first = Ledger(tmp_path)
    second = Ledger(tmp_path)
    first.reserve_event(
        reservation_id="event-1",
        idempotency_key=None,
        kind="command_terminal",
        producer_id="shell-test",
        semantic={},
        publish_token="secret-token",
        expires_at=200.0,
        now=100.0,
    )
    monitor, created = first.create_or_get(
        monitor_id="monitor-1",
        idempotency_key="monitor-request",
        semantic={"request": "same"},
        target=TargetGuard("test-thread", goal()),
        condition={"type": "command_terminal", "reservation_id": "event-1"},
        allow_heuristic_continuation=False,
        expires_at=300.0,
        event_reservation_id="event-1",
        event_kind="command_terminal",
        now=101.0,
    )
    assert created
    assert first.get_event("event-1").bound_monitor_id == monitor.monitor_id

    with pytest.raises(ConflictError):
        second.create_or_get(
            monitor_id="monitor-2",
            idempotency_key=None,
            semantic={"request": "other"},
            target=TargetGuard("other-thread", goal()),
            condition={"type": "command_terminal", "reservation_id": "event-1"},
            allow_heuristic_continuation=False,
            expires_at=300.0,
            event_reservation_id="event-1",
            event_kind="command_terminal",
            now=101.0,
        )
    assert second.get("monitor-2") is None


def test_terminal_before_binding_survives_restart_and_binds_before_deadline(tmp_path) -> None:
    ledger = Ledger(tmp_path)
    ledger.reserve_event(
        reservation_id="event-1",
        idempotency_key=None,
        kind="command_terminal",
        producer_id="shell-test",
        semantic={},
        publish_token="secret-token",
        expires_at=200.0,
        now=100.0,
    )
    ledger.publish_terminal_event(
        "event-1",
        publish_token="secret-token",
        terminal_event=command_terminal(),
        now=101.0,
    )
    ledger.close()

    reopened = Ledger(tmp_path)
    monitor, _ = reopened.create_or_get(
        monitor_id="monitor-1",
        idempotency_key=None,
        semantic={"request": "same"},
        target=TargetGuard("test-thread", goal()),
        condition={"type": "command_terminal", "reservation_id": "event-1"},
        allow_heuristic_continuation=False,
        expires_at=300.0,
        event_reservation_id="event-1",
        event_kind="command_terminal",
        now=102.0,
    )
    event = reopened.get_event("event-1")
    assert event.bound_monitor_id == monitor.monitor_id
    assert event.state.value == "terminal"


@pytest.mark.parametrize(
    "lifecycle", ["reserved", "bound", "terminal", "cancelled", "expired"]
)
def test_restart_preserves_every_event_lifecycle_fact(tmp_path, lifecycle: str) -> None:
    ledger = Ledger(tmp_path)
    ledger.reserve_event(
        reservation_id="event-1",
        idempotency_key=None,
        kind="command_terminal",
        producer_id="shell-test",
        semantic={},
        publish_token="secret-token",
        expires_at=200.0,
        now=100.0,
    )
    if lifecycle == "bound":
        ledger.create_or_get(
            monitor_id="monitor-1",
            idempotency_key=None,
            semantic={"restart": lifecycle},
            target=TargetGuard("test-thread", goal()),
            condition={"type": "command_terminal", "reservation_id": "event-1"},
            allow_heuristic_continuation=False,
            expires_at=300.0,
            event_reservation_id="event-1",
            event_kind="command_terminal",
            now=101.0,
        )
    elif lifecycle == "terminal":
        ledger.publish_terminal_event(
            "event-1",
            publish_token="secret-token",
            terminal_event=command_terminal(),
            now=101.0,
        )
    elif lifecycle == "cancelled":
        ledger.cancel_event("event-1", now=101.0)
    elif lifecycle == "expired":
        ledger.expire_events(now=200.0)
    before = ledger.get_event("event-1").status_dict()
    ledger.close()

    reopened = Ledger(tmp_path)
    after = reopened.get_event("event-1")

    assert after.state.value == lifecycle
    assert after.status_dict() == before
    if lifecycle == "terminal":
        replay = reopened.publish_terminal_event(
            "event-1",
            publish_token="secret-token",
            terminal_event=command_terminal(),
            now=202.0,
        )
        assert replay.status_dict() == before


@pytest.mark.parametrize("winner", ["cancel", "expire"])
def test_cancel_or_expiry_winner_prevents_orphan_monitor_binding(tmp_path, winner: str) -> None:
    first = Ledger(tmp_path)
    second = Ledger(tmp_path)
    first.reserve_event(
        reservation_id="event-1",
        idempotency_key=None,
        kind="command_terminal",
        producer_id="shell-test",
        semantic={},
        publish_token="secret-token",
        expires_at=101.0,
        now=100.0,
    )
    if winner == "cancel":
        first.cancel_event("event-1", now=100.5)
        bind_at = 100.5
    else:
        first.expire_events(now=101.0)
        bind_at = 101.0

    with pytest.raises(ConflictError):
        second.create_or_get(
            monitor_id="orphan",
            idempotency_key=None,
            semantic={"request": winner},
            target=TargetGuard("test-thread", goal()),
            condition={"type": "command_terminal", "reservation_id": "event-1"},
            allow_heuristic_continuation=False,
            expires_at=300.0,
            event_reservation_id="event-1",
            event_kind="command_terminal",
            now=bind_at,
        )
    assert second.get("orphan") is None


def test_event_snapshot_evaluation_and_claim_are_one_transaction(tmp_path) -> None:
    ledger = Ledger(tmp_path)
    ledger.reserve_event(
        reservation_id="event-1",
        idempotency_key=None,
        kind="command_terminal",
        producer_id="shell-test",
        semantic={},
        publish_token="secret-token",
        expires_at=200.0,
        now=100.0,
    )
    condition = {
        "type": "any",
        "children": [
            {"type": "command_terminal", "reservation_id": "event-1"},
            {
                "type": "heartbeat_stale",
                "reservation_id": "event-1",
                "stale_after_seconds": 10,
            },
        ],
    }
    monitor, _ = ledger.create_or_get(
        monitor_id="monitor-1",
        idempotency_key=None,
        semantic={"request": "same"},
        target=TargetGuard("test-thread", goal()),
        condition=condition,
        allow_heuristic_continuation=False,
        expires_at=105.0,
        event_reservation_id="event-1",
        event_kind="command_terminal",
        now=101.0,
    )
    ledger.arm(monitor.monitor_id)
    ledger.publish_event_heartbeat(
        "event-1",
        publish_token="secret-token",
        heartbeat=normalize_heartbeat({"sequence": 1, "payload": {}}),
        now=102.0,
    )
    ledger.publish_terminal_event(
        "event-1",
        publish_token="secret-token",
        terminal_event=command_terminal(),
        now=104.0,
    )

    claimed = ledger.evaluate_and_claim_event_monitor(
        "monitor-1",
        expected_evaluation_count=0,
        observed_condition=condition,
        external_evaluations={},
        now_factory=lambda: 106.0,
    )

    assert claimed is not None
    assert claimed.state.value == "claimed"
    assert claimed.wake_reason.value == "condition"
    assert claimed.evaluation_count == 1
    assert [item["type"] for item in claimed.witness] == ["command_terminal"]
    assert ledger.evaluate_and_claim_event_monitor(
        "monitor-1",
        expected_evaluation_count=1,
        observed_condition=condition,
        external_evaluations={},
        now_factory=lambda: 107.0,
    ) is None


def test_missing_bound_event_becomes_observer_failed_not_an_untyped_retry(tmp_path) -> None:
    ledger = Ledger(tmp_path)
    ledger.reserve_event(
        reservation_id="event-1",
        idempotency_key=None,
        kind="command_terminal",
        producer_id="shell-test",
        semantic={},
        publish_token="secret-token",
        expires_at=200.0,
        now=100.0,
    )
    condition = {"type": "command_terminal", "reservation_id": "event-1"}
    monitor, _ = ledger.create_or_get(
        monitor_id="monitor-1",
        idempotency_key=None,
        semantic={"request": "same"},
        target=TargetGuard("test-thread", goal()),
        condition=condition,
        allow_heuristic_continuation=False,
        expires_at=200.0,
        event_reservation_id="event-1",
        event_kind="command_terminal",
        now=101.0,
    )
    ledger.arm(monitor.monitor_id)
    ledger._connection.execute(
        "DELETE FROM event_reservations WHERE reservation_id = ?", ("event-1",)
    )

    failed = ledger.evaluate_and_claim_event_monitor(
        "monitor-1",
        expected_evaluation_count=0,
        observed_condition=condition,
        external_evaluations={},
        now_factory=lambda: 102.0,
    )

    assert failed is not None
    assert failed.state.value == "observer_failed"
    assert failed.evidence["kind"] == "missing_bound_reservation"


def test_downgrade_daemon_is_refused_while_a_bound_reservation_exists(tmp_path) -> None:
    ledger = Ledger(tmp_path)
    ledger.reserve_event(
        reservation_id="event-1",
        idempotency_key=None,
        kind="command_terminal",
        producer_id="shell-test",
        semantic={},
        publish_token="secret-token",
        expires_at=200.0,
        now=100.0,
    )
    ledger.create_or_get(
        monitor_id="monitor-1",
        idempotency_key=None,
        semantic={"request": "same"},
        target=TargetGuard("test-thread", goal()),
        condition={"type": "command_terminal", "reservation_id": "event-1"},
        allow_heuristic_continuation=False,
        expires_at=200.0,
        event_reservation_id="event-1",
        event_kind="command_terminal",
        now=101.0,
    )
    ledger.close()

    with pytest.raises(RuntimeError, match="event schema epoch"):
        assert_event_schema_compatible(tmp_path, supported_event_epoch=0)

    reopened = Ledger(tmp_path)
    reopened.transition(
        "monitor-1",
        expected=(reopened.get("monitor-1").state,),
        state=MonitorState.CANCELLED,
    )
    assert not reopened.event_capability_required()


def test_downgrade_is_refused_for_nonterminal_event_monitor_with_missing_row(
    tmp_path,
) -> None:
    """Joining only surviving reservation rows must make this test fail."""

    ledger = Ledger(tmp_path)
    ledger.reserve_event(
        reservation_id="event-1",
        idempotency_key=None,
        kind="command_terminal",
        producer_id="shell-test",
        semantic={},
        publish_token="secret-token",
        expires_at=200.0,
        now=100.0,
    )
    ledger.create_or_get(
        monitor_id="monitor-1",
        idempotency_key=None,
        semantic={"request": "same"},
        target=TargetGuard("test-thread", goal()),
        condition={"type": "command_terminal", "reservation_id": "event-1"},
        allow_heuristic_continuation=False,
        expires_at=200.0,
        event_reservation_id="event-1",
        event_kind="command_terminal",
        now=101.0,
    )
    ledger._connection.execute(
        "DELETE FROM event_reservations WHERE reservation_id = ?", ("event-1",)
    )
    ledger.close()

    with pytest.raises(RuntimeError, match="event schema epoch"):
        assert_event_schema_compatible(tmp_path, supported_event_epoch=0)

    reopened = Ledger(tmp_path)
    reopened.transition(
        "monitor-1",
        expected=(MonitorState.REGISTERING,),
        state=MonitorState.CANCELLED,
    )
    assert not reopened.event_capability_required()

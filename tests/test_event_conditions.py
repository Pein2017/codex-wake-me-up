from __future__ import annotations

import pytest

from codex_wake_me_up.conditions import (
    event_condition_binding,
    evaluate_event_condition_tree,
    witness_authorizes_continuation,
)
from codex_wake_me_up.models import TriState, ValidationError


def event_status(
    *,
    kind: str = "command_terminal",
    terminal_event: dict | None = None,
    heartbeat: dict | None = None,
    attestation: dict | None = None,
) -> dict:
    return {
        "reservation_id": "event-1",
        "kind": kind,
        "producer_id": "producer-1",
        "state": "terminal" if terminal_event else "bound",
        "bound_monitor_id": "monitor-1",
        "last_heartbeat": heartbeat,
        "terminal_event": terminal_event,
        "git_attestation": attestation,
    }


def test_event_binding_allows_one_terminal_and_shared_heartbeat_leaves() -> None:
    condition = {
        "type": "any",
        "children": [
            {"type": "command_terminal", "reservation_id": "event-1"},
            {
                "type": "heartbeat_stale",
                "reservation_id": "event-1",
                "stale_after_seconds": 60,
            },
        ],
    }
    assert event_condition_binding(condition) == ("event-1", "command_terminal")

    with pytest.raises(ValidationError, match="duplicate terminal"):
        event_condition_binding(
            {
                "type": "all",
                "children": [
                    {"type": "command_terminal", "reservation_id": "event-1"},
                    {"type": "command_terminal", "reservation_id": "event-1"},
                ],
            }
        )
    with pytest.raises(ValidationError, match="one distinct reservation"):
        event_condition_binding(
            {
                "type": "all",
                "children": [
                    {"type": "command_terminal", "reservation_id": "event-1"},
                    {
                        "type": "heartbeat_stale",
                        "reservation_id": "event-2",
                        "stale_after_seconds": 60,
                    },
                ],
            }
        )


def test_command_terminal_truth_is_handling_evidence_not_task_success() -> None:
    condition = {"type": "command_terminal", "reservation_id": "event-1"}
    status = event_status(
        terminal_event={
            "kind": "command_terminal",
            "status": "failed",
            "exit_code": 2,
            "lead_accepted": False,
        }
    )

    evaluation = evaluate_event_condition_tree(condition, status, now=100.0)

    assert evaluation.value is TriState.TRUE
    assert evaluation.witness[0]["classification"] == "command_termination"
    assert evaluation.witness[0]["task_success"] is False
    assert evaluation.witness[0]["lead_accepted"] is False
    assert witness_authorizes_continuation(
        evaluation.witness, allow_heuristic_continuation=False
    )


@pytest.mark.parametrize(
    ("attestation_status", "classification"),
    [("valid", "valid_delivery_candidate"), ("out_of_scope", "invalid_delivery")],
)
def test_worker_delivery_wakes_for_valid_and_invalid_attestation(
    attestation_status: str, classification: str
) -> None:
    condition = {"type": "worker_terminal", "reservation_id": "event-1"}
    status = event_status(
        kind="worker_terminal",
        terminal_event={
            "kind": "worker_terminal",
            "outcome": "delivered",
            "producer_task_id": "worker-1",
            "candidate_oid": "a" * 40,
            "lead_accepted": False,
        },
        attestation={"status": attestation_status, "lead_accepted": False},
    )

    evaluation = evaluate_event_condition_tree(condition, status, now=100.0)

    assert evaluation.value is TriState.TRUE
    assert evaluation.witness[0]["classification"] == classification
    assert evaluation.witness[0]["lead_accepted"] is False


def test_heartbeat_stale_is_unknown_without_a_heartbeat_and_false_after_terminal() -> None:
    condition = {
        "type": "heartbeat_stale",
        "reservation_id": "event-1",
        "stale_after_seconds": 10,
    }
    missing = evaluate_event_condition_tree(
        condition, event_status(), now=100.0
    )
    stale = evaluate_event_condition_tree(
        condition,
        event_status(heartbeat={"sequence": 3, "host_received_at": 80.0, "payload": {}}),
        now=100.0,
    )
    terminal = evaluate_event_condition_tree(
        condition,
        event_status(
            heartbeat={"sequence": 3, "host_received_at": 80.0, "payload": {}},
            terminal_event={"kind": "command_terminal", "status": "succeeded"},
        ),
        now=100.0,
    )

    assert missing.value is TriState.UNKNOWN
    assert not missing.fatal
    assert stale.value is TriState.TRUE
    assert stale.witness[0]["classification"] == "heuristic_stall"
    assert not witness_authorizes_continuation(
        stale.witness, allow_heuristic_continuation=False
    )
    assert terminal.value is TriState.FALSE


def test_missing_or_mismatched_bound_event_is_fatal_unknown() -> None:
    condition = {"type": "command_terminal", "reservation_id": "event-1"}
    assert evaluate_event_condition_tree(condition, None, now=100.0).fatal
    wrong = event_status(kind="worker_terminal")
    assert evaluate_event_condition_tree(condition, wrong, now=100.0).fatal


@pytest.mark.parametrize("fact", ["cancelled_at", "expired_at"])
def test_bound_cancelled_or_expired_event_fact_is_fatal_unknown(fact: str) -> None:
    condition = {
        "type": "heartbeat_stale",
        "reservation_id": "event-1",
        "stale_after_seconds": 10,
    }
    status = event_status(
        heartbeat={"sequence": 1, "host_received_at": 80.0, "payload": {}}
    )
    status[fact] = 90.0

    evaluation = evaluate_event_condition_tree(condition, status, now=100.0)

    assert evaluation.value is TriState.UNKNOWN
    assert evaluation.fatal
    assert evaluation.evidence["kind"] == "bound_reservation_terminality_corrupt"

"""Model-facing payload budgets without weakening the durable audit view."""

from __future__ import annotations

import asyncio
import json
import os

import pytest

from codex_wake_me_up import mcp_server
from codex_wake_me_up.models import AppServerError, ValidationError
from tests.helpers import Clock
from tests.test_service import _armed_deferred
from tests.test_thread_delivery import FakeThreadDelivery, _thread_service


ARM_BASELINE_BYTES = 2119
RECORDED_BASELINE_BYTES = 3058


def _canonical_bytes(value: object) -> int:
    return len(
        json.dumps(
            value, sort_keys=True, separators=(",", ":"), ensure_ascii=False
        ).encode()
    )


def test_compact_arm_receipt_is_at_least_60_percent_smaller(tmp_path) -> None:
    log = tmp_path / "job.log"
    log.write_text("booting\n", encoding="utf-8")
    service = _thread_service(tmp_path, FakeThreadDelivery(), Clock())

    receipt = asyncio.run(
        service.wait_for_event(
            thread_id="thread-1",
            condition={
                "type": "any",
                "children": [
                    {
                        "type": "log_pattern",
                        "path": str(log),
                        "patterns": [
                            {"name": "success", "regex": "DONE"},
                            {"name": "failure", "regex": "Traceback|FAILED"},
                        ],
                    },
                    {"type": "pid_exit", "pid": os.getpid()},
                ],
            },
            expires_in_seconds=3600,
            idempotency_key="arm-any-v1",
            start_daemon=False,
        )
    )

    assert _canonical_bytes(receipt) <= ARM_BASELINE_BYTES * 0.40
    assert receipt["condition"]["type"] == "any"
    assert receipt["delivery"] == {
        "kind": "thread",
        "delivery_id": receipt["delivery"]["delivery_id"],
        "origin_thread_id": "thread-1",
        "target_thread_id": "thread-1",
    }
    assert receipt["next_action"] == "end_current_turn"
    assert "capability" not in receipt["delivery"]
    assert "outcome" not in receipt
    assert len(receipt) <= 10


def test_recorded_decision_status_is_small_one_call_report_and_audit_is_full(
    tmp_path, monkeypatch
) -> None:
    clock = Clock()
    recorded = {
        "classification": "recorded",
        "runtime_status": "idle",
        "interrupted": False,
        "history_matches": 1,
        "history_modified": 0,
        "queue_matches": 0,
        "queue_modified": 0,
        "observed_pointer_count": 1,
        "history": [{"turn_id": "turn-1", "message_id": "message-1"}],
        "queue": [],
        "queue_count": 0,
    }
    service = _thread_service(
        tmp_path, FakeThreadDelivery(inspections=[recorded]), clock
    )
    receipt = asyncio.run(
        service.wait_for_event(
            thread_id="thread-1",
            condition={"type": "time", "after_seconds": 1},
            expires_in_seconds=100,
            idempotency_key="recorded-v1",
            start_daemon=False,
        )
    )
    clock.value = 102.0
    asyncio.run(service.reconcile_once())

    decision = service.decision_status(receipt["monitor_id"])
    audit = service.status(receipt["monitor_id"])
    budget = RECORDED_BASELINE_BYTES * 0.35

    assert _canonical_bytes(decision) <= budget
    # Sensitivity: replacing the decision projection with audit status breaks budget.
    assert _canonical_bytes(audit) > budget
    assert decision["wake_reason"] == "condition"
    assert decision["delivery"]["classification"] == "recorded"
    assert decision["delivery"]["observed_pointer_count"] == 1
    assert decision["witness"][0]["type"] == "time"
    assert "capability" not in decision["delivery"]
    assert "reconciliation" not in decision
    assert "delivery_outcome" not in decision

    monkeypatch.setattr(mcp_server, "_service", service)
    assert mcp_server.wake_me_up_status(receipt["monitor_id"]) == decision
    assert mcp_server.wake_me_up_status(receipt["monitor_id"], view="audit") == audit
    with pytest.raises(ValidationError, match="view must be decision or audit"):
        mcp_server.wake_me_up_status(receipt["monitor_id"], view="details")


def test_observer_failure_remains_decidable_in_one_compact_status(tmp_path) -> None:
    clock = Clock()
    log = tmp_path / "job.log"
    log.write_text("starting\n", encoding="utf-8")
    service, _fake, receipt = _armed_deferred(
        tmp_path,
        clock,
        condition={
            "type": "log_pattern",
            "path": str(log),
            "patterns": [
                {"name": "success", "regex": "Ready"},
                {"name": "failure", "regex": "Traceback|FAILED"},
            ],
        },
    )
    log.unlink()
    asyncio.run(service.reconcile_once())

    decision = service.decision_status(receipt["monitor_id"])

    assert decision["wake_reason"] == "observer_failed"
    assert decision["failure_detail"]["kind"] == "log_identity_lost"
    assert decision["delivery"]["kind"] == "goal"
    assert decision["armed_at"] is not None
    assert decision["fired_at"] is not None
    assert decision["evaluation_count"] >= 1


def test_delivery_uncertain_keeps_fail_closed_diagnostics_without_readding(
    tmp_path,
) -> None:
    clock = Clock()
    absent = {
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
    adapter = FakeThreadDelivery(
        add_result=AppServerError("transport uncertain"),
        inspections=[absent, absent],
    )
    service = _thread_service(tmp_path, adapter, clock)
    receipt = asyncio.run(
        service.wait_for_event(
            thread_id="thread-1",
            condition={"type": "time", "after_seconds": 1},
            expires_in_seconds=100,
            idempotency_key="uncertain-v1",
            start_daemon=False,
        )
    )
    clock.value = 102.0
    asyncio.run(service.reconcile_once())
    clock.value = 163.0
    asyncio.run(service.reconcile_once())

    decision = service.decision_status(receipt["monitor_id"])

    assert decision["delivery"]["classification"] == "delivery_uncertain"
    assert decision["delivery"]["diagnostic"]["absence_kind"] == (
        "unresolved_absence"
    )
    assert decision["delivery"]["reconciliation"]["error"] == (
        "transport uncertain"
    )
    assert decision["delivery"]["reconciliation"]["online_absence_seconds"] >= 60
    assert decision["witness"][0]["type"] == "time"
    assert len(adapter.add_calls) == 1


def test_decision_journal_collapses_consecutive_duplicates(tmp_path) -> None:
    clock = Clock()
    log = tmp_path / "job.log"
    log.write_text("starting\n", encoding="utf-8")
    service, _fake, receipt = _armed_deferred(
        tmp_path,
        clock,
        condition={
            "type": "log_pattern",
            "path": str(log),
            "patterns": [{"name": "failure", "regex": "GPU [01] failed"}],
        },
        allow_heuristic_continuation=True,
    )
    with log.open("a", encoding="utf-8") as handle:
        handle.write("GPU 0 failed\nGPU 0 failed\nGPU 1 failed\n")
    asyncio.run(service.reconcile_once())

    decision = service.decision_status(receipt["monitor_id"])

    assert decision["journal_tail"] == [
        {"line": "GPU 0 failed", "repeat_count": 2},
        {"line": "GPU 1 failed", "repeat_count": 1},
    ]
    assert decision["journal_dropped"] == 0


def test_failed_terminal_event_keeps_producer_evidence_and_false_flags(
    tmp_path,
) -> None:
    clock = Clock()
    service = _thread_service(tmp_path, FakeThreadDelivery(), clock)
    reserved = service.reserve_terminal_event(
        {
            "kind": "command_terminal",
            "expires_in_seconds": 100,
            "idempotency_key": "command-launch-1",
            "producer_identity": "shell-test",
        }
    )
    receipt = asyncio.run(
        service.wait_for_event(
            thread_id="thread-1",
            condition={
                "type": "command_terminal",
                "reservation_id": reserved["reservation_id"],
            },
            expires_in_seconds=100,
            idempotency_key="terminal-v1",
            start_daemon=False,
        )
    )
    service.publish_terminal_event(
        reserved["reservation_id"],
        publish_token=reserved["publish_token"],
        terminal_event={
            "kind": "command_terminal",
            "status": "failed",
            "command_label": "focused-test",
            "command_digest": "a" * 64,
            "exit_code": 2,
        },
    )
    asyncio.run(service.reconcile_once())

    decision = service.decision_status(receipt["monitor_id"])

    assert decision["task_success"] is False
    assert decision["lead_accepted"] is False
    assert decision["witness"][0]["classification"] == "command_termination"
    assert decision["terminal_event"]["producer_id"]
    assert decision["terminal_event"]["terminal_event"]["exit_code"] == 2
    assert "publish_token" not in json.dumps(decision)

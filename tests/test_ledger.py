from __future__ import annotations

import json
import sqlite3

from codex_wake_me_up.ledger import Ledger
from codex_wake_me_up.models import MonitorMode, MonitorState, TargetGuard

from .helpers import goal


def test_claim_is_once_and_cancel_wins_before_activation(tmp_path) -> None:
    ledger = Ledger(tmp_path)
    guard = TargetGuard("test-thread", goal())
    record, created = ledger.create_or_get(
        monitor_id="monitor",
        idempotency_key="key",
        semantic={"same": True},
        target=guard,
        condition={"type": "time", "deadline_utc": 0},
        allow_heuristic_continuation=True,
        expires_at=10_000,
    )
    assert created
    assert record.mode == MonitorMode.LEGACY
    assert not record.idle_barrier
    assert ledger.arm(record.monitor_id) is not None
    claimed = ledger.claim(
        record.monitor_id,
        condition={"type": "time", "deadline_utc": 0},
        evidence={"ok": True},
        witness=[{"type": "time"}],
    )
    assert claimed is not None
    assert ledger.claim(
        record.monitor_id,
        condition={"type": "time", "deadline_utc": 0},
        evidence={},
        witness=[],
    ) is None
    assert ledger.cancel(record.monitor_id) is not None
    assert ledger.begin_activation(record.monitor_id, activation_snapshot={}) is None
    assert ledger.get(record.monitor_id).state == MonitorState.CANCELLED


def test_recovery_consumes_a_durable_activating_attempt(tmp_path) -> None:
    ledger = Ledger(tmp_path)
    guard = TargetGuard("test-thread", goal())
    record, _ = ledger.create_or_get(
        monitor_id="monitor",
        idempotency_key=None,
        semantic={"same": True},
        target=guard,
        condition={"type": "time", "deadline_utc": 0},
        allow_heuristic_continuation=True,
        expires_at=10_000,
    )
    ledger.arm(record.monitor_id)
    ledger.claim(record.monitor_id, condition={"type": "time", "deadline_utc": 0}, evidence={}, witness=[])
    assert ledger.begin_activation(record.monitor_id, activation_snapshot={"before": True})
    ledger.close()

    reopened = Ledger(tmp_path)
    assert reopened.recover_activating() == 1
    recovered = reopened.get("monitor")
    assert recovered is not None
    assert recovered.state == MonitorState.ACTIVATION_UNCERTAIN
    assert recovered.activation_snapshot == {"before": True}


def test_recovery_closes_an_interrupted_registration_without_activation(tmp_path) -> None:
    ledger = Ledger(tmp_path)
    record, _ = ledger.create_or_get(
        monitor_id="registering",
        idempotency_key=None,
        semantic={"same": True},
        target=TargetGuard("test-thread", goal()),
        condition={"type": "time", "deadline_utc": 0},
        allow_heuristic_continuation=True,
        expires_at=10_000,
    )

    assert ledger.recover_registering() == 1
    recovered = ledger.get(record.monitor_id)
    assert recovered is not None
    assert recovered.state == MonitorState.SUPERSEDED
    assert recovered.outcome["kind"] == "registration_interrupted_by_restart"


def test_recovery_abandons_defer_intent_and_consumes_pausing(tmp_path) -> None:
    ledger = Ledger(tmp_path)
    guard = TargetGuard("test-thread", goal())
    intent, _ = ledger.create_or_get(
        monitor_id="intent",
        idempotency_key="intent-key",
        semantic={"request": "intent"},
        target=guard,
        condition={"type": "time", "deadline_utc": 0},
        allow_heuristic_continuation=True,
        expires_at=10_000,
        mode=MonitorMode.DEFERRED,
        idle_barrier=True,
        initial_state=MonitorState.DEFER_INTENT,
    )
    pausing, _ = ledger.create_or_get(
        monitor_id="pausing",
        idempotency_key="pausing-key",
        semantic={"request": "pausing"},
        target=guard,
        condition={"type": "time", "deadline_utc": 0},
        allow_heuristic_continuation=True,
        expires_at=10_000,
        mode=MonitorMode.DEFERRED,
        idle_barrier=True,
        initial_state=MonitorState.DEFER_INTENT,
    )
    assert ledger.begin_pause(pausing.monitor_id, pre_pause={}) is not None

    assert ledger.recover_defer_intent() == 1
    assert ledger.recover_pausing() == 1
    recovered_intent = ledger.get(intent.monitor_id)
    recovered_pausing = ledger.get(pausing.monitor_id)
    assert recovered_intent is not None
    assert recovered_intent.state == MonitorState.DEFER_ABANDONED
    assert recovered_intent.outcome["kind"] == "defer_intent_abandoned_after_restart"
    assert recovered_pausing is not None
    assert recovered_pausing.state == MonitorState.PAUSE_UNCERTAIN
    assert recovered_pausing.outcome["kind"] == "pause_uncertain_after_restart"


def test_existing_ledger_migrates_rows_to_legacy_mode(tmp_path) -> None:
    database = sqlite3.connect(tmp_path / "monitors.sqlite3")
    database.execute(
        """
        CREATE TABLE monitors (
            monitor_id TEXT PRIMARY KEY,
            idempotency_key TEXT UNIQUE,
            semantic_json TEXT NOT NULL,
            target_json TEXT NOT NULL,
            condition_json TEXT NOT NULL,
            state TEXT NOT NULL,
            allow_heuristic_continuation INTEGER NOT NULL,
            expires_at REAL NOT NULL,
            evidence_json TEXT NOT NULL DEFAULT '{}',
            witness_json TEXT NOT NULL DEFAULT '[]',
            outcome_json TEXT,
            activation_snapshot_json TEXT,
            created_at REAL NOT NULL,
            updated_at REAL NOT NULL
        )
        """
    )
    database.execute(
        """
        INSERT INTO monitors(
            monitor_id, idempotency_key, semantic_json, target_json,
            condition_json, state, allow_heuristic_continuation,
            expires_at, created_at, updated_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            "old",
            "old-key",
            json.dumps({"same": True}),
            json.dumps(TargetGuard("test-thread", goal()).as_dict()),
            json.dumps({"type": "time", "deadline_utc": 0}),
            MonitorState.ARMED.value,
            1,
            10_000,
            1,
            1,
        ),
    )
    database.commit()
    database.close()

    ledger = Ledger(tmp_path)
    migrated = ledger.get("old")
    assert migrated is not None
    assert migrated.mode == MonitorMode.LEGACY
    assert not migrated.idle_barrier

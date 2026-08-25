from __future__ import annotations

from dataclasses import replace
import json

import pytest

from codex_wake_me_up.models import ConflictError, ValidationError
from codex_wake_me_up.terminal_events import (
    EVENT_JSON_BUDGET,
    MAX_STRING_BYTES,
    CommandStatus,
    EventKind,
    Heartbeat,
    Reservation,
    ReservationState,
    WorkerOutcome,
    canonical_json,
    hash_publish_token,
    normalize_command_terminal,
    normalize_heartbeat,
    normalize_reservation,
    normalize_terminal_event,
    normalize_worker_terminal,
    payload_fingerprint,
    validate_heartbeat,
    validate_terminal_rewrite,
    verify_publish_token,
)


def command_payload(**overrides: object) -> dict[str, object]:
    payload: dict[str, object] = {
        "kind": "command_terminal",
        "status": "succeeded",
        "command_label": "unit-tests",
        "command_digest": "a" * 64,
        "exit_code": 0,
        "producer_at": 100.5,
        "log_paths": ["logs/unit-tests.log"],
        "artifact_paths": ["artifacts/result.json"],
    }
    payload.update(overrides)
    return payload


def worker_payload(**overrides: object) -> dict[str, object]:
    payload: dict[str, object] = {
        "kind": "worker_terminal",
        "outcome": "delivered",
        "producer_task_id": "worker-7",
        "candidate_oid": "b" * 40,
        "producer_at": 100.5,
    }
    payload.update(overrides)
    return payload


def reservation_payload(**overrides: object) -> dict[str, object]:
    payload: dict[str, object] = {
        "reservation_id": "evt-123",
        "kind": "worker_terminal",
        "expires_at": 200.0,
        "idempotency_key": "launch-7",
        "producer_task_id": "worker-7",
        "repository": "/srv/project/.git",
        "worktree": "/srv/project",
        "baseline_commit": "c" * 40,
        "allowed_path_prefixes": ["src", "tests"],
    }
    payload.update(overrides)
    return payload


def test_enums_are_typed_and_reject_unknown_values() -> None:
    assert EventKind.WORKER_TERMINAL.value == "worker_terminal"
    assert ReservationState.BOUND.value == "bound"
    assert CommandStatus.SIGNALED.value == "signaled"
    assert WorkerOutcome.DELIVERED.value == "delivered"

    with pytest.raises(ValidationError, match="event kind"):
        normalize_terminal_event(command_payload(kind="not-an-event"))


@pytest.mark.parametrize(
    "payload",
    [
        command_payload(status="succeeded"),
        command_payload(status="failed", exit_code=None),
        command_payload(status="failed", exit_code=0),
        command_payload(status="signaled", exit_code=None),
        command_payload(status="signaled", signal="SIGTERM", exit_code=2),
        command_payload(status="unknown"),
        command_payload(exit_code=True),
    ],
)
def test_command_status_evidence_is_literal_and_consistent(payload: dict[str, object]) -> None:
    if payload["status"] == "succeeded":
        payload["exit_code"] = None
    with pytest.raises(ValidationError):
        normalize_command_terminal(payload)


def test_success_and_failure_require_the_right_exit_evidence() -> None:
    success = normalize_command_terminal(command_payload())
    failure = normalize_command_terminal(command_payload(status="failed", exit_code=17))

    assert success.status is CommandStatus.SUCCEEDED
    assert success.exit_code == 0
    assert failure.status is CommandStatus.FAILED
    assert failure.exit_code == 17
    assert failure.as_dict()["status"] == "failed"


def test_command_rejects_raw_command_fields_and_unbounded_strings() -> None:
    for field in ("command", "raw_command", "argv", "shell_command"):
        with pytest.raises(ValidationError, match="raw command"):
            normalize_command_terminal(command_payload(**{field: "python train.py"}))

    with pytest.raises(ValidationError, match="4 KiB"):
        normalize_command_terminal(command_payload(command_label="x" * (MAX_STRING_BYTES + 1)))


def test_worker_delivery_requires_exactly_one_full_hex_oid() -> None:
    event = normalize_worker_terminal(worker_payload(candidate_oid="A" * 40))
    assert event.candidate_oid == "a" * 40

    for oid in (None, "b" * 7, "g" * 40, "b" * 39 + "!"):
        with pytest.raises(ValidationError, match="full commit"):
            normalize_worker_terminal(worker_payload(candidate_oid=oid))

    with pytest.raises(ValidationError, match="exactly one"):
        normalize_worker_terminal(
            worker_payload(candidate_oid="b" * 40, candidate_commit="b" * 40)
        )


@pytest.mark.parametrize("outcome", ["blocked", "failed", "cancelled"])
def test_worker_non_delivery_requires_reason_and_forbids_a_candidate(outcome: str) -> None:
    event = normalize_worker_terminal(
        worker_payload(outcome=outcome, candidate_oid=None, reason="worker stopped")
    )
    assert event.outcome.value == outcome
    assert event.candidate_oid is None
    assert "candidate_oid" not in event.as_dict()

    with pytest.raises(ValidationError, match="reason"):
        normalize_worker_terminal(worker_payload(outcome=outcome, candidate_oid=None))
    with pytest.raises(ValidationError, match="must not claim"):
        normalize_worker_terminal(
            worker_payload(outcome=outcome, reason="worker stopped", candidate_oid="b" * 40)
        )


def test_worker_identity_and_reason_are_bounded() -> None:
    with pytest.raises(ValidationError):
        normalize_worker_terminal(worker_payload(producer_task_id=""))
    with pytest.raises(ValidationError, match="4 KiB"):
        normalize_worker_terminal(
            worker_payload(producer_task_id="worker-7", reason="r" * (MAX_STRING_BYTES + 1))
        )


def test_event_json_budget_is_enforced_after_normalization() -> None:
    oversized = command_payload(log_paths=["x" * MAX_STRING_BYTES] * 64)
    with pytest.raises(ValidationError, match="64 KiB"):
        normalize_command_terminal(oversized)

    event = normalize_command_terminal(command_payload(log_paths=["x"] * 2))
    assert len(canonical_json(event.as_dict()).encode("utf-8")) <= EVENT_JSON_BUDGET


def test_normalizers_do_not_mutate_input_mappings_or_lists() -> None:
    raw = worker_payload(outcome="blocked", candidate_oid=None, reason="paused")
    original = json.loads(json.dumps(raw))
    event = normalize_worker_terminal(raw)
    raw["reason"] = "changed"
    assert raw == {**original, "reason": "changed"}
    assert event.reason == "paused"


def test_semantically_identical_dicts_have_one_order_independent_fingerprint() -> None:
    first = {"b": [1, 2], "a": {"z": "last", "y": True}}
    second = {"a": {"y": True, "z": "last"}, "b": [1, 2]}

    assert canonical_json(first) == canonical_json(second)
    assert payload_fingerprint(first) == payload_fingerprint(second)

    event_a = normalize_command_terminal(command_payload())
    event_b = normalize_command_terminal(
        {
            "artifact_paths": ["artifacts/result.json"],
            "producer_at": 100.5,
            "exit_code": 0,
            "command_digest": "A" * 64,
            "command_label": "unit-tests",
            "status": "succeeded",
            "kind": "command_terminal",
            "log_paths": ["logs/unit-tests.log"],
        }
    )
    assert event_a.fingerprint == event_b.fingerprint


def test_host_receive_time_is_not_producer_ordering_or_semantic_identity() -> None:
    first = normalize_command_terminal(command_payload(), host_received_at=1000.0)
    retry = normalize_command_terminal(command_payload(), host_received_at=900.0)

    assert first.host_received_at == 1000.0
    assert retry.host_received_at == 900.0
    assert first.fingerprint == retry.fingerprint
    assert first.as_dict()["host_received_at"] == 1000.0

    with pytest.raises(ValidationError, match="host receive"):
        normalize_command_terminal(command_payload(host_received_at=1.0))


def test_terminal_rewrite_is_immutable_but_identical_retry_is_idempotent() -> None:
    original = normalize_command_terminal(command_payload(), host_received_at=10.0)
    retry = normalize_command_terminal(command_payload(), host_received_at=20.0)
    assert validate_terminal_rewrite(original, retry) is original

    changed = normalize_command_terminal(command_payload(status="failed", exit_code=2))
    with pytest.raises(ConflictError, match="immutable"):
        validate_terminal_rewrite(original, changed)


def test_reservation_normalization_captures_worker_scope_and_state_is_typed() -> None:
    reservation = normalize_reservation(reservation_payload(), now=100.0)
    assert reservation.kind is EventKind.WORKER_TERMINAL
    assert reservation.state is ReservationState.RESERVED
    assert reservation.allowed_path_prefixes == ("src", "tests")
    assert reservation.baseline_commit == "c" * 40
    status = reservation.as_status()
    assert status["state"] == "reserved"
    assert "publish_token" not in status
    assert "token" not in status


def test_worker_reservation_rejects_unsafe_or_unrestricted_scope() -> None:
    for prefixes in ([], ["."], ["../src"], ["/absolute"], [".git"], ["--option"]):
        with pytest.raises(ValidationError):
            normalize_reservation(
                reservation_payload(allowed_path_prefixes=prefixes), now=100.0
            )

    with pytest.raises(ValidationError, match="absolute"):
        normalize_reservation(
            reservation_payload(worktree="relative/worktree"), now=100.0
        )
    with pytest.raises(ValidationError, match="full commit"):
        normalize_reservation(
            reservation_payload(baseline_commit="abc123"), now=100.0
        )


def test_command_reservation_does_not_accept_worker_delivery_scope() -> None:
    reservation = normalize_reservation(
        reservation_payload(
            kind="command_terminal",
            producer_task_id=None,
            repository=None,
            worktree=None,
            baseline_commit=None,
            allowed_path_prefixes=[],
        ),
        now=100.0,
    )
    assert reservation.kind is EventKind.COMMAND_TERMINAL
    with pytest.raises(ValidationError):
        normalize_reservation(
            reservation_payload(kind="command_terminal", repository="/x/.git"),
            now=100.0,
        )


def test_reservation_expiry_is_bounded_and_bool_is_not_a_timestamp() -> None:
    with pytest.raises(ValidationError):
        normalize_reservation(reservation_payload(expires_at=True), now=100.0)
    with pytest.raises(ValidationError):
        normalize_reservation(reservation_payload(expires_at=0), now=100.0)
    with pytest.raises(ValidationError):
        normalize_reservation(reservation_payload(expires_at=100.0), now=100.0)


def test_heartbeat_sequence_advances_and_identical_latest_retry_is_idempotent() -> None:
    first = normalize_heartbeat({"sequence": 1, "payload": {"phase": "compile"}}, host_received_at=10.0)
    retry = normalize_heartbeat({"sequence": 1, "payload": {"phase": "compile"}}, host_received_at=11.0)
    second = normalize_heartbeat({"sequence": 2, "payload": {"phase": "test"}}, host_received_at=12.0)

    assert isinstance(first, Heartbeat)
    assert validate_heartbeat(None, first) is first
    assert validate_heartbeat(first, retry) is first
    assert validate_heartbeat(first, second) is second

    with pytest.raises(ConflictError, match="heartbeat"):
        validate_heartbeat(second, first)
    with pytest.raises(ConflictError, match="heartbeat"):
        validate_heartbeat(first, normalize_heartbeat({"sequence": 1, "payload": {"phase": "other"}}))


def test_heartbeat_rejects_bool_sequence_oversized_payload_and_producer_receive_time() -> None:
    with pytest.raises(ValidationError, match="sequence"):
        normalize_heartbeat({"sequence": True, "payload": {}})
    with pytest.raises(ValidationError, match="64 KiB"):
        normalize_heartbeat(
            {"sequence": 1, "payload": {"data": ["x" * MAX_STRING_BYTES] * 20}}
        )
    with pytest.raises(ValidationError, match="host receive"):
        normalize_heartbeat({"sequence": 1, "host_received_at": 1.0, "payload": {}})


def test_publish_token_digest_is_salted_and_verification_is_redacted() -> None:
    digest = hash_publish_token("secret-token", salt=b"salt-1")
    assert digest != hash_publish_token("secret-token", salt=b"salt-2")
    assert verify_publish_token("secret-token", digest, salt=b"salt-1")
    assert not verify_publish_token("wrong-token", digest, salt=b"salt-1")
    assert "secret-token" not in digest


def test_status_shapes_keep_terminal_evidence_but_not_publish_tokens() -> None:
    event = normalize_worker_terminal(worker_payload())
    reservation = normalize_reservation(reservation_payload(), now=100.0)
    status = reservation.as_status(terminal_event=event, token_fingerprint="deadbeef" * 8)

    assert status["terminal_event"]["candidate_oid"] == "b" * 40
    assert status["terminal_event"]["lead_accepted"] is False
    assert status["publish_token_fingerprint"] == "deadbeef" * 8
    assert "secret-token" not in json.dumps(status, sort_keys=True)


def test_terminal_event_normalizer_rejects_kind_mismatch() -> None:
    with pytest.raises(ValidationError, match="kind"):
        normalize_terminal_event(worker_payload(), kind=EventKind.COMMAND_TERMINAL)


def test_reservation_fingerprint_excludes_runtime_state_and_is_deterministic() -> None:
    first = normalize_reservation(reservation_payload(), now=100.0)
    changed_state = replace(first, state=ReservationState.BOUND)
    assert first.fingerprint == changed_state.fingerprint
    assert first.fingerprint == payload_fingerprint(first.semantic_payload())


@pytest.mark.parametrize("value", [float("nan"), float("inf"), float("-inf"), 10**1000])
def test_malformed_numeric_inputs_are_closed_as_validation_errors(value: object) -> None:
    with pytest.raises(ValidationError):
        normalize_heartbeat({"sequence": value, "payload": {}})
    with pytest.raises(ValidationError):
        normalize_command_terminal(command_payload(exit_code=value))
    with pytest.raises(ValidationError):
        normalize_reservation(reservation_payload(expires_at=value), now=100.0)


def test_reservation_always_has_a_bounded_host_reference_time() -> None:
    with pytest.raises(ValidationError, match="now is required"):
        normalize_reservation(reservation_payload())
    with pytest.raises(ValidationError, match="bounded lifetime"):
        normalize_reservation(
            reservation_payload(expires_at=10_000_000.0), now=100.0
        )
    with pytest.raises(ValidationError, match="bounded lifetime"):
        Reservation(
            kind="command_terminal",
            reserved_at=100.0,
            expires_at=10_000_000.0,
        )


def test_heartbeat_payload_is_deeply_immutable_and_returns_a_copy() -> None:
    original = {"nested": {"phase": "compile"}, "items": ["a"]}
    heartbeat = normalize_heartbeat({"sequence": 1, "payload": original})
    fingerprint = heartbeat.fingerprint

    original["nested"]["phase"] = "changed"
    original["items"].append("b")
    assert heartbeat.as_dict()["payload"] == {
        "nested": {"phase": "compile"},
        "items": ["a"],
    }
    assert heartbeat.fingerprint == fingerprint
    with pytest.raises(TypeError):
        heartbeat.payload["nested"]["phase"] = "mutated"  # type: ignore[index]


@pytest.mark.parametrize(
    "key",
    ["publish_token", "publishToken", "token", "cmd", "commandLine", "command_line_args"],
)
def test_heartbeat_rejects_nested_secret_or_raw_command_fields(key: str) -> None:
    with pytest.raises(ValidationError):
        normalize_heartbeat(
            {"sequence": 1, "payload": {"nested": {key: "must-not-appear"}}}
        )


def test_reservation_status_rejects_a_terminal_event_of_the_wrong_kind() -> None:
    reservation = normalize_reservation(reservation_payload(), now=100.0)
    with pytest.raises(ValidationError, match="kind"):
        reservation.as_status(terminal_event=normalize_command_terminal(command_payload()))


def test_reservation_semantic_payload_obeys_the_aggregate_budget() -> None:
    prefixes = [f"src/{index}-" + "x" * 4000 for index in range(64)]
    with pytest.raises(ValidationError, match="64 KiB"):
        normalize_reservation(
            reservation_payload(allowed_path_prefixes=prefixes), now=100.0
        )

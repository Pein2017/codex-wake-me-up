from __future__ import annotations

import json
import time
from pathlib import Path

import pytest

from codex_wake_me_up import cli, mcp_server
from codex_wake_me_up.ledger import Ledger
from codex_wake_me_up.models import ValidationError
from codex_wake_me_up.payloads import load_private_json_payload


def test_private_payload_loader_rejects_world_readable_symlink_and_oversize(
    tmp_path,
) -> None:
    payload = tmp_path / "payload.json"
    payload.write_text(json.dumps({"reservation_id": "event-1"}))
    payload.chmod(0o600)
    assert load_private_json_payload(payload)["reservation_id"] == "event-1"

    payload.chmod(0o644)
    with pytest.raises(ValidationError, match="private"):
        load_private_json_payload(payload)
    payload.chmod(0o600)
    link = tmp_path / "link.json"
    link.symlink_to(payload)
    with pytest.raises(ValidationError, match="regular file"):
        load_private_json_payload(link)
    payload.write_text(json.dumps({"data": "x" * (64 * 1024)}))
    with pytest.raises(ValidationError, match="64 KiB"):
        load_private_json_payload(payload)


@pytest.mark.parametrize(
    "command",
    [
        "event-reserve",
        "event-status",
        "event-cancel",
        "event-heartbeat",
        "event-publish",
    ],
)
def test_cli_exposes_event_operations_without_token_arguments(command: str) -> None:
    parser = cli.build_parser()
    args = (
        [command, "--reservation-id", "event-1"]
        if command in {"event-status", "event-cancel"}
        else [command, "--payload", "/tmp/private.json"]
    )
    parsed = parser.parse_args(args)
    assert parsed.command == command
    assert not hasattr(parsed, "token")


def test_mcp_event_publishers_accept_only_a_private_payload_path() -> None:
    assert callable(mcp_server.wake_me_up_event_reserve)
    assert callable(mcp_server.wake_me_up_event_status)
    assert callable(mcp_server.wake_me_up_event_cancel)
    assert callable(mcp_server.wake_me_up_event_heartbeat)
    assert callable(mcp_server.wake_me_up_event_publish)


def test_cli_exposes_current_binary_rollback_compatibility_preflight() -> None:
    parsed = cli.build_parser().parse_args(
        ["event-compatibility-check", "--supported-event-epoch", "0"]
    )

    assert parsed.command == "event-compatibility-check"
    assert parsed.supported_event_epoch == 0


def _seed_scopeless_worker(root: Path) -> tuple[str, str]:
    reservation_id = "worker-event-1"
    token = "worker-secret-token"
    ledger = Ledger(root)
    try:
        ledger.reserve_event(
            reservation_id=reservation_id,
            idempotency_key="worker-reservation-1",
            kind="worker_terminal",
            producer_id="worker-1",
            semantic={"producer_task_id": "worker-1"},
            publish_token=token,
            expires_at=time.time() + 3600,
        )
    finally:
        ledger.close()
    return reservation_id, token


def _private_json(path: Path, value: dict) -> Path:
    path.write_text(json.dumps(value), encoding="utf-8")
    path.chmod(0o600)
    return path


def test_cli_preflights_a_worker_descriptor_read_only_and_redacted(tmp_path, capsys) -> None:
    root = tmp_path / "codex-home" / "runtime" / "codex-wake-me-up"
    root.mkdir(parents=True)
    reservation_id, token = _seed_scopeless_worker(root)
    descriptor = _private_json(
        root / "worker.publisher.json",
        {
            "schema": 1,
            "reservation_id": reservation_id,
            "kind": "worker_terminal",
            "publish_token": token,
        },
    )

    before = cli.main(
        [
            "--runtime-root",
            str(root),
            "event-worker-preflight",
            "--descriptor",
            str(descriptor),
            "--producer-task-id",
            "worker-1",
        ]
    )
    first = capsys.readouterr()

    assert before == 0
    receipt = json.loads(first.out)
    assert receipt["compatible"] is True
    assert receipt["reservation_id"] == reservation_id
    assert receipt["kind"] == "worker_terminal"
    assert receipt["producer_task_id"] == "worker-1"
    assert token not in first.out + first.err
    assert cli.main(
        [
            "--runtime-root",
            str(root),
            "event-status",
            "--reservation-id",
            reservation_id,
        ]
    ) == 0
    after = json.loads(capsys.readouterr().out)
    assert after["state"] == "reserved"
    assert after["terminal_event"] is None


@pytest.mark.parametrize(
    ("expected_producer", "descriptor_token"),
    [("other-worker", "worker-secret-token"), ("worker-1", "wrong-token")],
)
def test_cli_worker_descriptor_preflight_rejects_wrong_identity_without_state_change(
    tmp_path, capsys, expected_producer: str, descriptor_token: str
) -> None:
    root = tmp_path / "codex-home" / "runtime" / "codex-wake-me-up"
    root.mkdir(parents=True)
    reservation_id, token = _seed_scopeless_worker(root)
    descriptor = _private_json(
        root / "worker.publisher.json",
        {
            "schema": 1,
            "reservation_id": reservation_id,
            "kind": "worker_terminal",
            "publish_token": descriptor_token,
        },
    )

    assert cli.main(
        [
            "--runtime-root",
            str(root),
            "event-worker-preflight",
            "--descriptor",
            str(descriptor),
            "--producer-task-id",
            expected_producer,
        ]
    ) == 2
    output = capsys.readouterr()
    assert token not in output.out + output.err
    ledger = Ledger(root)
    try:
        status = ledger.get_event(reservation_id)
        assert status is not None
        assert status.state.value == "reserved"
        assert status.terminal_event is None
    finally:
        ledger.close()


def test_cli_publishes_completed_from_separate_private_event_file_idempotently(
    tmp_path, capsys
) -> None:
    root = tmp_path / "codex-home" / "runtime" / "codex-wake-me-up"
    root.mkdir(parents=True)
    reservation_id, token = _seed_scopeless_worker(root)
    descriptor = _private_json(
        root / "worker.publisher.json",
        {
            "schema": 1,
            "reservation_id": reservation_id,
            "kind": "worker_terminal",
            "publish_token": token,
        },
    )
    event_payload = _private_json(
        root / "worker.event.json",
        {
            "kind": "worker_terminal",
            "outcome": "completed",
            "producer_task_id": "worker-1",
        },
    )

    argv = [
        "--runtime-root",
        str(root),
        "event-worker-publish",
        "--descriptor",
        str(descriptor),
        "--event-payload",
        str(event_payload),
    ]
    assert cli.main(argv) == 0
    first = capsys.readouterr()
    assert cli.main(argv) == 0
    second = capsys.readouterr()

    first_receipt = json.loads(first.out)
    second_receipt = json.loads(second.out)
    assert first_receipt["state"] == "terminal"
    assert second_receipt == first_receipt
    assert token not in first.out + first.err + second.out + second.err
    assert str(event_payload) not in first.out + first.err + second.out + second.err
    assert json.dumps(json.loads(event_payload.read_text()), sort_keys=True) not in (
        first.out + first.err + second.out + second.err
    )

    ledger = Ledger(root)
    try:
        status = ledger.get_event(reservation_id)
        assert status is not None
        assert status.state.value == "terminal"
        assert status.terminal_event is not None
    finally:
        ledger.close()


@pytest.mark.parametrize("unsafe_input", ["descriptor", "event"])
def test_cli_worker_publication_rejects_unsafe_private_file_without_consuming_reservation(
    tmp_path, capsys, unsafe_input: str
) -> None:
    root = tmp_path / "codex-home" / "runtime" / "codex-wake-me-up"
    root.mkdir(parents=True)
    reservation_id, token = _seed_scopeless_worker(root)
    descriptor = _private_json(
        root / "worker.publisher.json",
        {
            "schema": 1,
            "reservation_id": reservation_id,
            "kind": "worker_terminal",
            "publish_token": token,
        },
    )
    event_payload = _private_json(
        root / "worker.event.json",
        {
            "kind": "worker_terminal",
            "outcome": "completed",
            "producer_task_id": "worker-1",
        },
    )
    (descriptor if unsafe_input == "descriptor" else event_payload).chmod(0o644)

    assert cli.main(
        [
            "--runtime-root",
            str(root),
            "event-worker-publish",
            "--descriptor",
            str(descriptor),
            "--event-payload",
            str(event_payload),
        ]
    ) == 2
    output = capsys.readouterr()
    assert token not in output.out + output.err
    ledger = Ledger(root)
    try:
        status = ledger.get_event(reservation_id)
        assert status is not None
        assert status.state.value == "reserved"
        assert status.terminal_event is None
    finally:
        ledger.close()

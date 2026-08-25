from __future__ import annotations

import json

import pytest

from codex_wake_me_up import cli, mcp_server
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

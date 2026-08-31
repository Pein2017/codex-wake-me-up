from __future__ import annotations

import json

import pytest

from codex_wake_me_up.models import ValidationError
from codex_wake_me_up.worker_delivery_adapter import (
    publish_worker_terminal_from_descriptor,
)


class Publisher:
    def __init__(self) -> None:
        self.calls: list[dict] = []

    def publish_terminal_event(self, reservation_id, *, publish_token, terminal_event):
        call = {
            "reservation_id": reservation_id,
            "publish_token": publish_token,
            "terminal_event": terminal_event,
        }
        self.calls.append(call)
        return {"published": True}


def descriptor(tmp_path, *, kind: str = "worker_terminal"):
    path = tmp_path / "publisher.json"
    path.write_text(
        json.dumps(
            {
                "schema": 1,
                "reservation_id": "event-1",
                "kind": kind,
                "publish_token": "secret-token",
            }
        )
    )
    path.chmod(0o600)
    return path


def test_native_or_harness_worker_uses_the_same_terminal_envelope(tmp_path) -> None:
    publisher = Publisher()
    envelope = {
        "kind": "worker_terminal",
        "outcome": "blocked",
        "producer_task_id": "worker-1",
        "reason": "needs lead input",
    }

    result = publish_worker_terminal_from_descriptor(
        publisher, descriptor(tmp_path), envelope
    )

    assert result == {"published": True}
    assert publisher.calls == [
        {
            "reservation_id": "event-1",
            "publish_token": "secret-token",
            "terminal_event": envelope,
        }
    ]


def test_worker_adapter_rejects_a_command_descriptor(tmp_path) -> None:
    with pytest.raises(ValidationError, match="worker_terminal"):
        publish_worker_terminal_from_descriptor(
            Publisher(),
            descriptor(tmp_path, kind="command_terminal"),
            {
                "kind": "worker_terminal",
                "outcome": "failed",
                "producer_task_id": "worker-1",
                "reason": "failed",
            },
        )


def test_worker_adapter_rejects_unclassified_uncertain_settlement_before_publish(tmp_path) -> None:
    publisher = Publisher()

    with pytest.raises(ValidationError, match="fixed classification"):
        publish_worker_terminal_from_descriptor(
            publisher,
            descriptor(tmp_path),
            {
                "kind": "worker_terminal",
                "outcome": "settlement_uncertain",
                "producer_task_id": "worker-1",
                "reason": "freeform uncertainty",
            },
        )

    assert publisher.calls == []


def test_worker_adapter_publishes_a_completed_settlement_without_a_candidate(
    tmp_path,
) -> None:
    publisher = Publisher()
    envelope = {
        "kind": "worker_terminal",
        "outcome": "completed",
        "producer_task_id": "worker-1",
    }

    result = publish_worker_terminal_from_descriptor(
        publisher, descriptor(tmp_path), envelope
    )

    assert result == {"published": True}
    assert publisher.calls == [
        {
            "reservation_id": "event-1",
            "publish_token": "secret-token",
            "terminal_event": envelope,
        }
    ]

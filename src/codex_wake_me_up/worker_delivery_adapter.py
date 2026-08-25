"""Reference adapter for native-subagent or HarnessDock worker completion."""

from __future__ import annotations

from pathlib import Path
from typing import Any, Mapping, Protocol

from .models import ValidationError
from .payloads import load_private_json_payload


class TerminalPublisher(Protocol):
    def publish_terminal_event(
        self,
        reservation_id: str,
        *,
        publish_token: str,
        terminal_event: Mapping[str, Any],
    ) -> dict[str, Any]: ...


def publish_worker_terminal_from_descriptor(
    publisher: TerminalPublisher,
    descriptor_path: str | Path,
    terminal_event: Mapping[str, Any],
) -> dict[str, Any]:
    """Publish one worker envelope without adding a watcher or scheduler."""

    descriptor = load_private_json_payload(descriptor_path)
    if descriptor.get("schema") != 1 or descriptor.get("kind") != "worker_terminal":
        raise ValidationError("publisher descriptor must name worker_terminal schema 1")
    reservation_id = descriptor.get("reservation_id")
    publish_token = descriptor.get("publish_token")
    if not isinstance(reservation_id, str) or not reservation_id:
        raise ValidationError("publisher descriptor has no reservation ID")
    if not isinstance(publish_token, str) or not publish_token:
        raise ValidationError("publisher descriptor has no publish capability")
    if terminal_event.get("kind") != "worker_terminal":
        raise ValidationError("worker adapter requires a worker_terminal envelope")
    return publisher.publish_terminal_event(
        reservation_id,
        publish_token=publish_token,
        terminal_event=terminal_event,
    )

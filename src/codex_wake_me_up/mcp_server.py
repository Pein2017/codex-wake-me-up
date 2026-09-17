"""Stdio MCP façade for monitor registration, inspection, and cancellation."""

from __future__ import annotations

from typing import Any, Literal

from mcp.server.fastmcp import Context, FastMCP

from .models import PreArmRegistrationError, ValidationError
from .payloads import load_private_json_payload
from .service import MonitorService


mcp = FastMCP(
    "Codex Wake Me Up",
    instructions=(
        "Use wait_for_event for ordinary waits regardless of goal state, bound to "
        "the trusted current task; do not create a goal or launch its producer. "
        "end the current turn only after state=armed. "
        "Delivery is billed, not exactly-once, and not task success; then read "
        "wake_me_up_status once. Use defer_goal_until_event only for explicit "
        "legacy goal pause/reactivation."
    ),
)
_service: MonitorService | None = None


def service() -> MonitorService:
    global _service
    if _service is None:
        _service = MonitorService()
    return _service


def _trusted_thread_id(ctx: Context | None) -> str | None:
    if ctx is None:
        return None
    try:
        request_context = ctx.request_context
    except ValueError:
        request_context = None
    meta = request_context.meta if request_context is not None else None
    thread_id = getattr(meta, "threadId", None)
    return thread_id.strip() if isinstance(thread_id, str) and thread_id.strip() else None


@mcp.tool(
    name="wait_for_event",
    description=(
        "Arm one durable monitor for the trusted current task and return immediately; "
        "never launch or join its producer. End the turn only after state=armed. "
        "Delivery is billed, not exactly-once, and not task success."
    ),
)
async def wait_for_event(
    condition: dict[str, Any],
    expires_in_seconds: float,
    idempotency_key: str,
    ctx: Context,
    rearm_of: str | None = None,
) -> dict[str, Any]:
    thread_id = _trusted_thread_id(ctx)
    if thread_id is None:
        raise ValidationError(
            "wait_for_event requires the trusted Codex caller thread identity"
        )
    try:
        return await service().wait_for_current_event(
            thread_id=thread_id,
            condition=condition,
            expires_in_seconds=expires_in_seconds,
            idempotency_key=idempotency_key,
            rearm_of=rearm_of,
        )
    except PreArmRegistrationError as exc:
        return exc.as_dict()


@mcp.tool(
    name="wake_me_up_capabilities",
    description=(
        "Read the current plugin, daemon, and Core capability boundary without "
        "creating a monitor or changing a task."
    ),
)
async def wake_me_up_capabilities() -> dict[str, Any]:
    return await service().runtime_capabilities()


@mcp.tool(
    name="defer_goal_until_event",
    description=(
        "Legacy only: pause/reactivate an explicitly selected active or paused goal; "
        "never create a goal or fall back to thread delivery."
    ),
)
async def defer_goal_until_event(
    thread_id: str,
    condition: dict[str, Any],
    expires_in_seconds: float,
    idempotency_key: str,
    allow_heuristic_continuation: bool = False,
    rearm_of: str | None = None,
) -> dict[str, Any]:
    return await service().defer(
        thread_id=thread_id,
        condition=condition,
        expires_in_seconds=expires_in_seconds,
        allow_heuristic_continuation=allow_heuristic_continuation,
        idempotency_key=idempotency_key,
        rearm_of=rearm_of,
    )


@mcp.tool(
    name="wake_me_up",
    description=(
        "Legacy goal-only arm for an already paused goal; never create a missing goal "
        "or retry. Heuristic continuation must be explicit; rearm_of records lineage."
    ),
)
async def wake_me_up(
    thread_id: str,
    condition: dict[str, Any],
    expires_in_seconds: float,
    allow_heuristic_continuation: bool = False,
    idempotency_key: str | None = None,
    rearm_of: str | None = None,
) -> dict[str, Any]:
    return await service().register(
        thread_id=thread_id,
        condition=condition,
        expires_in_seconds=expires_in_seconds,
        allow_heuristic_continuation=allow_heuristic_continuation,
        idempotency_key=idempotency_key,
        rearm_of=rearm_of,
    )


@mcp.tool(
    name="wake_me_up_defer",
    description=(
        "Legacy only: best-effort pause and arm an explicitly selected active goal; "
        "its thread ID is unauthenticated. Require a ready watcher, never create or "
        "retry a missing goal, and end only after state=armed. Expiry wakes the goal; "
        "rearm_of records lineage."
    ),
)
async def wake_me_up_defer(
    thread_id: str,
    condition: dict[str, Any],
    expires_in_seconds: float,
    idempotency_key: str,
    allow_heuristic_continuation: bool = False,
    rearm_of: str | None = None,
) -> dict[str, Any]:
    return await service().defer(
        thread_id=thread_id,
        condition=condition,
        expires_in_seconds=expires_in_seconds,
        allow_heuristic_continuation=allow_heuristic_continuation,
        idempotency_key=idempotency_key,
        rearm_of=rearm_of,
    )


@mcp.tool(
    name="wake_me_up_status",
    description=(
        "Read one monitor after a wake. decision is compact and default; audit is "
        "the full forensic status."
    ),
)
def wake_me_up_status(
    monitor_id: str,
    view: Literal["decision", "audit"] = "decision",
    ctx: Context | None = None,
) -> dict[str, Any]:
    trusted_target_thread_id = _trusted_thread_id(ctx)
    if view == "decision":
        return service().decision_status(
            monitor_id, trusted_target_thread_id=trusted_target_thread_id
        )
    if view == "audit":
        return service().status(
            monitor_id, trusted_target_thread_id=trusted_target_thread_id
        )
    raise ValidationError("view must be decision or audit")


@mcp.tool(
    name="wake_me_up_current_monitors",
    description="List compact monitors whose frozen thread-delivery target is the trusted current task.",
)
def wake_me_up_current_monitors(ctx: Context) -> list[dict[str, Any]]:
    thread_id = _trusted_thread_id(ctx)
    if thread_id is None:
        raise ValidationError(
            "wake_me_up_current_monitors requires the trusted Codex caller thread identity"
        )
    return service().list_for_target(thread_id)


@mcp.tool(
    name="wake_me_up_cancel",
    description="Cancel a registering, armed, or claimed monitor before it sends its one activation request.",
)
def wake_me_up_cancel(monitor_id: str) -> dict[str, Any]:
    return service().cancel(monitor_id)


@mcp.tool(
    name="wake_me_up_publish_receipt",
    description="Atomically publish a success or failure receipt for a monitor that requested one.",
)
def wake_me_up_publish_receipt(monitor_id: str, token: str, status: str) -> dict[str, Any]:
    return service().publish_receipt(monitor_id=monitor_id, token=token, status=status)


@mcp.tool(
    name="wake_me_up_event_reserve",
    description=(
        "Reserve a command/worker terminal event from a private mode-0600 JSON file. "
        "Return a raw token only on creation and a non-secret monitor_condition to "
        "arm separately."
    ),
)
def wake_me_up_event_reserve(payload_path: str) -> dict[str, Any]:
    payload = load_private_json_payload(payload_path)
    descriptor = payload.pop("publisher_descriptor_path", None)
    return service().reserve_terminal_event(payload, descriptor_path=descriptor)


@mcp.tool(
    name="wake_me_up_event_status",
    description="Inspect one redacted terminal-event reservation and its delivery evidence.",
)
def wake_me_up_event_status(reservation_id: str) -> dict[str, Any]:
    return service().event_status(reservation_id)


@mcp.tool(
    name="wake_me_up_event_cancel",
    description="Cancel one still-unbound terminal-event reservation.",
)
def wake_me_up_event_cancel(reservation_id: str) -> dict[str, Any]:
    return service().cancel_terminal_event(reservation_id)


@mcp.tool(
    name="wake_me_up_event_heartbeat",
    description=(
        "Publish a bounded producer heartbeat from a private mode-0600 JSON file; "
        "the token is never an MCP argument."
    ),
)
def wake_me_up_event_heartbeat(payload_path: str) -> dict[str, Any]:
    payload = load_private_json_payload(payload_path)
    return service().publish_event_heartbeat(
        str(payload["reservation_id"]),
        publish_token=str(payload["publish_token"]),
        heartbeat=payload["heartbeat"],
    )


@mcp.tool(
    name="wake_me_up_event_publish",
    description=(
        "Publish an immutable command/worker terminal event from a private mode-0600 "
        "JSON file; it is evidence, never acceptance."
    ),
)
def wake_me_up_event_publish(payload_path: str) -> dict[str, Any]:
    payload = load_private_json_payload(payload_path)
    return service().publish_terminal_event(
        str(payload["reservation_id"]),
        publish_token=str(payload["publish_token"]),
        terminal_event=payload["terminal_event"],
    )


def main() -> None:
    mcp.run(transport="stdio")


if __name__ == "__main__":
    main()

"""Stdio MCP façade for monitor registration, inspection, and cancellation."""

from __future__ import annotations

from typing import Any

from mcp.server.fastmcp import Context, FastMCP

from .models import ValidationError
from .payloads import load_private_json_payload
from .service import MonitorService


mcp = FastMCP(
    "Codex Wake Me Up",
    instructions=(
        "Use wait_for_event as the primary one-shot, host-local monitor for an "
        "exact task regardless of goal state; do not create a goal. After an "
        "armed receipt, end the current turn. Choose defer_goal_until_event only "
        "for explicit legacy goal pause/reactivation. A queued wake is billed and "
        "is not a success claim; inspect durable status once after delivery."
    ),
)
_service: MonitorService | None = None


def service() -> MonitorService:
    global _service
    if _service is None:
        _service = MonitorService()
    return _service


@mcp.tool(
    name="wait_for_event",
    description=(
        "Primary path: durably monitor one typed condition for one exact local "
        "Codex task, regardless of goal state. A fired monitor queues one small "
        "self-identifying pointer; queue delivery is billed and not exactly-once. "
        "After an armed receipt, end the current turn."
    ),
)
async def wait_for_event(
    condition: dict[str, Any],
    expires_in_seconds: float,
    idempotency_key: str,
    ctx: Context,
    rearm_of: str | None = None,
) -> dict[str, Any]:
    try:
        request_context = ctx.request_context
    except ValueError:
        request_context = None
    meta = request_context.meta if request_context is not None else None
    thread_id = getattr(meta, "threadId", None)
    if not isinstance(thread_id, str) or not thread_id.strip():
        raise ValidationError(
            "wait_for_event requires the trusted Codex caller thread identity"
        )
    return await service().wait_for_current_event(
        thread_id=thread_id.strip(),
        condition=condition,
        expires_in_seconds=expires_in_seconds,
        idempotency_key=idempotency_key,
        rearm_of=rearm_of,
    )


@mcp.tool(
    name="defer_goal_until_event",
    description=(
        "Optional legacy GoalDelivery path for an explicitly selected active or "
        "paused goal. It never creates a goal and never falls back to thread delivery."
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
        "Arm one typed, durable local monitor for a loaded paused Codex goal. "
        "If no unfinished goal exists, do not call create_goal or retry this tool. "
        "Use allow_heuristic_continuation only when time/GPU/PID/tmux/log/thread "
        "evidence is intentionally sufficient to continue the goal. Pass rearm_of "
        "to record lineage from the terminal monitor this one succeeds."
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
        "Best-effort pause and arm one typed monitor for an explicitly supplied "
        "loaded active goal. The supplied thread ID is not authenticated as the "
        "caller's current task. The watcher must be positively ready before pause; "
        "if no unfinished goal exists, do not call create_goal or retry this tool. "
        "After an armed receipt, end the current turn immediately without sleeping "
        "or polling. This tool cannot mechanically end a running turn. Expiry itself "
        "wakes a deferred goal; pass rearm_of to record lineage from a fired monitor."
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
    description="Inspect a monitor's target guard, evidence, outcome, and daemon supervision state.",
)
def wake_me_up_status(monitor_id: str) -> dict[str, Any]:
    return service().status(monitor_id)


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
        "Reserve one command/worker terminal event from a private mode-0600 JSON "
        "payload. The raw publish token is returned only on first creation."
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
        "Publish one bounded producer heartbeat from a private mode-0600 JSON "
        "payload; token text is never an MCP argument."
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
        "Publish the immutable command/worker terminal envelope from a private "
        "mode-0600 JSON payload; this is handling evidence, never acceptance."
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

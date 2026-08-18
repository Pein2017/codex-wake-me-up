"""Stdio MCP façade for monitor registration, inspection, and cancellation."""

from __future__ import annotations

from typing import Any

from mcp.server.fastmcp import FastMCP

from .service import MonitorService


mcp = FastMCP(
    "Codex Wake Me Up",
    instructions=(
        "Register a one-shot, host-local condition for a loaded paused Codex goal. "
        "GPU/tmux/PID/time evidence is heuristic and needs explicit authorization; "
        "receipt success is the authoritative completion path."
    ),
)
_service: MonitorService | None = None


def service() -> MonitorService:
    global _service
    if _service is None:
        _service = MonitorService()
    return _service


@mcp.tool(
    name="wake_me_up",
    description=(
        "Arm one typed, durable local monitor for a loaded paused Codex goal. "
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
        "after an armed receipt, end the current turn immediately without sleeping "
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


def main() -> None:
    mcp.run(transport="stdio")


if __name__ == "__main__":
    main()

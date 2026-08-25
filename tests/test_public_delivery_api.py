from __future__ import annotations

import asyncio
from typing import Any, cast

import pytest
from mcp.server.fastmcp import Context
from mcp.shared.context import RequestContext
from mcp.types import RequestParams

from codex_wake_me_up import cli, mcp_server
from codex_wake_me_up.models import ValidationError


class FakeService:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict]] = []

    async def wait_for_event(self, **payload):
        self.calls.append(("wait_for_event", dict(payload)))
        return {"delivery_kind": "thread"}

    async def wait_for_current_event(self, **payload):
        self.calls.append(("wait_for_current_event", dict(payload)))
        return {"delivery_kind": "thread"}

    async def defer(self, **payload):
        self.calls.append(("defer", dict(payload)))
        return {"delivery_kind": "goal"}


def _mcp_context(thread_id: str | None) -> Context:
    meta_payload = {} if thread_id is None else {"threadId": thread_id}
    return Context(
        request_context=RequestContext(
            request_id="request-1",
            meta=RequestParams.Meta.model_validate(meta_payload),
            session=cast(Any, object()),
            lifespan_context=None,
        )
    )


def test_primary_and_legacy_named_mcp_operations_select_explicit_delivery_kinds(
    monkeypatch,
) -> None:
    """Catches an alias silently switching between thread and goal delivery."""

    fake = FakeService()
    monkeypatch.setattr(mcp_server, "_service", fake)
    thread_result = asyncio.run(
        mcp_server.wait_for_event(
            condition={"type": "time", "after_seconds": 10},
            expires_in_seconds=100,
            idempotency_key="thread-1",
            ctx=_mcp_context("thread-1"),
        )
    )
    goal_result = asyncio.run(
        mcp_server.defer_goal_until_event(
            thread_id="thread-1",
            condition={"type": "time", "after_seconds": 10},
            expires_in_seconds=100,
            idempotency_key="goal-1",
        )
    )

    assert thread_result == {"delivery_kind": "thread"}
    assert goal_result == {"delivery_kind": "goal"}
    assert fake.calls == [
        (
            "wait_for_current_event",
            {
                "thread_id": "thread-1",
                "condition": {"type": "time", "after_seconds": 10},
                "expires_in_seconds": 100,
                "idempotency_key": "thread-1",
                "rearm_of": None,
            },
        ),
        (
            "defer",
            {
                "thread_id": "thread-1",
                "condition": {"type": "time", "after_seconds": 10},
                "expires_in_seconds": 100,
                "idempotency_key": "goal-1",
                "allow_heuristic_continuation": False,
                "rearm_of": None,
            },
        ),
    ]


def test_wait_for_event_schema_hides_thread_identity_from_the_model() -> None:
    tools = asyncio.run(mcp_server.mcp.list_tools())
    wait_tool = next(tool for tool in tools if tool.name == "wait_for_event")

    assert "thread_id" not in wait_tool.inputSchema["properties"]
    assert "ctx" not in wait_tool.inputSchema["properties"]


def test_wait_for_event_rejects_missing_trusted_identity_before_service(
    monkeypatch,
) -> None:
    fake = FakeService()
    monkeypatch.setattr(mcp_server, "_service", fake)

    with pytest.raises(ValidationError, match="trusted Codex caller thread identity"):
        asyncio.run(
            mcp_server.wait_for_event(
                condition={"type": "time", "after_seconds": 10},
                expires_in_seconds=100,
                idempotency_key="thread-1",
                ctx=_mcp_context(None),
            )
        )

    assert fake.calls == []


def test_wait_for_event_rejects_an_absent_request_context_before_service(
    monkeypatch,
) -> None:
    fake = FakeService()
    monkeypatch.setattr(mcp_server, "_service", fake)

    with pytest.raises(ValidationError, match="trusted Codex caller thread identity"):
        asyncio.run(
            mcp_server.wait_for_event(
                condition={"type": "time", "after_seconds": 10},
                expires_in_seconds=100,
                idempotency_key="thread-1",
                ctx=Context(),
            )
        )

    assert fake.calls == []


def test_cli_exposes_thread_wait_goal_defer_status_and_cancel_controls() -> None:
    """Catches shipping source behavior without inspectable operator controls."""

    parser = cli.build_parser()
    assert parser.parse_args(["wait-for-event", "--payload", "/tmp/p"]).__dict__["command"] == (
        "wait-for-event"
    )
    assert parser.parse_args(
        ["defer-goal-until-event", "--payload", "/tmp/p"]
    ).__dict__["command"] == "defer-goal-until-event"
    assert parser.parse_args(["status", "--monitor-id", "m"]).__dict__["command"] == (
        "status"
    )
    assert parser.parse_args(["cancel", "--monitor-id", "m"]).__dict__["command"] == (
        "cancel"
    )
    assert parser.parse_args(
        ["delivery-compatibility-check", "--supported-delivery-epoch", "0"]
    ).__dict__["command"] == "delivery-compatibility-check"


def test_mcp_prompt_presents_goal_independent_wait_as_the_primary_path() -> None:
    """Catches the server prompt telling a goal-less caller to stop."""

    instructions = mcp_server.mcp.instructions
    assert isinstance(instructions, str)
    assert "regardless of goal state" in instructions
    assert "do not create a goal" in instructions
    assert "end the current turn" in instructions
    assert "loaded paused Codex goal" not in instructions

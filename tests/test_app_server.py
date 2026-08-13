import asyncio

import pytest

from codex_wake_me_up.app_server import (
    AppServerClient,
    activation_params,
    pause_params,
)
from codex_wake_me_up.models import AppServerError
from pathlib import Path


def test_activation_payload_omits_every_optional_goal_field() -> None:
    assert activation_params("thread") == {"threadId": "thread", "status": "active"}


def test_pause_payload_omits_every_optional_goal_field() -> None:
    assert pause_params("thread") == {"threadId": "thread", "status": "paused"}


def test_goal_set_observation_preserves_returned_thread_identity() -> None:
    returned = AppServerClient._goal_set_observation(
        "requested",
        {
            "goal": {
                "threadId": "returned",
                "createdAt": 1,
                "objective": "smoke",
                "tokenBudget": 32,
                "status": "paused",
            }
        },
    )

    assert returned.thread_id == "returned"


def test_goal_set_observation_rejects_missing_returned_thread_identity() -> None:
    with pytest.raises(AppServerError):
        AppServerClient._goal_set_observation(
            "requested",
            {
                "goal": {
                    "createdAt": 1,
                    "objective": "smoke",
                    "tokenBudget": 32,
                    "status": "paused",
                }
            },
        )


@pytest.mark.parametrize("mismatch_source", ["thread/read", "thread/goal/get"])
def test_read_observation_rejects_returned_thread_identity_mismatch(
    mismatch_source,
) -> None:
    class StubClient(AppServerClient):
        def __init__(self) -> None:
            self.responses = {
                "thread/read": {
                    "thread": {
                        "id": "other" if mismatch_source == "thread/read" else "requested",
                        "status": {"type": "idle"},
                    }
                },
                "thread/goal/get": {
                    "goal": {
                        "threadId": (
                            "other" if mismatch_source == "thread/goal/get" else "requested"
                        ),
                        "createdAt": 1,
                        "objective": "smoke",
                        "tokenBudget": 32,
                        "status": "paused",
                    }
                },
            }

        async def _request(self, method, _params):
            return self.responses[method]

    with pytest.raises(AppServerError):
        asyncio.run(StubClient().read_observation("requested"))


@pytest.mark.parametrize("missing_source", ["thread/read", "thread/goal/get"])
def test_read_observation_rejects_missing_thread_identity(missing_source) -> None:
    class StubClient(AppServerClient):
        def __init__(self) -> None:
            thread = {"id": "requested", "status": {"type": "idle"}}
            goal = {
                "threadId": "requested",
                "createdAt": 1,
                "objective": "smoke",
                "tokenBudget": 32,
                "status": "paused",
            }
            if missing_source == "thread/read":
                thread.pop("id")
            else:
                goal.pop("threadId")
            self.responses = {
                "thread/read": {"thread": thread},
                "thread/goal/get": {"goal": goal},
            }

        async def _request(self, method, _params):
            return self.responses[method]

    with pytest.raises(AppServerError):
        asyncio.run(StubClient().read_observation("requested"))


def test_read_observation_accepts_matching_positive_thread_identities() -> None:
    class StubClient(AppServerClient):
        async def _request(self, method, _params):
            if method == "thread/read":
                return {
                    "thread": {
                        "id": "requested",
                        "status": {"type": "idle"},
                    }
                }
            return {
                "goal": {
                    "threadId": "requested",
                    "createdAt": 1,
                    "objective": "smoke",
                    "tokenBudget": 32,
                    "status": "paused",
                }
            }

    result = asyncio.run(StubClient().read_observation("requested"))
    assert result.thread_id == "requested"
    assert result.goal_status == "paused"


def test_activation_code_never_uses_turn_start() -> None:
    source_root = Path(__file__).resolve().parents[1] / "src" / "codex_wake_me_up"
    activation_surface = (source_root / "app_server.py").read_text(encoding="utf-8")
    activation_surface += (source_root / "service.py").read_text(encoding="utf-8")
    assert "turn/start" not in activation_surface

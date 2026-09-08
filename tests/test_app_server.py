import asyncio

import aiohttp
import pytest

from codex_wake_me_up.app_server import (
    AppServerClient,
    activation_params,
    initialize_params,
    pause_params,
)
from codex_wake_me_up.models import (
    AppServerError,
    AppServerRejectedError,
    AppServerTransportError,
)
from pathlib import Path


def test_activation_payload_omits_every_optional_goal_field() -> None:
    assert activation_params("thread") == {"threadId": "thread", "status": "active"}


def test_pause_payload_omits_every_optional_goal_field() -> None:
    assert pause_params("thread") == {"threadId": "thread", "status": "paused"}


def test_thread_delivery_connection_explicitly_enables_experimental_api() -> None:
    """Catches silently connecting without the queue capability surface."""

    assert initialize_params(experimental_api=True)["capabilities"][
        "experimentalApi"
    ] is True
    assert initialize_params(experimental_api=False)["capabilities"][
        "experimentalApi"
    ] is False


def test_request_timeout_is_typed_as_transport_failure(tmp_path) -> None:
    """Keeps a read timeout distinct from a conclusive capability denial."""

    class TimedOutWebsocket:
        async def send_json(self, _payload) -> None:
            return None

        async def receive(self):
            await asyncio.Future()

    client = AppServerClient(tmp_path, timeout_seconds=0.001)
    client._websocket = TimedOutWebsocket()

    with pytest.raises(AppServerTransportError, match="thread/read timed out"):
        asyncio.run(client._request("thread/read", {"threadId": "thread-1"}))


def test_socket_stat_permission_failure_is_definitive(tmp_path) -> None:
    class DeniedSocket:
        def stat(self):
            raise PermissionError("denied")

    client = AppServerClient(tmp_path)
    client.socket_path = DeniedSocket()

    with pytest.raises(AppServerError) as caught:
        client._check_socket()

    assert type(caught.value) is AppServerError


def test_websocket_handshake_rejection_is_definitive(tmp_path, monkeypatch) -> None:
    async def reject_handshake(*_args, **_kwargs):
        raise aiohttp.WSServerHandshakeError(
            None, (), status=403, message="Forbidden", headers=None
        )

    monkeypatch.setattr(AppServerClient, "_check_socket", lambda _self: None)
    monkeypatch.setattr(aiohttp.ClientSession, "ws_connect", reject_handshake)

    async def connect() -> None:
        async with AppServerClient(tmp_path):
            raise AssertionError("handshake rejection must not enter the context")

    with pytest.raises(AppServerRejectedError, match="HTTP 403"):
        asyncio.run(connect())


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

        async def _request(self, method, params):
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

        async def _request(self, method, params):
            return self.responses[method]

    with pytest.raises(AppServerError):
        asyncio.run(StubClient().read_observation("requested"))


def test_read_observation_accepts_matching_positive_thread_identities() -> None:
    class StubClient(AppServerClient):
        async def _request(self, method, params):
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


def test_native_worker_observer_binds_then_reads_one_exact_invocation() -> None:
    class StubClient(AppServerClient):
        def __init__(self) -> None:
            self.calls = []

        async def _request(self, method, params):
            self.calls.append((method, params))
            return {
                "taskName": params["taskName"],
                "childThreadId": "child-1",
                "invocationId": "turn-1",
                "status": "running",
            }

    client = StubClient()
    bound = asyncio.run(
        client.observe_native_worker(
            root_thread_id="root-1",
            task_name="/root/worker",
        )
    )
    observed = asyncio.run(
        client.observe_native_worker(
            root_thread_id="root-1",
            task_name="/root/worker",
            child_thread_id="child-1",
            invocation_id="turn-1",
        )
    )

    assert bound == observed == {
        "task_name": "/root/worker",
        "child_thread_id": "child-1",
        "invocation_id": "turn-1",
        "status": "running",
        "error": None,
    }
    assert client.calls == [
        (
            "thread/agent/observe",
            {"rootThreadId": "root-1", "taskName": "/root/worker"},
        ),
        (
            "thread/agent/observe",
            {
                "rootThreadId": "root-1",
                "taskName": "/root/worker",
                "childThreadId": "child-1",
                "invocationId": "turn-1",
            },
        ),
    ]


def test_native_worker_observer_accepts_core_bounded_non_ascii_error() -> None:
    class StubClient(AppServerClient):
        async def _request(self, _method, params):
            return {
                "taskName": params["taskName"],
                "childThreadId": "child-1",
                "invocationId": "turn-1",
                "status": "failed",
                "error": "错" * 512,
            }

    result = asyncio.run(
        StubClient().observe_native_worker(
            root_thread_id="root-1",
            task_name="/root/worker",
        )
    )

    assert result["error"] == "错" * 512


def test_delivery_code_has_no_child_continuation_or_alternate_start_surface() -> None:
    source_root = Path(__file__).resolve().parents[1] / "src" / "codex_wake_me_up"
    activation_surface = (source_root / "app_server.py").read_text(encoding="utf-8")
    activation_surface += (source_root / "service.py").read_text(encoding="utf-8")
    for forbidden in (
        "followup_task",
        "thread/queue/start",
        "turn/start",
        "turn/steer",
    ):
        assert forbidden not in activation_surface


class ThreadResolutionStub(AppServerClient):
    def __init__(self, threads: dict[str, dict]) -> None:
        self.threads = threads
        self.reads: list[str] = []
        self.server_info = {
            "serverInfo": {"version": "0.148.0"},
            "userAgent": "codex/0.148.0",
        }

    async def _request(self, method, params):
        if method == "thread/read":
            thread_id = params["threadId"]
            self.reads.append(thread_id)
            thread = self.threads.get(thread_id)
            if thread is None:
                raise AppServerError(f"missing thread: {thread_id}")
            return {"thread": dict(thread)}
        if method == "thread/queue/list":
            return {"data": [], "nextCursor": None}
        raise AssertionError(method)


def _root(thread_id: str) -> dict:
    return {
        "id": thread_id,
        "status": {"type": "idle"},
        "canAcceptDirectInput": True,
        "source": "appServer",
        "parentThreadId": None,
        "ephemeral": False,
    }


def _subagent(thread_id: str, parent_id: str | None) -> dict:
    return {
        "id": thread_id,
        "status": {"type": "idle"},
        "canAcceptDirectInput": False,
        "source": {
            "subAgent": {
                "thread_spawn": {"parent_thread_id": parent_id, "depth": 1}
            }
        },
        "parentThreadId": parent_id,
        "ephemeral": False,
    }


def test_thread_delivery_resolution_keeps_a_direct_root_as_its_own_target() -> None:
    client = ThreadResolutionStub({"root": _root("root")})

    result = asyncio.run(client.resolve_thread_delivery("root"))

    assert result["origin_thread_id"] == "root"
    assert result["delivery_thread_id"] == "root"
    assert result["relay_chain"] == ["root"]
    assert result["relayed_to_root"] is False


def test_thread_delivery_resolution_routes_a_child_to_its_root() -> None:
    client = ThreadResolutionStub(
        {"child": _subagent("child", "root"), "root": _root("root")}
    )

    result = asyncio.run(client.resolve_thread_delivery("child"))

    assert result["origin_thread_id"] == "child"
    assert result["delivery_thread_id"] == "root"
    assert result["relay_chain"] == ["child", "root"]
    assert result["relayed_to_root"] is True


def test_thread_delivery_resolution_rejects_a_nonspawn_review_delegate() -> None:
    review = {
        **_root("review"),
        "source": {"subAgent": "review"},
        "parentThreadId": "root",
    }

    with pytest.raises(AppServerError, match="unsupported parentThreadId"):
        asyncio.run(
            ThreadResolutionStub(
                {"review": review, "root": _root("root")}
            ).resolve_thread_delivery("review")
        )


def test_thread_delivery_resolution_routes_a_depth_two_child_to_the_top_root() -> None:
    client = ThreadResolutionStub(
        {
            "grandchild": _subagent("grandchild", "child"),
            "child": _subagent("child", "root"),
            "root": _root("root"),
        }
    )

    result = asyncio.run(client.resolve_thread_delivery("grandchild"))

    assert result["origin_thread_id"] == "grandchild"
    assert result["delivery_thread_id"] == "root"
    assert result["relay_chain"] == ["grandchild", "child", "root"]
    assert result["relayed_to_root"] is True
    assert client.reads[:3] == ["grandchild", "child", "root"]


@pytest.mark.parametrize(
    ("threads", "error"),
    [
        ({"child": _subagent("child", None)}, "missing parentThreadId"),
        (
            {
                "child": _subagent("child", "parent"),
                "parent": _subagent("parent", "child"),
            },
            "cycle",
        ),
        (
            {
                "child": {
                    **_subagent("child", "root"),
                    "source": "appServer",
                },
                "root": _root("root"),
            },
            "unsupported parentThreadId",
        ),
    ],
)
def test_thread_delivery_resolution_fails_closed_on_invalid_ancestry(
    threads, error
) -> None:
    with pytest.raises(AppServerError, match=error):
        asyncio.run(ThreadResolutionStub(threads).resolve_thread_delivery("child"))


def test_thread_delivery_resolution_rejects_an_over_bounded_chain() -> None:
    threads = {
        f"child-{index}": _subagent(
            f"child-{index}",
            f"child-{index + 1}" if index < 64 else "root",
        )
        for index in range(65)
    }
    threads["root"] = _root("root")

    with pytest.raises(AppServerError, match="exceeds 64"):
        asyncio.run(
            ThreadResolutionStub(threads).resolve_thread_delivery("child-0")
        )


@pytest.mark.parametrize(
    ("mutation", "error"),
    [
        ({"ephemeral": True}, "ephemeral"),
        ({"canAcceptDirectInput": "yes"}, "invalid shape"),
    ],
)
def test_thread_delivery_resolution_rejects_an_invalid_intermediate_ancestor(
    mutation, error
) -> None:
    parent = {**_subagent("parent", "root"), **mutation}
    threads = {
        "child": _subagent("child", "parent"),
        "parent": parent,
        "root": _root("root"),
    }

    with pytest.raises(AppServerError, match=error):
        asyncio.run(ThreadResolutionStub(threads).resolve_thread_delivery("child"))


def test_thread_delivery_resolution_rejects_a_denied_root() -> None:
    root = {**_root("root"), "canAcceptDirectInput": False}

    with pytest.raises(AppServerError, match="does not accept direct input"):
        asyncio.run(
            ThreadResolutionStub(
                {"child": _subagent("child", "root"), "root": root}
            ).resolve_thread_delivery("child")
        )

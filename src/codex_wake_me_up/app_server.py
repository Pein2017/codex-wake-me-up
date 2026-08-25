"""Narrow Unix-socket client for the local Codex app-server control plane."""

from __future__ import annotations

import asyncio
import hashlib
import os
import stat
from pathlib import Path
from typing import Any, Mapping, cast

import aiohttp

from .models import AppServerError, AppServerRejectedError, GoalMarker, TargetObservation
from .runtime import control_socket, resolve_codex_home


CLIENT_INFO = {
    "name": "codex-wake-me-up",
    "title": "Codex Wake Me Up",
    "version": "0.1.0",
}


def initialize_params(*, experimental_api: bool) -> dict[str, Any]:
    """Build the capability declaration for one dedicated local connection."""

    return {
        "clientInfo": CLIENT_INFO,
        "capabilities": {
            "experimentalApi": experimental_api,
            "requestAttestation": False,
            "optOutNotificationMethods": [
                "thread/updated",
                "thread/started",
                "thread/goal/updated",
            ],
        },
    }


def activation_params(thread_id: str) -> dict[str, str]:
    """Return the exact safe app-server goal-set payload.

    Optional fields deliberately do not appear. In particular, serializing
    ``tokenBudget: null`` is semantically different from omitting that field
    and would clear a user-owned budget.
    """

    return {"threadId": thread_id, "status": "active"}


def pause_params(thread_id: str) -> dict[str, str]:
    """Return the exact status-only payload for the one defer pause."""

    return {"threadId": thread_id, "status": "paused"}


class AppServerClient:
    """A serial request-id client over the current host's Unix WebSocket."""

    def __init__(
        self,
        codex_home: str | Path | None = None,
        *,
        timeout_seconds: float = 10.0,
        experimental_api: bool = False,
    ):
        self.codex_home = resolve_codex_home(codex_home)
        self.socket_path = control_socket(self.codex_home)
        self.timeout_seconds = timeout_seconds
        self.experimental_api = experimental_api
        self._session: aiohttp.ClientSession | None = None
        self._websocket: aiohttp.ClientWebSocketResponse | None = None
        self._next_request_id = 1
        self.server_info: Mapping[str, Any] | None = None

    async def __aenter__(self) -> "AppServerClient":
        self._check_socket()
        connector = aiohttp.UnixConnector(path=str(self.socket_path))
        self._session = aiohttp.ClientSession(connector=connector)
        try:
            try:
                self._websocket = await self._session.ws_connect(
                    "http://localhost/",
                    max_msg_size=0,
                    timeout=cast(Any, aiohttp.ClientWSTimeout)(
                        ws_receive=self.timeout_seconds,
                        ws_close=self.timeout_seconds,
                    ),
                )
            except (aiohttp.ClientError, OSError) as exc:
                raise AppServerError("cannot connect to the local app-server socket") from exc
            initialized = await self._request(
                "initialize",
                initialize_params(experimental_api=self.experimental_api),
            )
            server_home = initialized.get("codexHome") if isinstance(initialized, Mapping) else None
            if not isinstance(server_home, str) or Path(server_home).resolve() != self.codex_home:
                raise AppServerError(
                    "app-server CODEX_HOME does not match the local runtime root"
                )
            self.server_info = initialized
            await self._send({"method": "initialized"})
            return self
        except BaseException:
            await self.__aexit__(None, None, None)
            raise

    async def __aexit__(self, _exc_type: Any, _exc: Any, _traceback: Any) -> None:
        if self._websocket is not None:
            await self._websocket.close()
            self._websocket = None
        if self._session is not None:
            await self._session.close()
            self._session = None

    def _check_socket(self) -> None:
        try:
            socket_stat = self.socket_path.stat()
        except OSError as exc:
            raise AppServerError(f"local app-server socket is unavailable: {self.socket_path}") from exc
        if not stat.S_ISSOCK(socket_stat.st_mode):
            raise AppServerError("app-server control path is not a Unix socket")
        if socket_stat.st_uid not in {os.getuid(), 0}:
            raise AppServerError("app-server control socket is not owned by this local user")
        if socket_stat.st_mode & 0o077:
            raise AppServerError("app-server control socket permissions are not private")
        if not os.access(self.socket_path, os.R_OK | os.W_OK):
            raise AppServerError("current user cannot access the app-server control socket")

    async def _send(self, payload: Mapping[str, Any]) -> None:
        if self._websocket is None:
            raise AppServerError("app-server client is not connected")
        try:
            await self._websocket.send_json(dict(payload))
        except aiohttp.ClientError as exc:
            raise AppServerError("failed to write to local app-server") from exc

    async def _request(self, method: str, params: Mapping[str, Any]) -> Mapping[str, Any]:
        request_id = self._next_request_id
        self._next_request_id += 1
        await self._send({"id": request_id, "method": method, "params": dict(params)})
        if self._websocket is None:
            raise AppServerError("app-server client is not connected")
        try:
            async with asyncio.timeout(self.timeout_seconds):
                while True:
                    message = await self._websocket.receive()
                    if message.type == aiohttp.WSMsgType.TEXT:
                        try:
                            payload = message.json()
                        except Exception as exc:  # aiohttp exposes backend JSON errors directly.
                            raise AppServerError("app-server returned non-JSON text") from exc
                        if not isinstance(payload, Mapping) or payload.get("id") != request_id:
                            # Notifications and responses to no request of ours must not
                            # influence this serial request. The declared opt-out keeps this
                            # queue small; matching IDs preserves correctness if one arrives.
                            continue
                        if "error" in payload:
                            raise AppServerRejectedError(
                                f"app-server {method} rejected request: {payload['error']}"
                            )
                        result = payload.get("result")
                        if not isinstance(result, Mapping):
                            raise AppServerError(f"app-server {method} returned no object result")
                        return result
                    if message.type in {
                        aiohttp.WSMsgType.CLOSED,
                        aiohttp.WSMsgType.CLOSING,
                        aiohttp.WSMsgType.ERROR,
                    }:
                        raise AppServerError("local app-server WebSocket closed")
        except TimeoutError as exc:
            raise AppServerError(f"app-server {method} timed out") from exc

    async def read_observation(self, thread_id: str) -> TargetObservation:
        thread_result = await self._request(
            "thread/read", {"threadId": thread_id, "includeTurns": False}
        )
        thread = thread_result.get("thread")
        if not isinstance(thread, Mapping):
            raise AppServerError("thread/read returned no thread")
        returned_thread_id = thread.get("id")
        if (
            not isinstance(returned_thread_id, str)
            or not returned_thread_id
            or returned_thread_id != thread_id
        ):
            raise AppServerError("thread/read returned a different thread identity")
        status = thread.get("status")
        if not isinstance(status, Mapping) or not isinstance(status.get("type"), str):
            raise AppServerError("thread/read returned no runtime status")
        runtime_status = str(status["type"])
        is_loaded = runtime_status != "notLoaded"
        goal_result = await self._request("thread/goal/get", {"threadId": thread_id})
        goal = goal_result.get("goal")
        if goal is None:
            return TargetObservation(
                thread_id=thread_id,
                runtime_status=runtime_status,
                is_loaded=is_loaded,
                goal_status=None,
                goal=None,
                goal_snapshot=None,
            )
        if not isinstance(goal, Mapping):
            raise AppServerError("thread/goal/get returned an invalid goal")
        returned_goal_thread_id = goal.get("threadId")
        if (
            not isinstance(returned_goal_thread_id, str)
            or not returned_goal_thread_id
            or returned_goal_thread_id != thread_id
        ):
            raise AppServerError("thread/goal/get returned a different thread identity")
        try:
            marker = GoalMarker.from_wire(goal)
        except (KeyError, TypeError, ValueError) as exc:
            raise AppServerError("thread/goal/get returned an invalid goal marker") from exc
        return TargetObservation(
            thread_id=thread_id,
            runtime_status=runtime_status,
            is_loaded=is_loaded,
            goal_status=str(goal.get("status")) if goal.get("status") is not None else None,
            goal=marker,
            goal_snapshot={
                "createdAt": goal.get("createdAt"),
                "objective": goal.get("objective"),
                "tokenBudget": goal.get("tokenBudget"),
                "updatedAt": goal.get("updatedAt"),
                "tokensUsed": goal.get("tokensUsed"),
                "timeUsedSeconds": goal.get("timeUsedSeconds"),
                "status": goal.get("status"),
            },
        )

    async def preflight_thread_delivery(self, thread_id: str) -> dict[str, Any]:
        """Validate the exact experimental queue/read shapes without mutating state."""

        thread_result = await self._request(
            "thread/read", {"threadId": thread_id, "includeTurns": False}
        )
        thread = self._exact_thread(thread_result, thread_id)
        status = thread.get("status")
        if not isinstance(status, Mapping) or not isinstance(status.get("type"), str):
            raise AppServerError("thread/read returned no runtime status")
        if thread.get("ephemeral") is True:
            raise AppServerError("ephemeral target cannot receive durable delivery")
        can_accept = thread.get("canAcceptDirectInput")
        if can_accept is not None and not isinstance(can_accept, bool):
            raise AppServerError("thread.canAcceptDirectInput has an invalid shape")
        if can_accept is False:
            raise AppServerError("target thread does not accept direct input")
        if can_accept is None and self._looks_like_spawned_subagent(thread.get("source")):
            raise AppServerError("unloaded spawned target does not accept direct input")
        queue = await self._request(
            "thread/queue/list", {"threadId": thread_id, "limit": 100}
        )
        items = self._queue_items(queue)
        if queue.get("nextCursor") is not None:
            raise AppServerError("thread queue exceeds the observed capacity")
        server = self.server_info or {}
        server_identity = server.get("serverInfo")
        if not isinstance(server_identity, Mapping):
            server_identity = {}
        codex_version = server_identity.get("version") or thread.get("cliVersion")
        user_agent = server.get("userAgent")
        if not isinstance(codex_version, str) or not codex_version:
            if isinstance(user_agent, str) and user_agent:
                codex_version = user_agent
            else:
                raise AppServerError("app-server exposed no Codex version identity")
        return {
            "thread_id": thread_id,
            "runtime_status": str(status["type"]),
            "loaded": str(status["type"]) != "notLoaded",
            "can_accept_direct_input": can_accept,
            "source": thread.get("source"),
            "queue_count": len(items),
            "queue_capacity": 100,
            "codex_version": codex_version,
            "server": {
                **dict(server_identity),
                "user_agent": user_agent,
            },
        }

    async def resolve_thread_delivery(self, origin_thread_id: str) -> dict[str, Any]:
        """Resolve one caller to itself or to its exact topmost V2 root."""

        if not origin_thread_id:
            raise AppServerError("origin thread identity must be non-empty")
        chain: list[str] = []
        current_id = origin_thread_id
        while True:
            if current_id in chain:
                raise AppServerError("spawned-subagent ancestry contains a cycle")
            if len(chain) >= 64:
                raise AppServerError("spawned-subagent ancestry exceeds 64 threads")
            chain.append(current_id)
            result = await self._request(
                "thread/read", {"threadId": current_id, "includeTurns": False}
            )
            thread = self._exact_thread(result, current_id)
            status = thread.get("status")
            if not isinstance(status, Mapping) or not isinstance(
                status.get("type"), str
            ):
                raise AppServerError("thread/read returned no runtime status")
            if thread.get("ephemeral") is True:
                raise AppServerError("ephemeral thread cannot anchor durable delivery")
            can_accept = thread.get("canAcceptDirectInput")
            if can_accept is not None and not isinstance(can_accept, bool):
                raise AppServerError("thread.canAcceptDirectInput has an invalid shape")

            spawned = self._looks_like_spawned_subagent(thread.get("source"))
            parent_id = thread.get("parentThreadId")
            if not spawned:
                if parent_id is not None:
                    raise AppServerError(
                        "non-subagent thread exposed an unsupported parentThreadId"
                    )
                capability = await self.preflight_thread_delivery(current_id)
                return {
                    **capability,
                    "origin_thread_id": origin_thread_id,
                    "delivery_thread_id": current_id,
                    "relay_chain": chain,
                    "relayed_to_root": current_id != origin_thread_id,
                }

            if not isinstance(parent_id, str) or not parent_id:
                raise AppServerError(
                    "spawned-subagent ancestry has a missing parentThreadId"
                )
            current_id = parent_id

    async def add_thread_delivery(
        self, *, thread_id: str, delivery_id: str, pointer: str
    ) -> dict[str, Any]:
        result = await self._request(
            "thread/queue/add",
            {
                "threadId": thread_id,
                "input": [{"type": "text", "text": pointer}],
                "clientUserMessageId": delivery_id,
            },
        )
        item = result.get("queuedSubmission")
        if not isinstance(item, Mapping):
            raise AppServerError("thread/queue/add returned no queued submission")
        if item.get("clientUserMessageId") != delivery_id:
            raise AppServerError("thread/queue/add returned a different delivery identity")
        item_id = item.get("id")
        if not isinstance(item_id, str) or not item_id:
            raise AppServerError("thread/queue/add returned no queue item identity")
        returned_pointer = self._queued_pointer(item)
        if returned_pointer != pointer:
            raise AppServerError("thread/queue/add returned modified pointer content")
        return {
            "item_id": item_id,
            "client_user_message_id": delivery_id,
            "pointer": returned_pointer,
        }

    async def list_thread_queue(self, thread_id: str) -> list[dict[str, Any]]:
        """Read the complete bounded queue and preserve user-owned order."""

        result = await self._request(
            "thread/queue/list", {"threadId": thread_id, "limit": 100}
        )
        items = self._queue_items(result)
        if result.get("nextCursor") is not None:
            raise AppServerError("thread queue exceeds the observed capacity")
        snapshot: list[dict[str, Any]] = []
        for position, item in enumerate(items):
            item_id = item.get("id")
            client_id = item.get("clientUserMessageId")
            if not isinstance(item_id, str) or not item_id:
                raise AppServerError("queued submission has no item identity")
            if not isinstance(client_id, str) or not client_id:
                raise AppServerError("queued submission has no client message identity")
            pointer = self._queued_pointer(item)
            snapshot.append(
                {
                    "item_id": item_id,
                    "client_user_message_id": client_id,
                    "pointer": pointer,
                    "pointer_digest": hashlib.sha256(
                        pointer.encode("utf-8")
                    ).hexdigest(),
                    "position": position,
                }
            )
        return snapshot

    async def inspect_thread_delivery(
        self,
        *,
        thread_id: str,
        delivery_id: str,
        expected_pointer_digest: str,
    ) -> dict[str, Any]:
        """Reconcile exact history first, then the shared queue."""

        thread_result = await self._request(
            "thread/read", {"threadId": thread_id, "includeTurns": True}
        )
        thread = self._exact_thread(thread_result, thread_id)
        status = thread.get("status")
        if not isinstance(status, Mapping) or not isinstance(status.get("type"), str):
            raise AppServerError("thread/read returned no runtime status")
        turns = thread.get("turns")
        if not isinstance(turns, list) or any(not isinstance(turn, Mapping) for turn in turns):
            raise AppServerError("thread/read returned an invalid turn list")
        history: list[dict[str, Any]] = []
        interrupted = False
        for turn in turns:
            interrupted = interrupted or turn.get("status") == "interrupted"
            items = turn.get("items")
            if not isinstance(items, list):
                raise AppServerError("thread/read returned an invalid turn item list")
            for item in items:
                if not isinstance(item, Mapping) or item.get("type") != "userMessage":
                    continue
                if item.get("clientId") != delivery_id:
                    continue
                pointer = self._history_pointer(item)
                history.append(
                    {
                        "turn_id": turn.get("id"),
                        "message_id": item.get("id"),
                        "pointer_digest": hashlib.sha256(
                            pointer.encode("utf-8")
                        ).hexdigest(),
                    }
                )
        queue = await self.list_thread_queue(thread_id)
        matching_queue = [
            item for item in queue if item["client_user_message_id"] == delivery_id
        ]
        matching_positions = [int(item["position"]) for item in matching_queue]
        first_matching_position = min(matching_positions, default=len(queue))
        prior_items = queue[:first_matching_position]
        history_matches = sum(
            item["pointer_digest"] == expected_pointer_digest for item in history
        )
        history_modified = len(history) - history_matches
        queue_matches = sum(
            item["pointer_digest"] == expected_pointer_digest for item in matching_queue
        )
        queue_modified = len(matching_queue) - queue_matches
        if history_modified or queue_modified:
            classification = "delivery_modified"
        elif history_matches:
            classification = "recorded"
        elif queue_matches:
            classification = "queued"
        else:
            classification = "absent"
        return {
            "classification": classification,
            "runtime_status": str(status["type"]),
            "interrupted": interrupted,
            "history_matches": history_matches,
            "history_modified": history_modified,
            "queue_matches": queue_matches,
            "queue_modified": queue_modified,
            "observed_pointer_count": len(history) + len(matching_queue),
            "history": history,
            "queue": matching_queue,
            "queue_count": len(queue),
            "prior_item_count": len(prior_items),
            "prior_item_ids": [str(item["item_id"]) for item in prior_items],
        }

    async def resume_thread_delivery(self, thread_id: str) -> dict[str, Any]:
        result = await self._request("thread/resume", {"threadId": thread_id})
        thread = self._exact_thread(result, thread_id)
        status = thread.get("status")
        if not isinstance(status, Mapping) or not isinstance(status.get("type"), str):
            raise AppServerError("thread/resume returned no runtime status")
        return {
            "thread_id": thread_id,
            "runtime_status": str(status["type"]),
            "can_accept_direct_input": thread.get("canAcceptDirectInput"),
        }

    async def delete_thread_delivery(self, thread_id: str, item_id: str) -> bool:
        result = await self._request(
            "thread/queue/delete",
            {"threadId": thread_id, "queuedSubmissionId": item_id},
        )
        deleted = result.get("deleted")
        if not isinstance(deleted, bool):
            raise AppServerError("thread/queue/delete returned no conclusive result")
        return deleted

    @staticmethod
    def _exact_thread(result: Mapping[str, Any], thread_id: str) -> Mapping[str, Any]:
        thread = result.get("thread")
        if not isinstance(thread, Mapping):
            raise AppServerError("thread/read returned no thread")
        if thread.get("id") != thread_id:
            raise AppServerError("thread/read returned a different thread identity")
        return thread

    @staticmethod
    def _queue_items(result: Mapping[str, Any]) -> list[Mapping[str, Any]]:
        data = result.get("data")
        if not isinstance(data, list) or any(not isinstance(item, Mapping) for item in data):
            raise AppServerError("thread/queue/list returned an invalid item list")
        return list(data)

    @staticmethod
    def _queued_pointer(item: Mapping[str, Any]) -> str:
        inputs = item.get("input")
        if not isinstance(inputs, list) or len(inputs) != 1:
            raise AppServerError("queued submission has an invalid pointer input")
        value = inputs[0]
        if not isinstance(value, Mapping) or value.get("type") != "text":
            raise AppServerError("queued submission has a non-text pointer")
        text = value.get("text")
        if not isinstance(text, str):
            raise AppServerError("queued submission pointer has no text")
        return text

    @staticmethod
    def _history_pointer(item: Mapping[str, Any]) -> str:
        content = item.get("content")
        if not isinstance(content, list) or len(content) != 1:
            raise AppServerError("recorded delivery has invalid pointer content")
        value = content[0]
        if not isinstance(value, Mapping) or value.get("type") != "text":
            raise AppServerError("recorded delivery has non-text pointer content")
        text = value.get("text")
        if not isinstance(text, str):
            raise AppServerError("recorded delivery pointer has no text")
        return text

    @staticmethod
    def _looks_like_spawned_subagent(source: Any) -> bool:
        if not isinstance(source, Mapping):
            return False
        subagent = source.get("subAgent")
        return isinstance(subagent, Mapping) and "thread_spawn" in subagent

    async def activate_guarded_goal(self, thread_id: str) -> TargetObservation:
        result = await self._request("thread/goal/set", activation_params(thread_id))
        return self._goal_set_observation(thread_id, result)

    async def pause_guarded_goal(self, thread_id: str) -> TargetObservation:
        result = await self._request("thread/goal/set", pause_params(thread_id))
        return self._goal_set_observation(thread_id, result)

    @staticmethod
    def _goal_set_observation(
        thread_id: str, result: Mapping[str, Any]
    ) -> TargetObservation:
        goal = result.get("goal")
        if not isinstance(goal, Mapping):
            raise AppServerError("thread/goal/set returned no goal")
        returned_thread_id = goal.get("threadId")
        if not isinstance(returned_thread_id, str) or not returned_thread_id:
            raise AppServerError("thread/goal/set returned no valid thread identity")
        try:
            marker = GoalMarker.from_wire(goal)
        except (KeyError, TypeError, ValueError) as exc:
            raise AppServerError("thread/goal/set returned an invalid goal marker") from exc
        return TargetObservation(
            thread_id=returned_thread_id,
            runtime_status="unknown_after_goal_set",
            is_loaded=True,
            goal_status=str(goal.get("status")) if goal.get("status") is not None else None,
            goal=marker,
            goal_snapshot={
                "createdAt": goal.get("createdAt"),
                "objective": goal.get("objective"),
                "tokenBudget": goal.get("tokenBudget"),
                "updatedAt": goal.get("updatedAt"),
                "tokensUsed": goal.get("tokensUsed"),
                "timeUsedSeconds": goal.get("timeUsedSeconds"),
                "status": goal.get("status"),
            },
        )

    async def doctor(self, thread_id: str | None = None) -> dict[str, Any]:
        """Return protocol/liveness facts without mutating any target."""

        result: dict[str, Any] = {
            "socket": str(self.socket_path),
            "codex_home": str(self.codex_home),
            "server": dict(self.server_info or {}),
            "goals_checked": False,
        }
        if thread_id is not None:
            observation = await self.read_observation(thread_id)
            result["goals_checked"] = True
            result["observation"] = {
                "thread_id": observation.thread_id,
                "runtime_status": observation.runtime_status,
                "is_loaded": observation.is_loaded,
                "goal_status": observation.goal_status,
            }
        return result

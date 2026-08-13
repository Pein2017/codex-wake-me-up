"""Narrow Unix-socket client for the local Codex app-server control plane."""

from __future__ import annotations

import asyncio
import os
import stat
from pathlib import Path
from typing import Any, Mapping

import aiohttp

from .models import AppServerError, GoalMarker, TargetObservation
from .runtime import control_socket, resolve_codex_home


CLIENT_INFO = {
    "name": "codex-wake-me-up",
    "title": "Codex Wake Me Up",
    "version": "0.1.0",
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

    def __init__(self, codex_home: str | Path | None = None, *, timeout_seconds: float = 10.0):
        self.codex_home = resolve_codex_home(codex_home)
        self.socket_path = control_socket(self.codex_home)
        self.timeout_seconds = timeout_seconds
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
                    "http://localhost/", max_msg_size=0, timeout=self.timeout_seconds
                )
            except (aiohttp.ClientError, OSError) as exc:
                raise AppServerError("cannot connect to the local app-server socket") from exc
            initialized = await self._request(
                "initialize",
                {
                    "clientInfo": CLIENT_INFO,
                    "capabilities": {
                        "experimentalApi": False,
                        "requestAttestation": False,
                        "optOutNotificationMethods": [
                            "thread/updated",
                            "thread/started",
                            "thread/goal/updated",
                        ],
                    },
                },
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
                            raise AppServerError(f"app-server {method} error: {payload['error']}")
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

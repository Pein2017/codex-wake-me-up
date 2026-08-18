"""Fakes shared by focused wake-up monitor tests."""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass
from typing import Any, Callable, Iterable, Mapping

from codex_wake_me_up.models import GoalMarker, TargetObservation


@dataclass
class Clock:
    value: float = 100.0

    def now(self) -> float:
        return self.value


def goal(*, created_at: int = 1, objective: str = "smoke", token_budget: int | None = 32) -> GoalMarker:
    return GoalMarker(created_at=created_at, objective=objective, token_budget=token_budget)


def observation(
    *,
    thread_id: str = "test-thread",
    runtime_status: str = "active",
    is_loaded: bool = True,
    goal_status: str | None = "paused",
    marker: GoalMarker | None = None,
) -> TargetObservation:
    selected = marker if marker is not None else goal()
    return TargetObservation(
        thread_id=thread_id,
        runtime_status=runtime_status,
        is_loaded=is_loaded,
        goal_status=goal_status,
        goal=selected if goal_status is not None else None,
        goal_snapshot={
            "createdAt": selected.created_at,
            "objective": selected.objective,
            "tokenBudget": selected.token_budget,
            "updatedAt": 100,
            "tokensUsed": 0,
            "timeUsedSeconds": 0,
            "status": goal_status,
        }
        if goal_status is not None
        else None,
    )


class FakeAppServer:
    """A serial app-server fake which never opens a real socket."""

    def __init__(
        self,
        observations: Iterable[TargetObservation],
        *,
        activation: TargetObservation | Exception | None = None,
        pause: TargetObservation | Exception | None = None,
        on_pause: Callable[[], None] | None = None,
        thread_observations: Mapping[str, Any] | None = None,
    ):
        self.observations = deque(observations)
        self.last_observation: TargetObservation | None = None
        self.activation = activation
        self.pause = pause
        self.on_pause = on_pause
        self.activation_calls: list[str] = []
        self.pause_calls: list[str] = []
        # Reads for an explicitly named thread (a `thread_idle` child) are
        # served from here instead of the monitor target's own queue. A value
        # may be one observation, a list consumed in order, or an exception.
        self.thread_observations = dict(thread_observations or {})
        self.child_reads: list[str] = []

    async def __aenter__(self) -> "FakeAppServer":
        return self

    async def __aexit__(self, *_args: Any) -> None:
        return None

    async def read_observation(self, thread_id: str) -> TargetObservation:
        if thread_id in self.thread_observations:
            self.child_reads.append(thread_id)
            scripted = self.thread_observations[thread_id]
            if isinstance(scripted, list):
                if len(scripted) > 1:
                    scripted = scripted.pop(0)
                else:
                    scripted = scripted[0]
            if isinstance(scripted, Exception):
                raise scripted
            return scripted
        if self.observations:
            self.last_observation = self.observations.popleft()
        assert self.last_observation is not None
        return self.last_observation

    async def activate_guarded_goal(self, thread_id: str) -> TargetObservation:
        self.activation_calls.append(thread_id)
        if isinstance(self.activation, Exception):
            raise self.activation
        if self.activation is not None:
            return self.activation
        assert self.last_observation is not None
        return observation(
            thread_id=thread_id,
            runtime_status="idle",
            goal_status="active",
            marker=self.last_observation.goal,
        )

    async def pause_guarded_goal(self, thread_id: str) -> TargetObservation:
        self.pause_calls.append(thread_id)
        if self.on_pause is not None:
            self.on_pause()
        if isinstance(self.pause, Exception):
            raise self.pause
        if self.pause is not None:
            return self.pause
        assert self.last_observation is not None
        return observation(
            thread_id=thread_id,
            runtime_status=self.last_observation.runtime_status,
            goal_status="paused",
            marker=self.last_observation.goal,
        )

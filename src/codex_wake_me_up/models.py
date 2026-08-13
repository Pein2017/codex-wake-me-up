"""Stable value objects and monitor-state vocabulary."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from enum import StrEnum
from typing import Any, Mapping


class TriState(StrEnum):
    """A condition result whose unknown value can never satisfy a monitor."""

    TRUE = "true"
    FALSE = "false"
    UNKNOWN = "unknown"


class MonitorState(StrEnum):
    REGISTERING = "registering"
    DEFER_INTENT = "defer_intent"
    PAUSING = "pausing"
    ARMED = "armed"
    CLAIMED = "claimed"
    ACTIVATING = "activating"
    FIRED = "fired"
    CANCELLED = "cancelled"
    EXPIRED = "expired"
    SUPERSEDED = "superseded"
    UNLOADED_TARGET = "unloaded_target"
    OBSERVER_FAILED = "observer_failed"
    SATISFIED_REQUIRES_AUTHORIZATION = "satisfied_requires_authorization"
    ACTIVATION_UNCERTAIN = "activation_uncertain"
    ACTIVATION_FAILED = "activation_failed"
    MIS_TARGETED_ACTIVATION = "mis_targeted_activation"
    DAEMON_UNAVAILABLE = "daemon_unavailable"
    DEFER_ABANDONED = "defer_abandoned"
    PAUSE_UNCERTAIN = "pause_uncertain"
    PAUSE_REJECTED = "pause_rejected"
    MIS_TARGETED_PAUSE = "mis_targeted_pause"


class MonitorMode(StrEnum):
    LEGACY = "legacy"
    DEFERRED = "deferred"


TERMINAL_STATES = frozenset(
    {
        MonitorState.FIRED,
        MonitorState.CANCELLED,
        MonitorState.EXPIRED,
        MonitorState.SUPERSEDED,
        MonitorState.UNLOADED_TARGET,
        MonitorState.OBSERVER_FAILED,
        MonitorState.SATISFIED_REQUIRES_AUTHORIZATION,
        MonitorState.ACTIVATION_UNCERTAIN,
        MonitorState.ACTIVATION_FAILED,
        MonitorState.MIS_TARGETED_ACTIVATION,
        MonitorState.DAEMON_UNAVAILABLE,
        MonitorState.DEFER_ABANDONED,
        MonitorState.PAUSE_UNCERTAIN,
        MonitorState.PAUSE_REJECTED,
        MonitorState.MIS_TARGETED_PAUSE,
    }
)


class WakeMeUpError(RuntimeError):
    """Base error that is safe to surface through the MCP and CLI layers."""


class ValidationError(WakeMeUpError):
    """A typed monitor request or captured identity is invalid."""


class ConflictError(WakeMeUpError):
    """An idempotency key is reused with different semantics."""


class AppServerError(WakeMeUpError):
    """The local Codex app-server could not provide a conclusive response."""


@dataclass(frozen=True)
class GoalMarker:
    """The strongest paused-goal identity currently exposed by app-server."""

    created_at: int
    objective: str
    token_budget: int | None

    @classmethod
    def from_wire(cls, goal: Mapping[str, Any]) -> "GoalMarker":
        return cls(
            created_at=int(goal["createdAt"]),
            objective=str(goal["objective"]),
            token_budget=(
                int(goal["tokenBudget"])
                if goal.get("tokenBudget") is not None
                else None
            ),
        )

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class TargetGuard:
    """A local loaded target and the paused goal captured for one monitor."""

    thread_id: str
    goal: GoalMarker

    def as_dict(self) -> dict[str, Any]:
        return {"thread_id": self.thread_id, "goal": self.goal.as_dict()}

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "TargetGuard":
        goal = value["goal"]
        if not isinstance(goal, Mapping):
            raise ValidationError("stored target guard has no goal marker")
        return cls(
            thread_id=str(value["thread_id"]),
            goal=GoalMarker(
                created_at=int(goal["created_at"]),
                objective=str(goal["objective"]),
                token_budget=(
                    int(goal["token_budget"])
                    if goal.get("token_budget") is not None
                    else None
                ),
            ),
        )


@dataclass(frozen=True)
class TargetObservation:
    """The minimally required live state returned by the local app-server."""

    thread_id: str
    runtime_status: str
    is_loaded: bool
    goal_status: str | None
    goal: GoalMarker | None
    goal_snapshot: Mapping[str, Any] | None = None

    def matches_guard(self, guard: TargetGuard) -> bool:
        return (
            self.thread_id == guard.thread_id
            and self.is_loaded
            and self.goal_status == "paused"
            and self.goal == guard.goal
        )

    def activation_eligible(self, guard: TargetGuard) -> bool:
        return self.runtime_status == "idle" and self.matches_guard(guard)


@dataclass(frozen=True)
class Evaluation:
    """A condition result, evidence, and true-leaf witness for authorization."""

    value: TriState
    evidence: Mapping[str, Any]
    witness: tuple[Mapping[str, Any], ...] = ()
    fatal: bool = False


def is_terminal(state: str | MonitorState) -> bool:
    return MonitorState(state) in TERMINAL_STATES

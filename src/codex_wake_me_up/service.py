"""Monitor registration, reconciliation, and guarded one-shot activation."""

from __future__ import annotations

import copy
import math
import secrets
import time
import uuid
from dataclasses import asdict
from pathlib import Path
from typing import Any, AsyncContextManager, Callable, Mapping, MutableMapping, Sequence

from .app_server import AppServerClient
from .delivery import DeliveryKind, ThreadDeliveryState, build_thread_delivery
from .conditions import (
    ObserverContext,
    contains_event_condition,
    elide_condition_journals,
    event_condition_binding,
    evaluate_condition,
    journal_tail,
    observe_external_condition_leaves,
    prepare_condition,
    public_condition_semantics,
    receipt_payload,
    thread_idle_targets,
    witness_authorizes_continuation,
)
from .git_attestation import GitDeliveryScope, attest_candidate, capture_worktree_scope
from .ledger import Ledger, MonitorRecord
from .models import (
    AppServerError,
    AppServerRejectedError,
    ConflictError,
    MonitorMode,
    MonitorState,
    TargetGuard,
    TargetObservation,
    TriState,
    ValidationError,
    WakeReason,
    is_terminal,
)
from .runtime import (
    atomic_write_json,
    codex_home_for_runtime_root,
    daemon_is_healthy,
    DeferProtocolLock,
    ensure_daemon,
    ensure_daemon_ready,
    ensure_event_daemon_ready,
    ensure_delivery_daemon_ready,
    runtime_root,
    validate_private_output_path,
)
from .terminal_events import (
    EventKind,
    WorkerTerminalEvent,
    WorkerOutcome,
    normalize_heartbeat,
    normalize_reservation,
    normalize_terminal_event,
)


AppServerFactory = Callable[[], AsyncContextManager[AppServerClient]]
ObserverContextFactory = Callable[[], ObserverContext]

# The repo's long-poll yield floor: what one avoided polling turn would cost.
POLL_YIELD_FLOOR_SECONDS = 180.0
WAKE_JOURNAL_TAIL_LINES = 20
MAX_REARM_CHAIN = 20
_TRUSTED_MCP_CALLER_BINDING = object()


class MonitorService:
    """The single integration owner of durable monitor semantics."""

    def __init__(
        self,
        root: Path | None = None,
        *,
        ledger: Ledger | None = None,
        app_server_factory: AppServerFactory | None = None,
        thread_delivery_factory: AppServerFactory | None = None,
        observer_context_factory: ObserverContextFactory | None = None,
        daemon_starter: Callable[[Path], bool] = ensure_daemon,
        daemon_readiness: Callable[[Path], bool] = ensure_daemon_ready,
        event_daemon_readiness: Callable[[Path], bool] = ensure_event_daemon_ready,
        thread_delivery_readiness: Callable[[Path], bool] = ensure_delivery_daemon_ready,
    ):
        self.root = root or runtime_root()
        self.ledger = ledger or Ledger(self.root)
        self.app_server_factory = app_server_factory or (
            lambda: AppServerClient(codex_home_for_runtime_root(self.root))
        )
        self.thread_delivery_factory = thread_delivery_factory or (
            lambda: AppServerClient(
                codex_home_for_runtime_root(self.root), experimental_api=True
            )
        )
        self.observer_context_factory = observer_context_factory or (
            lambda: ObserverContext(runtime_root=self.root)
        )
        self.daemon_starter = daemon_starter
        self.daemon_readiness = daemon_readiness
        self.event_daemon_readiness = event_daemon_readiness
        self.thread_delivery_readiness = thread_delivery_readiness

    async def wait_for_current_event(
        self,
        *,
        thread_id: str,
        condition: Mapping[str, Any],
        expires_in_seconds: float,
        idempotency_key: str,
        rearm_of: str | None = None,
        start_daemon: bool = True,
    ) -> dict[str, Any]:
        """Arm a monitor whose origin was bound by trusted MCP metadata."""

        return await self.wait_for_event(
            thread_id=thread_id,
            condition=condition,
            expires_in_seconds=expires_in_seconds,
            idempotency_key=idempotency_key,
            rearm_of=rearm_of,
            start_daemon=start_daemon,
            _caller_binding=_TRUSTED_MCP_CALLER_BINDING,
        )

    async def wait_for_event(
        self,
        *,
        thread_id: str,
        condition: Mapping[str, Any],
        expires_in_seconds: float,
        idempotency_key: str,
        rearm_of: str | None = None,
        start_daemon: bool = True,
        _caller_binding: object | None = None,
    ) -> dict[str, Any]:
        """Arm the primary goal-independent delivery path for one exact thread."""

        if not thread_id:
            raise ValidationError("thread_id must be non-empty")
        if _caller_binding not in (None, _TRUSTED_MCP_CALLER_BINDING):
            raise ValidationError("thread delivery caller binding is invalid")
        trusted_caller = _caller_binding is _TRUSTED_MCP_CALLER_BINDING
        if not idempotency_key:
            raise ValidationError("idempotency_key is required for thread delivery")
        if (
            isinstance(expires_in_seconds, bool)
            or not isinstance(expires_in_seconds, (int, float))
            or not math.isfinite(float(expires_in_seconds))
            or expires_in_seconds <= 0
        ):
            raise ValidationError(
                "expires_in_seconds must be finite and greater than zero"
            )
        self._validate_rearm_of(rearm_of)
        semantic: dict[str, Any] = {
            "thread_id": thread_id,
            "condition": public_condition_semantics(condition),
            "expires_in_seconds": float(expires_in_seconds),
        }
        if rearm_of is not None:
            semantic["rearm_of"] = rearm_of
        existing = self.ledger.get_by_idempotency_key(idempotency_key)
        if existing is not None:
            replay_semantic = {**semantic, "delivery": existing.delivery}
            if (
                existing.delivery_kind != DeliveryKind.THREAD
                or existing.semantic != replay_semantic
            ):
                raise ValidationError(
                    "idempotency key already belongs to a monitor with different semantics"
                )
            return self._registration_response(existing)
        if not self.thread_delivery_readiness(self.root):
            raise ValidationError(
                "thread delivery requires an exact delivery-capable daemon before arming"
            )
        event_binding = event_condition_binding(condition)
        if event_binding is not None and not self.event_daemon_readiness(self.root):
            raise ValidationError(
                "event monitor requires an exact event-capable daemon before arming"
            )
        async with self.thread_delivery_factory() as app_server:
            capability = await app_server.resolve_thread_delivery(thread_id)
        if capability.get("origin_thread_id") != thread_id:
            raise ValidationError("thread delivery resolved a different origin")
        delivery_thread_id = capability.get("delivery_thread_id")
        relay_chain = capability.get("relay_chain")
        if (
            not isinstance(delivery_thread_id, str)
            or not delivery_thread_id
            or not isinstance(relay_chain, list)
            or not relay_chain
            or not all(isinstance(item, str) and item for item in relay_chain)
        ):
            raise ValidationError("thread delivery resolution is malformed")
        monitor_id = str(uuid.uuid4())
        delivery = build_thread_delivery(
            monitor_id=monitor_id,
            thread_id=delivery_thread_id,
            capability=capability,
            origin_thread_id=thread_id,
            relay_chain=relay_chain,
            origin_binding=(
                "trusted_mcp_caller" if trusted_caller else "explicit_local_target"
            ),
        )
        semantic["delivery"] = delivery
        observer_context = self.observer_context_factory()
        children = self._reject_self_wait(condition, delivery_thread_id)
        async with self.app_server_factory() as app_server:
            observer_context.thread_observations.update(
                await self._read_thread_observations(app_server, children)
            )
        receipt_token = secrets.token_urlsafe(32)
        prepared_condition = prepare_condition(
            condition,
            observer_context,
            monitor_id=monitor_id,
            receipt_token=receipt_token,
        )
        prepared_event_binding = event_condition_binding(prepared_condition)
        record, _created = self.ledger.create_or_get(
            monitor_id=monitor_id,
            idempotency_key=idempotency_key,
            semantic=semantic,
            target=None,
            delivery=delivery,
            condition=prepared_condition,
            allow_heuristic_continuation=False,
            expires_at=observer_context.now() + float(expires_in_seconds),
            rearm_of=rearm_of,
            event_reservation_id=(
                prepared_event_binding[0]
                if prepared_event_binding is not None
                else None
            ),
            event_kind=(
                prepared_event_binding[1]
                if prepared_event_binding is not None
                else None
            ),
            now=observer_context.now(),
        )
        if not self.thread_delivery_readiness(self.root):
            unavailable = self.ledger.transition(
                record.monitor_id,
                expected=(MonitorState.REGISTERING,),
                state=MonitorState.DAEMON_UNAVAILABLE,
                outcome={"kind": "delivery_daemon_mismatch_before_arm", "at": time.time()},
            )
            assert unavailable is not None
            record = unavailable
        else:
            armed = self.ledger.arm(record.monitor_id)
            assert armed is not None
            record = armed
        if start_daemon and record.state == MonitorState.ARMED:
            self.daemon_starter(self.root)
        return self._registration_response(record)

    def reserve_terminal_event(
        self,
        request: Mapping[str, Any],
        *,
        descriptor_path: str | Path | None = None,
    ) -> dict[str, Any]:
        selected_descriptor = (
            validate_private_output_path(Path(descriptor_path))
            if descriptor_path is not None
            else None
        )
        observed_at = self.observer_context_factory().now()
        reservation_id = str(request.get("reservation_id") or uuid.uuid4())
        normalized = normalize_reservation(
            {**dict(request), "reservation_id": reservation_id},
            now=observed_at,
        )
        producer_id = normalized.producer_identity or normalized.producer_task_id
        if producer_id is None:
            raise ValidationError("terminal reservation requires a producer identity")
        semantic = normalized.semantic_payload()
        if normalized.kind is EventKind.WORKER_TERMINAL:
            scope = capture_worktree_scope(
                worktree=str(normalized.worktree),
                baseline_commit=str(normalized.baseline_commit),
                producer_task_id=str(normalized.producer_task_id),
                allowed_prefixes=normalized.allowed_path_prefixes,
            )
            if Path(str(normalized.repository)).resolve() != Path(scope.common_dir):
                raise ValidationError(
                    "worker reservation repository does not match the Git common directory"
                )
            semantic = {**semantic, "git_scope": asdict(scope)}
        publish_token = secrets.token_urlsafe(32)
        record, created = self.ledger.reserve_event(
            reservation_id=reservation_id,
            idempotency_key=normalized.idempotency_key,
            kind=(
                normalized.kind.value
                if isinstance(normalized.kind, EventKind)
                else str(normalized.kind)
            ),
            producer_id=producer_id,
            semantic=semantic,
            publish_token=publish_token,
            expires_at=normalized.expires_at,
            now=observed_at,
        )
        response = record.status_dict()
        response["created"] = created
        if created:
            response["publish_token"] = publish_token
            if selected_descriptor is not None:
                atomic_write_json(
                    selected_descriptor,
                    {
                        "schema": 1,
                        "reservation_id": record.reservation_id,
                        "kind": record.kind,
                        "publish_token": publish_token,
                    },
                    mode=0o600,
                )
                response["publisher_descriptor"] = str(selected_descriptor)
        return response

    def event_status(self, reservation_id: str) -> dict[str, Any]:
        self.ledger.expire_events(now=self.observer_context_factory().now())
        record = self.ledger.get_event(reservation_id)
        if record is None:
            raise ValidationError(f"unknown event reservation: {reservation_id}")
        return record.status_dict()

    def cancel_terminal_event(self, reservation_id: str) -> dict[str, Any]:
        return self.ledger.cancel_event(reservation_id).status_dict()

    def publish_event_heartbeat(
        self,
        reservation_id: str,
        *,
        publish_token: str,
        heartbeat: Mapping[str, Any],
    ) -> dict[str, Any]:
        now = self.observer_context_factory().now()
        normalized = normalize_heartbeat(heartbeat, host_received_at=now)
        return self.ledger.publish_event_heartbeat(
            reservation_id,
            publish_token=publish_token,
            heartbeat=normalized,
            now=now,
        ).status_dict()

    def publish_terminal_event(
        self,
        reservation_id: str,
        *,
        publish_token: str,
        terminal_event: Mapping[str, Any],
    ) -> dict[str, Any]:
        record = self.ledger.get_event(reservation_id)
        if record is None:
            raise ValidationError(f"unknown event reservation: {reservation_id}")
        now = self.observer_context_factory().now()
        normalized = normalize_terminal_event(
            terminal_event, kind=EventKind(record.kind), host_received_at=now
        )
        frozen_worker_scope: Mapping[str, Any] | None = None
        if isinstance(normalized, WorkerTerminalEvent):
            raw_scope = record.semantic.get("git_scope")
            if isinstance(raw_scope, Mapping):
                frozen_worker_scope = raw_scope
                frozen_task_id = raw_scope.get("producer_task_id")
            else:
                frozen_task_id = None
            if (
                not isinstance(frozen_task_id, str)
                or normalized.producer_task_id != frozen_task_id
            ):
                raise ValidationError(
                    "worker terminal producer task identity does not match "
                    "the frozen reservation scope"
                )
        attestation: Mapping[str, Any] | None = None
        if (
            record.terminal_event is None
            and isinstance(normalized, WorkerTerminalEvent)
            and normalized.outcome is WorkerOutcome.DELIVERED
        ):
            assert frozen_worker_scope is not None
            scope = GitDeliveryScope(
                worktree_root=str(frozen_worker_scope["worktree_root"]),
                common_dir=str(frozen_worker_scope["common_dir"]),
                object_format=str(frozen_worker_scope["object_format"]),
                baseline_commit=str(frozen_worker_scope["baseline_commit"]),
                producer_task_id=str(frozen_worker_scope["producer_task_id"]),
                allowed_prefixes=tuple(frozen_worker_scope["allowed_prefixes"]),
            )
            attestation = {
                **asdict(attest_candidate(scope, str(normalized.candidate_oid))),
                "lead_accepted": False,
            }
        return self.ledger.publish_terminal_event(
            reservation_id,
            publish_token=publish_token,
            terminal_event=normalized,
            git_attestation=attestation,
            now=now,
        ).status_dict()

    @staticmethod
    def _registration_eligible(observation: TargetObservation) -> bool:
        return (
            observation.is_loaded
            and observation.goal_status == "paused"
            and observation.goal is not None
        )

    @staticmethod
    def _defer_eligible(observation: TargetObservation) -> bool:
        return (
            observation.is_loaded
            and observation.goal_status in {"active", "paused"}
            and observation.goal is not None
        )

    @staticmethod
    def _same_guard(first: TargetObservation, second: TargetObservation) -> bool:
        return (
            first.thread_id == second.thread_id
            and first.is_loaded
            and second.is_loaded
            and first.goal_status == "paused"
            and second.goal_status == "paused"
            and first.goal is not None
            and first.goal == second.goal
        )

    @staticmethod
    def _thread_observation_summary(observation: TargetObservation) -> dict[str, Any]:
        snapshot = observation.goal_snapshot or {}
        return {
            "thread_id": observation.thread_id,
            "runtime_status": observation.runtime_status,
            "is_loaded": observation.is_loaded,
            "goal_status": observation.goal_status,
            "usage": (
                {
                    "tokens_used": snapshot.get("tokensUsed"),
                    "time_used_seconds": snapshot.get("timeUsedSeconds"),
                    "token_budget": snapshot.get("tokenBudget"),
                }
                if observation.goal_snapshot
                else None
            ),
        }

    async def _read_thread_observations(
        self, app_server: Any, targets: Sequence[str]
    ) -> dict[str, Mapping[str, Any] | None]:
        """Pre-read every thread_idle child so evaluation stays synchronous."""

        observations: dict[str, Mapping[str, Any] | None] = {}
        for child_id in targets:
            try:
                child = await app_server.read_observation(child_id)
            except AppServerError:
                observations[child_id] = None
            else:
                observations[child_id] = self._thread_observation_summary(child)
        return observations

    @staticmethod
    def _reject_self_wait(condition: Mapping[str, Any], thread_id: str) -> list[str]:
        """A monitor cannot wait on the very thread its wake would activate."""

        children = thread_idle_targets(condition)
        if thread_id in children:
            raise ValidationError(
                "thread_idle must not name the monitor's own target thread"
            )
        return children

    def _validate_rearm_of(self, rearm_of: str | None) -> None:
        if rearm_of is None:
            return
        if not isinstance(rearm_of, str) or not rearm_of:
            raise ValidationError("rearm_of must be a non-empty monitor id when provided")
        referenced = self.ledger.get(rearm_of)
        if referenced is None:
            raise ValidationError(f"rearm_of names an unknown monitor: {rearm_of}")
        if not is_terminal(referenced.state):
            raise ValidationError(
                "rearm_of must name a terminal monitor; a live monitor cannot be a lineage parent"
            )

    def _rearm_chain(self, record: MonitorRecord) -> list[str]:
        chain: list[str] = []
        seen = {record.monitor_id}
        parent = record.rearm_of
        while parent is not None and parent not in seen and len(chain) < MAX_REARM_CHAIN:
            chain.append(parent)
            seen.add(parent)
            ancestor = self.ledger.get(parent)
            parent = ancestor.rearm_of if ancestor is not None else None
        return chain

    def _wake_report(
        self, record: MonitorRecord, *, fired_at: float
    ) -> dict[str, Any]:
        """Assemble everything one post-wake status call must answer."""

        reason = record.wake_reason
        if reason is None:
            # A row claimed before this change stores no reason; derive the
            # honest label from the witness rather than claiming authorization.
            reason = WakeReason.CONDITION
            if record.mode == MonitorMode.DEFERRED and not witness_authorizes_continuation(
                record.witness,
                allow_heuristic_continuation=record.allow_heuristic_continuation,
            ):
                reason = WakeReason.UNAUTHORIZED_EVIDENCE
        waited = (
            max(0.0, fired_at - record.armed_at) if record.armed_at is not None else None
        )
        report: dict[str, Any] = {
            "wake_reason": reason.value,
            "witness": list(record.witness),
            "armed_at": record.armed_at,
            "fired_at": fired_at,
            "waited_seconds": waited,
            "evaluation_count": record.evaluation_count,
            "avoided_poll_turns": (
                int(waited // POLL_YIELD_FLOOR_SECONDS) if waited is not None else None
            ),
        }
        if reason == WakeReason.OBSERVER_FAILED:
            report["failure_detail"] = dict(record.evidence)
        if any(
            item.get("type") in {"command_terminal", "worker_terminal"}
            for item in record.witness
        ):
            report["task_success"] = False
            report["lead_accepted"] = False
            event = self.ledger.event_for_monitor(record.monitor_id)
            if event is not None:
                report["terminal_event"] = event.status_dict()
        tail = journal_tail(record.condition, limit=WAKE_JOURNAL_TAIL_LINES)
        if tail:
            report["journal_tail"] = tail
        return report

    async def register(
        self,
        *,
        thread_id: str,
        condition: Mapping[str, Any],
        expires_in_seconds: float,
        allow_heuristic_continuation: bool = False,
        idempotency_key: str | None = None,
        rearm_of: str | None = None,
        start_daemon: bool = True,
    ) -> dict[str, Any]:
        if not thread_id:
            raise ValidationError("thread_id must be non-empty")
        if (
            isinstance(expires_in_seconds, bool)
            or not isinstance(expires_in_seconds, (int, float))
            or expires_in_seconds <= 0
        ):
            raise ValidationError("expires_in_seconds must be greater than zero")
        if not isinstance(allow_heuristic_continuation, bool):
            raise ValidationError("allow_heuristic_continuation must be boolean")
        if idempotency_key is not None and not idempotency_key:
            raise ValidationError("idempotency_key must be non-empty when provided")
        self._validate_rearm_of(rearm_of)
        event_binding = event_condition_binding(condition)
        if event_binding is not None and not self.event_daemon_readiness(self.root):
            raise ValidationError(
                "event monitor requires an exact event-capable daemon before arming"
            )

        observer_context = self.observer_context_factory()
        async with self.app_server_factory() as app_server:
            first = await app_server.read_observation(thread_id)
            if not first.is_loaded or first.goal is None:
                raise ValidationError(
                    "target must be locally loaded and own a non-null goal. "
                    "Do not create a goal to satisfy this precondition."
                )
            if not self._registration_eligible(first):
                raise ValidationError(
                    "target must be locally loaded and own a paused goal at registration"
                )
            assert first.goal is not None
            children = self._reject_self_wait(condition, thread_id)
            guard = TargetGuard(thread_id=thread_id, goal=first.goal)
            semantic = {
                "thread_id": thread_id,
                "guard": guard.as_dict(),
                "condition": public_condition_semantics(condition),
                "expires_in_seconds": float(expires_in_seconds),
                "allow_heuristic_continuation": bool(allow_heuristic_continuation),
            }
            if rearm_of is not None:
                # Only present when requested, so an idempotent replay of a
                # monitor armed before this change still matches byte for byte.
                semantic["rearm_of"] = rearm_of
            if idempotency_key is not None:
                existing = self.ledger.get_by_idempotency_key(idempotency_key)
                if existing is not None:
                    if (
                        existing.mode != MonitorMode.LEGACY
                        or existing.semantic != semantic
                    ):
                        raise ValidationError(
                            "idempotency key already belongs to a monitor with different semantics"
                        )
                    record = existing
                    if start_daemon and record.state == MonitorState.ARMED:
                        self.daemon_starter(self.root)
                    return self._registration_response(record)
            monitor_id = str(uuid.uuid4())
            receipt_token = secrets.token_urlsafe(32)
            observer_context.thread_observations.update(
                await self._read_thread_observations(app_server, children)
            )
            prepared_condition = prepare_condition(
                condition,
                observer_context,
                monitor_id=monitor_id,
                receipt_token=receipt_token,
            )
            prepared_event_binding = event_condition_binding(prepared_condition)
            record, created = self.ledger.create_or_get(
                monitor_id=monitor_id,
                idempotency_key=idempotency_key,
                semantic=semantic,
                target=guard,
                condition=prepared_condition,
                allow_heuristic_continuation=allow_heuristic_continuation,
                expires_at=observer_context.now() + float(expires_in_seconds),
                rearm_of=rearm_of,
                event_reservation_id=(
                    prepared_event_binding[0]
                    if prepared_event_binding is not None
                    else None
                ),
                event_kind=(
                    prepared_event_binding[1]
                    if prepared_event_binding is not None
                    else None
                ),
                now=observer_context.now(),
            )
            if created:
                second = await app_server.read_observation(thread_id)
                if self._same_guard(first, second):
                    if (
                        prepared_event_binding is not None
                        and not self.event_daemon_readiness(self.root)
                    ):
                        unavailable = self.ledger.transition(
                            record.monitor_id,
                            expected=(MonitorState.REGISTERING,),
                            state=MonitorState.DAEMON_UNAVAILABLE,
                            outcome={
                                "kind": "event_daemon_mismatch_before_arm",
                                "at": time.time(),
                            },
                        )
                        assert unavailable is not None
                        record = unavailable
                    else:
                        armed = self.ledger.arm(record.monitor_id)
                        assert armed is not None
                        record = armed
                else:
                    superseded = self.ledger.transition(
                        record.monitor_id,
                        expected=(MonitorState.REGISTERING,),
                        state=MonitorState.SUPERSEDED,
                        outcome={
                            "kind": "registration_guard_changed",
                            "first": self._observation_summary(first),
                            "second": self._observation_summary(second),
                            "at": time.time(),
                        },
                    )
                    assert superseded is not None
                    record = superseded
        if start_daemon and record.state == MonitorState.ARMED:
            self.daemon_starter(self.root)
        return self._registration_response(record)

    async def defer(
        self,
        *,
        thread_id: str,
        condition: Mapping[str, Any],
        expires_in_seconds: float,
        allow_heuristic_continuation: bool = False,
        idempotency_key: str,
        rearm_of: str | None = None,
    ) -> dict[str, Any]:
        """Attach one explicit loaded active or paused goal to a monitor."""

        if not thread_id:
            raise ValidationError("thread_id must be non-empty")
        if (
            isinstance(expires_in_seconds, bool)
            or not isinstance(expires_in_seconds, (int, float))
            or not math.isfinite(float(expires_in_seconds))
            or expires_in_seconds <= 0
        ):
            raise ValidationError(
                "expires_in_seconds must be a finite number greater than zero"
            )
        if not isinstance(allow_heuristic_continuation, bool):
            raise ValidationError("allow_heuristic_continuation must be boolean")
        if not isinstance(idempotency_key, str) or not idempotency_key:
            raise ValidationError("idempotency_key must be non-empty")
        self._validate_rearm_of(rearm_of)
        event_binding = event_condition_binding(condition)

        observer_context = self.observer_context_factory()
        async with self.app_server_factory() as app_server:
            first = await app_server.read_observation(thread_id)
            if not first.is_loaded or first.goal is None:
                raise ValidationError(
                    "defer target must be locally loaded and own a non-null goal. "
                    "Do not create a goal to satisfy this precondition."
                )
            children = self._reject_self_wait(condition, thread_id)
            guard = TargetGuard(thread_id=thread_id, goal=first.goal)
            semantic = {
                "thread_id": thread_id,
                "guard": guard.as_dict(),
                "condition": public_condition_semantics(condition),
                "expires_in_seconds": float(expires_in_seconds),
                "allow_heuristic_continuation": bool(
                    allow_heuristic_continuation
                ),
            }
            if rearm_of is not None:
                semantic["rearm_of"] = rearm_of
            existing = self.ledger.get_by_idempotency_key(idempotency_key)
            if existing is not None:
                if (
                    existing.mode != MonitorMode.DEFERRED
                    or existing.semantic != semantic
                ):
                    raise ValidationError(
                        "idempotency key already belongs to a monitor with different semantics"
                    )
                response = self._registration_response(existing)
                if existing.state == MonitorState.ARMED:
                    response["next_action"] = "end_current_turn"
                return response
            if not self._defer_eligible(first):
                raise ValidationError(
                    "defer target must own an active or paused goal"
                )

            monitor_id = str(uuid.uuid4())
            receipt_token = secrets.token_urlsafe(32)
            observer_context.thread_observations.update(
                await self._read_thread_observations(app_server, children)
            )
            prepared_condition = prepare_condition(
                condition,
                observer_context,
                monitor_id=monitor_id,
                receipt_token=receipt_token,
            )
            prepared_event_binding = event_condition_binding(prepared_condition)
            try:
                protocol_lock = DeferProtocolLock(self.root)
                protocol_lock.__enter__()
            except RuntimeError as exc:
                raise ValidationError(str(exc)) from exc
            try:
                record, created = self.ledger.create_or_get(
                    monitor_id=monitor_id,
                    idempotency_key=idempotency_key,
                    semantic=semantic,
                    target=guard,
                    condition=prepared_condition,
                    allow_heuristic_continuation=allow_heuristic_continuation,
                    expires_at=observer_context.now() + float(expires_in_seconds),
                    mode=MonitorMode.DEFERRED,
                    idle_barrier=True,
                    initial_state=MonitorState.DEFER_INTENT,
                    rearm_of=rearm_of,
                    event_reservation_id=(
                        prepared_event_binding[0]
                        if prepared_event_binding is not None
                        else None
                    ),
                    event_kind=(
                        prepared_event_binding[1]
                        if prepared_event_binding is not None
                        else None
                    ),
                    now=observer_context.now(),
                )
                if not created:
                    response = self._registration_response(record)
                    if record.state == MonitorState.ARMED:
                        response["next_action"] = "end_current_turn"
                    return response

                try:
                    ready = (
                        self.event_daemon_readiness(self.root)
                        if prepared_event_binding is not None
                        else self.daemon_readiness(self.root)
                    )
                except Exception as exc:
                    ready = False
                    readiness_error = f"{type(exc).__name__}: {exc}"
                else:
                    readiness_error = None
                if not ready:
                    unavailable = self.ledger.transition(
                        record.monitor_id,
                        expected=(MonitorState.DEFER_INTENT,),
                        state=MonitorState.DAEMON_UNAVAILABLE,
                        outcome={
                            "kind": "daemon_unavailable_before_pause",
                            "error": readiness_error,
                            "at": time.time(),
                        },
                    )
                    if unavailable is None:
                        unavailable = self.ledger.get(record.monitor_id)
                        assert unavailable is not None
                    return self._registration_response(unavailable)

                now = observer_context.now()
                if now >= record.expires_at:
                    expired = self.ledger.transition(
                        record.monitor_id,
                        expected=(MonitorState.DEFER_INTENT,),
                        state=MonitorState.EXPIRED,
                        outcome={"kind": "expired_before_pause", "at": now},
                    )
                    if expired is None:
                        expired = self.ledger.get(record.monitor_id)
                        assert expired is not None
                    return self._registration_response(expired)

                if first.goal_status == "paused":
                    confirmed = await app_server.read_observation(thread_id)
                    if not self._same_guard(first, confirmed):
                        superseded = self.ledger.transition(
                            record.monitor_id,
                            expected=(MonitorState.DEFER_INTENT,),
                            state=MonitorState.SUPERSEDED,
                            outcome={
                                "kind": "paused_guard_changed",
                                "first": self._observation_summary(first),
                                "second": self._observation_summary(confirmed),
                                "at": time.time(),
                            },
                        )
                        assert superseded is not None
                        return self._registration_response(superseded)
                    armed = self.ledger.arm_paused_deferred(
                        record.monitor_id,
                        confirmation=self._observation_summary(confirmed),
                    )
                    assert armed is not None
                    response = self._registration_response(armed)
                    response["next_action"] = "end_current_turn"
                    return response

                pausing = self.ledger.begin_pause(
                    record.monitor_id,
                    pre_pause=self._observation_summary(first),
                )
                if pausing is None:
                    current = self.ledger.get(record.monitor_id)
                    assert current is not None
                    return self._registration_response(current)
                try:
                    returned = await app_server.pause_guarded_goal(thread_id)
                except Exception as exc:
                    uncertain = self.ledger.transition(
                        record.monitor_id,
                        expected=(MonitorState.PAUSING,),
                        state=MonitorState.PAUSE_UNCERTAIN,
                        outcome={
                            "kind": "pause_send_or_response_uncertain",
                            "error": f"{type(exc).__name__}: {exc}",
                            "at": time.time(),
                        },
                    )
                    assert uncertain is not None
                    return self._registration_response(uncertain)

                terminal = self._validate_pause_observation(
                    record,
                    returned,
                    source="response",
                )
                if terminal is not None:
                    return self._registration_response(terminal)
                try:
                    confirmed = await app_server.read_observation(thread_id)
                except Exception as exc:
                    uncertain = self.ledger.transition(
                        record.monitor_id,
                        expected=(MonitorState.PAUSING,),
                        state=MonitorState.PAUSE_UNCERTAIN,
                        outcome={
                            "kind": "pause_reobservation_uncertain",
                            "error": f"{type(exc).__name__}: {exc}",
                            "at": time.time(),
                        },
                    )
                    assert uncertain is not None
                    return self._registration_response(uncertain)

                terminal = self._validate_pause_observation(
                    record,
                    confirmed,
                    source="confirmation",
                )
                if terminal is not None:
                    return self._registration_response(terminal)
                armed = self.ledger.arm_deferred(
                    record.monitor_id,
                    confirmation=self._observation_summary(confirmed),
                )
                assert armed is not None
                response = self._registration_response(armed)
                response["next_action"] = "end_current_turn"
                return response
            finally:
                protocol_lock.__exit__(None, None, None)

    def _validate_pause_observation(
        self,
        record: MonitorRecord,
        observation: TargetObservation,
        *,
        source: str,
    ) -> MonitorRecord | None:
        assert record.target is not None
        summary = self._observation_summary(observation)
        if not observation.is_loaded:
            return self.ledger.transition(
                record.monitor_id,
                expected=(MonitorState.PAUSING,),
                state=MonitorState.PAUSE_UNCERTAIN,
                outcome={
                    "kind": f"pause_{source}_unloaded",
                    source: summary,
                    "at": time.time(),
                },
            )
        if (
            observation.thread_id != record.target.thread_id
            or observation.goal != record.target.goal
        ):
            return self.ledger.transition(
                record.monitor_id,
                expected=(MonitorState.PAUSING,),
                state=MonitorState.MIS_TARGETED_PAUSE,
                outcome={
                    "kind": "mis_targeted_pause",
                    "source": source,
                    "captured_goal": record.target.goal.as_dict(),
                    source: summary,
                    "at": time.time(),
                },
            )
        if observation.goal_status != "paused":
            return self.ledger.transition(
                record.monitor_id,
                expected=(MonitorState.PAUSING,),
                state=MonitorState.PAUSE_REJECTED,
                outcome={
                    "kind": "pause_rejected",
                    "source": source,
                    "returned_status": observation.goal_status,
                    source: summary,
                    "at": time.time(),
                },
            )
        return None

    def _registration_response(self, record: MonitorRecord) -> dict[str, Any]:
        response = self.status(record.monitor_id)
        if record.delivery_kind == DeliveryKind.THREAD:
            caller_bound = (record.delivery or {}).get("origin_binding") == (
                "trusted_mcp_caller"
            )
            response["targeting"] = {
                "kind": (
                    "trusted_mcp_caller"
                    if caller_bound
                    else "best_effort_exact_local_thread_id"
                ),
                "authenticated_current_task": caller_bound,
                "same_thread_fifo_release_authorized": True,
            }
        elif record.mode == MonitorMode.DEFERRED:
            response["targeting"] = {
                "kind": "best_effort_explicit_thread_id",
                "authenticated_current_task": False,
            }
        if record.state == MonitorState.ARMED:
            response["next_action"] = "end_current_turn"
        receipts = self._receipt_instructions(record.condition)
        if receipts:
            response["receipt_instructions"] = receipts
        return response

    def status(self, monitor_id: str) -> dict[str, Any]:
        record = self.ledger.get(monitor_id)
        if record is None:
            raise ValidationError(f"unknown monitor: {monitor_id}")
        value = record.status_dict()
        # The wake report's journal_tail is the sole line carrier; here the
        # stored journal collapses to counters so post-wake context is spent once.
        value["condition"] = elide_condition_journals(record.condition)
        chain = self._rearm_chain(record)
        if chain:
            value["rearm_chain"] = chain
        try:
            event = self.ledger.event_for_monitor(record.monitor_id)
        except (ConflictError, ValidationError):
            value["terminal_event"] = {
                "state": "corrupt",
                "lead_accepted": False,
            }
            value["lead_accepted"] = False
        else:
            if event is not None:
                value["terminal_event"] = event.status_dict()
                value["lead_accepted"] = False
        if record.state in {MonitorState.ARMED, MonitorState.CLAIMED}:
            value["supervision"] = (
                "healthy" if daemon_is_healthy(self.root) else "unsupervised"
            )
        else:
            value["supervision"] = "not_required"
        return value

    def list(self) -> list[dict[str, Any]]:
        return [self.status(record.monitor_id) for record in self.ledger.list()]

    def cancel(self, monitor_id: str) -> dict[str, Any]:
        record = self.ledger.cancel(monitor_id)
        if record is None:
            current = self.ledger.get(monitor_id)
            if current is None:
                raise ValidationError(f"unknown monitor: {monitor_id}")
            return self.status(current.monitor_id)
        return self.status(record.monitor_id)

    def publish_receipt(self, *, monitor_id: str, token: str, status: str) -> dict[str, Any]:
        record = self.ledger.get(monitor_id)
        if record is None:
            raise ValidationError(f"unknown monitor: {monitor_id}")
        leaves = self._receipt_leaves(record.condition)
        selected = next((leaf for leaf in leaves if leaf.get("token") == token), None)
        if selected is None:
            raise ValidationError("receipt token does not match this monitor")
        path = Path(str(selected["path"]))
        payload = receipt_payload(monitor_id=monitor_id, token=token, status=status)
        atomic_write_json(path, payload)
        return {"monitor_id": monitor_id, "receipt_path": str(path), "status": status}

    def recover_after_daemon_start(self) -> int:
        recovered = self.ledger.recover_activating() + self.ledger.recover_registering()
        return recovered + self._recover_deferred_if_unlocked()

    def _recover_deferred_if_unlocked(self) -> int:
        try:
            with DeferProtocolLock(self.root):
                return self.ledger.recover_defer_intent() + self.ledger.recover_pausing()
        except RuntimeError:
            return 0

    async def reconcile_once(self) -> list[dict[str, Any]]:
        """Poll every eligible monitor once and return state transition receipts."""

        transitions: list[dict[str, Any]] = []
        self._recover_deferred_if_unlocked()
        now = self.observer_context_factory().now()
        self.ledger.expire_events(now=now)
        for record in self.ledger.list(include_terminal=False):
            if record.state not in {
                MonitorState.ARMED,
                MonitorState.CLAIMED,
                MonitorState.ADMISSION_IN_PROGRESS,
                MonitorState.QUEUE_ACCEPTED,
                MonitorState.CANCEL_REQUESTED,
                MonitorState.CANCELLATION_IN_PROGRESS,
            }:
                continue
            try:
                if (
                    record.expires_at <= now
                    and record.mode == MonitorMode.LEGACY
                    and record.delivery_kind == DeliveryKind.GOAL
                    and not contains_event_condition(record.condition)
                ):
                    expired = self.ledger.transition(
                        record.monitor_id,
                        expected=(record.state,),
                        state=MonitorState.EXPIRED,
                        outcome={"kind": "expired", "at": now},
                    )
                    if expired is not None:
                        transitions.append(self.status(expired.monitor_id))
                    continue
                # A deferred row past its expiry is not terminal here: an armed
                # one becomes claim-eligible with reason `expired` below, and a
                # claimed one (e.g. found after a daemon restart) continues its
                # wake. Writing EXPIRED here would re-strand the paused goal.
                if record.state == MonitorState.ARMED:
                    observer_context = self.observer_context_factory()
                    children = thread_idle_targets(record.condition)
                    if record.mode == MonitorMode.DEFERRED:
                        if not record.idle_barrier:
                            # A deferred row without its idle barrier is a
                            # corrupted row, not a wake-eligible fact: waking
                            # from unverified state is exactly what the barrier
                            # exists to prevent, so this stays fail-closed.
                            failed = self.ledger.transition(
                                record.monitor_id,
                                expected=(MonitorState.ARMED,),
                                state=MonitorState.OBSERVER_FAILED,
                                outcome={
                                    "kind": "deferred_idle_barrier_missing",
                                    "at": time.time(),
                                },
                            )
                            if failed is not None:
                                transitions.append(self.status(failed.monitor_id))
                            continue
                        can_evaluate = await self._deferred_target_is_idle(
                            record, children, observer_context
                        )
                        if not can_evaluate:
                            current = self.ledger.get(record.monitor_id)
                            if current is not None and current.state != MonitorState.ARMED:
                                transitions.append(self.status(current.monitor_id))
                            continue
                    elif children:
                        async with self.app_server_factory() as app_server:
                            observer_context.thread_observations.update(
                                await self._read_thread_observations(
                                    app_server, children
                                )
                            )
                    claimed = self._evaluate_and_claim(record, observer_context)
                    if claimed is None:
                        continue
                    record = claimed
                    transitions.append(self.status(record.monitor_id))
                    if record.state != MonitorState.CLAIMED:
                        continue
                outcome = (
                    await self._finish_thread_delivery(record)
                    if record.delivery_kind == DeliveryKind.THREAD
                    else await self._finish_claim(record)
                )
                if outcome is not None:
                    transitions.append(self.status(outcome.monitor_id))
            except Exception as exc:
                failed = self._record_unexpected_failure(record, exc)
                if failed is not None:
                    transitions.append(self.status(failed.monitor_id))
        return transitions

    async def _deferred_target_is_idle(
        self,
        record: MonitorRecord,
        children: Sequence[str],
        observer_context: ObserverContext,
    ) -> bool:
        """Preflight a deferred target before evaluating even a true condition.

        Child-thread summaries are read in this same session, and only once the
        target is genuinely evaluable, so a busy target costs one read per poll.
        """

        assert record.target is not None
        async with self.app_server_factory() as app_server:
            observation = await app_server.read_observation(record.target.thread_id)
            if not observation.is_loaded:
                self.ledger.transition(
                    record.monitor_id,
                    expected=(MonitorState.ARMED,),
                    state=MonitorState.UNLOADED_TARGET,
                    outcome={
                        "kind": "unloaded_target_before_deferred_evaluation",
                        "observation": self._observation_summary(observation),
                        "at": time.time(),
                    },
                )
                return False
            if not observation.matches_guard(record.target):
                self.ledger.transition(
                    record.monitor_id,
                    expected=(MonitorState.ARMED,),
                    state=MonitorState.SUPERSEDED,
                    outcome={
                        "kind": "deferred_evaluation_guard_changed",
                        "observation": self._observation_summary(observation),
                        "at": time.time(),
                    },
                )
                return False
            if observation.runtime_status != "idle":
                return False
            if children:
                observer_context.thread_observations.update(
                    await self._read_thread_observations(app_server, children)
                )
            return True

    def _record_unexpected_failure(
        self, record: MonitorRecord, error: Exception
    ) -> MonitorRecord | None:
        """Isolate one broken observer/transport from every other monitor."""

        current = self.ledger.get(record.monitor_id)
        if current is None:
            return None
        if (
            (
                current.mode == MonitorMode.DEFERRED
                or current.delivery_kind == DeliveryKind.THREAD
            )
            and current.state in {MonitorState.ARMED, MonitorState.CLAIMED}
        ):
            # An untyped failure (e.g. a transient app-server socket error) is
            # not an irrecoverable observer identity, and no activation packet
            # has been sent from either state. Stranding the paused goal or
            # burning its single wake would both be wrong: record the evidence,
            # stay retryable, and let expiry be the backstop.
            self.ledger.transition(
                current.monitor_id,
                expected=(current.state,),
                state=current.state,
                outcome={
                    "kind": "transient_observation_failure_recorded",
                    "error": f"{type(error).__name__}: {error}",
                    "at": time.time(),
                },
            )
            return None
        if current.state == MonitorState.ARMED:
            state = MonitorState.OBSERVER_FAILED
        elif current.state == MonitorState.CLAIMED:
            state = MonitorState.ACTIVATION_FAILED
        elif current.state == MonitorState.ACTIVATING:
            state = MonitorState.ACTIVATION_UNCERTAIN
        else:
            return None
        return self.ledger.transition(
            current.monitor_id,
            expected=(current.state,),
            state=state,
            outcome={
                "kind": "unexpected_monitor_failure",
                "error": f"{type(error).__name__}: {error}",
                "at": time.time(),
            },
        )

    def _evaluate_and_claim(
        self, record: MonitorRecord, observer_context: ObserverContext
    ) -> MonitorRecord | None:
        """Convert one observed fact into the monitor's single durable claim.

        For a deferred monitor every wake-eligible fact — an authorized
        condition, unauthorized heuristic evidence, an irrecoverable observer
        identity, and the deadline itself — is consumed through this one claim,
        so "at most one activation request" stays a single-table CAS fact.
        """

        if contains_event_condition(record.condition):
            contract_error: str | None = None
            try:
                event_binding = event_condition_binding(record.condition)
                if event_binding is None:
                    raise ValidationError(
                        "event condition contains no bindable reservation"
                    )
                condition, external_evaluations = observe_external_condition_leaves(
                    record.condition, observer_context
                )
            except ValidationError as exc:
                condition = copy.deepcopy(dict(record.condition))
                external_evaluations = {}
                contract_error = str(exc)
            return self.ledger.evaluate_and_claim_event_monitor(
                record.monitor_id,
                expected_evaluation_count=record.evaluation_count,
                observed_condition=condition,
                external_evaluations=external_evaluations,
                contract_error=contract_error,
                now_factory=observer_context.now,
            )

        condition: MutableMapping[str, Any] = copy.deepcopy(dict(record.condition))
        evaluation = evaluate_condition(condition, observer_context)
        updated = self.ledger.update_evaluation(
            record.monitor_id,
            condition=condition,
            evidence=evaluation.evidence,
            witness=evaluation.witness,
        )
        if updated is None:
            return None
        deferred = record.mode == MonitorMode.DEFERRED
        thread_delivery = record.delivery_kind == DeliveryKind.THREAD
        if evaluation.value == TriState.TRUE:
            wake_reason: WakeReason | None = None
            if deferred or thread_delivery:
                wake_reason = (
                    WakeReason.CONDITION
                    if thread_delivery
                    or witness_authorizes_continuation(
                        evaluation.witness,
                        allow_heuristic_continuation=record.allow_heuristic_continuation,
                    )
                    else WakeReason.UNAUTHORIZED_EVIDENCE
                )
            return self.ledger.claim(
                record.monitor_id,
                condition=condition,
                evidence=evaluation.evidence,
                witness=evaluation.witness,
                wake_reason=wake_reason,
            )
        if evaluation.fatal:
            if deferred or thread_delivery:
                return self.ledger.claim(
                    record.monitor_id,
                    condition=condition,
                    evidence=evaluation.evidence,
                    witness=evaluation.witness,
                    wake_reason=WakeReason.OBSERVER_FAILED,
                )
            return self.ledger.transition(
                record.monitor_id,
                expected=(MonitorState.ARMED,),
                state=MonitorState.OBSERVER_FAILED,
                evidence=evaluation.evidence,
                witness=evaluation.witness,
                outcome={"kind": "observer_identity_failed", "at": time.time()},
            )
        if (deferred or thread_delivery) and observer_context.now() >= record.expires_at:
            return self.ledger.claim(
                record.monitor_id,
                condition=condition,
                evidence=evaluation.evidence,
                witness=evaluation.witness,
                wake_reason=WakeReason.EXPIRED,
            )
        return None

    async def _finish_thread_delivery(
        self, record: MonitorRecord
    ) -> MonitorRecord | None:
        """Advance one claimed/accepted pointer without ever reopening admission."""

        delivery = dict(record.delivery or {})
        thread_id = str(delivery.get("thread_id", ""))
        delivery_id = str(delivery.get("delivery_id", ""))
        pointer = str(delivery.get("pointer", ""))
        pointer_digest = str(delivery.get("pointer_digest", ""))
        if not all((thread_id, delivery_id, pointer, pointer_digest)):
            return self.ledger.update_thread_delivery(
                record.monitor_id,
                expected=(record.state,),
                state=MonitorState.DELIVERY_REJECTED,
                delivery_state=ThreadDeliveryState.DELIVERY_REJECTED,
                delivery_outcome={
                    "kind": "invalid_stored_delivery_envelope",
                    "at": time.time(),
                },
            )

        now = self.observer_context_factory().now()
        adapter = self.thread_delivery_factory()
        try:
            async with adapter as app_server:
                if record.state == MonitorState.CLAIMED:
                    try:
                        capability = await app_server.preflight_thread_delivery(
                            thread_id
                        )
                    except AppServerError as exc:
                        return self.ledger.update_thread_delivery(
                            record.monitor_id,
                            expected=(MonitorState.CLAIMED,),
                            state=MonitorState.DELIVERY_CAPABILITY_UNAVAILABLE,
                            delivery_state=ThreadDeliveryState.DELIVERY_REJECTED,
                            delivery_outcome={
                                "kind": "delivery_capability_unavailable",
                                "error": str(exc),
                                "at": now,
                            },
                            now=now,
                        )
                    admitted = self.ledger.begin_thread_admission(
                        record.monitor_id,
                        capability=capability,
                        now=now,
                    )
                    if admitted is None:
                        return None
                    record = admitted
                    try:
                        receipt = await app_server.add_thread_delivery(
                            thread_id=thread_id,
                            delivery_id=delivery_id,
                            pointer=pointer,
                        )
                    except AppServerRejectedError as exc:
                        return self.ledger.update_thread_delivery(
                            record.monitor_id,
                            expected=(MonitorState.ADMISSION_IN_PROGRESS,),
                            state=MonitorState.DELIVERY_REJECTED,
                            delivery_state=ThreadDeliveryState.DELIVERY_REJECTED,
                            delivery_outcome={
                                "kind": "queue_admission_rejected",
                                "error": str(exc),
                                "at": now,
                            },
                            now=now,
                        )
                    except AppServerError as exc:
                        record = self.ledger.update_thread_delivery(
                            record.monitor_id,
                            expected=(MonitorState.ADMISSION_IN_PROGRESS,),
                            state=MonitorState.ADMISSION_IN_PROGRESS,
                            delivery_state=ThreadDeliveryState.ADMISSION_IN_PROGRESS,
                            reconciliation={
                                "classification": "admission_transport_uncertain",
                                "error": str(exc),
                                "online_absence_seconds": 0.0,
                                "last_online_at": None,
                                "at": now,
                            },
                            now=now,
                        ) or record
                    else:
                        accepted = self.ledger.record_thread_queue_ack(
                            record.monitor_id, receipt=receipt, now=now
                        )
                        if accepted is None:
                            return None
                        record = accepted
                        if not bool(capability.get("loaded")):
                            queue = await app_server.list_thread_queue(thread_id)
                            item_index = next(
                                (
                                    index
                                    for index, item in enumerate(queue)
                                    if item.get("item_id") == receipt.get("item_id")
                                ),
                                None,
                            )
                            if item_index is None:
                                snapshot = {
                                    "classification": "pre_resume_pointer_absent",
                                    "pre_resume_item_count": len(queue),
                                    "pre_resume_item_ids": [
                                        str(item.get("item_id")) for item in queue
                                    ],
                                    "resume_attempted_at": None,
                                    "at": now,
                                }
                            else:
                                ahead = queue[:item_index]
                                snapshot = {
                                    "classification": "pre_resume_snapshot",
                                    "pre_resume_item_count": len(ahead),
                                    "pre_resume_item_ids": [
                                        str(item.get("item_id")) for item in ahead
                                    ],
                                    # Persisted before the request below; recovery
                                    # never sends a second resume.
                                    "resume_attempted_at": now,
                                    "at": now,
                                }
                            updated = self.ledger.update_thread_delivery(
                                record.monitor_id,
                                expected=(MonitorState.QUEUE_ACCEPTED,),
                                state=MonitorState.QUEUE_ACCEPTED,
                                delivery_state=ThreadDeliveryState.QUEUE_ACCEPTED,
                                reconciliation=snapshot,
                                now=now,
                            )
                            if updated is not None:
                                record = updated
                            if item_index is not None:
                                try:
                                    await app_server.resume_thread_delivery(thread_id)
                                except AppServerError as exc:
                                    snapshot["resume_result"] = "transport_uncertain"
                                    snapshot["resume_error"] = str(exc)
                                    self.ledger.update_thread_delivery(
                                        record.monitor_id,
                                        expected=(MonitorState.QUEUE_ACCEPTED,),
                                        state=MonitorState.QUEUE_ACCEPTED,
                                        delivery_state=ThreadDeliveryState.QUEUE_ACCEPTED,
                                        reconciliation=snapshot,
                                        now=now,
                                    )

                observation = await app_server.inspect_thread_delivery(
                    thread_id=thread_id,
                    delivery_id=delivery_id,
                    expected_pointer_digest=pointer_digest,
                )
        except AppServerError as exc:
            offline = dict(record.reconciliation or {})
            offline.update(
                {
                    "classification": "app_server_offline",
                    "error": str(exc),
                    # The reconciliation deadline is measured only while an
                    # online app-server can prove exact absence. Break the
                    # sampling interval across every offline observation.
                    "last_online_at": None,
                    "at": now,
                }
            )
            return self.ledger.update_thread_delivery(
                record.monitor_id,
                expected=(
                    MonitorState.ADMISSION_IN_PROGRESS,
                    MonitorState.QUEUE_ACCEPTED,
                ),
                state=record.state,
                delivery_state=(
                    ThreadDeliveryState.ADMISSION_IN_PROGRESS
                    if record.state == MonitorState.ADMISSION_IN_PROGRESS
                    else ThreadDeliveryState.QUEUE_ACCEPTED
                ),
                reconciliation=offline,
                now=now,
            )

        classification = observation.get("classification")
        reconciliation = dict(record.reconciliation or {})
        reconciliation.update(dict(observation))
        reconciliation["at"] = now
        if record.state in {
            MonitorState.CANCEL_REQUESTED,
            MonitorState.CANCELLATION_IN_PROGRESS,
        }:
            if classification == "delivery_modified":
                return self.ledger.update_thread_delivery(
                    record.monitor_id,
                    expected=(record.state,),
                    state=MonitorState.DELIVERY_MODIFIED,
                    delivery_state=ThreadDeliveryState.DELIVERY_MODIFIED,
                    reconciliation=reconciliation,
                    delivery_outcome={"kind": "delivery_modified", "at": now},
                    now=now,
                )
            if classification == "recorded":
                return self.ledger.update_thread_delivery(
                    record.monitor_id,
                    expected=(record.state,),
                    state=MonitorState.CANCELLATION_TOO_LATE,
                    delivery_state=ThreadDeliveryState.CANCELLATION_TOO_LATE,
                    reconciliation=reconciliation,
                    delivery_outcome={"kind": "cancellation_too_late", "at": now},
                    now=now,
                )
            if (
                classification == "queued"
                and record.state == MonitorState.CANCEL_REQUESTED
            ):
                queue = observation.get("queue")
                item_id = None
                if isinstance(queue, list):
                    for item in queue:
                        if isinstance(item, Mapping) and isinstance(
                            item.get("item_id"), str
                        ):
                            item_id = str(item["item_id"])
                            break
                if item_id is None and record.queue_receipt is not None:
                    receipt_item = record.queue_receipt.get("item_id")
                    if isinstance(receipt_item, str):
                        item_id = receipt_item
                if item_id is not None:
                    reconciliation["deletion_attempted_at"] = now
                    reconciliation["deletion_item_id"] = item_id
                    begun = self.ledger.begin_thread_cancellation(
                        record.monitor_id,
                        reconciliation=reconciliation,
                        now=now,
                    )
                    if begun is None:
                        return None
                    try:
                        async with self.thread_delivery_factory() as remover:
                            deleted = await remover.delete_thread_delivery(
                                thread_id, item_id
                            )
                    except AppServerError as exc:
                        reconciliation["deletion_result"] = "transport_uncertain"
                        reconciliation["deletion_error"] = str(exc)
                        return self.ledger.update_thread_delivery(
                            record.monitor_id,
                            expected=(MonitorState.CANCELLATION_IN_PROGRESS,),
                            state=MonitorState.CANCELLATION_IN_PROGRESS,
                            delivery_state=ThreadDeliveryState.CANCELLATION_IN_PROGRESS,
                            reconciliation=reconciliation,
                            now=now,
                        )
                    if deleted:
                        return self.ledger.update_thread_delivery(
                            record.monitor_id,
                            expected=(MonitorState.CANCELLATION_IN_PROGRESS,),
                            state=MonitorState.CANCELLED,
                            delivery_state=ThreadDeliveryState.CANCELLED,
                            reconciliation={
                                **reconciliation,
                                "deletion_result": "removed",
                            },
                            delivery_outcome={"kind": "cancelled", "at": now},
                            now=now,
                        )
                    reconciliation["deletion_result"] = "not_found"
                    return self.ledger.update_thread_delivery(
                        record.monitor_id,
                        expected=(MonitorState.CANCELLATION_IN_PROGRESS,),
                        state=MonitorState.CANCELLATION_IN_PROGRESS,
                        delivery_state=ThreadDeliveryState.CANCELLATION_IN_PROGRESS,
                        reconciliation=reconciliation,
                        now=now,
                    )
            if classification == "absent":
                online_seconds = float(
                    reconciliation.get("online_absence_seconds", 0.0)
                )
                prior_online_at = (record.reconciliation or {}).get(
                    "last_online_at"
                )
                if isinstance(prior_online_at, (int, float)):
                    online_seconds += max(0.0, now - float(prior_online_at))
                reconciliation["online_absence_seconds"] = online_seconds
                reconciliation["last_online_at"] = now
                if online_seconds >= 60.0:
                    return self.ledger.update_thread_delivery(
                        record.monitor_id,
                        expected=(record.state,),
                        state=MonitorState.DELIVERY_UNCERTAIN,
                        delivery_state=ThreadDeliveryState.DELIVERY_UNCERTAIN,
                        reconciliation=reconciliation,
                        delivery_outcome={
                            "kind": "cancellation_uncertain",
                            "absence_kind": "unresolved_absence",
                            "at": now,
                        },
                        now=now,
                    )
            elif classification == "queued":
                reconciliation["online_absence_seconds"] = 0.0
                reconciliation["last_online_at"] = now
            # A consumed removal attempt is never repeated. Continued queue
            # presence or absence is reconciled in later passes.
            return self.ledger.update_thread_delivery(
                record.monitor_id,
                expected=(record.state,),
                state=record.state,
                delivery_state=(
                    ThreadDeliveryState.CANCEL_REQUESTED
                    if record.state == MonitorState.CANCEL_REQUESTED
                    else ThreadDeliveryState.CANCELLATION_IN_PROGRESS
                ),
                reconciliation=reconciliation,
                now=now,
            )
        if classification == "delivery_modified":
            return self.ledger.update_thread_delivery(
                record.monitor_id,
                expected=(
                    MonitorState.ADMISSION_IN_PROGRESS,
                    MonitorState.QUEUE_ACCEPTED,
                ),
                state=MonitorState.DELIVERY_MODIFIED,
                delivery_state=ThreadDeliveryState.DELIVERY_MODIFIED,
                reconciliation=reconciliation,
                delivery_outcome={
                    "kind": "delivery_modified",
                    "observed_pointer_count": observation.get(
                        "observed_pointer_count", 0
                    ),
                    "at": now,
                },
                now=now,
            )
        if classification == "recorded":
            return self.ledger.update_thread_delivery(
                record.monitor_id,
                expected=(
                    MonitorState.ADMISSION_IN_PROGRESS,
                    MonitorState.QUEUE_ACCEPTED,
                ),
                state=MonitorState.RECORDED,
                delivery_state=ThreadDeliveryState.RECORDED,
                reconciliation=reconciliation,
                delivery_outcome={
                    "kind": "recorded",
                    "observed_pointer_count": observation.get(
                        "observed_pointer_count", 0
                    ),
                    "at": now,
                    **self._wake_report(record, fired_at=now),
                },
                now=now,
            )
        if classification == "queued":
            reconciliation["online_absence_seconds"] = 0.0
            reconciliation["last_online_at"] = now
            if observation.get("interrupted"):
                reconciliation["stall"] = "stalled_interrupted"
            if record.queue_receipt is None:
                matching_queue = observation.get("queue")
                recovered_item = (
                    matching_queue[0]
                    if isinstance(matching_queue, list)
                    and matching_queue
                    and isinstance(matching_queue[0], Mapping)
                    else None
                )
                recovered_item_id = (
                    recovered_item.get("item_id")
                    if recovered_item is not None
                    else None
                )
                if not isinstance(recovered_item_id, str) or not recovered_item_id:
                    raise AppServerError(
                        "queued delivery observation has no stable queue item identity"
                    )
                accepted = self.ledger.record_thread_queue_ack(
                    record.monitor_id,
                    receipt={
                        "item_id": recovered_item_id,
                        "client_user_message_id": delivery_id,
                        "pointer_digest": pointer_digest,
                        "recovered_from_queue": True,
                    },
                    now=now,
                )
                if accepted is None:
                    return None
                record = accepted
                if observation.get("runtime_status") == "notLoaded":
                    try:
                        async with self.thread_delivery_factory() as resumer:
                            queue = await resumer.list_thread_queue(thread_id)
                            item_index = next(
                                (
                                    index
                                    for index, item in enumerate(queue)
                                    if item.get("item_id") == recovered_item_id
                                ),
                                None,
                            )
                            if item_index is None:
                                reconciliation.update(
                                    {
                                        "classification": "pre_resume_pointer_absent",
                                        "pre_resume_item_count": len(queue),
                                        "pre_resume_item_ids": [
                                            str(item.get("item_id")) for item in queue
                                        ],
                                        "resume_attempted_at": None,
                                    }
                                )
                            else:
                                ahead = queue[:item_index]
                                reconciliation.update(
                                    {
                                        "classification": "pre_resume_snapshot",
                                        "pre_resume_item_count": len(ahead),
                                        "pre_resume_item_ids": [
                                            str(item.get("item_id")) for item in ahead
                                        ],
                                        "resume_attempted_at": now,
                                    }
                                )
                            persisted = self.ledger.update_thread_delivery(
                                record.monitor_id,
                                expected=(MonitorState.QUEUE_ACCEPTED,),
                                state=MonitorState.QUEUE_ACCEPTED,
                                delivery_state=ThreadDeliveryState.QUEUE_ACCEPTED,
                                reconciliation=reconciliation,
                                now=now,
                            )
                            if persisted is None:
                                return None
                            record = persisted
                            if item_index is not None:
                                await resumer.resume_thread_delivery(thread_id)
                    except AppServerError as exc:
                        reconciliation["resume_result"] = "transport_uncertain"
                        reconciliation["resume_error"] = str(exc)
                        updated = self.ledger.update_thread_delivery(
                            record.monitor_id,
                            expected=(MonitorState.QUEUE_ACCEPTED,),
                            state=MonitorState.QUEUE_ACCEPTED,
                            delivery_state=ThreadDeliveryState.QUEUE_ACCEPTED,
                            reconciliation=reconciliation,
                            now=now,
                        )
                        if updated is not None:
                            record = updated
            return self.ledger.update_thread_delivery(
                record.monitor_id,
                expected=(
                    MonitorState.ADMISSION_IN_PROGRESS,
                    MonitorState.QUEUE_ACCEPTED,
                ),
                state=MonitorState.QUEUE_ACCEPTED,
                delivery_state=ThreadDeliveryState.QUEUE_ACCEPTED,
                reconciliation=reconciliation,
                now=now,
            )

        previous_online = float(reconciliation.get("online_absence_seconds", 0.0))
        prior_online_at = (record.reconciliation or {}).get("last_online_at")
        if isinstance(prior_online_at, (int, float)):
            previous_online += max(0.0, now - float(prior_online_at))
        reconciliation["online_absence_seconds"] = previous_online
        reconciliation["last_online_at"] = now
        if previous_online >= 60.0:
            outcome = {
                "kind": "delivery_uncertain",
                "absence_kind": "unresolved_absence",
                "possible_causes": [
                    "user_delete",
                    "app_server_crash",
                    "archive",
                    "storage_failure",
                ],
                "at": now,
            }
            return self.ledger.update_thread_delivery(
                record.monitor_id,
                expected=(
                    MonitorState.ADMISSION_IN_PROGRESS,
                    MonitorState.QUEUE_ACCEPTED,
                ),
                state=MonitorState.DELIVERY_UNCERTAIN,
                delivery_state=ThreadDeliveryState.DELIVERY_UNCERTAIN,
                reconciliation=reconciliation,
                delivery_outcome=outcome,
                now=now,
            )
        return self.ledger.update_thread_delivery(
            record.monitor_id,
            expected=(
                MonitorState.ADMISSION_IN_PROGRESS,
                MonitorState.QUEUE_ACCEPTED,
            ),
            state=record.state,
            delivery_state=(
                ThreadDeliveryState.ADMISSION_IN_PROGRESS
                if record.state == MonitorState.ADMISSION_IN_PROGRESS
                else ThreadDeliveryState.QUEUE_ACCEPTED
            ),
            reconciliation=reconciliation,
            now=now,
        )

    async def _finish_claim(self, record: MonitorRecord) -> MonitorRecord | None:
        # `mode`, not the stored wake reason, decides the two deferred skips: a
        # deferred row claimed before this change carries a NULL reason after
        # migration, and gating on NULL would strand it exactly as before.
        if record.delivery_kind == DeliveryKind.THREAD:
            return await self._finish_thread_delivery(record)
        deferred = record.mode == MonitorMode.DEFERRED
        assert record.target is not None
        if not deferred and not witness_authorizes_continuation(
            record.witness,
            allow_heuristic_continuation=record.allow_heuristic_continuation,
        ):
            return self.ledger.transition(
                record.monitor_id,
                expected=(MonitorState.CLAIMED,),
                state=MonitorState.SATISFIED_REQUIRES_AUTHORIZATION,
                outcome={
                    "kind": "satisfied_requires_authorization",
                    "witness": list(record.witness),
                    "at": time.time(),
                },
            )
        try:
            async with self.app_server_factory() as app_server:
                observation = await app_server.read_observation(record.target.thread_id)
                if not observation.is_loaded:
                    return self.ledger.transition(
                        record.monitor_id,
                        expected=(MonitorState.CLAIMED,),
                        state=MonitorState.UNLOADED_TARGET,
                        outcome={
                            "kind": "unloaded_target",
                            "observation": self._observation_summary(observation),
                            "at": time.time(),
                        },
                    )
                if not observation.activation_eligible(record.target):
                    return self.ledger.transition(
                        record.monitor_id,
                        expected=(MonitorState.CLAIMED,),
                        state=MonitorState.SUPERSEDED,
                        outcome={
                            "kind": "activation_guard_changed",
                            "observation": self._observation_summary(observation),
                            "at": time.time(),
                        },
                    )
                activation_now = self.observer_context_factory().now()
                if not deferred and activation_now >= record.expires_at:
                    return self.ledger.transition(
                        record.monitor_id,
                        expected=(MonitorState.CLAIMED,),
                        state=MonitorState.EXPIRED,
                        outcome={
                            "kind": "expired_before_activation",
                            "at": activation_now,
                        },
                    )
                activating = self.ledger.begin_activation(
                    record.monitor_id,
                    activation_snapshot={
                        "target": self._observation_summary(observation),
                        "goal_snapshot": dict(observation.goal_snapshot or {}),
                        "at": time.time(),
                    },
                )
                if activating is None:
                    return None
                try:
                    returned = await app_server.activate_guarded_goal(record.target.thread_id)
                except AppServerError as exc:
                    return self.ledger.transition(
                        record.monitor_id,
                        expected=(MonitorState.ACTIVATING,),
                        state=MonitorState.ACTIVATION_UNCERTAIN,
                        outcome={"kind": "activation_transport_uncertain", "error": str(exc), "at": time.time()},
                    )
        except AppServerError as exc:
            if deferred:
                # Nothing has been sent yet, so this read failure is safely
                # retryable. Terminating here would strand the paused goal.
                self.ledger.transition(
                    record.monitor_id,
                    expected=(MonitorState.CLAIMED,),
                    state=MonitorState.CLAIMED,
                    outcome={
                        "kind": "deferred_preflight_retryable",
                        "error": str(exc),
                        "at": time.time(),
                    },
                )
                return None
            return self.ledger.transition(
                record.monitor_id,
                expected=(MonitorState.CLAIMED,),
                state=MonitorState.ACTIVATION_FAILED,
                outcome={"kind": "preflight_failed", "error": str(exc), "at": time.time()},
            )

        if (
            returned.thread_id != record.target.thread_id
            or returned.goal != record.target.goal
        ):
            return self.ledger.transition(
                record.monitor_id,
                expected=(MonitorState.ACTIVATING,),
                state=MonitorState.MIS_TARGETED_ACTIVATION,
                outcome={
                    "kind": "mis_targeted_activation",
                    "captured_thread_id": record.target.thread_id,
                    "returned_thread_id": returned.thread_id,
                    "captured_goal": record.target.goal.as_dict(),
                    "returned_goal": returned.goal.as_dict() if returned.goal else None,
                    "returned_status": returned.goal_status,
                    "at": time.time(),
                },
            )
        if returned.goal_status != "active":
            return self.ledger.transition(
                record.monitor_id,
                expected=(MonitorState.ACTIVATING,),
                state=MonitorState.ACTIVATION_FAILED,
                outcome={
                    "kind": "activation_not_confirmed",
                    "returned_status": returned.goal_status,
                    "at": time.time(),
                },
            )
        fired_at = time.time()
        return self.ledger.transition(
            record.monitor_id,
            expected=(MonitorState.ACTIVATING,),
            state=MonitorState.FIRED,
            outcome={
                "kind": "activation_confirmed",
                "returned_goal": returned.goal.as_dict() if returned.goal else None,
                "at": fired_at,
                **self._wake_report(record, fired_at=fired_at),
            },
        )

    @staticmethod
    def _observation_summary(observation: TargetObservation) -> dict[str, Any]:
        return {
            "thread_id": observation.thread_id,
            "runtime_status": observation.runtime_status,
            "is_loaded": observation.is_loaded,
            "goal_status": observation.goal_status,
            "goal": observation.goal.as_dict() if observation.goal else None,
        }

    @classmethod
    def _receipt_leaves(cls, condition: Mapping[str, Any]) -> list[Mapping[str, Any]]:
        if condition.get("type") == "receipt_success":
            return [condition]
        children = condition.get("children")
        if not isinstance(children, list):
            return []
        return [leaf for child in children if isinstance(child, Mapping) for leaf in cls._receipt_leaves(child)]

    @classmethod
    def _receipt_instructions(cls, condition: Mapping[str, Any]) -> list[dict[str, str]]:
        return [
            {"path": str(leaf["path"]), "token": str(leaf["token"])}
            for leaf in cls._receipt_leaves(condition)
        ]

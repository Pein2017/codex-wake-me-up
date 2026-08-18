"""Monitor registration, reconciliation, and guarded one-shot activation."""

from __future__ import annotations

import copy
import math
import secrets
import time
import uuid
from pathlib import Path
from typing import Any, AsyncContextManager, Callable, Mapping, MutableMapping, Sequence

from .app_server import AppServerClient
from .conditions import (
    ObserverContext,
    elide_condition_journals,
    evaluate_condition,
    journal_tail,
    prepare_condition,
    public_condition_semantics,
    receipt_payload,
    thread_idle_targets,
    witness_authorizes_continuation,
)
from .ledger import Ledger, MonitorRecord
from .models import (
    AppServerError,
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
    runtime_root,
)


AppServerFactory = Callable[[], AsyncContextManager[AppServerClient]]
ObserverContextFactory = Callable[[], ObserverContext]

# The repo's long-poll yield floor: what one avoided polling turn would cost.
POLL_YIELD_FLOOR_SECONDS = 180.0
WAKE_JOURNAL_TAIL_LINES = 20
MAX_REARM_CHAIN = 20


class MonitorService:
    """The single integration owner of durable monitor semantics."""

    def __init__(
        self,
        root: Path | None = None,
        *,
        ledger: Ledger | None = None,
        app_server_factory: AppServerFactory | None = None,
        observer_context_factory: ObserverContextFactory | None = None,
        daemon_starter: Callable[[Path], bool] = ensure_daemon,
        daemon_readiness: Callable[[Path], bool] = ensure_daemon_ready,
    ):
        self.root = root or runtime_root()
        self.ledger = ledger or Ledger(self.root)
        self.app_server_factory = app_server_factory or (
            lambda: AppServerClient(codex_home_for_runtime_root(self.root))
        )
        self.observer_context_factory = observer_context_factory or (
            lambda: ObserverContext(runtime_root=self.root)
        )
        self.daemon_starter = daemon_starter
        self.daemon_readiness = daemon_readiness

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
            and observation.goal_status == "active"
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

        observer_context = self.observer_context_factory()
        async with self.app_server_factory() as app_server:
            first = await app_server.read_observation(thread_id)
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
            record, created = self.ledger.create_or_get(
                monitor_id=monitor_id,
                idempotency_key=idempotency_key,
                semantic=semantic,
                target=guard,
                condition=prepared_condition,
                allow_heuristic_continuation=allow_heuristic_continuation,
                expires_at=observer_context.now() + float(expires_in_seconds),
                rearm_of=rearm_of,
            )
            if created:
                second = await app_server.read_observation(thread_id)
                if self._same_guard(first, second):
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
        """Best-effort pause of one explicit loaded active goal into a monitor."""

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

        observer_context = self.observer_context_factory()
        async with self.app_server_factory() as app_server:
            first = await app_server.read_observation(thread_id)
            if not first.is_loaded or first.goal is None:
                raise ValidationError(
                    "defer target must be locally loaded and own a non-null goal"
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
                    "defer target must own an active goal before pause"
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
                )
                if not created:
                    response = self._registration_response(record)
                    if record.state == MonitorState.ARMED:
                        response["next_action"] = "end_current_turn"
                    return response

                try:
                    ready = self.daemon_readiness(self.root)
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
        if record.mode == MonitorMode.DEFERRED:
            response["targeting"] = {
                "kind": "best_effort_explicit_thread_id",
                "authenticated_current_task": False,
            }
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
        for record in self.ledger.list(include_terminal=False):
            if record.state not in {MonitorState.ARMED, MonitorState.CLAIMED}:
                continue
            try:
                if record.expires_at <= now and record.mode == MonitorMode.LEGACY:
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
                outcome = await self._finish_claim(record)
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
            current.mode == MonitorMode.DEFERRED
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
        if evaluation.value == TriState.TRUE:
            wake_reason: WakeReason | None = None
            if deferred:
                wake_reason = (
                    WakeReason.CONDITION
                    if witness_authorizes_continuation(
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
            if deferred:
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
        if deferred and observer_context.now() >= record.expires_at:
            return self.ledger.claim(
                record.monitor_id,
                condition=condition,
                evidence=evaluation.evidence,
                witness=evaluation.witness,
                wake_reason=WakeReason.EXPIRED,
            )
        return None

    async def _finish_claim(self, record: MonitorRecord) -> MonitorRecord | None:
        # `mode`, not the stored wake reason, decides the two deferred skips: a
        # deferred row claimed before this change carries a NULL reason after
        # migration, and gating on NULL would strand it exactly as before.
        deferred = record.mode == MonitorMode.DEFERRED
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

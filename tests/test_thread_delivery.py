from __future__ import annotations

import asyncio
import hashlib
import subprocess
from typing import cast

import pytest

import codex_wake_me_up.service as service_module
from codex_wake_me_up.app_server import AppServerClient
from codex_wake_me_up.conditions import ObserverContext
from codex_wake_me_up.delivery import (
    DELIVERY_POINTER_MAX_CHARS,
    DeliveryKind,
    ThreadDeliveryState,
    build_thread_delivery,
)
from codex_wake_me_up.daemon import (
    assert_delivery_runtime_compatible,
    assert_delivery_schema_compatible,
)
from codex_wake_me_up.ledger import Ledger
from codex_wake_me_up.models import (
    AppServerError,
    AppServerRejectedError,
    AppServerTransportError,
    ConflictError,
    GoalMarker,
    MonitorState,
    TargetGuard,
    ValidationError,
)
from codex_wake_me_up.service import AppServerFactory, MonitorService

from .helpers import Clock, FakeAppServer, observation


def _git(repository, *arguments: str) -> str:
    return subprocess.run(
        ("git", "-C", str(repository), *arguments),
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


def _git_commit(repository, name: str, content: str, message: str) -> str:
    (repository / name).write_text(content, encoding="utf-8")
    _git(repository, "add", name)
    _git(repository, "commit", "-m", message)
    return _git(repository, "rev-parse", "HEAD")


class FakeThreadDelivery:
    def __init__(
        self,
        *,
        loaded: bool = True,
        origin_thread_id: str | None = None,
        delivery_thread_id: str | None = None,
        relay_chain: list[str] | None = None,
        add_result: dict | Exception | None = None,
        inspections: list[dict | Exception] | None = None,
        queue: list[dict] | None = None,
    ) -> None:
        self.loaded = loaded
        self.origin_thread_id = origin_thread_id
        self.delivery_thread_id = delivery_thread_id
        self.relay_chain = relay_chain
        self.add_result = add_result or {
            "item_id": "queue-1",
            "client_user_message_id": "unused",
            "pointer": "unused",
        }
        self.inspections = list(inspections or [])
        self.last_inspection = {
            "classification": "queued",
            "runtime_status": "idle" if loaded else "notLoaded",
            "interrupted": False,
            "history_matches": 0,
            "history_modified": 0,
            "queue_matches": 1,
            "queue_modified": 0,
            "observed_pointer_count": 1,
            "history": [],
            "queue": [{"item_id": "queue-1", "position": 0}],
            "queue_count": 1,
        }
        self.queue = list(queue or [])
        self.preflight_calls: list[str] = []
        self.add_calls: list[dict] = []
        self.resume_calls: list[str] = []
        self.delete_calls: list[tuple[str, str]] = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args):
        return None

    async def preflight_thread_delivery(self, thread_id: str) -> dict:
        self.preflight_calls.append(thread_id)
        return {
            "thread_id": thread_id,
            "runtime_status": "idle" if self.loaded else "notLoaded",
            "loaded": self.loaded,
            "can_accept_direct_input": True if self.loaded else None,
            "source": "appServer",
            "queue_count": len(self.queue),
            "queue_capacity": 100,
            "codex_version": "0.148.0",
            "server": {"version": "0.148.0"},
        }

    async def resolve_thread_delivery(self, thread_id: str) -> dict:
        origin = self.origin_thread_id or thread_id
        target = self.delivery_thread_id or thread_id
        chain = self.relay_chain or ([origin] if origin == target else [origin, target])
        capability = await self.preflight_thread_delivery(target)
        return {
            **capability,
            "origin_thread_id": origin,
            "delivery_thread_id": target,
            "relay_chain": chain,
            "relayed_to_root": origin != target,
        }

    async def add_thread_delivery(self, **request) -> dict:
        self.add_calls.append(dict(request))
        if isinstance(self.add_result, Exception):
            raise self.add_result
        return {
            "item_id": self.add_result["item_id"],
            "client_user_message_id": request["delivery_id"],
            "pointer": request["pointer"],
        }

    async def inspect_thread_delivery(self, **_request) -> dict:
        if self.inspections:
            inspection = self.inspections.pop(0)
            if isinstance(inspection, Exception):
                raise inspection
            self.last_inspection = inspection
        return dict(self.last_inspection)

    async def list_thread_queue(self, _thread_id: str) -> list[dict]:
        return [dict(item) for item in self.queue]

    async def resume_thread_delivery(self, thread_id: str) -> dict:
        self.resume_calls.append(thread_id)
        self.loaded = True
        return {
            "thread_id": thread_id,
            "runtime_status": "idle",
            "can_accept_direct_input": True,
        }

    async def delete_thread_delivery(self, thread_id: str, item_id: str) -> bool:
        self.delete_calls.append((thread_id, item_id))
        return True


def _native_worker_snapshot(
    status: str,
    *,
    task_name: str = "/root/worker",
    child_thread_id: str | None = "child-1",
    invocation_id: str | None = "turn-1",
    error: str | None = None,
) -> dict:
    snapshot = {"task_name": task_name, "status": status}
    if child_thread_id is not None:
        snapshot["child_thread_id"] = child_thread_id
    if invocation_id is not None:
        snapshot["invocation_id"] = invocation_id
    if error is not None:
        snapshot["error"] = error
    return snapshot


def _thread_service(
    tmp_path,
    adapter: FakeThreadDelivery,
    clock: Clock,
    *,
    observer_adapter: FakeAppServer | None = None,
    thread_ready=lambda _root: True,
) -> MonitorService:
    return MonitorService(
        tmp_path,
        ledger=Ledger(tmp_path),
        app_server_factory=cast(
            AppServerFactory, lambda: observer_adapter or FakeAppServer([])
        ),
        thread_delivery_factory=cast(AppServerFactory, lambda: adapter),
        observer_context_factory=lambda: ObserverContext(
            runtime_root=tmp_path, now=clock.now
        ),
        daemon_starter=lambda _root: False,
        daemon_readiness=lambda _root: True,
        event_daemon_readiness=lambda _root: True,
        thread_delivery_readiness=thread_ready,
    )


def test_thread_delivery_records_daemon_unavailable_when_retirement_wins_second_gate(
    tmp_path,
) -> None:
    readiness = iter((True, False))
    service = _thread_service(
        tmp_path,
        FakeThreadDelivery(),
        Clock(),
        thread_ready=lambda _root: next(readiness),
    )

    result = asyncio.run(
        service.wait_for_event(
            thread_id="thread-1",
            condition={"type": "time", "after_seconds": 10},
            expires_in_seconds=100,
            idempotency_key="retiring-delivery",
            start_daemon=False,
        )
    )

    assert result["state"] == MonitorState.DAEMON_UNAVAILABLE
    assert result["outcome"]["kind"] == "delivery_daemon_mismatch_before_arm"


def test_thread_delivery_readiness_failure_reports_current_stage_and_reason(
    tmp_path,
) -> None:
    service = _thread_service(
        tmp_path,
        FakeThreadDelivery(),
        Clock(),
        thread_ready=lambda _root: False,
    )

    with pytest.raises(
        ValidationError,
        match="stage=delivery_daemon_readiness reason=missing_heartbeat",
    ):
        asyncio.run(
            service.wait_for_event(
                thread_id="thread-1",
                condition={"type": "time", "after_seconds": 10},
                expires_in_seconds=100,
                idempotency_key="daemon-not-ready",
                start_daemon=False,
            )
        )


def test_thread_delivery_resolution_timeout_reports_stage_before_row_creation(
    tmp_path,
) -> None:
    class TimedOutDelivery(FakeThreadDelivery):
        async def resolve_thread_delivery(self, _thread_id: str) -> dict:
            raise AppServerError("app-server thread/read timed out")

    service = _thread_service(tmp_path, TimedOutDelivery(), Clock())

    with pytest.raises(AppServerError, match="stage=resolve_delivery_target"):
        asyncio.run(
            service.wait_for_event(
                thread_id="thread-1",
                condition={"type": "time", "after_seconds": 10},
                expires_in_seconds=100,
                idempotency_key="resolution-timeout",
                start_daemon=False,
            )
        )
    assert service.ledger.list() == []


def test_thread_delivery_retries_one_typed_prearm_transport_failure(tmp_path) -> None:
    class FlakyDelivery(FakeThreadDelivery):
        def __init__(self) -> None:
            super().__init__()
            self.resolve_calls = 0

        async def resolve_thread_delivery(self, thread_id: str) -> dict:
            self.resolve_calls += 1
            if self.resolve_calls == 1:
                raise AppServerTransportError("app-server thread/read timed out")
            return await super().resolve_thread_delivery(thread_id)

    adapter = FlakyDelivery()
    service = _thread_service(tmp_path, adapter, Clock())

    receipt = asyncio.run(
        service.wait_for_event(
            thread_id="thread-1",
            condition={"type": "time", "after_seconds": 10},
            expires_in_seconds=100,
            idempotency_key="retry-prearm-transport",
            start_daemon=False,
        )
    )

    assert receipt["state"] == MonitorState.ARMED
    assert adapter.resolve_calls == 2
    assert len(service.ledger.list()) == 1


def test_thread_delivery_app_server_read_budget_is_shared_and_rowless(
    tmp_path, monkeypatch
) -> None:
    class SlowDelivery(FakeThreadDelivery):
        async def resolve_thread_delivery(self, thread_id: str) -> dict:
            await asyncio.sleep(0.02)
            return await super().resolve_thread_delivery(thread_id)

    class SlowObserver(FakeAppServer):
        async def read_observation(self, thread_id: str, *, include_goal: bool = True):
            await asyncio.sleep(0.02)
            return await super().read_observation(
                thread_id, include_goal=include_goal
            )

    monkeypatch.setattr(service_module, "PREARM_REGISTRATION_BUDGET_SECONDS", 0.03)
    delivery = SlowDelivery()
    service = _thread_service(
        tmp_path,
        delivery,
        Clock(),
        observer_adapter=SlowObserver(
            [], thread_observations={"child": observation(thread_id="child")}
        ),
    )

    with pytest.raises(AppServerError) as caught:
        asyncio.run(
            service.wait_for_event(
                thread_id="thread-1",
                condition={"type": "thread_idle", "thread_id": "child"},
                expires_in_seconds=100,
                idempotency_key="shared-prearm-budget",
                start_daemon=False,
            )
        )

    error = caught.value
    assert getattr(error, "error_kind", None) == "timeout"
    assert getattr(error, "stage", None) == "prepare_condition"
    assert getattr(error, "budget_seconds", None) == 0.03
    assert getattr(error, "safe_to_retry", None) is True
    assert getattr(error, "retry_attempted", None) is False
    assert error.as_dict()["budget_seconds"] == 0.03
    assert service.ledger.list() == []
    assert delivery.add_calls == []


def test_target_status_observation_is_not_acceptance_and_list_is_scoped(tmp_path) -> None:
    clock = Clock()
    service = _thread_service(tmp_path, FakeThreadDelivery(), clock)
    receipt = asyncio.run(
        service.wait_for_event(
            thread_id="thread-1",
            condition={"type": "time", "after_seconds": 1},
            expires_in_seconds=100,
            idempotency_key="target-status-observed",
            start_daemon=False,
        )
    )
    clock.value = 102.0
    asyncio.run(service.reconcile_once())

    wrong = service.decision_status(
        receipt["monitor_id"], trusted_target_thread_id="another-thread"
    )
    assert "target_status_observed_at" not in wrong["delivery"]

    decision = service.decision_status(
        receipt["monitor_id"], trusted_target_thread_id="thread-1"
    )
    assert decision["delivery"]["target_status_observed_at"] == clock.value
    assert decision.get("lead_accepted") is not True
    assert decision.get("task_success") is not True
    listed = service.list_for_target("thread-1")
    assert [item["monitor_id"] for item in listed] == [receipt["monitor_id"]]
    assert listed[0]["condition"]["type"] == "time"
    assert listed[0]["delivery"]["state"] == ThreadDeliveryState.QUEUE_ACCEPTED
    assert listed[0]["delivery"]["classification"] == "queued"
    assert listed[0]["delivery"]["queue_receipt"]["item_id"] == "queue-1"
    assert listed[0]["supervision"] in {"healthy", "unsupervised"}
    assert listed[0]["created_at"] <= listed[0]["updated_at"]
    assert listed[0]["expires_at"] == 200.0
    assert service.list_for_target("another-thread") == []


def test_thread_delivery_exhausted_prearm_transport_is_structured_and_rowless(
    tmp_path,
) -> None:
    class TimedOutDelivery(FakeThreadDelivery):
        async def resolve_thread_delivery(self, _thread_id: str) -> dict:
            raise AppServerTransportError("app-server thread/read timed out")

    service = _thread_service(tmp_path, TimedOutDelivery(), Clock())

    with pytest.raises(AppServerError) as caught:
        asyncio.run(
            service.wait_for_event(
                thread_id="thread-1",
                condition={"type": "time", "after_seconds": 10},
                expires_in_seconds=100,
                idempotency_key="exhaust-prearm-transport",
                start_daemon=False,
            )
        )

    assert getattr(caught.value, "stage", None) == "resolve_delivery_target"
    assert getattr(caught.value, "monitor_created", None) is False
    assert getattr(caught.value, "safe_to_retry", None) is True
    assert service.ledger.list() == []


def test_thread_condition_read_timeout_reports_stage_before_row_creation(
    tmp_path,
) -> None:
    service = _thread_service(
        tmp_path,
        FakeThreadDelivery(),
        Clock(),
        observer_adapter=FakeAppServer(
            [],
            thread_observations={
                "child": AppServerError("app-server thread/read timed out")
            },
        ),
    )

    with pytest.raises(AppServerError, match="stage=prepare_condition"):
        asyncio.run(
            service.wait_for_event(
                thread_id="thread-1",
                condition={"type": "thread_idle", "thread_id": "child"},
                expires_in_seconds=100,
                idempotency_key="condition-timeout",
                start_daemon=False,
            )
        )
    assert service.ledger.list() == []


def test_native_worker_bind_pending_does_not_arm_ambiguous_subscription(
    tmp_path,
) -> None:
    observer = FakeAppServer(
        [],
        native_worker_observations={
            "/root/worker": _native_worker_snapshot(
                "bindPending", child_thread_id=None, invocation_id=None
            )
        },
    )
    service = _thread_service(
        tmp_path, FakeThreadDelivery(), Clock(), observer_adapter=observer
    )

    with pytest.raises(ValidationError, match="has no exact invocation yet"):
        asyncio.run(
            service.wait_for_current_event(
                thread_id="root-1",
                condition={
                    "type": "native_worker_terminal",
                    "task_name": "/root/worker",
                },
                expires_in_seconds=100,
                idempotency_key="native-bind-pending",
                start_daemon=False,
            )
        )

    assert service.ledger.list() == []


def test_native_worker_requires_trusted_current_root_binding(tmp_path) -> None:
    observer = FakeAppServer(
        [],
        native_worker_observations={
            "/root/worker": _native_worker_snapshot("running")
        },
    )
    service = _thread_service(
        tmp_path, FakeThreadDelivery(), Clock(), observer_adapter=observer
    )

    with pytest.raises(ValidationError, match="trusted current root"):
        asyncio.run(
            service.wait_for_event(
                thread_id="root-1",
                condition={
                    "type": "native_worker_terminal",
                    "task_name": "/root/worker",
                },
                expires_in_seconds=100,
                idempotency_key="native-untrusted-root",
                start_daemon=False,
            )
        )

    assert observer.native_worker_reads == []
    assert service.ledger.list() == []


def test_native_worker_completed_before_arm_is_latched_and_wakes_once(tmp_path) -> None:
    delivery = FakeThreadDelivery()
    observer = FakeAppServer(
        [],
        native_worker_observations={
            "/root/worker": _native_worker_snapshot("completed")
        },
    )
    service = _thread_service(
        tmp_path, delivery, Clock(), observer_adapter=observer
    )

    receipt = asyncio.run(
        service.wait_for_current_event(
            thread_id="root-1",
            condition={
                "type": "native_worker_terminal",
                "task_name": "/root/worker",
            },
            expires_in_seconds=100,
            idempotency_key="native-already-completed",
            start_daemon=False,
        )
    )
    asyncio.run(service.reconcile_once())
    decision = service.decision_status(receipt["monitor_id"])

    assert decision["state"] == MonitorState.QUEUE_ACCEPTED
    witness = decision["witness"][0]["evidence"]
    assert witness["status"] == "completed"
    assert witness["task_success"] is False
    assert witness["lead_accepted"] is False
    assert len(observer.native_worker_reads) == 1
    assert len(delivery.add_calls) == 1


@pytest.mark.parametrize("terminal_status", ["completed", "failed", "interrupted"])
def test_native_worker_exact_invocation_terminal_statuses_are_settlement_only(
    tmp_path, terminal_status
) -> None:
    delivery = FakeThreadDelivery()
    observer = FakeAppServer(
        [],
        native_worker_observations={
            "/root/worker": [
                _native_worker_snapshot("running"),
                _native_worker_snapshot(terminal_status),
            ]
        },
    )
    service = _thread_service(
        tmp_path, delivery, Clock(), observer_adapter=observer
    )
    receipt = asyncio.run(
        service.wait_for_current_event(
            thread_id="root-1",
            condition={
                "type": "native_worker_terminal",
                "task_name": "/root/worker",
            },
            expires_in_seconds=100,
            idempotency_key=f"native-{terminal_status}",
            start_daemon=False,
        )
    )

    asyncio.run(service.reconcile_once())
    decision = service.decision_status(receipt["monitor_id"])

    assert observer.native_worker_reads[-1] == {
        "root_thread_id": "root-1",
        "task_name": "/root/worker",
        "child_thread_id": "child-1",
        "invocation_id": "turn-1",
    }
    assert decision["state"] == MonitorState.QUEUE_ACCEPTED
    witness = decision["witness"][0]["evidence"]
    assert witness["status"] == terminal_status
    assert witness["task_success"] is False
    assert witness["lead_accepted"] is False
    assert len(delivery.add_calls) == 1


@pytest.mark.parametrize("failure_status", ["unavailable", "mismatch"])
def test_native_worker_disappearance_or_invocation_mismatch_wakes_fail_closed(
    tmp_path, failure_status
) -> None:
    delivery = FakeThreadDelivery()
    observer = FakeAppServer(
        [],
        native_worker_observations={
            "/root/worker": [
                _native_worker_snapshot("running"),
                _native_worker_snapshot(
                    failure_status, error="exact invocation is unavailable"
                ),
            ]
        },
    )
    service = _thread_service(
        tmp_path, delivery, Clock(), observer_adapter=observer
    )
    receipt = asyncio.run(
        service.wait_for_current_event(
            thread_id="root-1",
            condition={
                "type": "native_worker_terminal",
                "task_name": "/root/worker",
            },
            expires_in_seconds=100,
            idempotency_key=f"native-{failure_status}",
            start_daemon=False,
        )
    )

    asyncio.run(service.reconcile_once())
    decision = service.decision_status(receipt["monitor_id"])

    assert decision["state"] == MonitorState.QUEUE_ACCEPTED
    assert decision["wake_reason"] == "observer_failed"
    assert decision["failure_detail"]["kind"] == failure_status
    assert decision.get("task_success") is not True
    assert len(delivery.add_calls) == 1


@pytest.mark.parametrize(("condition_type", "polls"), [("any", 1), ("all", 2)])
def test_native_worker_composition_uses_one_root_queue_add(
    tmp_path, condition_type, polls
) -> None:
    delivery = FakeThreadDelivery()
    observer = FakeAppServer(
        [],
        native_worker_observations={
            "/root/a": [
                _native_worker_snapshot("running", task_name="/root/a"),
                _native_worker_snapshot("completed", task_name="/root/a"),
            ],
            "/root/b": [
                _native_worker_snapshot("running", task_name="/root/b"),
                _native_worker_snapshot("running", task_name="/root/b"),
                _native_worker_snapshot("failed", task_name="/root/b"),
            ],
        },
    )
    service = _thread_service(
        tmp_path, delivery, Clock(), observer_adapter=observer
    )
    receipt = asyncio.run(
        service.wait_for_current_event(
            thread_id="root-1",
            condition={
                "type": condition_type,
                "children": [
                    {"type": "native_worker_terminal", "task_name": "/root/a"},
                    {"type": "native_worker_terminal", "task_name": "/root/b"},
                ],
            },
            expires_in_seconds=100,
            idempotency_key=f"native-{condition_type}",
            start_daemon=False,
        )
    )

    for index in range(polls):
        asyncio.run(service.reconcile_once())
        if condition_type == "all" and index == 0:
            assert service.status(receipt["monitor_id"])["state"] == MonitorState.ARMED

    assert service.status(receipt["monitor_id"])["state"] == MonitorState.QUEUE_ACCEPTED
    assert len(delivery.add_calls) == 1


def test_thread_pointer_is_stable_bounded_and_contains_no_wake_evidence() -> None:
    """Catches unstable IDs, oversized prompts, and full evidence leaking to Core."""

    first = build_thread_delivery(
        monitor_id="monitor-1",
        thread_id="thread-1",
        capability={"codex_version": "0.148.0", "queue_capacity": 100},
    )
    second = build_thread_delivery(
        monitor_id="monitor-1",
        thread_id="thread-1",
        capability={"codex_version": "0.148.0", "queue_capacity": 100},
    )

    assert first == second
    assert first["kind"] == DeliveryKind.THREAD
    assert first["schema_epoch"] == 1
    assert first["delivery_id"] == "77de7ce7-72be-5f1a-b4ca-5fc2c90e3a77"
    assert first["pointer"] == (
        "[codex-wake-me-up monitor=monitor-1 "
        "delivery=77de7ce7-72be-5f1a-b4ca-5fc2c90e3a77 "
        "origin=thread-1] "
        'Call wake_me_up_status(monitor_id, view="decision") exactly once. '
        "This pointer is not a success claim."
    )
    assert len(first["pointer"]) <= DELIVERY_POINTER_MAX_CHARS
    assert first["pointer_digest"] == hashlib.sha256(
        first["pointer"].encode("utf-8")
    ).hexdigest()
    assert "capability" in first
    assert "witness" not in first["pointer"]
    assert "publish_token" not in first["pointer"]


def test_ledger_consumes_thread_admission_before_send_and_never_reopens_it(
    tmp_path,
) -> None:
    """Catches a crash-recovery branch that could issue a second queue-add."""

    ledger = Ledger(tmp_path)
    delivery = build_thread_delivery(
        monitor_id="monitor-1",
        thread_id="thread-1",
        capability={"codex_version": "0.148.0", "queue_capacity": 100},
    )
    record, created = ledger.create_or_get(
        monitor_id="monitor-1",
        idempotency_key="thread-key",
        semantic={"delivery": delivery, "condition": {"type": "time"}},
        target=None,
        delivery=delivery,
        condition={"type": "time", "deadline_utc": 0},
        allow_heuristic_continuation=False,
        expires_at=10_000,
    )

    assert created
    assert record.delivery_kind == DeliveryKind.THREAD
    assert record.target is None
    assert ledger.arm(record.monitor_id) is not None
    assert ledger.claim(
        record.monitor_id,
        condition=record.condition,
        evidence={"type": "time"},
        witness=[{"type": "time"}],
    ) is not None

    admitted = ledger.begin_thread_admission(
        record.monitor_id,
        capability={"codex_version": "0.148.0", "queue_capacity": 100},
        now=20.0,
    )
    assert admitted is not None
    assert admitted.state == MonitorState.ADMISSION_IN_PROGRESS
    assert admitted.thread_delivery_state == ThreadDeliveryState.ADMISSION_IN_PROGRESS
    assert admitted.admission_attempted_at == 20.0
    assert ledger.begin_thread_admission(
        record.monitor_id,
        capability={"codex_version": "0.148.0", "queue_capacity": 100},
        now=21.0,
    ) is None

    ledger.close()
    recovered = Ledger(tmp_path).get(record.monitor_id)
    assert recovered is not None
    assert recovered.state == MonitorState.ADMISSION_IN_PROGRESS
    assert recovered.admission_attempted_at == 20.0


def test_ledger_rejects_an_ambiguous_goal_guard_plus_thread_delivery(tmp_path) -> None:
    """Catches a row selecting both delivery branches at registration."""

    ledger = Ledger(tmp_path)
    delivery = build_thread_delivery(
        monitor_id="monitor-1",
        thread_id="thread-1",
        capability={"codex_version": "0.148.0", "queue_capacity": 100},
    )
    guard = TargetGuard(
        thread_id="thread-1",
        goal=GoalMarker(created_at=1, objective="legacy", token_budget=16),
    )

    with pytest.raises(ConflictError, match="exactly one delivery kind"):
        ledger.create_or_get(
            monitor_id="monitor-1",
            idempotency_key="ambiguous-key",
            semantic={"delivery": delivery},
            target=guard,
            delivery=delivery,
            condition={"type": "time", "deadline_utc": 0},
            allow_heuristic_continuation=False,
            expires_at=10_000,
        )


def test_experimental_queue_adapter_uses_exact_target_and_delivery_id() -> None:
    """Catches accidental turn-start/steer use or an unstable queue identity."""

    class StubClient(AppServerClient):
        def __init__(self) -> None:
            self.calls: list[tuple[str, dict]] = []
            self.server_info = {
                "codexHome": "/data/CoordExp/.codex",
                "serverInfo": {"name": "codex-app-server", "version": "0.148.0"},
            }

        async def _request(self, method, params):
            self.calls.append((method, dict(params)))
            if method == "thread/read":
                return {
                    "thread": {
                        "id": "thread-1",
                        "status": {"type": "idle"},
                        "canAcceptDirectInput": True,
                        "source": "appServer",
                        "turns": [],
                    }
                }
            if method == "thread/queue/list":
                return {"data": [], "nextCursor": None}
            if method == "thread/queue/add":
                return {
                    "queuedSubmission": {
                        "id": "queue-1",
                        "input": params["input"],
                        "clientUserMessageId": params["clientUserMessageId"],
                    }
                }
            raise AssertionError(method)

    client = StubClient()
    capability = asyncio.run(client.preflight_thread_delivery("thread-1"))
    receipt = asyncio.run(
        client.add_thread_delivery(
            thread_id="thread-1",
            delivery_id="delivery-1",
            pointer="bounded pointer",
        )
    )

    assert capability["thread_id"] == "thread-1"
    assert capability["runtime_status"] == "idle"
    assert capability["can_accept_direct_input"] is True
    assert capability["queue_capacity"] == 100
    assert receipt == {
        "item_id": "queue-1",
        "client_user_message_id": "delivery-1",
        "pointer": "bounded pointer",
    }
    assert client.calls == [
        ("thread/read", {"threadId": "thread-1", "includeTurns": False}),
        ("thread/queue/list", {"threadId": "thread-1", "limit": 100}),
        (
            "thread/queue/add",
            {
                "threadId": "thread-1",
                "input": [{"type": "text", "text": "bounded pointer"}],
                "clientUserMessageId": "delivery-1",
            },
        ),
    ]


def test_queue_adapter_rejects_unsupported_direct_input_before_admission() -> None:
    """Catches delivery to a spawned target Core itself will reject."""

    class StubClient(AppServerClient):
        def __init__(self) -> None:
            self.server_info = {"serverInfo": {"version": "0.148.0"}}

        async def _request(self, method, params):
            if method == "thread/read":
                return {
                    "thread": {
                        "id": "thread-1",
                        "status": {"type": "idle"},
                        "canAcceptDirectInput": False,
                        "source": {"subAgent": {"thread_spawn": {}}},
                        "turns": [],
                    }
                }
            if method == "thread/queue/list":
                return {"data": [], "nextCursor": None}
            raise AssertionError(method)

    with pytest.raises(AppServerError, match="does not accept direct input"):
        asyncio.run(StubClient().preflight_thread_delivery("thread-1"))


def test_queue_adapter_rejects_ephemeral_target_before_admission() -> None:
    """Catches queue delivery to a thread that is not durably materialized."""

    class StubClient(AppServerClient):
        def __init__(self) -> None:
            self.server_info = {"serverInfo": {"version": "0.148.0"}}

        async def _request(self, method, params):
            if method == "thread/read":
                return {
                    "thread": {
                        "id": "thread-1",
                        "status": {"type": "idle"},
                        "ephemeral": True,
                        "canAcceptDirectInput": True,
                        "source": "appServer",
                        "turns": [],
                    }
                }
            raise AssertionError(method)

    with pytest.raises(AppServerError, match="ephemeral"):
        asyncio.run(StubClient().preflight_thread_delivery("thread-1"))


@pytest.mark.parametrize(
    ("thread_result", "queue_result", "error"),
    [
        ({}, {"data": [], "nextCursor": None}, "no thread"),
        (
            {"thread": {"id": "thread-1", "status": "idle"}},
            {"data": [], "nextCursor": None},
            "runtime status",
        ),
        (
            {
                "thread": {
                    "id": "thread-1",
                    "status": {"type": "idle"},
                    "canAcceptDirectInput": "yes",
                }
            },
            {"data": [], "nextCursor": None},
            "invalid shape",
        ),
        (
            {
                "thread": {
                    "id": "thread-1",
                    "status": {"type": "idle"},
                    "canAcceptDirectInput": True,
                }
            },
            {"data": "not-a-list", "nextCursor": None},
            "invalid item list",
        ),
    ],
)
def test_thread_preflight_rejects_malformed_read_only_shapes(
    thread_result, queue_result, error: str
) -> None:
    """Catches arming from a drifted experimental response shape."""

    class StubClient(AppServerClient):
        def __init__(self) -> None:
            self.server_info = {"serverInfo": {"version": "0.148.0"}}

        async def _request(self, method, params):
            if method == "thread/read":
                return thread_result
            if method == "thread/queue/list":
                return queue_result
            raise AssertionError(method)

    with pytest.raises(AppServerError, match=error):
        asyncio.run(StubClient().preflight_thread_delivery("thread-1"))


@pytest.mark.parametrize(
    ("result", "error"),
    [
        ({}, "no queued submission"),
        (
            {
                "queuedSubmission": {
                    "id": "queue-1",
                    "clientUserMessageId": "other",
                    "input": [{"type": "text", "text": "pointer"}],
                }
            },
            "different delivery identity",
        ),
        (
            {
                "queuedSubmission": {
                    "id": "queue-1",
                    "clientUserMessageId": "delivery-1",
                    "input": [{"type": "text", "text": "modified"}],
                }
            },
            "modified pointer",
        ),
    ],
)
def test_thread_queue_add_rejects_malformed_or_modified_ack(result, error: str) -> None:
    """Catches treating an inconclusive queue response as accepted delivery."""

    class StubClient(AppServerClient):
        async def _request(self, method, params):
            assert method == "thread/queue/add"
            return result

    with pytest.raises(AppServerError, match=error):
        asyncio.run(
            StubClient().add_thread_delivery(
                thread_id="thread-1",
                delivery_id="delivery-1",
                pointer="pointer",
            )
        )


@pytest.mark.parametrize(
    "error",
    [AppServerError("transport unavailable"), AppServerRejectedError("target archived")],
)
def test_thread_preflight_propagates_unavailable_or_rejected_target(error) -> None:
    """Catches fallback or retarget after an exact target cannot be read."""

    class StubClient(AppServerClient):
        def __init__(self) -> None:
            self.server_info = {"serverInfo": {"version": "0.148.0"}}

        async def _request(self, method, params):
            assert method == "thread/read"
            raise error

    with pytest.raises(type(error), match=str(error)):
        asyncio.run(StubClient().preflight_thread_delivery("thread-1"))


def test_reconciliation_reads_history_before_queue_and_detects_modified_content() -> None:
    """Catches queue-first reconciliation or silent restoration of user edits."""

    class StubClient(AppServerClient):
        def __init__(self) -> None:
            self.calls: list[str] = []

        async def _request(self, method, params):
            self.calls.append(method)
            if method == "thread/read":
                return {
                    "thread": {
                        "id": "thread-1",
                        "status": {"type": "idle"},
                        "canAcceptDirectInput": True,
                        "source": "appServer",
                        "turns": [
                            {
                                "id": "turn-1",
                                "status": "completed",
                                "items": [
                                    {
                                        "type": "userMessage",
                                        "id": "message-1",
                                        "clientId": "delivery-1",
                                        "content": [
                                            {"type": "text", "text": "changed pointer"}
                                        ],
                                    }
                                ],
                            }
                        ],
                    }
                }
            if method == "thread/queue/list":
                return {
                    "data": [
                        {
                            "id": "queue-1",
                            "clientUserMessageId": "delivery-1",
                            "input": [{"type": "text", "text": "expected pointer"}],
                        }
                    ],
                    "nextCursor": None,
                }
            raise AssertionError(method)

    result = asyncio.run(
        StubClient().inspect_thread_delivery(
            thread_id="thread-1",
            delivery_id="delivery-1",
            expected_pointer_digest=hashlib.sha256(
                b"expected pointer"
            ).hexdigest(),
        )
    )

    assert result["classification"] == "delivery_modified"
    assert result["history_matches"] == 0
    assert result["history_modified"] == 1
    assert result["queue_matches"] == 1
    assert result["observed_pointer_count"] == 2
    assert result["runtime_status"] == "idle"
    assert result["interrupted"] is False
    assert result["history"][0]["message_id"] == "message-1"
    assert result["queue"][0]["item_id"] == "queue-1"


def test_reconciliation_reports_prior_fifo_items_without_exposing_their_text() -> None:
    """Catches hiding earlier billed work or leaking user-authored queue text."""

    class StubClient(AppServerClient):
        def __init__(self) -> None:
            self.calls: list[str] = []

        async def _request(self, method, params):
            self.calls.append(method)
            if method == "thread/read":
                return {
                    "thread": {
                        "id": "thread-1",
                        "status": {"type": "idle"},
                        "ephemeral": False,
                        "canAcceptDirectInput": True,
                        "source": "appServer",
                        "turns": [],
                    }
                }
            if method == "thread/queue/list":
                return {
                    "data": [
                        {
                            "id": "user-1",
                            "clientUserMessageId": "user-message-1",
                            "input": [{"type": "text", "text": "private user work"}],
                        },
                        {
                            "id": "queue-1",
                            "clientUserMessageId": "delivery-1",
                            "input": [{"type": "text", "text": "expected pointer"}],
                        },
                    ],
                    "nextCursor": None,
                }
            raise AssertionError(method)

    result = asyncio.run(
        StubClient().inspect_thread_delivery(
            thread_id="thread-1",
            delivery_id="delivery-1",
            expected_pointer_digest=hashlib.sha256(b"expected pointer").hexdigest(),
        )
    )

    assert result["classification"] == "queued"
    assert result["queue"][0]["position"] == 1
    assert result["prior_item_count"] == 1
    assert result["prior_item_ids"] == ["user-1"]
    assert "private user work" not in repr(result)


def test_resume_and_delete_use_only_exact_thread_queue_methods() -> None:
    """Catches ordinary turn/start, queue/start, or imprecise removal fallback."""

    class StubClient(AppServerClient):
        def __init__(self) -> None:
            self.calls: list[tuple[str, dict]] = []

        async def _request(self, method, params):
            self.calls.append((method, dict(params)))
            if method == "thread/resume":
                return {
                    "thread": {
                        "id": "thread-1",
                        "status": {"type": "idle"},
                        "canAcceptDirectInput": True,
                        "source": "appServer",
                        "turns": [],
                    }
                }
            if method == "thread/queue/delete":
                return {"deleted": True}
            raise AssertionError(method)

    client = StubClient()
    resumed = asyncio.run(client.resume_thread_delivery("thread-1"))
    deleted = asyncio.run(client.delete_thread_delivery("thread-1", "queue-1"))

    assert resumed["thread_id"] == "thread-1"
    assert resumed["runtime_status"] == "idle"
    assert deleted is True
    assert client.calls == [
        ("thread/resume", {"threadId": "thread-1"}),
        (
            "thread/queue/delete",
            {"threadId": "thread-1", "queuedSubmissionId": "queue-1"},
        ),
    ]


def test_wait_for_event_arms_exact_thread_without_reading_or_mutating_a_goal(
    tmp_path,
) -> None:
    """Catches accidental fallback to the legacy paused-goal admission path."""

    clock = Clock()
    adapter = FakeThreadDelivery()
    service = _thread_service(tmp_path, adapter, clock)

    result = asyncio.run(
        service.wait_for_event(
            thread_id="thread-1",
            condition={"type": "time", "after_seconds": 10},
            expires_in_seconds=100,
            idempotency_key="thread-monitor-1",
            start_daemon=False,
        )
    )

    assert result["state"] == MonitorState.ARMED
    assert result["delivery"]["kind"] == DeliveryKind.THREAD
    assert result["delivery"]["target_thread_id"] == "thread-1"
    assert result["next_action"].startswith("end turn; wait for time")
    assert "observation expires" in result["next_action"]
    assert result["next_action"].endswith("separate")
    assert result["targeting"]["authenticated_current_task"] is False
    assert adapter.preflight_calls == ["thread-1"]
    assert adapter.add_calls == []


def test_subagent_registration_preserves_origin_and_targets_only_the_root(
    tmp_path,
) -> None:
    clock = Clock()
    adapter = FakeThreadDelivery(
        origin_thread_id="child",
        delivery_thread_id="root",
        relay_chain=["child", "parent", "root"],
    )
    service = _thread_service(tmp_path, adapter, clock)

    result = asyncio.run(
        service.wait_for_event(
            thread_id="child",
            condition={"type": "time", "after_seconds": 10},
            expires_in_seconds=100,
            idempotency_key="child-monitor-1",
            start_daemon=False,
        )
    )

    assert result["delivery"]["origin_thread_id"] == "child"
    assert result["delivery"]["target_thread_id"] == "root"
    assert service.status(result["monitor_id"])["delivery"]["relay_chain"] == [
        "child",
        "parent",
        "root",
    ]
    record = service.ledger.get(result["monitor_id"])
    assert record is not None
    assert record.semantic["thread_id"] == "child"
    assert adapter.preflight_calls == ["root"]
    assert adapter.add_calls == []


def test_subagent_may_wait_for_its_own_turn_to_end_because_root_is_target(
    tmp_path,
) -> None:
    service = _thread_service(
        tmp_path,
        FakeThreadDelivery(
            origin_thread_id="child",
            delivery_thread_id="root",
            relay_chain=["child", "root"],
        ),
        Clock(),
    )

    assert service._reject_self_wait(
        {"type": "thread_idle", "thread_id": "child"}, "root"
    ) == ["child"]


def test_subagent_registration_allows_its_own_thread_idle_for_a_root_wake(
    tmp_path,
) -> None:
    adapter = FakeThreadDelivery(
        origin_thread_id="child",
        delivery_thread_id="root",
        relay_chain=["child", "root"],
    )
    observer_adapter = FakeAppServer(
        [],
        thread_observations={
            "child": observation(
                thread_id="child", runtime_status="active", goal_status=None
            )
        },
    )
    service = _thread_service(
        tmp_path,
        adapter,
        Clock(),
        observer_adapter=observer_adapter,
    )

    result = asyncio.run(
        service.wait_for_event(
            thread_id="child",
            condition={"type": "thread_idle", "thread_id": "child"},
            expires_in_seconds=100,
            idempotency_key="child-self-idle",
            start_daemon=False,
        )
    )

    assert result["state"] == MonitorState.ARMED
    assert result["delivery"]["target_thread_id"] == "root"
    assert observer_adapter.child_reads == ["child"]


def test_subagent_cannot_wait_for_the_root_that_its_wake_targets(tmp_path) -> None:
    service = _thread_service(
        tmp_path,
        FakeThreadDelivery(
            origin_thread_id="child",
            delivery_thread_id="root",
            relay_chain=["child", "root"],
        ),
        Clock(),
    )

    with pytest.raises(ValidationError, match="own target thread"):
        service._reject_self_wait(
            {"type": "thread_idle", "thread_id": "root"}, "root"
        )


def test_explicit_service_caller_cannot_forge_trusted_mcp_binding(tmp_path) -> None:
    service = _thread_service(tmp_path, FakeThreadDelivery(), Clock())

    with pytest.raises(ValidationError, match="caller binding"):
        asyncio.run(
            service.wait_for_event(
                thread_id="thread-1",
                condition={"type": "time", "after_seconds": 10},
                expires_in_seconds=100,
                idempotency_key="forged-binding",
                _caller_binding="trusted_mcp_caller",
                start_daemon=False,
            )
        )


def test_trusted_mcp_service_entry_records_authenticated_origin(tmp_path) -> None:
    service = _thread_service(tmp_path, FakeThreadDelivery(), Clock())

    result = asyncio.run(
        service.wait_for_current_event(
            thread_id="thread-1",
            condition={"type": "time", "after_seconds": 10},
            expires_in_seconds=100,
            idempotency_key="trusted-binding",
            start_daemon=False,
        )
    )

    assert result["targeting"] == {
        "kind": "trusted_mcp_caller",
        "authenticated_current_task": True,
        "same_thread_fifo_release_authorized": True,
    }
    assert service.status(result["monitor_id"])["delivery"]["origin_binding"] == (
        "trusted_mcp_caller"
    )


@pytest.mark.parametrize("expiry", [float("nan"), float("inf")])
def test_wait_for_event_rejects_nonfinite_expiry_before_capability_io(
    tmp_path, expiry
) -> None:
    adapter = FakeThreadDelivery()
    service = _thread_service(tmp_path, adapter, Clock())

    with pytest.raises(ValidationError, match="finite"):
        asyncio.run(
            service.wait_for_event(
                thread_id="thread-1",
                condition={"type": "time", "after_seconds": 1},
                expires_in_seconds=expiry,
                idempotency_key="thread-monitor-1",
                start_daemon=False,
            )
        )

    assert adapter.preflight_calls == []


def test_untyped_thread_observation_failure_stays_armed_without_a_claim(
    tmp_path,
) -> None:
    """Catches consuming delivery for a retryable observer transport failure."""

    adapter = FakeThreadDelivery()
    service = _thread_service(tmp_path, adapter, Clock())
    registered = asyncio.run(
        service.wait_for_event(
            thread_id="thread-1",
            condition={"type": "time", "after_seconds": 10},
            expires_in_seconds=100,
            idempotency_key="thread-monitor-1",
            start_daemon=False,
        )
    )
    record = service.ledger.get(registered["monitor_id"])
    assert record is not None

    assert service._record_unexpected_failure(record, AppServerError("offline")) is None
    status = service.status(record.monitor_id)

    assert status["state"] == MonitorState.ARMED
    assert status["wake_reason"] is None
    assert status["outcome"]["kind"] == "transient_observation_failure_recorded"
    assert adapter.add_calls == []


def test_identical_thread_replay_returns_original_during_capability_outage(
    tmp_path,
) -> None:
    adapter = FakeThreadDelivery()
    service = _thread_service(tmp_path, adapter, Clock())
    request = {
        "thread_id": "thread-1",
        "condition": {"type": "time", "after_seconds": 1},
        "expires_in_seconds": 100,
        "idempotency_key": "thread-monitor-1",
        "start_daemon": False,
    }
    original = asyncio.run(service.wait_for_event(**request))
    service.thread_delivery_readiness = lambda _root: False

    replay = asyncio.run(service.wait_for_event(**request))

    assert replay["monitor_id"] == original["monitor_id"]
    assert adapter.preflight_calls == ["thread-1"]


def test_thread_delivery_consumes_one_add_and_reconciles_queue_without_readd(
    tmp_path,
) -> None:
    """Catches a daemon pass that blindly re-enqueues an accepted pointer."""

    clock = Clock()
    adapter = FakeThreadDelivery()
    service = _thread_service(tmp_path, adapter, clock)
    registered = asyncio.run(
        service.wait_for_event(
            thread_id="thread-1",
            condition={"type": "time", "after_seconds": 1},
            expires_in_seconds=100,
            idempotency_key="thread-monitor-1",
            start_daemon=False,
        )
    )
    clock.value = 102.0

    asyncio.run(service.reconcile_once())
    first = service.status(registered["monitor_id"])
    asyncio.run(service.reconcile_once())
    second = service.status(registered["monitor_id"])

    assert first["state"] == MonitorState.QUEUE_ACCEPTED
    assert second["state"] == MonitorState.QUEUE_ACCEPTED
    assert len(adapter.add_calls) == 1
    assert adapter.add_calls[0]["thread_id"] == "thread-1"
    assert adapter.add_calls[0]["delivery_id"] == first["delivery"]["delivery_id"]
    assert first["queue_receipt"]["item_id"] == "queue-1"
    assert second["reconciliation"]["classification"] == "queued"


def test_claimed_delivery_retries_only_transient_preflight_before_one_add(
    tmp_path,
) -> None:
    """Reproduces the claimed wake lost by a transient thread/read timeout."""

    clock = Clock()
    adapter = FakeThreadDelivery()
    service = _thread_service(tmp_path, adapter, clock)
    registered = asyncio.run(
        service.wait_for_event(
            thread_id="thread-1",
            condition={"type": "time", "after_seconds": 1},
            expires_in_seconds=100,
            idempotency_key="preflight-recovers",
            start_daemon=False,
        )
    )
    original_preflight = adapter.preflight_thread_delivery
    failures = iter(
        [
            AppServerTransportError("app-server thread/read timed out"),
            AppServerTransportError("app-server thread/read timed out"),
        ]
    )

    async def flaky_preflight(thread_id: str) -> dict:
        failure = next(failures, None)
        if failure is not None:
            adapter.preflight_calls.append(thread_id)
            raise failure
        return await original_preflight(thread_id)

    adapter.preflight_thread_delivery = flaky_preflight
    clock.value = 102.0
    asyncio.run(service.reconcile_once())
    first = service.status(registered["monitor_id"])

    assert first["state"] == MonitorState.CLAIMED
    assert first["delivery_state"] == ThreadDeliveryState.UNATTEMPTED
    assert first["admission_attempted_at"] is None
    assert first["queue_receipt"] is None
    assert first["reconciliation"]["classification"] == (
        "preflight_transport_unavailable"
    )
    assert adapter.add_calls == []

    # Trigger expiry bounds observation only; an already claimed delivery stays
    # pending until preflight recovers or the user cancels it.
    clock.value = 201.0
    asyncio.run(service.reconcile_once())
    second = service.status(registered["monitor_id"])
    assert second["state"] == MonitorState.CLAIMED
    assert second["admission_attempted_at"] is None
    assert adapter.add_calls == []

    clock.value = 202.0
    asyncio.run(service.reconcile_once())
    recovered = service.status(registered["monitor_id"])

    assert recovered["state"] == MonitorState.QUEUE_ACCEPTED
    assert recovered["admission_attempted_at"] == 202.0
    assert recovered["queue_receipt"]["item_id"] == "queue-1"
    assert len(adapter.add_calls) == 1


@pytest.mark.parametrize(
    "failure",
    [
        AppServerRejectedError("target does not accept direct input"),
        AppServerError("app-server CODEX_HOME does not match the local runtime root"),
        AppServerError("thread/read returned no runtime status"),
    ],
)
def test_claimed_delivery_definitive_preflight_denial_is_terminal(
    tmp_path, failure
) -> None:
    clock = Clock()
    adapter = FakeThreadDelivery()
    service = _thread_service(tmp_path, adapter, clock)
    registered = asyncio.run(
        service.wait_for_event(
            thread_id="thread-1",
            condition={"type": "time", "after_seconds": 1},
            expires_in_seconds=100,
            idempotency_key="preflight-denied",
            start_daemon=False,
        )
    )

    async def deny_preflight(thread_id: str) -> dict:
        adapter.preflight_calls.append(thread_id)
        raise failure

    adapter.preflight_thread_delivery = deny_preflight
    clock.value = 102.0
    asyncio.run(service.reconcile_once())
    denied = service.status(registered["monitor_id"])

    assert denied["state"] == MonitorState.DELIVERY_CAPABILITY_UNAVAILABLE
    assert denied["delivery_state"] == ThreadDeliveryState.DELIVERY_REJECTED
    assert denied["admission_attempted_at"] is None
    assert adapter.add_calls == []


def test_claimed_delivery_can_be_cancelled_before_preflight_retry(tmp_path) -> None:
    clock = Clock()
    adapter = FakeThreadDelivery()
    service = _thread_service(tmp_path, adapter, clock)
    registered = asyncio.run(
        service.wait_for_event(
            thread_id="thread-1",
            condition={"type": "time", "after_seconds": 1},
            expires_in_seconds=100,
            idempotency_key="preflight-cancelled",
            start_daemon=False,
        )
    )

    async def timeout_preflight(thread_id: str) -> dict:
        adapter.preflight_calls.append(thread_id)
        raise AppServerTransportError("app-server thread/read timed out")

    adapter.preflight_thread_delivery = timeout_preflight
    clock.value = 102.0
    asyncio.run(service.reconcile_once())
    cancelled = service.cancel(registered["monitor_id"])
    asyncio.run(service.reconcile_once())

    assert cancelled["state"] == MonitorState.CANCELLED
    assert service.status(registered["monitor_id"])["state"] == MonitorState.CANCELLED
    assert adapter.add_calls == []


def test_queue_accepted_monitor_can_parent_fresh_lineage_without_readd(
    tmp_path,
) -> None:
    """Reproduces a consumed pointer rejected only because recording lagged."""

    clock = Clock()
    adapter = FakeThreadDelivery()
    service = _thread_service(tmp_path, adapter, clock)
    parent = asyncio.run(
        service.wait_for_event(
            thread_id="thread-1",
            condition={"type": "time", "after_seconds": 1},
            expires_in_seconds=100,
            idempotency_key="consumed-parent",
            start_daemon=False,
        )
    )
    clock.value = 102.0
    asyncio.run(service.reconcile_once())
    assert service.status(parent["monitor_id"])["state"] == MonitorState.QUEUE_ACCEPTED

    child = asyncio.run(
        service.wait_for_event(
            thread_id="thread-1",
            condition={"type": "time", "after_seconds": 10},
            expires_in_seconds=100,
            idempotency_key="fresh-child",
            rearm_of=parent["monitor_id"],
            start_daemon=False,
        )
    )

    parent_status = service.decision_status(parent["monitor_id"])
    assert parent_status["state"] == MonitorState.QUEUE_ACCEPTED
    assert parent_status.get("task_success") is not True
    assert child["state"] == MonitorState.ARMED
    assert child["rearm_of"] == parent["monitor_id"]
    assert len(adapter.add_calls) == 1


def test_subagent_event_admits_only_to_the_frozen_root_queue(tmp_path) -> None:
    clock = Clock()
    adapter = FakeThreadDelivery(
        origin_thread_id="child",
        delivery_thread_id="root",
        relay_chain=["child", "parent", "root"],
    )
    service = _thread_service(tmp_path, adapter, clock)
    registered = asyncio.run(
        service.wait_for_event(
            thread_id="child",
            condition={"type": "time", "after_seconds": 1},
            expires_in_seconds=100,
            idempotency_key="child-root-delivery-1",
            start_daemon=False,
        )
    )
    clock.value = 102.0

    asyncio.run(service.reconcile_once())
    result = service.status(registered["monitor_id"])

    assert result["state"] == MonitorState.QUEUE_ACCEPTED
    assert adapter.preflight_calls == ["root", "root"]
    assert [call["thread_id"] for call in adapter.add_calls] == ["root"]
    assert result["delivery"]["origin_thread_id"] == "child"
    assert result["delivery"]["thread_id"] == "root"


def test_uncertain_admission_reconciles_online_window_and_never_readds(
    tmp_path,
) -> None:
    """Catches blind retry after a queue-add transport ambiguity."""

    clock = Clock()
    absent = {
        "classification": "absent",
        "runtime_status": "idle",
        "interrupted": False,
        "history_matches": 0,
        "history_modified": 0,
        "queue_matches": 0,
        "queue_modified": 0,
        "observed_pointer_count": 0,
        "history": [],
        "queue": [],
        "queue_count": 0,
    }
    adapter = FakeThreadDelivery(
        add_result=AppServerTransportError("thread/queue/add timed out"),
        inspections=[absent, absent],
    )
    service = _thread_service(tmp_path, adapter, clock)
    registered = asyncio.run(
        service.wait_for_event(
            thread_id="thread-1",
            condition={"type": "time", "after_seconds": 1},
            expires_in_seconds=100,
            idempotency_key="thread-monitor-1",
            start_daemon=False,
        )
    )
    clock.value = 102.0
    asyncio.run(service.reconcile_once())
    assert service.status(registered["monitor_id"])["state"] == (
        MonitorState.ADMISSION_IN_PROGRESS
    )

    clock.value = 163.0
    asyncio.run(service.reconcile_once())
    terminal = service.status(registered["monitor_id"])

    assert terminal["state"] == MonitorState.DELIVERY_UNCERTAIN
    assert terminal["delivery_outcome"]["absence_kind"] == "unresolved_absence"
    assert terminal["reconciliation"]["online_absence_seconds"] >= 60
    assert len(adapter.add_calls) == 1


def test_reconciliation_excludes_app_server_offline_time_from_online_window(
    tmp_path,
) -> None:
    clock = Clock()
    absent = {
        "classification": "absent",
        "runtime_status": "idle",
        "interrupted": False,
        "history_matches": 0,
        "history_modified": 0,
        "queue_matches": 0,
        "queue_modified": 0,
        "observed_pointer_count": 0,
        "history": [],
        "queue": [],
        "queue_count": 0,
    }
    adapter = FakeThreadDelivery(
        add_result=AppServerError("transport uncertain"),
        inspections=[absent, AppServerError("offline"), absent, absent],
    )
    service = _thread_service(tmp_path, adapter, clock)
    registered = asyncio.run(
        service.wait_for_event(
            thread_id="thread-1",
            condition={"type": "time", "after_seconds": 1},
            expires_in_seconds=1000,
            idempotency_key="thread-monitor-1",
            start_daemon=False,
        )
    )

    clock.value = 102.0
    asyncio.run(service.reconcile_once())
    clock.value = 202.0
    asyncio.run(service.reconcile_once())
    clock.value = 302.0
    asyncio.run(service.reconcile_once())
    after_outage = service.status(registered["monitor_id"])

    assert after_outage["state"] == MonitorState.ADMISSION_IN_PROGRESS
    assert after_outage["reconciliation"]["online_absence_seconds"] == 0.0

    clock.value = 363.0
    asyncio.run(service.reconcile_once())
    assert service.status(registered["monitor_id"])["state"] == (
        MonitorState.DELIVERY_UNCERTAIN
    )


def test_unloaded_delivery_persists_items_ahead_before_exact_resume(tmp_path) -> None:
    """Catches resume before the billed FIFO boundary is durably visible."""

    clock = Clock()
    adapter = FakeThreadDelivery(
        loaded=False,
        queue=[
            {
                "item_id": "user-1",
                "client_user_message_id": "user-message-1",
                "pointer": "user work",
                "pointer_digest": hashlib.sha256(b"user work").hexdigest(),
                "position": 0,
            },
            {
                "item_id": "queue-1",
                "client_user_message_id": "placeholder",
                "pointer": "placeholder",
                "pointer_digest": hashlib.sha256(b"placeholder").hexdigest(),
                "position": 1,
            },
        ],
    )
    service = _thread_service(tmp_path, adapter, clock)
    registered = asyncio.run(
        service.wait_for_event(
            thread_id="thread-1",
            condition={"type": "time", "after_seconds": 1},
            expires_in_seconds=100,
            idempotency_key="thread-monitor-1",
            start_daemon=False,
        )
    )
    clock.value = 102.0
    asyncio.run(service.reconcile_once())
    status = service.status(registered["monitor_id"])

    assert adapter.resume_calls == ["thread-1"]
    assert status["reconciliation"]["pre_resume_item_count"] == 1
    assert status["reconciliation"]["pre_resume_item_ids"] == ["user-1"]
    assert status["reconciliation"]["resume_attempted_at"] == 102.0


def test_matching_history_is_terminal_recorded_and_duplicate_count_is_visible(
    tmp_path,
) -> None:
    """Catches conflating queue acceptance with durable history recording."""

    clock = Clock()
    recorded = {
        "classification": "recorded",
        "runtime_status": "idle",
        "interrupted": False,
        "history_matches": 2,
        "history_modified": 0,
        "queue_matches": 0,
        "queue_modified": 0,
        "observed_pointer_count": 2,
        "history": [
            {"turn_id": "turn-1", "message_id": "message-1"},
            {"turn_id": "turn-2", "message_id": "message-2"},
        ],
        "queue": [],
        "queue_count": 0,
    }
    adapter = FakeThreadDelivery(inspections=[recorded])
    service = _thread_service(tmp_path, adapter, clock)
    registered = asyncio.run(
        service.wait_for_event(
            thread_id="thread-1",
            condition={"type": "time", "after_seconds": 1},
            expires_in_seconds=100,
            idempotency_key="thread-monitor-1",
            start_daemon=False,
        )
    )
    clock.value = 102.0
    asyncio.run(service.reconcile_once())
    status = service.status(registered["monitor_id"])

    assert status["state"] == MonitorState.RECORDED
    assert status["delivery_state"] == ThreadDeliveryState.RECORDED
    assert status["delivery_outcome"]["kind"] == "recorded"
    assert status["delivery_outcome"]["observed_pointer_count"] == 2
    assert len(adapter.add_calls) == 1


def test_post_ack_cancellation_consumes_one_exact_delete_before_transport(
    tmp_path,
) -> None:
    """Catches repeated removal or cancellation claimed before queue deletion."""

    clock = Clock()
    adapter = FakeThreadDelivery()
    service = _thread_service(tmp_path, adapter, clock)
    registered = asyncio.run(
        service.wait_for_event(
            thread_id="thread-1",
            condition={"type": "time", "after_seconds": 1},
            expires_in_seconds=100,
            idempotency_key="thread-monitor-1",
            start_daemon=False,
        )
    )
    clock.value = 102.0
    asyncio.run(service.reconcile_once())

    requested = service.cancel(registered["monitor_id"])
    assert requested["state"] == MonitorState.CANCEL_REQUESTED
    assert adapter.delete_calls == []

    asyncio.run(service.reconcile_once())
    cancelled = service.status(registered["monitor_id"])
    asyncio.run(service.reconcile_once())

    assert cancelled["state"] == MonitorState.CANCELLED
    assert cancelled["delivery_state"] == ThreadDeliveryState.CANCELLED
    assert adapter.delete_calls == [("thread-1", "queue-1")]


def test_delivery_epoch_downgrade_refuses_nonterminal_thread_rows(tmp_path) -> None:
    """Catches an older daemon opening a live queue-delivery state machine."""

    ledger = Ledger(tmp_path)
    delivery = build_thread_delivery(
        monitor_id="monitor-1",
        thread_id="thread-1",
        capability={"codex_version": "0.148.0", "queue_capacity": 100},
    )
    ledger.create_or_get(
        monitor_id="monitor-1",
        idempotency_key="thread-key",
        semantic={"delivery": delivery},
        target=None,
        delivery=delivery,
        condition={"type": "time", "deadline_utc": 0},
        allow_heuristic_continuation=False,
        expires_at=10_000,
    )
    ledger.close()

    with pytest.raises(RuntimeError, match="thread deliveries require"):
        assert_delivery_schema_compatible(
            tmp_path, supported_delivery_epoch=0
        )
    assert_delivery_schema_compatible(tmp_path, supported_delivery_epoch=1)


def test_delivery_downgrade_refuses_completed_row_with_matching_queued_pointer(
    tmp_path,
) -> None:
    """Catches downgrade while Core still owns a V1 pointer outside the ledger."""

    ledger = Ledger(tmp_path)
    delivery = build_thread_delivery(
        monitor_id="monitor-1",
        thread_id="thread-1",
        capability={"codex_version": "0.148.0", "queue_capacity": 100},
    )
    record, _ = ledger.create_or_get(
        monitor_id="monitor-1",
        idempotency_key="thread-key",
        semantic={"delivery": delivery},
        target=None,
        delivery=delivery,
        condition={"type": "time", "deadline_utc": 0},
        allow_heuristic_continuation=False,
        expires_at=10_000,
    )
    ledger.transition(
        record.monitor_id,
        expected=(MonitorState.REGISTERING,),
        state=MonitorState.RECORDED,
        outcome={"kind": "historical"},
    )
    ledger.close()

    queued = FakeThreadDelivery()
    with pytest.raises(RuntimeError, match="matching queue pointer"):
        asyncio.run(
            assert_delivery_runtime_compatible(
                tmp_path,
                supported_delivery_epoch=0,
                app_server_factory=lambda: queued,
            )
        )
    assert queued.add_calls == []


def test_uncertain_queue_removal_uses_online_window_without_second_delete(
    tmp_path,
) -> None:
    """Catches repeated delete or permanent cancel-pending after ambiguous absence."""

    clock = Clock()
    absent = {
        "classification": "absent",
        "runtime_status": "idle",
        "interrupted": False,
        "history_matches": 0,
        "history_modified": 0,
        "queue_matches": 0,
        "queue_modified": 0,
        "observed_pointer_count": 0,
        "history": [],
        "queue": [],
        "queue_count": 0,
    }
    adapter = FakeThreadDelivery(inspections=[])

    async def uncertain_delete(thread_id: str, item_id: str) -> bool:
        adapter.delete_calls.append((thread_id, item_id))
        adapter.last_inspection = absent
        raise AppServerError("delete transport uncertain")

    adapter.delete_thread_delivery = uncertain_delete
    service = _thread_service(tmp_path, adapter, clock)
    registered = asyncio.run(
        service.wait_for_event(
            thread_id="thread-1",
            condition={"type": "time", "after_seconds": 1},
            expires_in_seconds=100,
            idempotency_key="thread-monitor-1",
            start_daemon=False,
        )
    )
    clock.value = 102.0
    asyncio.run(service.reconcile_once())
    service.cancel(registered["monitor_id"])
    asyncio.run(service.reconcile_once())
    assert service.status(registered["monitor_id"])["state"] == (
        MonitorState.CANCELLATION_IN_PROGRESS
    )

    clock.value = 103.0
    asyncio.run(service.reconcile_once())
    clock.value = 164.0
    asyncio.run(service.reconcile_once())
    terminal = service.status(registered["monitor_id"])

    assert terminal["state"] == MonitorState.DELIVERY_UNCERTAIN
    assert terminal["delivery_outcome"]["kind"] == "cancellation_uncertain"
    assert terminal["reconciliation"]["online_absence_seconds"] >= 60
    assert adapter.delete_calls == [("thread-1", "queue-1")]


def test_failed_command_event_reaches_thread_queue_without_success_claim(
    tmp_path,
) -> None:
    """Catches terminal-event evidence being dropped or promoted to success."""

    clock = Clock()
    adapter = FakeThreadDelivery()
    service = _thread_service(tmp_path, adapter, clock)
    reserved = service.reserve_terminal_event(
        {
            "kind": "command_terminal",
            "expires_in_seconds": 100,
            "idempotency_key": "command-launch-1",
            "producer_identity": "shell-test",
        }
    )
    registered = asyncio.run(
        service.wait_for_event(
            thread_id="thread-1",
            condition={
                "type": "command_terminal",
                "reservation_id": reserved["reservation_id"],
            },
            expires_in_seconds=100,
            idempotency_key="thread-monitor-1",
            start_daemon=False,
        )
    )
    service.publish_terminal_event(
        reserved["reservation_id"],
        publish_token=reserved["publish_token"],
        terminal_event={
            "kind": "command_terminal",
            "status": "failed",
            "command_label": "focused-test",
            "command_digest": "a" * 64,
            "exit_code": 2,
        },
    )

    asyncio.run(service.reconcile_once())
    status = service.status(registered["monitor_id"])

    assert status["state"] == MonitorState.QUEUE_ACCEPTED
    assert status["witness"][0]["classification"] == "command_termination"
    assert status["witness"][0]["task_success"] is False
    assert status["witness"][0]["lead_accepted"] is False
    assert len(adapter.add_calls) == 1


def test_uncertain_add_promotes_exact_queue_match_without_a_second_add(tmp_path) -> None:
    """Catches stranding an exact accepted row in admission-in-progress."""

    clock = Clock()
    adapter = FakeThreadDelivery(add_result=AppServerError("transport uncertain"))
    service = _thread_service(tmp_path, adapter, clock)
    registered = asyncio.run(
        service.wait_for_event(
            thread_id="thread-1",
            condition={"type": "time", "after_seconds": 1},
            expires_in_seconds=100,
            idempotency_key="thread-monitor-1",
            start_daemon=False,
        )
    )
    clock.value = 102.0

    asyncio.run(service.reconcile_once())
    status = service.status(registered["monitor_id"])

    assert status["state"] == MonitorState.QUEUE_ACCEPTED
    assert status["queue_receipt"]["item_id"] == "queue-1"
    assert status["queue_receipt"]["recovered_from_queue"] is True
    assert len(adapter.add_calls) == 1


def test_uncertain_add_resumes_unloaded_thread_after_persisted_snapshot(tmp_path) -> None:
    """Recovered admission retains the same unconditional resume contract."""

    clock = Clock()
    adapter = FakeThreadDelivery(
        loaded=False,
        add_result=AppServerError("transport uncertain"),
        queue=[
            {"item_id": "older-item"},
            {"item_id": "queue-1"},
        ],
    )
    service = _thread_service(tmp_path, adapter, clock)
    registered = asyncio.run(
        service.wait_for_event(
            thread_id="thread-1",
            condition={"type": "time", "after_seconds": 1},
            expires_in_seconds=100,
            idempotency_key="thread-monitor-1",
            start_daemon=False,
        )
    )
    clock.value = 102.0

    asyncio.run(service.reconcile_once())
    status = service.status(registered["monitor_id"])

    assert adapter.resume_calls == ["thread-1"]
    assert status["reconciliation"]["pre_resume_item_count"] == 1
    assert status["reconciliation"]["pre_resume_item_ids"] == ["older-item"]
    assert status["reconciliation"]["resume_attempted_at"] == 102.0


def test_git_ref_change_wakes_the_thread_once_without_claiming_success(
    tmp_path,
) -> None:
    repository = tmp_path / "repo"
    _git(tmp_path, "init", str(repository))
    _git(repository, "config", "user.name", "Test User")
    _git(repository, "config", "user.email", "test@example.invalid")
    baseline = _git_commit(repository, "tracked.txt", "one\n", "baseline")
    recorded = {
        "classification": "recorded",
        "runtime_status": "idle",
        "interrupted": False,
        "history_matches": 1,
        "history_modified": 0,
        "queue_matches": 0,
        "queue_modified": 0,
        "observed_pointer_count": 1,
        "history": [{"turn_id": "turn-1", "message_id": "message-1"}],
        "queue": [],
        "queue_count": 0,
    }
    adapter = FakeThreadDelivery(inspections=[recorded])
    service = _thread_service(tmp_path, adapter, Clock())
    receipt = asyncio.run(
        service.wait_for_event(
            thread_id="thread-1",
            condition={
                "type": "git_ref_change",
                "worktree": str(repository),
                "ref": "HEAD",
            },
            expires_in_seconds=100,
            idempotency_key="git-ref-thread",
            start_daemon=False,
        )
    )
    current = _git_commit(repository, "tracked.txt", "two\n", "successor")

    asyncio.run(service.reconcile_once())
    decision = service.decision_status(receipt["monitor_id"])

    assert receipt["condition"]["direct_oid"] == baseline
    assert decision["state"] == "recorded"
    assert decision["wake_reason"] == "condition"
    assert decision["witness"][0]["classification"] == "fast_forward"
    assert decision["witness"][0]["old_direct_oid"] == baseline
    assert decision["witness"][0]["new_direct_oid"] == current
    assert decision["witness"][0]["task_success"] is False
    assert decision["witness"][0]["lead_accepted"] is False
    assert decision["delivery"]["classification"] == "recorded"
    assert len(adapter.add_calls) == 1


def test_git_ref_observer_failure_wakes_once_with_fixed_diagnostic(
    tmp_path,
) -> None:
    repository = tmp_path / "repo"
    _git(tmp_path, "init", str(repository))
    _git(repository, "config", "user.name", "Test User")
    _git(repository, "config", "user.email", "test@example.invalid")
    _git_commit(repository, "tracked.txt", "one\n", "baseline")
    adapter = FakeThreadDelivery()
    service = _thread_service(tmp_path, adapter, Clock())
    receipt = asyncio.run(
        service.wait_for_event(
            thread_id="thread-1",
            condition={
                "type": "git_ref_change",
                "worktree": str(repository),
                "ref": "HEAD",
            },
            expires_in_seconds=100,
            idempotency_key="git-ref-observer-failure",
            start_daemon=False,
        )
    )
    (repository / ".git").rename(repository / ".git-moved")

    asyncio.run(service.reconcile_once())
    decision = service.decision_status(receipt["monitor_id"])

    assert decision["state"] == "queue_accepted"
    assert decision["wake_reason"] == "observer_failed"
    assert decision["failure_detail"] == {
        "type": "git_ref_change",
        "kind": "git_ref_observer_failed",
        "error": "repository_identity_changed",
    }
    assert len(adapter.add_calls) == 1

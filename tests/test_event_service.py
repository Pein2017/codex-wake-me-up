from __future__ import annotations

import asyncio
import json
import os
import stat
import subprocess
from pathlib import Path

import pytest

from codex_wake_me_up.conditions import ObserverContext
from codex_wake_me_up.ledger import Ledger
from codex_wake_me_up.models import ConflictError, ValidationError
from codex_wake_me_up.service import MonitorService

from .helpers import Clock, FakeAppServer, observation


def service(
    tmp_path,
    app_server: FakeAppServer,
    clock: Clock,
    *,
    event_ready=lambda _root: True,
) -> MonitorService:
    return MonitorService(
        tmp_path,
        ledger=Ledger(tmp_path),
        app_server_factory=lambda: app_server,
        observer_context_factory=lambda: ObserverContext(
            runtime_root=tmp_path, now=clock.now
        ),
        daemon_starter=lambda _root: False,
        daemon_readiness=lambda _root: True,
        event_daemon_readiness=event_ready,
    )


def command_reservation() -> dict:
    return {
        "kind": "command_terminal",
        "expires_in_seconds": 100,
        "idempotency_key": "command-launch-1",
        "producer_identity": "shell-test",
    }


def command_terminal(*, status: str = "failed", exit_code: int = 2) -> dict:
    return {
        "kind": "command_terminal",
        "status": status,
        "command_label": "focused-test",
        "command_digest": "a" * 64,
        "exit_code": exit_code,
    }


def git(repository: Path, *args: str) -> str:
    return subprocess.run(
        ("git", "-C", str(repository), *args),
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


def git_commit(repository: Path, path: str, content: str, message: str) -> str:
    selected = repository / path
    selected.parent.mkdir(parents=True, exist_ok=True)
    selected.write_text(content)
    git(repository, "add", "-A")
    git(repository, "commit", "-m", message)
    return git(repository, "rev-parse", "HEAD")


def test_reserve_returns_token_once_and_optional_descriptor_is_mode_0600(
    tmp_path,
) -> None:
    clock = Clock()
    monitor_service = service(tmp_path, FakeAppServer([observation()]), clock)
    descriptor = tmp_path / "handoff" / "publisher.json"

    created = monitor_service.reserve_terminal_event(
        command_reservation(), descriptor_path=descriptor
    )
    replay = monitor_service.reserve_terminal_event(command_reservation())

    assert created["created"] is True
    assert created["publish_token"]
    assert created["reservation_id"] == replay["reservation_id"]
    assert replay["created"] is False
    assert "publish_token" not in replay
    assert descriptor.exists()
    assert stat.S_IMODE(descriptor.stat().st_mode) == 0o600
    assert "publish_token" not in str(monitor_service.event_status(created["reservation_id"]))


def test_status_persists_prebind_expiry_before_reporting_lifecycle(tmp_path) -> None:
    clock = Clock()
    monitor_service = service(tmp_path, FakeAppServer([observation()]), clock)
    reserved = monitor_service.reserve_terminal_event(command_reservation())

    clock.value = 201.0
    status = monitor_service.event_status(reserved["reservation_id"])

    assert status["state"] == "expired"
    assert status["expired_at"] == 201.0
    with pytest.raises(ConflictError, match="no longer writable"):
        monitor_service.publish_terminal_event(
            reserved["reservation_id"],
            publish_token=reserved["publish_token"],
            terminal_event=command_terminal(),
        )


def test_publisher_descriptor_replaces_a_precreated_predictable_temp_as_0600(
    tmp_path,
) -> None:
    monitor_service = service(tmp_path, FakeAppServer([observation()]), Clock())
    parent = tmp_path / "handoff"
    parent.mkdir(mode=0o700)
    descriptor = parent / "publisher.json"
    predictable = parent / f".{descriptor.name}.{os.getpid()}.tmp"
    predictable.write_text("attacker-controlled")
    predictable.chmod(0o644)

    monitor_service.reserve_terminal_event(
        command_reservation(), descriptor_path=descriptor
    )

    assert stat.S_IMODE(descriptor.stat().st_mode) == 0o600
    assert predictable.read_text() == "attacker-controlled"


def test_publisher_descriptor_rejects_symlink_and_shared_parent(tmp_path) -> None:
    monitor_service = service(tmp_path, FakeAppServer([observation()]), Clock())
    private = tmp_path / "private"
    private.mkdir(mode=0o700)
    target = private / "target.json"
    target.write_text("do-not-overwrite")
    target.chmod(0o600)
    link = private / "publisher.json"
    link.symlink_to(target)

    with pytest.raises(ValidationError, match="symlink"):
        monitor_service.reserve_terminal_event(
            {**command_reservation(), "idempotency_key": "symlink-reserve"},
            descriptor_path=link,
        )
    assert target.read_text() == "do-not-overwrite"

    shared = tmp_path / "shared"
    shared.mkdir(mode=0o777)
    shared.chmod(0o777)
    with pytest.raises(ValidationError, match="private directory"):
        monitor_service.reserve_terminal_event(
            {**command_reservation(), "idempotency_key": "shared-reserve"},
            descriptor_path=shared / "publisher.json",
        )


def test_event_register_rejects_legacy_daemon_before_arm(tmp_path) -> None:
    clock = Clock()
    app_server = FakeAppServer([observation()])
    monitor_service = service(
        tmp_path, app_server, clock, event_ready=lambda _root: False
    )
    reserved = monitor_service.reserve_terminal_event(command_reservation())

    with pytest.raises(ValidationError, match="event-capable daemon"):
        asyncio.run(
            monitor_service.register(
                thread_id="test-thread",
                condition={
                    "type": "command_terminal",
                    "reservation_id": reserved["reservation_id"],
                },
                expires_in_seconds=100,
                start_daemon=False,
            )
        )

    assert monitor_service.event_status(reserved["reservation_id"])[
        "bound_monitor_id"
    ] is None
    assert monitor_service.ledger.list(include_terminal=True) == []


@pytest.mark.parametrize(
    ("terminal_status", "exit_code"), [("succeeded", 0), ("failed", 2)]
)
def test_command_terminal_event_authorizes_one_guarded_handling_wake(
    tmp_path, terminal_status: str, exit_code: int
) -> None:
    clock = Clock()
    app_server = FakeAppServer(
        [observation(), observation(), observation(runtime_status="idle")]
    )
    monitor_service = service(tmp_path, app_server, clock)
    reserved = monitor_service.reserve_terminal_event(command_reservation())
    registered = asyncio.run(
        monitor_service.register(
            thread_id="test-thread",
            condition={
                "type": "command_terminal",
                "reservation_id": reserved["reservation_id"],
            },
            expires_in_seconds=100,
            start_daemon=False,
        )
    )
    monitor_service.publish_terminal_event(
        reserved["reservation_id"],
        publish_token=reserved["publish_token"],
        terminal_event=command_terminal(
            status=terminal_status, exit_code=exit_code
        ),
    )

    asyncio.run(monitor_service.reconcile_once())
    status = monitor_service.status(registered["monitor_id"])

    assert status["state"] == "fired"
    assert len(app_server.activation_calls) == 1
    assert status["witness"][0]["classification"] == "command_termination"
    assert status["witness"][0]["task_success"] is False
    assert status["witness"][0]["lead_accepted"] is False
    assert status["terminal_event"]["terminal_event"]["status"] == terminal_status


def test_event_defer_checks_epoch_before_pause(tmp_path) -> None:
    clock = Clock()
    active = observation(goal_status="active")
    app_server = FakeAppServer([active])
    monitor_service = service(
        tmp_path, app_server, clock, event_ready=lambda _root: False
    )
    reserved = monitor_service.reserve_terminal_event(command_reservation())

    result = asyncio.run(
        monitor_service.defer(
            thread_id="test-thread",
            condition={
                "type": "command_terminal",
                "reservation_id": reserved["reservation_id"],
            },
            expires_in_seconds=100,
            idempotency_key="event-defer-1",
        )
    )

    assert result["state"] == "daemon_unavailable"
    assert app_server.pause_calls == []
    assert monitor_service.event_status(reserved["reservation_id"])[
        "bound_monitor_id"
    ] == result["monitor_id"]
    with pytest.raises(ConflictError):
        monitor_service.publish_terminal_event(
            reserved["reservation_id"],
            publish_token=reserved["publish_token"],
            terminal_event=command_terminal(),
        )


def test_worker_delivery_attestation_is_stored_once_and_never_means_acceptance(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repository = tmp_path / "repo"
    git(tmp_path, "init", str(repository))
    git(repository, "config", "user.name", "Test User")
    git(repository, "config", "user.email", "test@example.invalid")
    baseline = git_commit(repository, "allowed/base.txt", "base\n", "baseline")
    runtime = tmp_path / "runtime"
    runtime.mkdir()
    monitor_service = service(runtime, FakeAppServer([observation()]), Clock())
    reserved = monitor_service.reserve_terminal_event(
        {
            "kind": "worker_terminal",
            "expires_in_seconds": 100,
            "producer_task_id": "worker-1",
            "repository": str(repository / ".git"),
            "worktree": str(repository),
            "baseline_commit": baseline,
            "allowed_path_prefixes": ["allowed"],
        }
    )
    candidate = git_commit(
        repository, "allowed/candidate.txt", "candidate\n", "candidate"
    )
    envelope = {
        "kind": "worker_terminal",
        "outcome": "delivered",
        "producer_task_id": "worker-1",
        "candidate_oid": candidate,
    }

    published = monitor_service.publish_terminal_event(
        reserved["reservation_id"],
        publish_token=reserved["publish_token"],
        terminal_event=envelope,
    )
    monkeypatch.setattr(
        "codex_wake_me_up.service.attest_candidate",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("reattested")),
    )
    replay = monitor_service.publish_terminal_event(
        reserved["reservation_id"],
        publish_token=reserved["publish_token"],
        terminal_event=envelope,
    )

    assert published["git_attestation"]["status"] == "valid"
    assert published["git_attestation"]["lead_accepted"] is False
    assert replay == published


@pytest.mark.parametrize("outcome", ["delivered", "failed"])
def test_worker_terminal_rejects_task_identity_mismatch_before_attestation(
    tmp_path, monkeypatch: pytest.MonkeyPatch, outcome: str
) -> None:
    """Removing the frozen producer-task comparison must make this test fail."""

    repository = tmp_path / "repo"
    git(tmp_path, "init", str(repository))
    git(repository, "config", "user.name", "Test User")
    git(repository, "config", "user.email", "test@example.invalid")
    baseline = git_commit(repository, "allowed/base.txt", "base\n", "baseline")
    runtime = tmp_path / "runtime"
    runtime.mkdir()
    monitor_service = service(runtime, FakeAppServer([observation()]), Clock())
    reserved = monitor_service.reserve_terminal_event(
        {
            "kind": "worker_terminal",
            "expires_in_seconds": 100,
            "producer_task_id": "worker-1",
            "repository": str(repository / ".git"),
            "worktree": str(repository),
            "baseline_commit": baseline,
            "allowed_path_prefixes": ["allowed"],
        }
    )
    envelope = {
        "kind": "worker_terminal",
        "outcome": outcome,
        "producer_task_id": "worker-2",
    }
    if outcome == "delivered":
        envelope["candidate_oid"] = baseline
    else:
        envelope["reason"] = "worker stopped"
    monkeypatch.setattr(
        "codex_wake_me_up.service.attest_candidate",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("identity mismatch reached Git attestation")
        ),
    )

    with pytest.raises(ValidationError, match="producer task identity"):
        monitor_service.publish_terminal_event(
            reserved["reservation_id"],
            publish_token=reserved["publish_token"],
            terminal_event=envelope,
        )

    assert monitor_service.event_status(reserved["reservation_id"])["state"] == "reserved"


def test_invalid_worker_delivery_still_wakes_for_independent_review(tmp_path) -> None:
    repository = tmp_path / "repo"
    git(tmp_path, "init", str(repository))
    git(repository, "config", "user.name", "Test User")
    git(repository, "config", "user.email", "test@example.invalid")
    baseline = git_commit(repository, "allowed/base.txt", "base\n", "baseline")
    runtime = tmp_path / "runtime"
    runtime.mkdir()
    app_server = FakeAppServer(
        [observation(), observation(), observation(runtime_status="idle")]
    )
    monitor_service = service(runtime, app_server, Clock())
    reserved = monitor_service.reserve_terminal_event(
        {
            "kind": "worker_terminal",
            "expires_in_seconds": 100,
            "producer_task_id": "worker-1",
            "repository": str(repository / ".git"),
            "worktree": str(repository),
            "baseline_commit": baseline,
            "allowed_path_prefixes": ["allowed"],
        }
    )
    registered = asyncio.run(
        monitor_service.register(
            thread_id="test-thread",
            condition={
                "type": "worker_terminal",
                "reservation_id": reserved["reservation_id"],
            },
            expires_in_seconds=100,
            start_daemon=False,
        )
    )
    candidate = git_commit(repository, "outside.txt", "outside\n", "outside")
    published = monitor_service.publish_terminal_event(
        reserved["reservation_id"],
        publish_token=reserved["publish_token"],
        terminal_event={
            "kind": "worker_terminal",
            "outcome": "delivered",
            "producer_task_id": "worker-1",
            "candidate_oid": candidate,
        },
    )

    asyncio.run(monitor_service.reconcile_once())
    status = monitor_service.status(registered["monitor_id"])

    assert published["git_attestation"]["status"] == "out_of_scope"
    assert status["state"] == "fired"
    assert status["witness"][0]["classification"] == "invalid_delivery"
    assert status["witness"][0]["lead_accepted"] is False
    assert len(app_server.activation_calls) == 1


def test_corrupt_persisted_event_cardinality_fails_closed_in_deferred_mode(
    tmp_path,
) -> None:
    """A corrupt event AST must consume the observer-failure wake, not retry forever."""

    clock = Clock()
    app_server = FakeAppServer(
        [
            observation(),
            observation(runtime_status="idle"),
            observation(runtime_status="idle"),
        ]
    )
    monitor_service = service(tmp_path, app_server, clock)
    reserved = monitor_service.reserve_terminal_event(command_reservation())
    registered = asyncio.run(
        monitor_service.register(
            thread_id="test-thread",
            condition={
                "type": "command_terminal",
                "reservation_id": reserved["reservation_id"],
            },
            expires_in_seconds=100,
            start_daemon=False,
        )
    )
    corrupt = {
        "type": "all",
        "children": [
            {
                "type": "command_terminal",
                "reservation_id": reserved["reservation_id"],
            },
            {
                "type": "command_terminal",
                "reservation_id": reserved["reservation_id"],
            },
        ],
    }
    monitor_service.ledger._connection.execute(
        """
        UPDATE monitors
        SET condition_json = ?, mode = 'deferred', idle_barrier = 1
        WHERE monitor_id = ?
        """,
        (json.dumps(corrupt), registered["monitor_id"]),
    )
    monitor_service.ledger._connection.commit()

    asyncio.run(monitor_service.reconcile_once())
    status = monitor_service.status(registered["monitor_id"])

    assert status["state"] == "fired"
    assert status["wake_reason"] == "observer_failed"
    assert status["evidence"]["kind"] == "invalid_event_condition_contract"
    assert len(app_server.activation_calls) == 1


@pytest.mark.parametrize(
    "terminal_json",
    ["{not-json", json.dumps({"kind": "command_terminal"})],
)
def test_corrupt_persisted_event_snapshot_gets_one_observer_failure_wake(
    tmp_path, terminal_json: str
) -> None:
    clock = Clock()
    app_server = FakeAppServer(
        [
            observation(),
            observation(runtime_status="idle"),
            observation(runtime_status="idle"),
        ]
    )
    monitor_service = service(tmp_path, app_server, clock)
    reserved = monitor_service.reserve_terminal_event(command_reservation())
    registered = asyncio.run(
        monitor_service.register(
            thread_id="test-thread",
            condition={
                "type": "command_terminal",
                "reservation_id": reserved["reservation_id"],
            },
            expires_in_seconds=100,
            start_daemon=False,
        )
    )
    monitor_service.ledger._connection.execute(
        """
        UPDATE monitors
        SET mode = 'deferred', idle_barrier = 1
        WHERE monitor_id = ?
        """,
        (registered["monitor_id"],),
    )
    monitor_service.ledger._connection.execute(
        """
        UPDATE event_reservations
        SET terminal_json = ?, terminal_fingerprint = ?, state = 'terminal'
        WHERE reservation_id = ?
        """,
        (terminal_json, "0" * 64, reserved["reservation_id"]),
    )
    monitor_service.ledger._connection.commit()
    clock.value = 201.0

    asyncio.run(monitor_service.reconcile_once())
    status = monitor_service.status(registered["monitor_id"])

    assert status["state"] == "fired"
    assert status["wake_reason"] == "observer_failed"
    assert status["evidence"]["kind"] == "corrupt_bound_reservation"
    assert status["evaluation_count"] == 1
    assert status["terminal_event"]["state"] == "corrupt"
    assert len(app_server.activation_calls) == 1

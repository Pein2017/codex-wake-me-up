"""Operator CLI for local diagnostics and receipt publication."""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path
from typing import Any

from .app_server import AppServerClient
from .daemon import (
    DaemonLock,
    assert_delivery_runtime_compatible,
    assert_event_schema_compatible,
    main as daemon_main,
)
from .payloads import load_private_json_payload
from .runtime import codex_home_for_runtime_root, runtime_root
from .service import MonitorService


def _root(value: Path | None) -> Path:
    root = value.resolve() if value is not None else runtime_root()
    codex_home_for_runtime_root(root)
    return root


def _print(value: Any) -> None:
    print(json.dumps(value, sort_keys=True, indent=2, default=str))


async def _doctor(root: Path, thread_id: str | None) -> dict[str, Any]:
    async with AppServerClient(codex_home_for_runtime_root(root)) as app_server:
        result = await app_server.doctor(thread_id)
    result["runtime_root"] = str(root)
    result["goals_feature_note"] = (
        "verified against the supplied target" if thread_id else "supply --thread-id to verify goals"
    )
    return result


async def _reconcile(root: Path) -> list[dict[str, Any]]:
    with DaemonLock(root):
        service = MonitorService(root)
        service.recover_after_daemon_start()
        return await service.reconcile_once()


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="codex-wake-me-up")
    parser.add_argument("--runtime-root", type=Path, default=None)
    subparsers = parser.add_subparsers(dest="command", required=True)

    doctor = subparsers.add_parser("doctor", help="check the local Codex control plane")
    doctor.add_argument("--thread-id")

    subparsers.add_parser("list", help="list durable monitor state")

    wait = subparsers.add_parser(
        "wait-for-event", help="arm ThreadDelivery from a private JSON payload"
    )
    wait.add_argument("--payload", type=Path, required=True)
    defer = subparsers.add_parser(
        "defer-goal-until-event",
        help="arm explicit legacy GoalDelivery from a private JSON payload",
    )
    defer.add_argument("--payload", type=Path, required=True)
    status = subparsers.add_parser("status", help="inspect one monitor")
    status.add_argument("--monitor-id", required=True)
    cancel = subparsers.add_parser("cancel", help="cancel one monitor")
    cancel.add_argument("--monitor-id", required=True)

    reconcile = subparsers.add_parser("reconcile", help="run one monitor evaluation pass")
    reconcile.add_argument(
        "--unsafe-with-daemon",
        action="store_true",
        help="allow a manual pass even if a daemon may be running",
    )

    receipt = subparsers.add_parser("receipt", help="atomically publish a monitor receipt")
    receipt.add_argument("--monitor-id", required=True)
    receipt.add_argument("--token", required=True)
    receipt.add_argument("--status", choices=("success", "failed"), required=True)

    event_reserve = subparsers.add_parser(
        "event-reserve", help="reserve one terminal event from a private JSON payload"
    )
    event_reserve.add_argument("--payload", type=Path, required=True)
    event_status = subparsers.add_parser(
        "event-status", help="inspect one redacted event reservation"
    )
    event_status.add_argument("--reservation-id", required=True)
    event_cancel = subparsers.add_parser(
        "event-cancel", help="cancel one unbound event reservation"
    )
    event_cancel.add_argument("--reservation-id", required=True)
    event_heartbeat = subparsers.add_parser(
        "event-heartbeat", help="publish a heartbeat from a private JSON payload"
    )
    event_heartbeat.add_argument("--payload", type=Path, required=True)
    event_publish = subparsers.add_parser(
        "event-publish", help="publish a terminal event from a private JSON payload"
    )
    event_publish.add_argument("--payload", type=Path, required=True)
    compatibility = subparsers.add_parser(
        "event-compatibility-check",
        help="preflight a target daemon epoch before replacing current source",
    )
    compatibility.add_argument("--supported-event-epoch", type=int, required=True)
    delivery_compatibility = subparsers.add_parser(
        "delivery-compatibility-check",
        help="preflight delivery epoch and exact queued pointers before downgrade",
    )
    delivery_compatibility.add_argument(
        "--supported-delivery-epoch", type=int, required=True
    )

    daemon = subparsers.add_parser("daemon", help="run the long-lived monitor daemon")
    daemon.add_argument("--interval-seconds", type=float, default=2.0)
    daemon.add_argument("--once", action="store_true")
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    arguments = parser.parse_args(argv)
    root = _root(arguments.runtime_root)
    try:
        if arguments.command == "doctor":
            _print(asyncio.run(_doctor(root, arguments.thread_id)))
            return 0
        if arguments.command == "list":
            _print(MonitorService(root).list())
            return 0
        if arguments.command == "wait-for-event":
            payload = load_private_json_payload(arguments.payload)
            _print(asyncio.run(MonitorService(root).wait_for_event(**payload)))
            return 0
        if arguments.command == "defer-goal-until-event":
            payload = load_private_json_payload(arguments.payload)
            _print(asyncio.run(MonitorService(root).defer(**payload)))
            return 0
        if arguments.command == "status":
            _print(MonitorService(root).status(arguments.monitor_id))
            return 0
        if arguments.command == "cancel":
            _print(MonitorService(root).cancel(arguments.monitor_id))
            return 0
        if arguments.command == "reconcile":
            # A normal operator should let the lock-owning daemon reconcile.
            # This manual command remains a diagnostic surface, so it spells
            # out the concurrency risk instead of silently racing it.
            if not arguments.unsafe_with_daemon:
                raise RuntimeError(
                    "reconcile is diagnostic only; pass --unsafe-with-daemon after confirming no daemon owns the runtime"
                )
            _print(asyncio.run(_reconcile(root)))
            return 0
        if arguments.command == "receipt":
            _print(
                MonitorService(root).publish_receipt(
                    monitor_id=arguments.monitor_id,
                    token=arguments.token,
                    status=arguments.status,
                )
            )
            return 0
        if arguments.command == "event-reserve":
            payload = load_private_json_payload(arguments.payload)
            descriptor = payload.pop("publisher_descriptor_path", None)
            _print(
                MonitorService(root).reserve_terminal_event(
                    payload, descriptor_path=descriptor
                )
            )
            return 0
        if arguments.command == "event-status":
            _print(MonitorService(root).event_status(arguments.reservation_id))
            return 0
        if arguments.command == "event-cancel":
            _print(
                MonitorService(root).cancel_terminal_event(arguments.reservation_id)
            )
            return 0
        if arguments.command == "event-heartbeat":
            payload = load_private_json_payload(arguments.payload)
            _print(
                MonitorService(root).publish_event_heartbeat(
                    str(payload["reservation_id"]),
                    publish_token=str(payload["publish_token"]),
                    heartbeat=payload["heartbeat"],
                )
            )
            return 0
        if arguments.command == "event-publish":
            payload = load_private_json_payload(arguments.payload)
            _print(
                MonitorService(root).publish_terminal_event(
                    str(payload["reservation_id"]),
                    publish_token=str(payload["publish_token"]),
                    terminal_event=payload["terminal_event"],
                )
            )
            return 0
        if arguments.command == "event-compatibility-check":
            assert_event_schema_compatible(
                root, supported_event_epoch=arguments.supported_event_epoch
            )
            _print(
                {
                    "compatible": True,
                    "supported_event_epoch": arguments.supported_event_epoch,
                }
            )
            return 0
        if arguments.command == "delivery-compatibility-check":
            asyncio.run(
                assert_delivery_runtime_compatible(
                    root,
                    supported_delivery_epoch=arguments.supported_delivery_epoch,
                )
            )
            _print(
                {
                    "compatible": True,
                    "supported_delivery_epoch": arguments.supported_delivery_epoch,
                }
            )
            return 0
        if arguments.command == "daemon":
            daemon_arguments = ["--runtime-root", str(root), "--interval-seconds", str(arguments.interval_seconds)]
            if arguments.once:
                daemon_arguments.append("--once")
            return daemon_main(daemon_arguments)
        raise AssertionError(f"unhandled command: {arguments.command}")
    except Exception as exc:
        print(f"codex-wake-me-up: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())

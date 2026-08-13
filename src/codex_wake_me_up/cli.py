"""Operator CLI for local diagnostics and receipt publication."""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path
from typing import Any

from .app_server import AppServerClient
from .daemon import DaemonLock, main as daemon_main
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

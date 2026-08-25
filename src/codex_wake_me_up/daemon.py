"""Detached polling daemon for durable wake-up monitors."""

from __future__ import annotations

import argparse
import asyncio
import fcntl
import os
import sys
from contextlib import AbstractContextManager
from pathlib import Path
from typing import Any, Callable

from .app_server import AppServerClient
from .delivery import DeliveryKind
from .ledger import Ledger
from .runtime import (
    DELIVERY_CAPABILITY_EPOCH,
    EVENT_CAPABILITY_EPOCH,
    codex_home_for_runtime_root,
    runtime_root,
    write_heartbeat,
)
from .service import MonitorService


class DaemonLock(AbstractContextManager["DaemonLock"]):
    """An advisory local lock: exactly one daemon evaluates a ledger at once."""

    def __init__(self, root: Path):
        self.path = root / "daemon.lock"
        self.handle = None

    def __enter__(self) -> "DaemonLock":
        self.handle = self.path.open("a+", encoding="utf-8")
        try:
            fcntl.flock(self.handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            self.handle.close()
            self.handle = None
            raise RuntimeError("another codex-wake-me-up daemon owns this runtime root") from exc
        self.handle.seek(0)
        self.handle.truncate()
        self.handle.write(f"pid={os.getpid()}\n")
        self.handle.flush()
        os.fsync(self.handle.fileno())
        return self

    def __exit__(self, _exc_type, _exc, _traceback) -> None:
        if self.handle is not None:
            try:
                fcntl.flock(self.handle.fileno(), fcntl.LOCK_UN)
            finally:
                self.handle.close()
                self.handle = None


def assert_event_schema_compatible(
    root: Path, *, supported_event_epoch: int
) -> None:
    ledger = Ledger(root)
    try:
        required = ledger.event_capability_required()
    finally:
        ledger.close()
    if required and supported_event_epoch != EVENT_CAPABILITY_EPOCH:
        raise RuntimeError(
            "bound event reservations require the exact event schema epoch"
        )


def assert_delivery_schema_compatible(
    root: Path, *, supported_delivery_epoch: int
) -> None:
    ledger = Ledger(root)
    try:
        required = ledger.delivery_capability_required()
    finally:
        ledger.close()
    if required and supported_delivery_epoch != DELIVERY_CAPABILITY_EPOCH:
        raise RuntimeError(
            "nonterminal thread deliveries require the exact delivery schema epoch"
        )


async def assert_delivery_runtime_compatible(
    root: Path,
    *,
    supported_delivery_epoch: int,
    app_server_factory: Callable[[], Any] | None = None,
) -> None:
    """Also refuse downgrade while Core still stores a matching queue pointer."""

    if supported_delivery_epoch == DELIVERY_CAPABILITY_EPOCH:
        return
    assert_delivery_schema_compatible(
        root, supported_delivery_epoch=supported_delivery_epoch
    )
    ledger = Ledger(root)
    try:
        records = [
            record
            for record in ledger.list(include_terminal=True)
            if record.delivery_kind == DeliveryKind.THREAD
        ]
    finally:
        ledger.close()
    factory = app_server_factory or (
        lambda: AppServerClient(
            codex_home_for_runtime_root(root), experimental_api=True
        )
    )
    for record in records:
        delivery = record.delivery or {}
        async with factory() as app_server:
            observation = await app_server.inspect_thread_delivery(
                thread_id=str(delivery.get("thread_id", "")),
                delivery_id=str(delivery.get("delivery_id", "")),
                expected_pointer_digest=str(delivery.get("pointer_digest", "")),
            )
        if int(observation.get("queue_matches", 0)) + int(
            observation.get("queue_modified", 0)
        ):
            raise RuntimeError(
                "matching queue pointer requires the exact delivery schema epoch"
            )


async def run_daemon(root: Path, *, interval_seconds: float, once: bool = False) -> int:
    if interval_seconds <= 0:
        raise ValueError("interval_seconds must be positive")
    assert_event_schema_compatible(
        root, supported_event_epoch=EVENT_CAPABILITY_EPOCH
    )
    assert_delivery_schema_compatible(
        root, supported_delivery_epoch=DELIVERY_CAPABILITY_EPOCH
    )
    service = MonitorService(root)
    with DaemonLock(root):
        service.recover_after_daemon_start()
        while True:
            write_heartbeat(root)
            await service.reconcile_once()
            write_heartbeat(root)
            if once:
                return 0
            await asyncio.sleep(interval_seconds)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="run the codex-wake-me-up daemon")
    parser.add_argument("--runtime-root", type=Path, default=None)
    parser.add_argument("--interval-seconds", type=float, default=2.0)
    parser.add_argument("--once", action="store_true")
    return parser


def main(argv: list[str] | None = None) -> int:
    arguments = build_parser().parse_args(argv)
    root = arguments.runtime_root.resolve() if arguments.runtime_root else runtime_root()
    root.mkdir(mode=0o700, parents=True, exist_ok=True)
    try:
        return asyncio.run(
            run_daemon(root, interval_seconds=arguments.interval_seconds, once=arguments.once)
        )
    except RuntimeError as exc:
        print(f"codex-wake-me-up daemon: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
    codex_home_for_runtime_root,

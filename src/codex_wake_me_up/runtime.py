"""Runtime-root, daemon-heartbeat, and local process helpers."""

from __future__ import annotations

import json
import fcntl
import os
import subprocess
import sys
import time
from contextlib import AbstractContextManager
from pathlib import Path
from typing import Any, Callable


RUNTIME_DIR_NAME = "codex-wake-me-up"


def resolve_codex_home(value: str | Path | None = None) -> Path:
    """Resolve one canonical CODEx home without depending on a shell cwd."""

    raw = value or os.environ.get("CODEX_HOME") or (Path.home() / ".codex")
    return Path(raw).expanduser().resolve()


def runtime_root(value: str | Path | None = None) -> Path:
    root = resolve_codex_home(value) / "runtime" / RUNTIME_DIR_NAME
    root.mkdir(mode=0o700, parents=True, exist_ok=True)
    try:
        root.chmod(0o700)
    except PermissionError:
        pass
    return root


def codex_home_for_runtime_root(root: str | Path) -> Path:
    """Derive CODEX_HOME only from the documented runtime-root layout."""

    resolved = Path(root).resolve()
    if resolved.name != RUNTIME_DIR_NAME or resolved.parent.name != "runtime":
        raise RuntimeError(
            "runtime root must be $CODEX_HOME/runtime/codex-wake-me-up; "
            "an arbitrary directory cannot safely identify an app-server"
        )
    return resolved.parents[1]


def control_socket(codex_home: str | Path | None = None) -> Path:
    return resolve_codex_home(codex_home) / "app-server-control" / "app-server-control.sock"


def atomic_write_json(path: Path, value: Any, *, mode: int = 0o600) -> None:
    """Publish a small receipt/heartbeat atomically in the private runtime root."""

    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    payload = json.dumps(value, sort_keys=True, separators=(",", ":"))
    fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, mode)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        if temporary.exists():
            try:
                temporary.unlink()
            except FileNotFoundError:
                pass


def read_json(path: Path) -> dict[str, Any] | None:
    try:
        with path.open("r", encoding="utf-8") as handle:
            value = json.load(handle)
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return None
    return value if isinstance(value, dict) else None


def heartbeat_path(root: Path) -> Path:
    return root / "daemon-heartbeat.json"


def write_heartbeat(root: Path) -> None:
    atomic_write_json(heartbeat_path(root), {"pid": os.getpid(), "at": time.time()})


def daemon_is_healthy(root: Path, *, max_age_seconds: float = 15.0) -> bool:
    value = read_json(heartbeat_path(root))
    if not value:
        return False
    try:
        pid = int(value["pid"])
        at = float(value["at"])
    except (KeyError, TypeError, ValueError):
        return False
    if time.time() - at > max_age_seconds:
        return False
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    return True


class DeferProtocolLock(AbstractContextManager["DeferProtocolLock"]):
    """Keep daemon recovery from racing one live pause protocol."""

    def __init__(self, root: Path):
        self.path = root / "defer-protocol.lock"
        self.handle = None

    def __enter__(self) -> "DeferProtocolLock":
        self.handle = self.path.open("a+", encoding="utf-8")
        try:
            fcntl.flock(self.handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            self.handle.close()
            self.handle = None
            raise RuntimeError("another defer protocol is already in progress") from exc
        return self

    def __exit__(self, _exc_type: Any, _exc: Any, _traceback: Any) -> None:
        if self.handle is not None:
            try:
                fcntl.flock(self.handle.fileno(), fcntl.LOCK_UN)
            finally:
                self.handle.close()
                self.handle = None


def ensure_daemon(root: Path) -> bool:
    """Start an isolated daemon only when a healthy one is not already known.

    The function intentionally returns after spawning. A subsequent status call
    exposes an absent/stale heartbeat as `unsupervised` rather than pretending
    that the daemon is durable supervision.
    """

    if daemon_is_healthy(root):
        return False
    environment = os.environ.copy()
    source_root = Path(__file__).resolve().parents[1]
    previous = environment.get("PYTHONPATH")
    environment["PYTHONPATH"] = (
        str(source_root) if not previous else f"{source_root}{os.pathsep}{previous}"
    )
    subprocess.Popen(
        [sys.executable, "-m", "codex_wake_me_up.daemon", "--runtime-root", str(root)],
        cwd=str(source_root.parent),
        env=environment,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
        close_fds=True,
    )
    return True


def ensure_daemon_ready(
    root: Path,
    *,
    timeout_seconds: float = 2.0,
    poll_interval_seconds: float = 0.05,
    starter: Callable[[Path], bool] = ensure_daemon,
    health_check: Callable[[Path], bool] = daemon_is_healthy,
    monotonic: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], None] = time.sleep,
) -> bool:
    """Start supervision if needed and positively observe it within a bound."""

    if timeout_seconds <= 0 or poll_interval_seconds <= 0:
        raise ValueError("daemon readiness bounds must be positive")
    if health_check(root):
        return True
    try:
        starter(root)
    except (OSError, RuntimeError, subprocess.SubprocessError):
        return False
    deadline = monotonic() + timeout_seconds
    while monotonic() < deadline:
        if health_check(root):
            return True
        remaining = deadline - monotonic()
        if remaining <= 0:
            break
        sleep(min(poll_interval_seconds, remaining))
    return health_check(root)

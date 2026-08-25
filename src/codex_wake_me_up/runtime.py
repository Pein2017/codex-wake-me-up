"""Runtime-root, daemon-heartbeat, and local process helpers."""

from __future__ import annotations

import hashlib
import json
import fcntl
import os
import stat
import subprocess
import sys
import tempfile
import time
from contextlib import AbstractContextManager
from pathlib import Path
from typing import Any, Callable

from .models import ValidationError


RUNTIME_DIR_NAME = "codex-wake-me-up"
EVENT_CAPABILITY_EPOCH = 1
DELIVERY_CAPABILITY_EPOCH = 1


def _compute_loaded_source_identity() -> str:
    digest = hashlib.sha256()
    package_root = Path(__file__).resolve().parent
    for name in (
        "conditions.py",
        "app_server.py",
        "daemon.py",
        "delivery.py",
        "git_attestation.py",
        "ledger.py",
        "models.py",
        "runtime.py",
        "service.py",
        "terminal_events.py",
    ):
        path = package_root / name
        digest.update(name.encode("utf-8"))
        digest.update(b"\0")
        if path.exists():
            digest.update(path.read_bytes())
        digest.update(b"\0")
    return digest.hexdigest()


_LOADED_SOURCE_IDENTITY = _compute_loaded_source_identity()


def loaded_source_identity() -> str:
    return _LOADED_SOURCE_IDENTITY


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


def validate_private_output_path(path: Path) -> Path:
    """Resolve a private, current-user-owned output location without symlinks."""

    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    try:
        parent = path.parent.resolve(strict=True)
        parent_metadata = parent.stat()
    except OSError as exc:
        raise ValidationError("output parent must be a usable private directory") from exc
    if (
        not stat.S_ISDIR(parent_metadata.st_mode)
        or parent_metadata.st_uid != os.geteuid()
        or stat.S_IMODE(parent_metadata.st_mode) & 0o077
    ):
        raise ValidationError(
            "output parent must be a current-user-owned private directory"
        )
    selected = parent / path.name
    try:
        destination_metadata = selected.lstat()
    except FileNotFoundError:
        return selected
    except OSError as exc:
        raise ValidationError("output destination cannot be inspected safely") from exc
    if stat.S_ISLNK(destination_metadata.st_mode):
        raise ValidationError("output destination must not be a symlink")
    if (
        not stat.S_ISREG(destination_metadata.st_mode)
        or destination_metadata.st_uid != os.geteuid()
    ):
        raise ValidationError(
            "output destination must be a current-user-owned regular file"
        )
    return selected


def atomic_write_json(path: Path, value: Any, *, mode: int = 0o600) -> None:
    """Publish JSON atomically through a unique forced-mode temporary file."""

    if mode & 0o077:
        raise ValidationError("atomic JSON output mode must be private")
    selected = validate_private_output_path(path)
    payload = json.dumps(value, sort_keys=True, separators=(",", ":"))
    fd, temporary_name = tempfile.mkstemp(
        prefix=f".{selected.name}.", suffix=".tmp", dir=selected.parent
    )
    temporary = Path(temporary_name)
    try:
        os.fchmod(fd, mode)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, selected)
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
    atomic_write_json(
        heartbeat_path(root),
        {
            "pid": os.getpid(),
            "at": time.time(),
            "event_capability_epoch": EVENT_CAPABILITY_EPOCH,
            "delivery_capability_epoch": DELIVERY_CAPABILITY_EPOCH,
            "loaded_source_identity": loaded_source_identity(),
        },
    )


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


def daemon_event_capable(root: Path, *, max_age_seconds: float = 15.0) -> bool:
    if not daemon_is_healthy(root, max_age_seconds=max_age_seconds):
        return False
    value = read_json(heartbeat_path(root))
    if not value:
        return False
    try:
        pid = int(value["pid"])
    except (KeyError, TypeError, ValueError):
        return False
    return (
        value.get("event_capability_epoch") == EVENT_CAPABILITY_EPOCH
        and value.get("loaded_source_identity") == loaded_source_identity()
        and _daemon_lock_is_held_by(root, pid)
    )


def daemon_delivery_capable(root: Path, *, max_age_seconds: float = 15.0) -> bool:
    """Require the exact queue-delivery schema, source, and lock owner."""

    if not daemon_is_healthy(root, max_age_seconds=max_age_seconds):
        return False
    value = read_json(heartbeat_path(root))
    if not value:
        return False
    try:
        pid = int(value["pid"])
    except (KeyError, TypeError, ValueError):
        return False
    return (
        value.get("delivery_capability_epoch") == DELIVERY_CAPABILITY_EPOCH
        and value.get("event_capability_epoch") == EVENT_CAPABILITY_EPOCH
        and value.get("loaded_source_identity") == loaded_source_identity()
        and _daemon_lock_is_held_by(root, pid)
    )


def _daemon_lock_is_held_by(root: Path, advertised_pid: int) -> bool:
    """Prove the advertised PID also owns the runtime's reconciliation lock."""

    path = root / "daemon.lock"
    flags = os.O_RDWR | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags)
    except OSError:
        return False
    try:
        metadata = os.fstat(descriptor)
        if not stat.S_ISREG(metadata.st_mode) or metadata.st_uid != os.geteuid():
            return False
        os.lseek(descriptor, 0, os.SEEK_SET)
        raw = os.read(descriptor, 128).decode("ascii", errors="strict").strip()
        if raw != f"pid={advertised_pid}":
            return False
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return _process_owns_flock(metadata, advertised_pid)
        else:
            fcntl.flock(descriptor, fcntl.LOCK_UN)
            return False
    except (OSError, UnicodeError):
        return False
    finally:
        os.close(descriptor)


def _process_owns_flock(metadata: os.stat_result, advertised_pid: int) -> bool:
    """Fail closed unless Linux reports this PID as the inode's flock owner."""

    expected_device = (
        os.major(metadata.st_dev),
        os.minor(metadata.st_dev),
        metadata.st_ino,
    )
    try:
        with Path("/proc/locks").open("r", encoding="ascii") as handle:
            for line in handle:
                fields = line.split()
                if (
                    len(fields) < 8
                    or fields[1] != "FLOCK"
                    or fields[3] != "WRITE"
                ):
                    continue
                try:
                    owner_pid = int(fields[4])
                    major, minor, inode = fields[5].split(":")
                    device = (int(major, 16), int(minor, 16), int(inode))
                except (TypeError, ValueError):
                    continue
                if owner_pid == advertised_pid and device == expected_device:
                    return True
    except (OSError, UnicodeError):
        return False
    return False


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


def ensure_event_daemon_ready(root: Path, **kwargs: Any) -> bool:
    """Require the exact event schema and loaded source before event use."""

    return ensure_daemon_ready(root, health_check=daemon_event_capable, **kwargs)


def ensure_delivery_daemon_ready(root: Path, **kwargs: Any) -> bool:
    """Require exact event and queue delivery epochs before thread registration."""

    return ensure_daemon_ready(root, health_check=daemon_delivery_capable, **kwargs)

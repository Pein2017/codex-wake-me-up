"""Read-only Git delivery-scope capture and candidate attestation."""

from __future__ import annotations

import hashlib
import os
import re
import selectors
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Literal, TypedDict


MAX_RETAINED_PATHS = 256
MAX_PATH_PAYLOAD_BYTES = 64 * 1024
MAX_SCAN_BYTES = 8 * 1024 * 1024
_SUPPORTED_OBJECT_FORMATS = {"sha1": 40, "sha256": 64}
_HEX_OID = re.compile(r"^[0-9a-fA-F]+$")
_GIT_TIMEOUT_SECONDS = 5.0

AttestationStatus = Literal[
    "valid",
    "invalid_commit",
    "baseline_mismatch",
    "out_of_scope",
    "attestation_error",
]


@dataclass(frozen=True)
class GitDeliveryScope:
    """Immutable repository facts and path authority captured for one worker."""

    worktree_root: str
    common_dir: str
    object_format: str
    baseline_commit: str
    producer_task_id: str
    allowed_prefixes: tuple[str, ...]


@dataclass(frozen=True)
class GitAttestation:
    """Bounded, non-acceptance evidence for one candidate commit."""

    status: AttestationStatus
    candidate_commit: str | None
    path_count: int
    paths: tuple[str, ...]
    paths_digest: str | None
    paths_truncated: bool
    path_bytes_truncated: bool
    error: str | None = None


class _GitFailure(RuntimeError):
    pass


def capture_worktree_scope(
    *,
    worktree: str | Path,
    baseline_commit: str,
    producer_task_id: str,
    allowed_prefixes: Iterable[str],
) -> GitDeliveryScope:
    """Capture and validate the immutable Git facts for a worker reservation.

    ``worktree`` must name the worktree root itself, rather than merely a
    directory inside it.  The baseline must already be a full commit object ID.
    """

    root = _canonical_existing_path(worktree)
    if not isinstance(producer_task_id, str) or not producer_task_id.strip() or "\0" in producer_task_id:
        raise ValueError("producer_task_id must be a non-empty string")
    prefixes = _normalize_prefixes(allowed_prefixes)

    try:
        discovered_root, common_dir, object_format = _discover_repository(root)
        if discovered_root != root:
            raise ValueError("worktree must be the repository worktree root")
        oid_length = _oid_length(object_format)
        if not _is_full_oid(baseline_commit, oid_length):
            raise ValueError("baseline_commit must be a full object ID")
        if _object_type(root, baseline_commit) != "commit":
            raise ValueError("baseline_commit must resolve to a commit")
    except _GitFailure as error:
        raise ValueError("worktree is not a usable Git repository") from error

    return GitDeliveryScope(
        worktree_root=str(discovered_root),
        common_dir=str(common_dir),
        object_format=object_format,
        baseline_commit=baseline_commit.lower(),
        producer_task_id=producer_task_id,
        allowed_prefixes=prefixes,
    )


def attest_candidate(scope: GitDeliveryScope, candidate_commit: str) -> GitAttestation:
    """Read-only attest a single full candidate object against ``scope``.

    Adapter failures (including Git timeouts) deliberately become
    ``attestation_error`` evidence; they do not reject the worker's terminal
    publication or attempt any compensating Git action.
    """

    try:
        root, oid_length = _verify_captured_scope(scope)
    except (ValueError, _GitFailure, OSError) as error:
        return _error_attestation(candidate_commit, _error_code(error))

    if not _is_full_oid(candidate_commit, oid_length):
        return _empty_attestation("invalid_commit", candidate_commit)
    candidate = candidate_commit.lower()

    try:
        if _object_type(root, candidate) != "commit":
            return _empty_attestation("invalid_commit", candidate)
        if _object_type(root, scope.baseline_commit) != "commit":
            return _error_attestation(candidate, "captured_baseline_unavailable")
        if not _is_ancestor(root, scope.baseline_commit, candidate):
            return _empty_attestation("baseline_mismatch", candidate)
        parsed_paths = _changed_paths(root, scope.baseline_commit, candidate)
        post_root, post_oid_length = _verify_captured_scope(scope)
        if post_root != root or post_oid_length != oid_length:
            raise _GitFailure("repository identity changed during attestation")
    except (ValueError, OSError, _GitFailure) as error:
        return _error_attestation(candidate, _error_code(error))

    evidence = _path_evidence(parsed_paths)
    in_scope = all(_path_is_allowed(path, scope.allowed_prefixes) for path in parsed_paths)
    return GitAttestation(
        status="valid" if in_scope else "out_of_scope",
        candidate_commit=candidate,
        **evidence,
    )


def _canonical_existing_path(path: str | Path) -> Path:
    try:
        return Path(path).resolve(strict=True)
    except (OSError, TypeError) as error:
        raise ValueError("worktree must be an existing path") from error


def _discover_repository(root: Path) -> tuple[Path, Path, str]:
    discovered_root = _output_path(root, _git(root, "rev-parse", "--show-toplevel"))
    common_dir_raw = _output_text(_git(root, "rev-parse", "--git-common-dir"))
    common_dir = Path(common_dir_raw)
    if not common_dir.is_absolute():
        common_dir = root / common_dir
    try:
        common_dir = common_dir.resolve(strict=True)
    except OSError as error:
        raise _GitFailure("common directory cannot be resolved") from error
    object_format = _output_text(_git(root, "rev-parse", "--show-object-format"))
    _oid_length(object_format)
    return discovered_root, common_dir, object_format


def _verify_captured_scope(scope: GitDeliveryScope) -> tuple[Path, int]:
    if not isinstance(scope, GitDeliveryScope):
        raise ValueError("scope must be a GitDeliveryScope")
    root = _canonical_existing_path(scope.worktree_root)
    captured_common_dir = _canonical_existing_path(scope.common_dir)
    discovered_root, common_dir, object_format = _discover_repository(root)
    if discovered_root != root or common_dir != captured_common_dir or object_format != scope.object_format:
        raise ValueError("captured repository identity no longer matches")
    oid_length = _oid_length(scope.object_format)
    if not _is_full_oid(scope.baseline_commit, oid_length):
        raise ValueError("captured baseline is not a full object ID")
    _normalize_prefixes(scope.allowed_prefixes)
    return root, oid_length


def _normalize_prefixes(prefixes: Iterable[str]) -> tuple[str, ...]:
    if isinstance(prefixes, str):
        raise ValueError("allowed_prefixes must be an iterable of paths")
    try:
        values = tuple(prefixes)
    except TypeError as error:
        raise ValueError("allowed_prefixes must be an iterable of paths") from error
    normalized: set[str] = set()
    for value in values:
        if not isinstance(value, str) or not value or "\0" in value or value.startswith(("/", "\\", "-")):
            raise ValueError("allowed prefix is unsafe")
        if "\\" in value or re.match(r"^[A-Za-z]:", value):
            raise ValueError("allowed prefix is not repository-relative")
        parts = value.split("/")
        if any(part in (".", "..", ".git") for part in parts):
            raise ValueError("allowed prefix contains a forbidden component")
        cleaned = "/".join(part for part in parts if part)
        if not cleaned:
            raise ValueError("allowed prefix is empty or unrestricted")
        normalized.add(cleaned)
    if not normalized:
        raise ValueError("allowed_prefixes must not be empty")
    return tuple(sorted(normalized))


def _oid_length(object_format: str) -> int:
    try:
        return _SUPPORTED_OBJECT_FORMATS[object_format]
    except KeyError as error:
        raise ValueError("unsupported Git object format") from error


def _is_full_oid(value: object, expected_length: int) -> bool:
    return isinstance(value, str) and len(value) == expected_length and bool(_HEX_OID.fullmatch(value))


def _object_type(root: Path, oid: str) -> str | None:
    completed = _git(root, "cat-file", "-t", oid, check=False)
    if completed.returncode == 0:
        return _output_text(completed)
    return None


def _is_ancestor(root: Path, baseline: str, candidate: str) -> bool:
    completed = _git(root, "merge-base", "--is-ancestor", baseline, candidate, check=False)
    if completed.returncode == 0:
        return True
    if completed.returncode == 1:
        return False
    raise _GitFailure("merge-base failed")


def _changed_paths(root: Path, baseline: str, candidate: str) -> tuple[bytes, ...]:
    output = _git(
        root,
        "diff",
        "--name-only",
        "-z",
        "--no-renames",
        baseline,
        candidate,
        stdout_limit=MAX_SCAN_BYTES,
    ).stdout
    if output and not output.endswith(b"\0"):
        raise _GitFailure("Git returned a non-NUL-delimited path list")
    paths = output.split(b"\0")[:-1] if output else []
    if any(not path for path in paths):
        raise _GitFailure("Git returned an empty changed path")
    return tuple(sorted(set(paths)))


class _PathEvidence(TypedDict):
    path_count: int
    paths: tuple[str, ...]
    paths_digest: str
    paths_truncated: bool
    path_bytes_truncated: bool


def _path_evidence(paths: tuple[bytes, ...]) -> _PathEvidence:
    digest = hashlib.sha256(b"\0".join(paths)).hexdigest()
    retained: list[str] = []
    payload_bytes = 0
    path_bytes_truncated = False
    for path in paths:
        if len(retained) >= MAX_RETAINED_PATHS:
            break
        if payload_bytes + len(path) > MAX_PATH_PAYLOAD_BYTES:
            path_bytes_truncated = True
            continue
        retained.append(os.fsdecode(path))
        payload_bytes += len(path)
    paths_truncated = len(retained) != len(paths)
    return {
        "path_count": len(paths),
        "paths": tuple(retained),
        "paths_digest": digest,
        "paths_truncated": paths_truncated,
        "path_bytes_truncated": path_bytes_truncated,
    }


def _path_is_allowed(path: bytes, prefixes: tuple[str, ...]) -> bool:
    decoded = os.fsdecode(path)
    return any(decoded == prefix or decoded.startswith(prefix + "/") for prefix in prefixes)


def _empty_attestation(status: AttestationStatus, candidate: str) -> GitAttestation:
    return GitAttestation(
        status=status,
        candidate_commit=candidate.lower() if isinstance(candidate, str) else None,
        path_count=0,
        paths=(),
        paths_digest=None,
        paths_truncated=False,
        path_bytes_truncated=False,
    )


def _error_attestation(candidate: object, error: str) -> GitAttestation:
    return GitAttestation(
        status="attestation_error",
        candidate_commit=candidate.lower() if isinstance(candidate, str) else None,
        path_count=0,
        paths=(),
        paths_digest=None,
        paths_truncated=False,
        path_bytes_truncated=False,
        error=error,
    )


def _git(
    root: Path,
    *arguments: str,
    check: bool = True,
    stdout_limit: int | None = None,
) -> subprocess.CompletedProcess[bytes]:
    environment = {
        key: value for key, value in os.environ.items() if not key.startswith("GIT_")
    }
    environment.update({"GIT_OPTIONAL_LOCKS": "0", "LC_ALL": "C", "LANG": "C"})
    argv = (
        "git",
        "--no-replace-objects",
        "-c",
        "core.hooksPath=/dev/null",
        "-C",
        str(root),
        *arguments,
    )
    try:
        completed = (
            subprocess.run(
                argv,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                check=False,
                shell=False,
                env=environment,
                timeout=_GIT_TIMEOUT_SECONDS,
            )
            if stdout_limit is None
            else _run_bounded_git(argv, environment, stdout_limit)
        )
    except (OSError, subprocess.TimeoutExpired) as error:
        raise _GitFailure("Git invocation failed") from error
    if check and completed.returncode != 0:
        raise _GitFailure("Git command failed")
    return completed


def _run_bounded_git(
    argv: tuple[str, ...], environment: dict[str, str], stdout_limit: int
) -> subprocess.CompletedProcess[bytes]:
    """Run one Git scan with a hard wall-time and captured-stdout ceiling."""

    if stdout_limit < 0:
        raise _GitFailure("invalid Git stdout limit")
    process = subprocess.Popen(
        argv,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        shell=False,
        env=environment,
    )
    assert process.stdout is not None and process.stderr is not None
    selector = selectors.DefaultSelector()
    selector.register(process.stdout, selectors.EVENT_READ, "stdout")
    selector.register(process.stderr, selectors.EVENT_READ, "stderr")
    stdout = bytearray()
    stderr = bytearray()
    deadline = time.monotonic() + _GIT_TIMEOUT_SECONDS
    try:
        while selector.get_map():
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise subprocess.TimeoutExpired(argv, _GIT_TIMEOUT_SECONDS)
            events = selector.select(remaining)
            if not events:
                raise subprocess.TimeoutExpired(argv, _GIT_TIMEOUT_SECONDS)
            for key, _ in events:
                chunk = os.read(key.fd, 64 * 1024)
                if not chunk:
                    selector.unregister(key.fileobj)
                    continue
                if key.data == "stdout":
                    if len(stdout) + len(chunk) > stdout_limit:
                        raise _GitFailure("Git path scan exceeded the byte ceiling")
                    stdout.extend(chunk)
                elif len(stderr) < 64 * 1024:
                    stderr.extend(chunk[: 64 * 1024 - len(stderr)])
        remaining = max(0.0, deadline - time.monotonic())
        returncode = process.wait(timeout=remaining)
    except BaseException:
        process.kill()
        process.wait()
        raise
    finally:
        selector.close()
        process.stdout.close()
        process.stderr.close()
    return subprocess.CompletedProcess(argv, returncode, bytes(stdout), bytes(stderr))


def _output_text(completed: subprocess.CompletedProcess[bytes]) -> str:
    return completed.stdout.rstrip(b"\n").decode("ascii")


def _output_path(root: Path, completed: subprocess.CompletedProcess[bytes]) -> Path:
    value = os.fsdecode(completed.stdout.rstrip(b"\n"))
    try:
        return Path(value).resolve(strict=True)
    except OSError as error:
        raise _GitFailure("Git returned an unusable repository path") from error


def _error_code(error: BaseException) -> str:
    if isinstance(error, ValueError):
        return str(error)
    return "git_adapter_failed"

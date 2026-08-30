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
GitRefClassification = Literal[
    "unchanged",
    "fast_forward",
    "ref_rewrite",
    "ref_deleted",
    "head_retarget",
    "observer_error",
]
GitRefAncestry = Literal["same", "descendant", "diverged", "deleted"]
GitRefObserverError = Literal[
    "invalid_binding",
    "repository_identity_changed",
    "git_failed",
    "malformed_git_output",
    "ref_unresolvable",
    "observation_race",
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


@dataclass(frozen=True)
class GitRefBinding:
    """Immutable identity and exact direct-ref state captured at registration."""

    worktree_root: str
    common_dir: str
    git_dir: str
    object_format: str
    ref: str
    direct_oid: str
    peeled_commit: str
    head_target: str | None
    worktree_device: int
    worktree_inode: int
    common_dir_device: int
    common_dir_inode: int
    git_dir_device: int
    git_dir_inode: int


@dataclass(frozen=True)
class GitRefSample:
    """Bounded one-shot comparison evidence for a captured Git ref."""

    classification: GitRefClassification
    changed: bool
    old_direct_oid: str | None
    new_direct_oid: str | None
    old_peeled_commit: str | None
    new_peeled_commit: str | None
    old_head_target: str | None
    new_head_target: str | None
    ancestry: GitRefAncestry | None
    coalesced: bool
    error: GitRefObserverError | None = None


class _GitFailure(RuntimeError):
    pass


class _GitRefBindingFailure(RuntimeError):
    pass


class _GitRefIdentityFailure(RuntimeError):
    pass


class _GitRefMalformedFailure(RuntimeError):
    pass


class _GitRefResolutionFailure(RuntimeError):
    pass


class _GitRefRaceFailure(RuntimeError):
    pass


@dataclass(frozen=True)
class _RawGitRef:
    direct_oid: str
    peeled_commit: str
    head_target: str | None


def capture_git_ref(*, worktree: str | Path, ref: str) -> GitRefBinding:
    """Capture one exact local direct ref without changing repository state."""

    try:
        requested_root = Path(worktree)
    except TypeError as error:
        raise ValueError("worktree must be an absolute local path") from error
    if not requested_root.is_absolute():
        raise ValueError("worktree must be an absolute local path")
    root = _canonical_existing_path(requested_root)
    _validate_git_ref_shape(ref)

    try:
        discovered_root, common_dir, object_format = _discover_repository(root)
        if discovered_root != root:
            raise ValueError("worktree must be the repository worktree root")
        oid_length = _oid_length(object_format)
        git_dir = _discover_git_dir(root)
        if ref != "HEAD":
            _require_valid_direct_ref(root, ref)
        root_identity = _path_identity(root)
        common_identity = _path_identity(common_dir)
        git_dir_identity = _path_identity(git_dir)
        observed = _read_git_ref(root, ref, oid_length, allow_missing=False)
        assert observed is not None
        binding = GitRefBinding(
            worktree_root=str(root),
            common_dir=str(common_dir),
            git_dir=str(git_dir),
            object_format=object_format,
            ref=ref,
            direct_oid=observed.direct_oid,
            peeled_commit=observed.peeled_commit,
            head_target=observed.head_target,
            worktree_device=root_identity[0],
            worktree_inode=root_identity[1],
            common_dir_device=common_identity[0],
            common_dir_inode=common_identity[1],
            git_dir_device=git_dir_identity[0],
            git_dir_inode=git_dir_identity[1],
        )
        _verify_git_ref_binding(binding)
        confirmed = _read_git_ref(root, ref, oid_length, allow_missing=False)
        _verify_git_ref_binding(binding)
        if confirmed != observed:
            raise _GitRefRaceFailure("ref changed while it was captured")
    except ValueError:
        raise
    except _GitRefResolutionFailure as error:
        raise ValueError("ref must exist and peel to a commit") from error
    except (
        _GitFailure,
        _GitRefBindingFailure,
        _GitRefIdentityFailure,
        _GitRefMalformedFailure,
        _GitRefRaceFailure,
    ) as error:
        raise ValueError("Git ref binding cannot be proven") from error
    return binding


def sample_git_ref(binding: GitRefBinding) -> GitRefSample:
    """Revalidate and compare one captured ref, returning only fixed/bounded data."""

    try:
        root, oid_length = _verify_git_ref_binding(binding)
        first = _read_git_ref(root, binding.ref, oid_length, allow_missing=True)
        _verify_git_ref_binding(binding)
        second = _read_git_ref(root, binding.ref, oid_length, allow_missing=True)
        _verify_git_ref_binding(binding)
        if first != second:
            raise _GitRefRaceFailure("ref changed while it was observed")
        return _classify_git_ref(binding, root, first)
    except (
        ValueError,
        OSError,
        _GitFailure,
        _GitRefBindingFailure,
        _GitRefIdentityFailure,
        _GitRefMalformedFailure,
        _GitRefResolutionFailure,
        _GitRefRaceFailure,
    ) as error:
        return _git_ref_error_sample(_git_ref_error_code(error))


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


def _discover_git_dir(root: Path) -> Path:
    value = Path(os.fsdecode(_git(root, "rev-parse", "--git-dir").stdout.rstrip(b"\n")))
    if not value.is_absolute():
        value = root / value
    try:
        return value.resolve(strict=True)
    except OSError as error:
        raise _GitFailure("Git directory cannot be resolved") from error


def _path_identity(path: Path) -> tuple[int, int]:
    try:
        stat = path.stat()
    except OSError as error:
        raise _GitRefIdentityFailure("repository path cannot be identified") from error
    return stat.st_dev, stat.st_ino


def _validate_git_ref_shape(ref: object) -> None:
    if ref == "HEAD":
        return
    if (
        not isinstance(ref, str)
        or not ref.startswith("refs/")
        or ref.startswith("refs/remotes/")
        or len(ref.encode("utf-8")) > 1024
        or any(ord(character) < 32 or ord(character) == 127 for character in ref)
    ):
        raise ValueError("ref must be literal HEAD or one bounded local refs/... name")


def _require_valid_direct_ref(root: Path, ref: str) -> None:
    completed = _git(root, "check-ref-format", ref, check=False)
    if completed.returncode != 0:
        raise ValueError("ref must be a valid full direct refs/... name")
    symbolic = _git(root, "symbolic-ref", "-q", ref, check=False)
    if symbolic.returncode == 0:
        raise ValueError("ref must name a direct ref, not a symbolic ref")
    if symbolic.returncode != 1:
        raise _GitFailure("Git symbolic-ref check failed")


def _verify_git_ref_binding(binding: GitRefBinding) -> tuple[Path, int]:
    if not isinstance(binding, GitRefBinding):
        raise _GitRefBindingFailure("binding has the wrong type")
    try:
        _validate_git_ref_shape(binding.ref)
        if binding.ref != "HEAD" and binding.head_target is not None:
            raise ValueError("direct ref binding cannot contain a HEAD target")
        if binding.ref == "HEAD" and binding.head_target is not None:
            _validate_git_ref_shape(binding.head_target)
        if not all(
            isinstance(value, int) and value >= 0
            for value in (
                binding.worktree_device,
                binding.worktree_inode,
                binding.common_dir_device,
                binding.common_dir_inode,
                binding.git_dir_device,
                binding.git_dir_inode,
            )
        ):
            raise ValueError("binding contains an invalid path identity")
        oid_length = _oid_length(binding.object_format)
        if not _is_full_oid(binding.direct_oid, oid_length) or not _is_full_oid(
            binding.peeled_commit, oid_length
        ):
            raise ValueError("binding contains an invalid object ID")
    except (TypeError, UnicodeError, ValueError) as error:
        raise _GitRefBindingFailure("stored Git ref binding is malformed") from error

    try:
        root = _canonical_existing_path(binding.worktree_root)
        common = _canonical_existing_path(binding.common_dir)
        git_dir = _canonical_existing_path(binding.git_dir)
    except ValueError as error:
        raise _GitRefIdentityFailure("captured repository paths are unavailable") from error
    if (
        str(root) != binding.worktree_root
        or str(common) != binding.common_dir
        or str(git_dir) != binding.git_dir
    ):
        raise _GitRefIdentityFailure("captured repository paths changed")
    if _path_identity(root) != (binding.worktree_device, binding.worktree_inode) or _path_identity(
        common
    ) != (binding.common_dir_device, binding.common_dir_inode):
        raise _GitRefIdentityFailure("captured repository path identity changed")
    if _path_identity(git_dir) != (binding.git_dir_device, binding.git_dir_inode):
        raise _GitRefIdentityFailure("captured Git directory identity changed")
    try:
        discovered_root, discovered_common, object_format = _discover_repository(root)
        discovered_git_dir = _discover_git_dir(root)
    except (ValueError, _GitFailure) as error:
        raise _GitRefIdentityFailure("captured repository cannot be rediscovered") from error
    if (
        discovered_root != root
        or discovered_common != common
        or discovered_git_dir != git_dir
        or object_format != binding.object_format
    ):
        raise _GitRefIdentityFailure("captured repository identity changed")
    if _path_identity(root) != (binding.worktree_device, binding.worktree_inode) or _path_identity(
        common
    ) != (binding.common_dir_device, binding.common_dir_inode):
        raise _GitRefIdentityFailure("repository identity changed while it was checked")
    if _path_identity(git_dir) != (binding.git_dir_device, binding.git_dir_inode):
        raise _GitRefIdentityFailure("Git directory identity changed while it was checked")
    try:
        if binding.ref != "HEAD":
            _require_valid_direct_ref(root, binding.ref)
        if binding.head_target is not None:
            _require_valid_direct_ref(root, binding.head_target)
    except ValueError as error:
        raise _GitRefBindingFailure("stored Git ref name is malformed") from error
    return root, oid_length


def _read_git_ref(
    root: Path, ref: str, oid_length: int, *, allow_missing: bool
) -> _RawGitRef | None:
    head_target: str | None = None
    if ref == "HEAD":
        symbolic = _git(root, "symbolic-ref", "-q", "HEAD", check=False)
        if symbolic.returncode == 0:
            head_target = _output_ref_name(symbolic)
            try:
                _validate_git_ref_shape(head_target)
                _require_valid_direct_ref(root, head_target)
            except ValueError as error:
                raise _GitRefResolutionFailure("HEAD has an unusable symbolic target") from error
        elif symbolic.returncode != 1:
            raise _GitFailure("symbolic-ref failed")
        direct_oid = _resolve_head_oid(root, oid_length)
        if direct_oid is None:
            raise _GitRefResolutionFailure("HEAD is unborn or unresolvable")
    else:
        exists = _git(root, "show-ref", "--verify", "--quiet", ref, check=False)
        if exists.returncode == 1:
            if allow_missing:
                return None
            raise _GitRefResolutionFailure("ref is absent")
        if exists.returncode != 0:
            raise _GitFailure("show-ref failed")
        completed = _git(root, "show-ref", "--verify", "--hash", ref, check=False)
        if completed.returncode != 0:
            raise _GitFailure("show-ref failed")
        direct_oid = _output_oid(completed, oid_length)
    peeled_commit = _peel_commit(root, direct_oid, oid_length)
    return _RawGitRef(
        direct_oid=direct_oid,
        peeled_commit=peeled_commit,
        head_target=head_target,
    )


def _resolve_head_oid(root: Path, oid_length: int) -> str | None:
    completed = _git(root, "rev-parse", "--verify", "HEAD", check=False)
    if completed.returncode == 1 or completed.returncode == 128:
        return None
    if completed.returncode != 0:
        raise _GitFailure("rev-parse failed")
    return _output_oid(completed, oid_length)


def _peel_commit(root: Path, direct_oid: str, oid_length: int) -> str:
    completed = _git(root, "rev-parse", "--verify", f"{direct_oid}^{{commit}}", check=False)
    if completed.returncode != 0:
        raise _GitRefResolutionFailure("ref does not peel to a commit")
    return _output_oid(completed, oid_length)


def _output_oid(completed: subprocess.CompletedProcess[bytes], oid_length: int) -> str:
    value = completed.stdout.rstrip(b"\n")
    try:
        decoded = value.decode("ascii")
    except UnicodeDecodeError as error:
        raise _GitRefMalformedFailure("Git returned a non-ASCII object ID") from error
    if not _is_full_oid(decoded, oid_length):
        raise _GitRefMalformedFailure("Git returned a malformed object ID")
    return decoded.lower()


def _output_ref_name(completed: subprocess.CompletedProcess[bytes]) -> str:
    value = completed.stdout.rstrip(b"\n")
    try:
        decoded = value.decode("utf-8")
    except UnicodeDecodeError as error:
        raise _GitRefMalformedFailure("Git returned an undecodable ref name") from error
    if not decoded or b"\n" in value or b"\r" in value or b"\0" in value:
        raise _GitRefMalformedFailure("Git returned a malformed ref name")
    return decoded


def _classify_git_ref(
    binding: GitRefBinding, root: Path, current: _RawGitRef | None
) -> GitRefSample:
    if current is None:
        return _git_ref_sample(binding, None, "ref_deleted", "deleted")

    if current.peeled_commit == binding.peeled_commit:
        ancestry: GitRefAncestry = "same"
    elif _is_ancestor(root, binding.peeled_commit, current.peeled_commit):
        ancestry = "descendant"
    else:
        ancestry = "diverged"

    if binding.ref == "HEAD" and current.head_target != binding.head_target:
        classification: GitRefClassification = "head_retarget"
    elif current.direct_oid == binding.direct_oid:
        classification = "unchanged"
    elif current.peeled_commit == binding.peeled_commit:
        classification = "ref_rewrite"
    elif ancestry == "descendant":
        classification = "fast_forward"
    else:
        classification = "ref_rewrite"
    return _git_ref_sample(binding, current, classification, ancestry)


def _git_ref_sample(
    binding: GitRefBinding,
    current: _RawGitRef | None,
    classification: GitRefClassification,
    ancestry: GitRefAncestry,
) -> GitRefSample:
    changed = classification != "unchanged"
    return GitRefSample(
        classification=classification,
        changed=changed,
        old_direct_oid=binding.direct_oid,
        new_direct_oid=current.direct_oid if current else None,
        old_peeled_commit=binding.peeled_commit,
        new_peeled_commit=current.peeled_commit if current else None,
        old_head_target=binding.head_target,
        new_head_target=current.head_target if current else None,
        ancestry=ancestry,
        coalesced=changed,
    )


def _git_ref_error_sample(error: GitRefObserverError) -> GitRefSample:
    return GitRefSample(
        classification="observer_error",
        changed=False,
        old_direct_oid=None,
        new_direct_oid=None,
        old_peeled_commit=None,
        new_peeled_commit=None,
        old_head_target=None,
        new_head_target=None,
        ancestry=None,
        coalesced=False,
        error=error,
    )


def _git_ref_error_code(error: BaseException) -> GitRefObserverError:
    if isinstance(error, _GitRefBindingFailure):
        return "invalid_binding"
    if isinstance(error, _GitRefIdentityFailure):
        return "repository_identity_changed"
    if isinstance(error, _GitRefMalformedFailure):
        return "malformed_git_output"
    if isinstance(error, _GitRefResolutionFailure):
        return "ref_unresolvable"
    if isinstance(error, _GitRefRaceFailure):
        return "observation_race"
    return "git_failed"


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

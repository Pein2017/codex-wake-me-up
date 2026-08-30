from __future__ import annotations

import hashlib
import subprocess
from dataclasses import replace
from pathlib import Path

import pytest

import codex_wake_me_up.git_attestation as git_attestation
from codex_wake_me_up.git_attestation import (
    attest_candidate,
    capture_git_ref,
    capture_worktree_scope,
    sample_git_ref,
)


def git(repository: Path, *args: str) -> str:
    completed = subprocess.run(
        ("git", "-C", str(repository), *args),
        check=True,
        capture_output=True,
        text=True,
    )
    return completed.stdout.strip()


def write(repository: Path, name: str, content: str) -> None:
    path = repository / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content)


def commit(repository: Path, message: str) -> str:
    git(repository, "add", "-A")
    git(repository, "commit", "-m", message)
    return git(repository, "rev-parse", "HEAD")


@pytest.fixture
def repository(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    git(tmp_path, "init", str(repo))
    git(repo, "config", "user.name", "Test User")
    git(repo, "config", "user.email", "test@example.invalid")
    write(repo, "allowed/base.txt", "base\n")
    commit(repo, "baseline")
    return repo


def scope(repository: Path, prefixes: tuple[str, ...] = ("allowed",)):
    return capture_worktree_scope(
        worktree=repository,
        baseline_commit=git(repository, "rev-parse", "HEAD"),
        producer_task_id="worker-17",
        allowed_prefixes=prefixes,
    )


def snapshot_git_state(repository: Path) -> dict[str, str]:
    return {
        "head": git(repository, "rev-parse", "HEAD"),
        "status": git(repository, "status", "--porcelain=v1", "-z"),
        "index": git(repository, "ls-files", "-s"),
        "branch": git(repository, "symbolic-ref", "-q", "HEAD"),
        "remote": git(repository, "remote"),
        "config": git(repository, "config", "--list", "--show-origin"),
    }


def test_attests_a_descendant_commit_with_canonical_frozen_scope(repository: Path) -> None:
    frozen = scope(repository)
    write(repository, "allowed/new.txt", "candidate\n")
    candidate = commit(repository, "candidate")

    result = attest_candidate(frozen, candidate)

    assert frozen.worktree_root == str(repository.resolve())
    assert frozen.producer_task_id == "worker-17"
    assert frozen.allowed_prefixes == ("allowed",)
    assert frozen.baseline_commit != candidate
    assert len(frozen.baseline_commit) in (40, 64)
    assert result.status == "valid"
    assert result.candidate_commit == candidate
    assert result.path_count == 1
    assert result.paths == ("allowed/new.txt",)
    assert result.paths_digest
    assert not result.paths_truncated
    assert not result.path_bytes_truncated


def test_rejects_missing_and_foreign_candidate_commits(repository: Path, tmp_path: Path) -> None:
    frozen = scope(repository)
    missing = "0" * len(frozen.baseline_commit)

    missing_result = attest_candidate(frozen, missing)
    assert missing_result.status == "invalid_commit"

    foreign = tmp_path / "foreign"
    git(tmp_path, "init", str(foreign))
    git(foreign, "config", "user.name", "Test User")
    git(foreign, "config", "user.email", "test@example.invalid")
    write(foreign, "other.txt", "other\n")
    foreign_commit = commit(foreign, "foreign")
    foreign_result = attest_candidate(frozen, foreign_commit)

    assert foreign_result.status == "invalid_commit"


def test_rejects_non_descendant_candidate(repository: Path) -> None:
    baseline = git(repository, "rev-parse", "HEAD")
    write(repository, "allowed/main.txt", "main\n")
    commit(repository, "main")
    git(repository, "checkout", "--orphan", "other")
    git(repository, "rm", "-rf", ".")
    write(repository, "allowed/other.txt", "other\n")
    candidate = commit(repository, "other root")
    frozen = capture_worktree_scope(
        worktree=repository,
        baseline_commit=baseline,
        producer_task_id="worker-17",
        allowed_prefixes=("allowed",),
    )

    result = attest_candidate(frozen, candidate)

    assert result.status == "baseline_mismatch"


def test_reports_out_of_scope_paths_even_when_the_evidence_is_bounded(repository: Path) -> None:
    frozen = scope(repository)
    write(repository, "allowed/ok.txt", "ok\n")
    write(repository, "outside.txt", "outside\n")
    candidate = commit(repository, "mixed")

    result = attest_candidate(frozen, candidate)

    assert result.status == "out_of_scope"
    assert result.path_count == 2
    assert result.paths == ("allowed/ok.txt", "outside.txt")


def test_bounds_retained_paths_and_bytes_but_digests_the_complete_path_set(repository: Path) -> None:
    frozen = scope(repository)
    for index in range(256):
        write(repository, f"allowed/many/{index:03d}.txt", "x\n")
    exact_candidate = commit(repository, "exact path limit")

    exact_result = attest_candidate(frozen, exact_candidate)
    assert exact_result.status == "valid"
    assert exact_result.path_count == 256
    assert len(exact_result.paths) == 256
    assert not exact_result.paths_truncated
    assert not exact_result.path_bytes_truncated

    write(repository, "allowed/257.txt", "x\n")
    newline_name = "allowed/newline\nname.txt"
    write(repository, newline_name, "newline\n")
    over_candidate = commit(repository, "over path limit")
    result = attest_candidate(frozen, over_candidate)

    raw_paths = subprocess.run(
        ("git", "-C", str(repository), "diff", "--name-only", "-z", "--no-renames", frozen.baseline_commit, over_candidate),
        check=True,
        capture_output=True,
    ).stdout
    expected_digest = hashlib.sha256(b"\0".join(sorted(set(raw_paths.split(b"\0")[:-1])))).hexdigest()
    assert result.status == "valid"
    assert result.path_count == 258
    assert len(result.paths) == 256
    assert result.paths_digest == expected_digest
    assert result.paths_truncated

    byte_scope = scope(repository)
    for index in range(256):
        write(repository, f"allowed/big/{index:03d}-" + ("x" * 245), "large\n")
    huge_candidate = commit(repository, "large path")
    huge_result = attest_candidate(byte_scope, huge_candidate)

    assert huge_result.status == "valid"
    assert huge_result.path_count == 256
    assert len(huge_result.paths) < 256
    assert huge_result.path_bytes_truncated
    assert huge_result.paths_truncated
    assert huge_result.paths_digest


@pytest.mark.parametrize(
    "prefixes",
    [(), ("",), (".",), ("../outside",), ("allowed/../other",), (".git",), ("allowed/.git/file",), ("/absolute",), ("-option",)],
)
def test_rejects_unsafe_or_unrestricted_allowed_scope(repository: Path, prefixes: tuple[str, ...]) -> None:
    with pytest.raises(ValueError):
        scope(repository, prefixes)


def test_rejects_nonrepository_nonroot_and_unsafe_baseline(repository: Path, tmp_path: Path) -> None:
    with pytest.raises(ValueError):
        capture_worktree_scope(
            worktree=tmp_path / "not-a-repository",
            baseline_commit=git(repository, "rev-parse", "HEAD"),
            producer_task_id="worker-17",
            allowed_prefixes=("allowed",),
        )
    with pytest.raises(ValueError):
        capture_worktree_scope(
            worktree=repository / "allowed",
            baseline_commit=git(repository, "rev-parse", "HEAD"),
            producer_task_id="worker-17",
            allowed_prefixes=("allowed",),
        )
    with pytest.raises(ValueError):
        capture_worktree_scope(
            worktree=repository,
            baseline_commit="HEAD",
            producer_task_id="worker-17",
            allowed_prefixes=("allowed",),
        )


def test_attestation_does_not_change_refs_index_worktree_config_or_remotes(repository: Path) -> None:
    frozen = scope(repository)
    write(repository, "allowed/new.txt", "candidate\n")
    candidate = commit(repository, "candidate")
    before = snapshot_git_state(repository)

    result = attest_candidate(frozen, candidate)
    after = snapshot_git_state(repository)

    assert result.status == "valid"
    assert after == before


def test_attestation_ignores_hostile_inherited_git_environment(
    repository: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    frozen = scope(repository)
    write(repository, "allowed/new.txt", "candidate\n")
    candidate = commit(repository, "candidate")
    foreign_git_dir = tmp_path / "foreign.git"
    git(tmp_path, "init", "--bare", str(foreign_git_dir))

    monkeypatch.setenv("GIT_DIR", str(foreign_git_dir))
    monkeypatch.setenv("GIT_WORK_TREE", str(tmp_path / "wrong-worktree"))
    monkeypatch.setenv("GIT_INDEX_FILE", str(tmp_path / "wrong-index"))
    monkeypatch.setenv("GIT_NAMESPACE", "hostile")
    monkeypatch.setenv("GIT_OBJECT_DIRECTORY", str(tmp_path / "wrong-objects"))
    monkeypatch.setenv("GIT_ALTERNATE_OBJECT_DIRECTORIES", str(tmp_path / "alternate"))

    assert attest_candidate(frozen, candidate).status == "valid"


def test_attestation_disables_replacement_objects(repository: Path) -> None:
    frozen = scope(repository)
    write(repository, "allowed/new.txt", "candidate\n")
    candidate = commit(repository, "candidate")
    tree = git(repository, "rev-parse", f"{candidate}^{{tree}}")
    unrelated_commit = git(repository, "commit-tree", tree, "-m", "replacement")
    git(repository, "replace", candidate, unrelated_commit)

    result = attest_candidate(frozen, candidate)

    assert result.status == "valid"


def test_allowed_prefix_matching_uses_path_component_boundaries(repository: Path) -> None:
    frozen = scope(repository, ("allowed/a",))
    write(repository, "allowed/ab/file.txt", "collision\n")
    candidate = commit(repository, "prefix collision")

    assert attest_candidate(frozen, candidate).status == "out_of_scope"


def test_complete_path_scan_over_byte_ceiling_is_attestation_error(
    repository: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    frozen = scope(repository)
    write(repository, "allowed/" + "x" * 80, "large path\n")
    candidate = commit(repository, "large scan")
    monkeypatch.setattr(git_attestation, "MAX_SCAN_BYTES", 32)

    result = attest_candidate(frozen, candidate)

    assert result.status == "attestation_error"
    assert result.paths_digest is None
    assert result.path_count == 0


def test_repository_identity_is_rechecked_after_path_scan(
    repository: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    frozen = scope(repository)
    write(repository, "allowed/new.txt", "candidate\n")
    candidate = commit(repository, "candidate")
    original = git_attestation._verify_captured_scope
    calls = 0

    def counted(scope_value):
        nonlocal calls
        calls += 1
        return original(scope_value)

    monkeypatch.setattr(git_attestation, "_verify_captured_scope", counted)

    assert attest_candidate(frozen, candidate).status == "valid"
    assert calls == 2


def test_git_ref_capture_and_fast_forward_are_exact_and_read_only(repository: Path) -> None:
    before = snapshot_git_state(repository)
    binding = capture_git_ref(worktree=repository, ref="HEAD")
    old_oid = binding.direct_oid
    old_commit = binding.peeled_commit
    head_target = git(repository, "symbolic-ref", "HEAD")

    unchanged = sample_git_ref(binding)
    assert unchanged.classification == "unchanged"
    assert unchanged.changed is False
    assert unchanged.error is None
    assert unchanged.coalesced is False

    write(repository, "allowed/next.txt", "next\n")
    new_commit = commit(repository, "next")
    after_commit = snapshot_git_state(repository)
    result = sample_git_ref(binding)

    assert binding.worktree_root == str(repository.resolve())
    assert Path(binding.common_dir).is_absolute()
    assert Path(binding.git_dir).is_absolute()
    assert binding.ref == "HEAD"
    assert binding.head_target == head_target
    assert binding.direct_oid == old_oid == old_commit
    assert result.classification == "fast_forward"
    assert result.changed is True
    assert result.old_direct_oid == old_oid
    assert result.new_direct_oid == new_commit
    assert result.old_peeled_commit == old_commit
    assert result.new_peeled_commit == new_commit
    assert result.ancestry == "descendant"
    assert result.coalesced is True
    assert result.error is None
    assert snapshot_git_state(repository) == after_commit
    assert before["remote"] == after_commit["remote"]


def test_git_ref_classifies_rewrite_delete_and_head_retarget(repository: Path) -> None:
    baseline = git(repository, "rev-parse", "HEAD")
    git(repository, "branch", "watched")
    branch_binding = capture_git_ref(worktree=repository, ref="refs/heads/watched")

    write(repository, "allowed/main.txt", "main\n")
    commit(repository, "main")
    git(repository, "checkout", "--orphan", "other")
    git(repository, "rm", "-rf", ".")
    write(repository, "allowed/other.txt", "other\n")
    unrelated = commit(repository, "other")
    git(repository, "update-ref", "refs/heads/watched", unrelated)

    rewritten = sample_git_ref(branch_binding)
    assert rewritten.classification == "ref_rewrite"
    assert rewritten.ancestry == "diverged"
    assert rewritten.old_peeled_commit == baseline
    assert rewritten.new_peeled_commit == unrelated

    git(repository, "update-ref", "-d", "refs/heads/watched")
    deleted = sample_git_ref(branch_binding)
    assert deleted.classification == "ref_deleted"
    assert deleted.new_direct_oid is None
    assert deleted.ancestry == "deleted"

    head_binding = capture_git_ref(worktree=repository, ref="HEAD")
    current = git(repository, "rev-parse", "HEAD")
    git(repository, "checkout", "--detach", current)

    retargeted = sample_git_ref(head_binding)
    assert retargeted.classification == "head_retarget"
    assert retargeted.old_head_target is not None
    assert retargeted.new_head_target is None
    assert retargeted.old_direct_oid == retargeted.new_direct_oid == current
    assert retargeted.ancestry == "same"


def test_git_ref_direct_object_rewrite_with_same_peeled_commit(repository: Path) -> None:
    commit_oid = git(repository, "rev-parse", "HEAD")
    git(repository, "tag", "-a", "one", "-m", "one", commit_oid)
    git(repository, "tag", "-a", "two", "-m", "two", commit_oid)
    tag_one = git(repository, "rev-parse", "refs/tags/one")
    tag_two = git(repository, "rev-parse", "refs/tags/two")
    git(repository, "update-ref", "refs/tags/watched", tag_one)
    binding = capture_git_ref(worktree=repository, ref="refs/tags/watched")

    git(repository, "update-ref", "refs/tags/watched", tag_two)
    result = sample_git_ref(binding)

    assert tag_one != tag_two
    assert result.classification == "ref_rewrite"
    assert result.old_direct_oid == tag_one
    assert result.new_direct_oid == tag_two
    assert result.old_peeled_commit == result.new_peeled_commit == commit_oid
    assert result.ancestry == "same"


@pytest.mark.parametrize(
    "ref",
    (
        "main",
        "HEAD~1",
        "refs/heads/main^{commit}",
        "refs/remotes/origin/main",
        "refs/heads/../main",
        "refs/heads/missing",
    ),
)
def test_git_ref_capture_rejects_non_direct_remote_or_absent_refs(repository: Path, ref: str) -> None:
    with pytest.raises(ValueError):
        capture_git_ref(worktree=repository, ref=ref)


def test_git_ref_rejects_non_head_symbolic_refs_at_capture_and_revalidation(
    repository: Path,
) -> None:
    git(repository, "branch", "watched")
    git(repository, "branch", "other")
    binding = capture_git_ref(worktree=repository, ref="refs/heads/watched")

    git(repository, "symbolic-ref", "refs/heads/watched", "refs/heads/other")

    with pytest.raises(ValueError):
        capture_git_ref(worktree=repository, ref="refs/heads/watched")
    sample = sample_git_ref(binding)
    assert sample.classification == "observer_error"
    assert sample.error == "invalid_binding"


def test_git_ref_capture_rejects_relative_nonroot_unborn_and_noncommit(
    repository: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    relative = Path(repository.name)
    monkeypatch.chdir(repository.parent)
    with pytest.raises(ValueError):
        capture_git_ref(worktree=relative, ref="HEAD")
    with pytest.raises(ValueError):
        capture_git_ref(worktree=repository / "allowed", ref="HEAD")

    unborn = tmp_path / "unborn"
    git(tmp_path, "init", str(unborn))
    with pytest.raises(ValueError):
        capture_git_ref(worktree=unborn, ref="HEAD")

    blob = git(repository, "hash-object", "-w", "allowed/base.txt")
    git(repository, "update-ref", "refs/tags/blob", blob)
    with pytest.raises(ValueError):
        capture_git_ref(worktree=repository, ref="refs/tags/blob")


def test_git_ref_sample_fails_closed_for_repository_replacement(
    repository: Path, tmp_path: Path
) -> None:
    binding = capture_git_ref(worktree=repository, ref="HEAD")
    moved = tmp_path / "moved"
    repository.rename(moved)
    git(tmp_path, "init", str(repository))
    git(repository, "config", "user.name", "Test User")
    git(repository, "config", "user.email", "test@example.invalid")
    write(repository, "replacement.txt", "replacement\n")
    commit(repository, "replacement")

    result = sample_git_ref(binding)

    assert result.classification == "observer_error"
    assert result.changed is False
    assert result.error == "repository_identity_changed"
    assert result.new_direct_oid is None


def test_git_ref_sample_scrubs_git_parse_resolution_and_race_errors(
    repository: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    binding = capture_git_ref(worktree=repository, ref="HEAD")

    monkeypatch.setattr(
        git_attestation,
        "_read_git_ref",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(git_attestation._GitFailure("secret stderr")),
    )
    failed = sample_git_ref(binding)
    assert failed.classification == "observer_error"
    assert failed.error == "git_failed"
    assert "secret" not in repr(failed)

    monkeypatch.undo()
    binding = capture_git_ref(worktree=repository, ref="HEAD")
    original = git_attestation._read_git_ref
    calls = 0

    def raced(*args, **kwargs):
        nonlocal calls
        calls += 1
        state = original(*args, **kwargs)
        if calls == 2:
            return git_attestation._RawGitRef(
                direct_oid="0" * len(state.direct_oid),
                peeled_commit=state.peeled_commit,
                head_target=state.head_target,
            )
        return state

    monkeypatch.setattr(git_attestation, "_read_git_ref", raced)
    raced_result = sample_git_ref(binding)
    assert raced_result.classification == "observer_error"
    assert raced_result.error == "observation_race"


def test_git_ref_sample_fails_closed_for_object_format_parse_and_resolution_errors(
    repository: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    git(repository, "branch", "watched")
    binding = capture_git_ref(worktree=repository, ref="refs/heads/watched")
    original_discover = git_attestation._discover_repository

    def wrong_format(root: Path):
        worktree, common, _object_format = original_discover(root)
        return worktree, common, "sha256"

    monkeypatch.setattr(git_attestation, "_discover_repository", wrong_format)
    mismatch = sample_git_ref(binding)
    assert mismatch.classification == "observer_error"
    assert mismatch.error == "repository_identity_changed"

    monkeypatch.undo()
    binding = capture_git_ref(worktree=repository, ref="refs/heads/watched")
    original_git = git_attestation._git

    def malformed(root: Path, *arguments: str, **kwargs):
        completed = original_git(root, *arguments, **kwargs)
        if arguments[:3] == ("show-ref", "--verify", "--hash"):
            return subprocess.CompletedProcess(completed.args, 0, b"not-an-oid\n", b"")
        return completed

    monkeypatch.setattr(git_attestation, "_git", malformed)
    malformed_result = sample_git_ref(binding)
    assert malformed_result.classification == "observer_error"
    assert malformed_result.error == "malformed_git_output"

    monkeypatch.undo()
    git(repository, "update-ref", "refs/tags/watched", git(repository, "rev-parse", "HEAD"))
    binding = capture_git_ref(worktree=repository, ref="refs/tags/watched")
    blob = git(repository, "hash-object", "-w", "allowed/base.txt")
    git(repository, "update-ref", "refs/tags/watched", blob)
    unresolvable = sample_git_ref(binding)
    assert unresolvable.classification == "observer_error"
    assert unresolvable.error == "ref_unresolvable"


def test_git_ref_sample_fails_closed_for_post_read_identity_race(
    repository: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    binding = capture_git_ref(worktree=repository, ref="HEAD")
    original = git_attestation._verify_git_ref_binding
    calls = 0

    def identity_race(binding_value):
        nonlocal calls
        calls += 1
        if calls == 2:
            raise git_attestation._GitRefIdentityFailure("secret replacement detail")
        return original(binding_value)

    monkeypatch.setattr(git_attestation, "_verify_git_ref_binding", identity_race)
    result = sample_git_ref(binding)
    assert result.classification == "observer_error"
    assert result.error == "repository_identity_changed"
    assert "secret" not in repr(result)


def test_git_ref_sample_rejects_malformed_stored_binding(repository: Path) -> None:
    binding = capture_git_ref(worktree=repository, ref="HEAD")

    result = sample_git_ref(replace(binding, direct_oid="HEAD"))

    assert result.classification == "observer_error"
    assert result.error == "invalid_binding"

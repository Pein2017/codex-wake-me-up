## 1. Freeze the lifecycle and race regressions

- [x] 1.1 Add heartbeat tests proving an explicit `accepting_work=false` is rejected by base, event, and delivery readiness while a current active heartbeat is accepted and a legacy heartbeat retains its prior compatibility; observe the retiring case fail against the current implementation.
- [x] 1.2 Add deterministic daemon-loop tests for a non-terminal row resetting idleness, an empty ledger exiting only after the 60-second grace, and work appearing after the retirement marker restoring acceptance; observe the idle-exit cases fail before implementation.
- [x] 1.3 Add one process-level startup race test in which a retiring generation still owns `daemon.lock` while concurrent callers request supervision; require exactly one replacement lock owner and no orphaned extra daemon, with exact PID cleanup in the test.
- [x] 1.4 Cover each affected error disposition: event registration and thread delivery remain `daemon_unavailable` before arm, deferred registration remains `daemon_unavailable` before pause, and a successful retirement writes no monitor terminal state or deletes ledger evidence.
- [x] 1.5 Record the pre-change full-suite failure set so final verification can compare failures rather than pass counts.

## 2. Implement bounded daemon residency

- [x] 2.1 Extend atomic daemon heartbeats with the accepting state and make all readiness predicates reject only an explicit retiring state without changing source/epoch/lock checks.
- [x] 2.2 Reuse the existing polling loop and non-terminal ledger query to track a monotonic 60-second idle grace, publish retirement before the final query, resume on newly durable work, and release the daemon lock on confirmed emptiness; keep `--once` unchanged.
- [x] 2.3 Add the runtime-root advisory start lock and make the default starter wait out an exact retiring lock owner, recheck health, spawn at most one replacement, and remain bounded until base readiness or failure.
- [x] 2.4 Keep event and delivery readiness layered on the serialized base startup, and verify that genuine startup failure preserves each caller's existing fail-closed or `unsupervised` evidence without retrying activation.

## 3. Verify source and installed behavior

- [x] 3.1 Run the focused lifecycle, runtime, and service tests in the `ms` Conda environment and demonstrate the load-bearing race test fails when the new acceptance/start coordination is disabled.
- [x] 3.2 Run the full test suite in the `ms` Conda environment and compare its failure set with the recorded baseline; run strict OpenSpec validation for this change.
- [x] 3.3 Install through the existing plugin replacement workflow, restart only the exact runtime-root lock owner, and verify `/proc` command line, `PYTHONPATH`, loaded source identity, accepting heartbeat, capability epochs, and kernel lock ownership. Receipt: `verification.md` (installed `0.1.0+codex.20260901013840`, replacement PID `1321833`).
- [x] 3.4 Run one bounded disposable fresh-task smoke: settle its monitor, prove the daemon exits after the grace without ledger loss, then register new work and prove one on-demand daemon generation becomes ready. Record any real app-server boundary that blocks the smoke rather than substituting a fake result. Receipt: `verification.md` (isolated runtime `/tmp/wake-residency-smoke-4a4zj6pl`, retired PID `1309060`, replacement PID `1309454`).

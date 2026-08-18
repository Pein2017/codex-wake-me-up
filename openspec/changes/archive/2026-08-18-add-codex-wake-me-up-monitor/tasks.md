## 1. Source Contract and Plugin Surface

- [x] 1.1 Scaffold the standalone `codex-wake-me-up/` source module and
  source-only Codex plugin manifest without installing it into the active app.
- [x] 1.2 Define validated canonical request/condition schemas, immutable
  paused-goal guards, monitor states, evidence classifications, and terminal
  outcome receipts.
- [x] 1.3 Add unit tests that reject raw shell predicates, invalid process
  identities, cross-host/unloaded targets, unsafe idempotency-key reuse, and
  heuristic continuation without explicit authorization by terminating without
  activation.

## 2. Durable Monitor and Typed Observers

- [x] 2.1 Implement the SQLite runtime ledger, atomic state transitions,
  idempotent registration, cancellation, expiry, daemon lock, and restart
  recovery for armed, claimed, and activating monitors, including a fully
  synchronized pre-send activation record and cancellation through `claimed`.
- [x] 2.2 Implement three-valued `all`/`any` condition evaluation with
  evidence records, satisfying-witness selection, and focused tests for time,
  composed conditions, and `any(receipt, time)` authorization.
- [x] 2.3 Implement GPU-stability, PID-exit, tmux-exit, and receipt-success
  observers with captured identity checks and tests for unknown/reuse/restart
  failure paths.
- [x] 2.4 Implement receipt publication with per-monitor tokens and atomic
  file replacement; test valid, malformed, and mismatched receipts.

## 3. Guarded Codex Continuation

- [x] 3.1 Implement the Unix-socket app-server adapter, initialization and
  protocol/permission doctor checks (including a read-only live goal check),
  canonical runtime-root validation, and a fake adapter test seam.
- [x] 3.2 Implement observe-register-observe target capture and the durable
  compare-and-set trigger claim, permitting an active caller to register its
  own paused goal but requiring idle only before activation, including
  target-changed and unloaded-target outcomes.
- [x] 3.3 Implement the single pre-recorded `thread/goal/set(status: active)`
  activation attempt and its confirmed, failed, uncertain, and
  `mis-targeted-activation` terminal receipts; assert that its serialized
  request contains exactly `threadId` and `status`, and verify it never calls
  `turn/start`, compensates, or retries.

## 4. Operator Interfaces

- [x] 4.1 Implement the host-local daemon runner and matching CLI commands
  for `doctor`, `list`, `reconcile`, and receipt publication, with detached
  spawn, lock, heartbeat, and unsupervised-status reporting.
- [x] 4.2 Implement MCP `wake_me_up`, `wake_me_up_status`, and
  `wake_me_up_cancel` tools that register against a currently loaded paused
  goal and expose evidence/state/outcome summaries.
- [x] 4.3 Add operator documentation with typed examples for delay, GPU,
  tmux, PID, and receipt conditions, the heuristic opt-in boundary, and the
  explicitly unsupported unloaded/cross-host behavior.

## 5. Verification and Review Gate

- [x] 5.1 Run the focused unit suite, manifest validation, static checks, and
  OpenSpec strict validation; repair any P0/P1 findings.
- [x] 5.2 Use `doctor` against the current local control socket and run a
  disposable-session smoke: arm a short time monitor for a new benign paused
  goal with explicit heuristic-continuation authorization, observe one guarded
  activation, and confirm the named real target was not read or changed by the
  smoke.
- [x] 5.3 Obtain an independent implementation review for state-machine,
  at-most-once, target-guard, socket-security, and test-scope compliance;
  address any accepted findings before handoff.
- [x] 5.4 Re-run focused verification, inspect the final diff and runtime
  residue, and record the implementation/verification evidence in this change
  without installing or globally enabling the plugin.

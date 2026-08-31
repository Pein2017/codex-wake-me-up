## 1. Freeze Terminal-Event Behavior With RED Tests

- [x] 1.1 Add failing normalization and reservation tests for complete-or-absent
  worker delivery scope, including partial-scope and changed-idempotency rejection.
- [x] 1.2 Add failing terminal-event and condition tests for `completed`, no
  candidate, no attestation, false task-success/lead-acceptance, and immutable
  identical retry.
- [x] 1.3 Add failing tests that reject scopeless `delivered`, `completed` with a
  candidate, wrong producer identity, wrong descriptor bearer, unsafe private
  files, and conflicting terminal rewrites.
- [x] 1.4 Add failing CLI tests for read-only descriptor preflight and
  descriptor/event-file publication through the existing service sink.

## 2. Admit Scopeless Completed Workers

- [x] 2.1 Make worker delivery scope complete-or-absent in reservation
  normalization and durable semantic/idempotency projections without changing
  the ledger schema.
- [x] 2.2 Add `completed` to worker normalization, status, condition witnesses,
  decision/audit projections, and payload budgets with no success or acceptance
  promotion.
- [x] 2.3 Preserve existing scoped `delivered` attestation and every bounded
  blocked, failed, cancelled, and settlement-uncertain path; reject delivery
  without complete frozen scope.

## 3. Expose The Private Descriptor Boundary

- [x] 3.1 Add a read-only CLI descriptor preflight that validates private-file
  ownership/mode, schema/kind, reservation lifecycle, token fingerprint, runtime
  root, and expected producer identity without changing state.
- [x] 3.2 Add descriptor-based CLI publication that reads a separate mode-0600
  terminal-event payload, delegates to `publish_worker_terminal_from_descriptor`,
  and emits redacted status only.
- [x] 3.3 Advance `EVENT_CAPABILITY_EPOCH`, loaded-source identity inputs, and
  compatibility tests so an older daemon cannot supervise `completed` events.

## 4. Document Mixed Agent Waiting

- [x] 4.1 Update README and wake-me-up terminal/subagent guidance with the
  cooperative native-L1 final-publish clause and the HarnessDock
  descriptor-in/terminal-event-out seam.
- [x] 4.2 Document one composed `all` monitor for all-settled wake, independent
  single-leaf monitors for continuing first-settlement control, typed `any`
  loser consumption, explicit expiry, and no automatic re-arm/follow-up.
- [x] 4.3 State the native missed-publish counterexample and the deferred
  host-observed ThreadId upgrade gate without claiming live child evidence.

## 5. Verify Source Acceptance

- [x] 5.1 Run focused terminal/event/CLI tests with observed RED receipts, then
  compare the full pytest failure set with the pre-change baseline.
- [x] 5.2 Run scoped Ruff, compileall, plugin/skill validation, strict OpenSpec
  validation, and `git diff --check` for the exact wake-me-up paths.
- [x] 5.3 Run a source-only vertical slice that reserves a scopeless worker,
  preflights its descriptor, publishes `completed` through the real CLI, binds
  its returned condition, and observes terminal non-success evidence.
- [x] 5.4 Stop before install, daemon restart, live Agent spend, archive, commit,
  or push; report those as separate authorization gates.

## 1. Queue-accepted re-arm lineage

- [x] 1.1 Add a sanitized regression reproducing rejection when a prior thread
  monitor is `queue_accepted`; verify it fails on the current lineage validator.
- [x] 1.2 Accept `queue_accepted` for lineage only and verify the parent remains
  nonterminal, receives exactly one queue-add, and contributes no inherited success.

## 2. Registration diagnostics

- [x] 2.1 Add heartbeat fixtures for missing/malformed, retiring, stale, dead,
  capability-mismatched, source-mismatched, and lock-mismatched delivery daemons;
  verify each produces one stable bounded reason.
- [x] 2.2 Add sanitized app-server failure fixtures for target resolution and
  condition preparation; verify each reports its stage before row creation.
- [x] 2.3 Implement read-only readiness diagnosis and stage-labelled registration
  failures without changing timeout, retry, fallback, or terminal-state behavior;
  verify the focused diagnostic tests pass.

## 3. Exact native worker settlement

- [x] 3.1 Freeze the user-approved Core wire fields for canonical task name, child
  thread, exact invocation/generation, and retained native status; update this
  change if the final Core contract differs and verify strict OpenSpec validation.
- [x] 3.2 After the Core read surface exists, add RED plugin adapter/condition tests
  for bind, completed-before-arm, completed, failed, interrupted,
  disappearance, and invocation reuse/mismatch.
- [x] 3.3 Implement the native-worker condition leaf using only the frozen Core
  binding and existing any/all plus root queue; verify all terminal outcomes remain
  settlement-only with `task_success=false` and `lead_accepted=false`.
- [x] 3.4 Run source-level composed any/all tests against the Core contract and leave
  runtime acceptance pending until installation is separately authorized.

## 4. Guidance and acceptance

- [x] 4.1 Update README and packaged skill/reference guidance for queue-accepted
  lineage, staged failures, and the exact native-worker availability boundary.
- [x] 4.2 Run focused tests, the full plugin suite, Ruff, compileall, and strict
  OpenSpec validation; diff failure sets against the pre-change baseline.
- [ ] 4.3 With separate authorization, install the exact Core/plugin sources and run
  two real native workers with root final, proving any wakes once after one exact
  invocation settles and all wakes once after both; include already-finished,
  failure, interruption, and invocation-reuse cases before claiming live acceptance.

## 5. Claimed delivery preflight recovery

- [x] 5.1 Add a sanitized claimed-monitor regression for repeated `thread/read`
  transport timeout followed by daemon-step recovery; prove it fails under the old
  catch-all terminal disposition and that no admission timestamp or queue receipt
  exists before recovery.
- [x] 5.2 Type transport failure at the app-server boundary and preserve only that
  subtype as claimed/unattempted; keep definitive preflight failures terminal and
  post-admission uncertainty on existing no-retry reconciliation.
- [x] 5.3 Cover trigger-expiry distinction, explicit cancellation before recovery,
  definitive denial, and post-send timeout with exact queue-add counts.
- [x] 5.4 Replay the full plugin suite, Ruff, compileall, skill validation, strict
  OpenSpec validation, and owned-path whitespace checks. Keep installed runtime and
  the already terminal historical row unchanged.

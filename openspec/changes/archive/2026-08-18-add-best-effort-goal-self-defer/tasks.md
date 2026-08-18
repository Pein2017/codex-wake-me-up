## 1. Durable defer protocol

- [x] 1.1 Add explicit defer monitor states, terminal receipts, and additive
  legacy-safe ledger storage for defer mode and its idle barrier.
- [x] 1.2 Add the narrow guarded App Server pause action and deterministic
  fakes/tests that assert its exact status-only payload, preserving the
  existing single guarded active-status continuation request.
- [x] 1.3 Implement watcher-readiness-before-pause, durable pause intent,
  response/re-observation checks, crash recovery, and idempotency semantics.

## 2. One-shot reconciliation and interfaces

- [x] 2.1 Gate a deferred armed monitor on a matching paused target becoming
  idle before condition evaluation or trigger claim, then preserve one guarded
  active-status continuation for both deferred and legacy monitors.
- [x] 2.2 Expose `wake_me_up_defer` with precise best-effort/error receipts and
  retain cancellation/expiry as non-compensating operations.
- [x] 2.3 Update the README and packaged skill with concise automatic-defer
  guidance, the 15-minute/no-independent-work trigger, and all safety limits.

## 3. Verification and delivery gate

- [x] 3.1 Add focused tests for ordering, all defer terminal paths, restart
  recovery, idempotency, idle barrier, and legacy monitor regression.
- [x] 3.2 Run the complete module test suite, static/plugin validation, and
  strict OpenSpec validation; inspect runtime residue without touching the
  named real task.
- [x] 3.3 Run a disposable-task local App Server pause-to-wake smoke, proving
  the installed monitor can arm after an active-goal defer and emit one guarded
  continuation result.
- [x] 3.4 Obtain an independent fixed-tree audit of state transitions,
  at-most-once behavior, target guards, and test scope; repair accepted P0/P1
  findings, then reinstall through the local marketplace cachebuster flow.

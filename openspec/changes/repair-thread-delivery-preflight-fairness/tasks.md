## 1. Exact preflight budget

- [x] 1.1 Raise the bounded app-server request budget to accommodate measured large-thread metadata reads while keeping the shared registration deadline; verify the request timeout remains typed as transport failure.
- [x] 1.2 Add focused preflight/admission regression coverage for the enlarged bounded budget, typed timeout, and normal single queue-admission path without a weaker fallback.

## 2. Fair reconciliation and backoff

- [x] 2.1 Order reconciliation so claimed/in-progress work runs before historical admitted pointers, preserving creation order within each priority; verify a fresh claimed monitor is attempted first.
- [x] 2.2 Persist bounded exponential `retry_after`/retry-count facts for transport/offline admitted and cancellation rows, skip rows before their retry time, and clear the facts after an online observation; verify no second queue add is possible.
- [x] 2.3 Cover cancellation and admission error paths with focused regression tests, including restart-readable reconciliation fields.

## 3. Operator surface

- [x] 3.1 Update the packaged skill/README guidance to explain preflight transport delays, fair retry state, and the continued no-blind-resend boundary; verify packaged files remain valid.

## 4. Verification and reinstall

- [x] 4.1 Run focused tests, full pytest, lint, compile, plugin validation, and strict OpenSpec validation; record the failure baseline and final result.
- [x] 4.2 Update the local cachebuster, reinstall from `coordexp-local`, restart only the exact old wake daemon after auditing its identity, and verify installed source identity plus a real app-server preflight/reconciliation smoke. The supplied monitor was already `cancelled`, so no new queue write was issued.

Verification evidence (2026-09-19 UTC): 470 pytest tests passed; focused
thread-delivery tests passed (101); Ruff, compileall, plugin validation, and
strict OpenSpec validation passed. The installed-cache daemon heartbeat reports
the same source identity as the worktree. A live final-cache preflight against
the supplied target returned `idle` with an empty queue in 15.09 seconds; its
cancelled monitor was not re-armed or resent.

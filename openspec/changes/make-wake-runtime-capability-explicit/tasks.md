## 1. Capability and safe pre-arm recovery

- [x] 1.1 Add failing adapter/service regressions for sanitized unsupported
  native-worker capability, indeterminate transport, and one read-only pre-arm
  retry; verify they demonstrate no ledger row or queue admission before the
  implementation.
- [x] 1.2 Implement bounded app-server rejection classification and a read-only
  tri-state capability report; verify focused app-server and service tests pass
  without exposing an unbounded Core method list.
- [x] 1.3 Implement exactly one typed pre-arm transport retry and structured
  MCP failure receipts; verify definitive failures remain non-retryable and
  post-admission reconciliation never sends a second queue add.

## 2. Observation and completion evidence

- [x] 2.1 Add a runtime-only thread observation mode and use it only for
  `thread_idle`; verify focused tests prove no goal request while goal-delivery
  guard tests remain green.
- [x] 2.2 Add RED worker-terminal payload, persistence, idempotency, and
  decision-report tests for declared invocation/result references; verify they
  reject empty or over-budget values and never assert success or acceptance.
- [x] 2.3 Implement bounded opaque worker references with explicit publisher
  provenance; verify `tests/test_terminal_events.py` and affected wake-report
  tests pass.

## 3. Delivery-consumption and ordinary MCP surface

- [x] 3.1 Add additive ledger metadata plus regressions for trusted-target
  status observation versus another caller; verify old rows decode unchanged and
  status observation never changes delivery or acceptance outcome.
- [x] 3.2 Add the scoped current-monitor query and concise armed summary; verify
  trusted caller scoping, absent-identity failure, and normal register/status/
  cancel behavior through public MCP tests.
- [x] 3.3 Update README and packaged wake-me-up guidance to query capability
  first, state the fail-closed native boundary, and describe result references
  and status consumption; validate the packaged skill's documented commands.

## 4. Acceptance

- [x] 4.1 Run targeted regressions, the full plugin suite, Ruff, compileall,
  whitespace checks, and strict OpenSpec validation; record failure sets against
  the pre-change baseline.
- [x] 4.2 Run the installed plugin's read-only capability report without
  restarting its live daemon; retain the observed unsupported native Core result
  as a HOLD rather than claiming a worker-to-lead end-to-end wake.
- [x] 4.3 Request one fresh read-only Astra-low audit of the fixed diff and
  acceptance evidence; address one reproducible blocking counterexample, then
  re-run its affected verification.

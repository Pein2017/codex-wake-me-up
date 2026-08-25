## 1. Contract Tests

- [x] 1.1 Add a public MCP regression proving `wait_for_event` derives the caller from `_meta.threadId`, omits `thread_id` from its model-facing schema, and rejects missing metadata before service invocation; observe the focused test fail for the intended reason.
- [x] 1.2 Add app-server ancestry regressions for direct root delivery, depth-one and depth-two root relay, malformed or missing parents, cycles, unsupported denied threads, and the 64-node bound; observe the focused tests fail for the intended reasons.
- [x] 1.3 Add service and delivery regressions proving semantic identity remains the origin, queue operations use only the frozen root target, status preserves origin and chain provenance, and no child continuation surface is called; observe the focused tests fail for the intended reasons.

## 2. Implementation

- [x] 2.1 Inject FastMCP request context into `wait_for_event`, extract and validate the trusted Codex caller UUID, and remove the public `thread_id` argument without changing legacy goal-delivery aliases.
- [x] 2.2 Implement bounded, cycle-checked app-server root resolution and require existing queue preflight on the exact resolved root.
- [x] 2.3 Persist origin, root delivery target, and relay-chain provenance while keeping idempotency and self-wait checks origin-bound and all queue lifecycle operations target-bound.
- [x] 2.4 Update the packaged skill and README to document caller binding, root-only waking, root-owned judgment, CLI explicit targeting, and the absence of automatic subagent follow-up.

## 3. Verification and Installation

- [x] 3.1 Run focused regressions, the complete test suite, static diagnostics, diff checks, and strict OpenSpec validation; compare failures against the pre-change baseline.
- [x] 3.2 Bump the local cachebuster and reinstall `codex-wake-me-up@coordexp-local` from the source-owned marketplace without publishing or committing.
- [x] 3.3 Restart and verify the exact lock-owning daemon cache/source, command line, `PYTHONPATH`, heartbeat epochs, and lock ownership, then record any live app-server acceptance boundary that could not be safely exercised in the current task.

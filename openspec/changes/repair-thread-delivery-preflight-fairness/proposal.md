## Why

A fired `ThreadDelivery` can remain `claimed/unattempted` when Core's exact
`thread/read` preflight takes slightly longer than the current 10-second RPC
budget. At the same time, old admitted pointers are reconciled serially before
new work, so one slow or large thread can delay every later wake.

## What Changes

- Give app-server RPCs a modestly larger request budget suitable for large
  thread metadata reads, while keeping the existing bounded registration
  budget.
- Reconcile ready-to-admit monitors before old admitted pointers, so a fresh
  claimed wake is not hidden behind stale history checks.
- Persist bounded exponential retry timing for transport/offline delivery
  reconciliation, including cancellation, to prevent a stale row from
  monopolizing every daemon cycle.
- Preserve exact target capability revalidation, one trigger claim, one queue
  admission attempt, FIFO ownership, and no-blind-resend behavior.

## Capabilities

### New Capabilities

None.

### Modified Capabilities

- `codex-wake-me-up-monitor`: bound thread-delivery RPC latency and require
  fair, backoff-aware reconciliation without weakening fail-closed admission.

## Impact

- `src/codex_wake_me_up/app_server.py` and `service.py` for request timing and
  monitor scheduling/backoff.
- Focused thread-delivery and daemon tests, plus the packaged operator guidance
  if the receipt/status wording needs clarification.
- No Core API change, alternate delivery path, database migration, automatic
  cleanup, replay, or runtime-data deletion.

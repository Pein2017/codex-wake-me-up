## Why

A delivered wake pointer can be consumed by the root while its monitor remains
`queue_accepted`; rejecting that monitor as re-arm lineage forces operators to
drop provenance even though its single trigger and queue-add attempts are already
consumed. Registration failures also hide which readiness or app-server stage
failed. A newly observed claimed wake was terminalized when its read-only
`thread/read` delivery preflight timed out, before queue admission was attempted,
so its pointer was never queued. Native worker paths also need exact
host-observed terminal status rather than approximation.

## What Changes

- Accept `queue_accepted` as settled for lineage only, without making it terminal,
  successful, or eligible for another queue-add.
- Report a stable registration stage for app-server failures and a bounded reason
  for delivery-daemon readiness failure; do not lengthen timeouts or claim an
  unproven cause for a `thread/read` timeout.
- Keep a claimed pointer durably pending when a typed transport failure interrupts
  read-only delivery preflight, then retry that preflight on the existing daemon
  cadence without consuming or repeating queue admission.
- Freeze the native-worker bridge boundary: a public canonical task name must bind
  one exact Core invocation and host-observed terminal status before it can feed an
  existing event leaf. Until Core exposes that read-only binding, the plugin remains
  on HOLD rather than approximating completion from thread idleness, disappearance,
  or cooperative publish.
- Keep any/all composition and root queue delivery unchanged.

This change does not weaken one-trigger, one-admission, or fail-closed rules. It
authorizes no runtime install, daemon restart, live monitor, Core mutation, model
continuation, or other material spend.

## Capabilities

### New Capabilities

None.

### Modified Capabilities

- `codex-wake-me-up-monitor`: Distinguish lineage settlement from delivery
  reconciliation, expose bounded registration diagnostics, and require exact native
  invocation binding before native worker settlement can be monitored.

## Impact

- Plugin source and focused tests: re-arm validation, thread registration errors,
  daemon readiness inspection, and typed pre-admission transport recovery.
- Operator guidance and this change record: source acceptance is separate from live
  installed-runtime acceptance; the native bridge remains pending a user-approved
  Core read surface and a real root-final wake.
- No new dependency, scheduler, composition engine, delivery route, or database
  migration.

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
- Keep the daemon heartbeat fresh while its event loop awaits reconciliation I/O;
  long serial read passes must not make a live lock owner spuriously unready.

This change does not weaken one-trigger, one-admission, or fail-closed rules.
The initial source-only authorization excluded runtime installation and live
continuation. On 2026-09-08 the user explicitly authorized overcoming both observed
runtime blockers and installing the result locally. This continuation includes
the matching Core/plugin build and installation, coordinated daemon/Core
activation, and bounded real native-worker wake acceptance under task 4.3.
Unrelated active tasks, native state, monitor records, and rollback paths must be
preserved; source or build success alone is not installed-runtime acceptance.

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

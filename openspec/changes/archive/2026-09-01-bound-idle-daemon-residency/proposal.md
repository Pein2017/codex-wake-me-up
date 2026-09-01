## Why

The detached Wake daemon currently remains resident indefinitely after the last
monitor becomes terminal, even though all durable monitor state is already in
the ledger and the next registration can start a daemon on demand. This leaves
an avoidable host-wide process and memory footprint after Wake has no
outstanding work.

## What Changes

- Make daemon residency proportional to durable non-terminal monitor work: once
  the ledger stays empty through a bounded idle grace, the lock-owning daemon
  retires cleanly.
- Add an observable retirement handshake so a concurrent registration cannot
  accept a daemon that has already committed to exit.
- Preserve on-demand daemon startup and the existing pre-arm and pre-delivery
  readiness checks; a retirement race must refuse or restart supervision rather
  than leave an armed monitor unsupervised.
- Preserve the current MCP and CLI interfaces, condition semantics, `all`/`any`
  aggregation, delivery paths, and one-activation/fail-closed invariants.
- Do not delete terminal history, event reservations, receipts, or audit rows as
  part of daemon retirement.

## Capabilities

### New Capabilities

None.

### Modified Capabilities

- `codex-wake-me-up-monitor`: Require the detached daemon to retire only after
  durable work is absent, and require concurrent registration to retain
  positively observed supervision across that transition.

## Impact

- Affected implementation: `src/codex_wake_me_up/daemon.py`, daemon readiness
  helpers in `src/codex_wake_me_up/runtime.py`, and narrow lifecycle regression
  tests.
- No public command, MCP schema, stored monitor meaning, transport, dependency,
  or Codex Core change is introduced.
- The change adds no material model spend and no irreversible behavior. It does
  not weaken one-shot activation or fail-closed delivery; it narrows daemon
  residency only when no non-terminal monitor requires supervision.

## Why

The source implementation already reports typed pre-arm failures and separates
queue acceptance from later delivery facts, but a long target-resolution or
condition-preparation sequence can still outlive the MCP call budget. The armed
receipt also describes the observation expiry as if it were a delivery deadline,
and the current-task monitor view does not show enough state to explain what a
non-terminal monitor is waiting for. These inconsistencies make a disabled or
stale installation harder to diagnose without changing the durable delivery
contract.

## What Changes

- Bound the primary `ThreadDelivery` app-server preflight and observation reads
  with one fixed, documented budget. Keep the existing at-most-one read-only
  transport retry; return `monitor_created=false`, the failed stage, a bounded
  timeout reason, and retry safety when that read budget expires.
- Keep the read budget strictly before ledger creation and queue admission. The
  synchronous condition identity capture keeps its existing per-observer
  bounds and a final rowless deadline gate; it is not presented as an
  interruptible MCP wall-clock guarantee. Do not retry or compensate an
  uncertain queue write, and do not change the one trigger/one admission or
  fail-closed rules.
- Change the armed receipt wording to call `expires_at` an observation expiry;
  state explicitly that delivery, target status consumption, task success, and
  lead acceptance are separate facts.
- Enrich the trusted current-task monitor query with a bounded condition
  summary, delivery state, supervision, and existing lifecycle timestamps so
  `claimed`, `admission_in_progress`, and `queue_accepted` work is visible.
  Preserve all rows and do not auto-cancel, delete, replay, quarantine, or
  declare success for old or stale targets.
- Update focused tests, operator guidance, and OpenSpec receipts. Keep
  `send_message_to_thread` investigation outside this plugin and leave the
  unavailable native-Core end-to-end path on HOLD.

## Capabilities

### New Capabilities

None.

### Modified Capabilities

- `codex-wake-me-up-monitor`: bound rowless pre-arm registration, clarify
  observation-expiry semantics, and expose compact non-terminal monitor state.

## Impact

- `src/codex_wake_me_up/service.py` and `models.py` for pre-arm deadlines,
  timeout receipts, registration wording, and current-monitor projections.
- Focused thread-delivery/public API tests and the packaged README/skill
  guidance.
- No Core change, new dependency, scheduler, queue route, database migration,
  automatic stale-row cleanup, cache installation, daemon restart, or change to
  the upstream Codex App message-tool surface.

## Context

See `proposal.md` for the observed failures and the user-facing motivation.
Registration currently performs serial read-only app-server calls before the
first durable write, while the primary MCP operation has a shorter end-to-end
budget. Existing ledger rows already carry the timestamps and reconciliation
fields needed for diagnosis; the missing pieces are a shared app-server read
deadline, unambiguous receipt wording, and a compact projection for the trusted
current task. Synchronous condition identity capture already uses bounded native
observers; arbitrary injected callbacks cannot be safely interrupted without a
new process boundary, so this design does not claim that guarantee.

## Goals / Non-Goals

**Goals:**

- Bound the primary `ThreadDelivery` app-server preflight/observation reads and
  preserve their fail-closed, at-most-one-retry behavior.
- Keep timeout failures side-effect free and machine-readable.
- Keep condition identity capture rowless if it overruns the read deadline,
  without pretending arbitrary synchronous callbacks are interruptible.
- Make observation expiry, notification, target consumption, and acceptance
  distinct in receipts and current-task status.
- Reuse existing ledger fields and condition summaries to expose stale work.

**Non-Goals:**

- No Core capability workaround, new delivery path, scheduler, or queue
  semantics.
- No automatic cleanup, replay, migration, installation, daemon restart, or
  re-enable of the disabled plugin.
- No change to the upstream Desktop `send_message_to_thread` surface.

## Decisions

1. **One monotonic app-server read deadline.** Start the fixed 25-second budget
   before the first target read and pass its deadline through app-server
   condition observations. Each app-server call receives only the remaining
   time. This bounds serial protocol work while preserving the existing single
   transport retry. The deadline ends before ledger creation; queue admission
   is outside it. A per-call timeout alone is insufficient because two calls
   plus retry can exceed the MCP budget.

2. **Typed timeout receipt, no compensation.** Convert deadline expiry into
   the existing `PreArmRegistrationError` shape with a bounded timeout kind,
   stage, retry disposition, and optional budget value. Since no row exists,
   callers may safely retry; once a queue write has begun, the existing
   uncertain-write reconciliation rules remain unchanged.

3. **Reuse durable facts for visibility.** Add a small projection over
   `MonitorRecord.status_dict()` and `_condition_binding_summary()` for the
   trusted current-task query. Include condition, delivery/reconciliation
   state, supervision, and non-null lifecycle timestamps; do not add a second
   ledger or background cleanup path.

4. **Receipt wording follows evidence boundaries.** Keep the compact response
   contract but call `expires_at` an observation expiry and explicitly state
   that a wake/notification is not task acceptance. This changes text only;
   trigger, admission, reconciliation, and guarded continuation writers remain
   the same.

## Risks / Trade-offs

- [A long app-server read may be cancelled at the deadline] → return a rowless
  typed timeout and allow a safe caller retry; never create a partial row.
- [A fixed app-server read budget may be too short on a very slow host] → keep
  it documented, expose the value in the timeout receipt, and tune it only with
  measured app-server latency evidence.
- [The current-task projection can still show stale rows] → that is intended;
  preserve timestamps and reconciliation facts and leave cleanup/replay to an
  explicit future decision.

## Migration Plan

No data migration is required. Source tests and packaged guidance change
in-place; existing ledger rows remain readable because new fields are optional.
Rollback is the normal source rollback, with no daemon or database operation in
this change.

## Open Questions

None. Remaining native-Core end-to-end qualification is already tracked as a
separate HOLD task and is not changed here.

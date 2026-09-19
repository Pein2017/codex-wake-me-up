## Context

See `proposal.md` and the modified monitor delta. The live failure is in the
post-claim path: exact `thread/read` metadata can take just over the current
10-second client budget, while older `queue_accepted` rows are visited first
and each retry performs another history read. The durable ledger already has a
JSON reconciliation field, so this can be repaired without a schema migration.

## Goals / Non-Goals

**Goals:**

- Allow a slow but responsive exact preflight to complete within a finite,
  documented budget.
- Let claimed and in-progress work run before historical admitted work.
- Keep repeated offline observations bounded and visible through existing
  reconciliation fields.
- Preserve one claim, one queue admission, exact capability revalidation, and
  no-blind-resend semantics.

**Non-Goals:**

- No queue-add fallback based only on an old capability snapshot.
- No new Core endpoint, scheduler process, database migration, or automatic
  cleanup/replay of stale monitors.
- No change to condition strength, target routing, or upstream message APIs.

## Decisions

1. **Use a 15-second app-server RPC budget.** The observed target metadata read
   completed in about 10.5 seconds, just beyond the old default. A 15-second
   default gives that exact read room without turning a single RPC into a long
   unbounded wait. The existing 25-second registration budget remains the outer
   bound and still owns retry/rowless failure semantics.

2. **Prioritize state, then preserve creation order.** `CLAIMED`, admission in
   progress, cancellation in progress, and `ARMED` records are scheduled before
   `QUEUE_ACCEPTED` history reconciliation. Within a priority, creation order
   remains stable. This keeps the change local to one reconciliation pass and
   avoids a second queue or scheduler abstraction.

3. **Persist retry timing in reconciliation JSON.** On transport/offline
   reconciliation, increment a bounded retry counter and write `retry_after`.
   The daemon skips admitted/cancellation rows before that time, then retries
   them normally. Successful online observations clear the retry fields. The
   values are diagnostic, survive restart, and require no migration.

4. **Keep exact preflight fail-closed.** A timed-out preflight remains
   `claimed/unattempted`; the plugin does not use the arm-time capability as a
   substitute and does not send until the exact target is revalidated. This
   deliberately trades a possible extra wait for no mis-targeted or duplicate
   queue write.

## Risks / Trade-offs

- [A Core read may still exceed 15 seconds] → keep the monitor retryable and
  expose `preflight_transport_unavailable`; a future Core-side lightweight
  metadata route can be evaluated separately.
- [The first retry of many old rows still costs one slow read each] → state
  priority puts fresh claims first, and persisted exponential backoff prevents
  the same stale rows from consuming every subsequent cycle.
- [Retry fields add status payload] → keep only a counter and timestamp in the
  existing bounded reconciliation object.

## Migration Plan

1. Run focused unit tests and the full plugin validation suite.
2. Update the local plugin cachebuster and reinstall from `coordexp-local`.
3. Stop the old wake daemon only after recording its exact PID/cwd/PYTHONPATH;
   start the daemon from the new installed cache while preserving the runtime
   ledger and WAL.
4. Verify the lock owner, installed source identity, capability report, and a
   real app-server preflight/reconciliation smoke. Exercise queue admission only
   when an explicitly disposable, still-claimed monitor exists; the reported
   monitor was already cancelled, so no new queue write is safe during reinstall.
   Rollback is the previous cache version plus the same daemon restart; runtime
   data is retained.

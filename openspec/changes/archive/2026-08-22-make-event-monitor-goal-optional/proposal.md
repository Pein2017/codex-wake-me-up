## Why

Durable event observation is useful whether or not a Codex task has a goal, but
the current plugin admits every monitor through a paused-goal guard. This makes an
optional goal mechanism the prerequisite for the common case and prevents a
goal-less task from ending its turn and later receiving the event.

## What Changes

- Make `wait_for_event` the primary API. It durably registers one typed, bounded,
  one-shot monitor for one exact local Codex thread and does not require, create,
  pause, or reactivate a goal.
- Deliver a fired monitor as a small self-identifying pointer through Codex's
  existing experimental per-thread durable FIFO. The pointer directs the task to
  inspect the plugin's durable status record; the full evidence stays in the
  plugin ledger.
- When the target thread is active, leave the pointer queued for a separate later
  turn. When it is stored but unloaded, resume/load that exact thread so its queue
  can drain in existing FIFO order. Resume may also release user-authored items
  already ahead of the pointer, so registration explicitly carries that local
  model-spend boundary. Never steer the active turn and never use
  `codex exec resume` as a second thread writer.
- Treat queue admission, queue dispatch, and durable history recording as
  separate observable facts. V1 makes at most one plugin admission attempt and
  reconciles the queue item with thread history using a stable delivery ID. It
  does not claim exactly-once delivery: an app-server crash around queue dispatch
  can lose or duplicate the tiny pointer. Ambiguous absence becomes
  `delivery_uncertain`; the daemon does not blindly re-enqueue it.
- Respect the queue as shared user-owned state. Queue capacity, prior items,
  reorder/edit/delete, interrupted turns, and user cancellation are visible in
  status and never silently bypassed.
- Preflight the installed app-server's experimental queue capability before
  arming. Capability drift, unsupported targets, queue saturation, and
  inconclusive admission fail closed with typed receipts.
- Keep `defer_goal_until_event` and the existing `wake_me_up` aliases as optional
  compatibility surfaces for an already active or paused goal. Goal delivery
  retains its existing guarded, at-most-once goal-status path and is never chosen
  implicitly for a goal-less task.
- Preserve typed-condition evidence, immutable terminal-event publication,
  expiry, cancellation, redaction, Git attestation, durable trigger claims,
  daemon recovery, old-row compatibility, and the rule that a wake or worker
  delivery does not imply task success or lead acceptance.
- Remove the proposed Codex Core `turn/startIfIdle` patch, latest-turn CAS,
  idle-only admission, and installed-Core build dependency. Existing
  `turn/start`, steer, history injection, Desktop coordination messages, and CLI
  resume are not fallback delivery paths.

This change depends on the source contract in
`add-terminal-and-worker-delivery-events`. Its strict-valid event implementation
and immutable publication/attestation semantics must be frozen and synced before
shared implementation is integrated; its remaining live smoke is folded into the
new queue-delivery acceptance rather than preserving a goal-first architecture.

This change explicitly renegotiates the former invariant that continuation is
only a guarded goal-status write. The new default is one host-local queue-admission
attempt for a bounded event pointer. Goal activation remains a separate legacy
delivery kind. Material model spend is limited to explicitly registered delivery
and bounded disposable acceptance smokes; no remote listener, arbitrary command,
merge, push, or publication is introduced.

## Capabilities

### New Capabilities

None.

### Modified Capabilities

- `codex-wake-me-up-monitor`: Replace mandatory paused-goal targeting with an
  explicit thread-or-goal delivery union; add durable per-thread queue delivery,
  capability preflight, shared-queue semantics, reconciliation, and truthful
  uncertainty reporting.
- `codex-goal-self-defer`: Present event monitoring as primary, make goal defer an
  optional explicit compatibility path, and retain the legacy aliases without
  allowing them to create a goal.

## Impact

- Public MCP/service/CLI and packaged guidance gain `wait_for_event` as the
  common path and `defer_goal_until_event` as the explicit legacy goal path.
- Monitor persistence and status gain a tagged delivery envelope, stable
  delivery ID, queue receipt, dispatch/history observations, and typed terminal
  delivery outcomes while preserving legacy goal rows.
- The app-server adapter enables `experimentalApi` on its dedicated local
  connection and composes `thread/queue/add`, `thread/queue/list`,
  `thread/queue/delete`, `thread/read`, and `thread/resume` without a Core
  patch. It never calls `thread/queue/start`.
- Daemon reconciliation continues after the registration MCP request returns;
  the MCP timeout is not the condition lifetime.

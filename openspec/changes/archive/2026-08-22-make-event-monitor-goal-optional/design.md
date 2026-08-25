## Context

See [proposal.md](proposal.md) for motivation. The existing plugin durably
observes typed conditions but routes every claim to a guarded goal-status write.
The pending `add-terminal-and-worker-delivery-events` change adds immutable
producer events and an atomic event evaluate-and-claim seam; this change consumes
that seam and changes only what happens after a monitor is claimed.

Codex `rust-v0.148.0` already exposes an experimental, SQLite-backed,
per-thread FIFO. `thread/queue/add` can persist input for a stored unloaded
thread, loaded active threads drain it after their current turn, and a cold
`thread/resume` allows a persisted row to dispatch. Queue operations for one
thread are serialized. This is the closest existing equivalent to sending a
message to a task regardless of whether it is currently active.

The queue is not an exactly-once transport. It has no unique delivery-key
constraint and deletes a row after Core reports `Started`, before the eventual
user message is guaranteed durable in rollout history. A crash in that interval
can lose a pointer; a crash before deletion can replay it. The queue is also
shared user state with a capacity of 100 and user-visible edit, delete, and
reorder controls. These constraints are part of the V1 contract, not hidden
implementation details.

`codex exec resume` starts another in-process app-server/Core and is unsafe as a
daemon delivery path against a Desktop-loaded task. Existing `turn/start` is
start-or-steer, `thread/inject_items` is history-only, and Desktop
`send_message_to_thread` has no public durable receipt/idempotency contract.
The previously drafted `turn/startIfIdle` patch solves volatile CAS admission,
not eventual delivery, and is superseded.

The trust boundary is one same-user local host: any local MCP client that can
reach this plugin and names an exact stored thread can request delivery to it.
The plugin does not authenticate that thread as the caller's current task. It
never opens TCP, never retargets, and makes every queued pointer self-identifying
so the receiving task can inspect its provenance.

## Goals / Non-Goals

**Goals:**

- Make one exact local thread, not a goal, the common monitor delivery target.
- Reuse typed observation, durable event binding, one trigger claim, expiry,
  cancellation, redaction, and daemon recovery before branching by delivery kind.
- Admit one bounded pointer to the existing thread FIFO, never steer active work,
  and actively load a stored target so its existing FIFO can drain without
  depending on the user reopening the task, subject to interrupted stalls.
- Expose queue admission, dispatch/history confirmation, user queue intervention,
  and ambiguity as distinct durable facts.
- Load historical goal rows without rewriting them and preserve the existing
  guarded goal path for explicit legacy use.

**Non-Goals:**

- Exactly-once pointer recording, invisible queue ownership, queue priority over
  user messages, or bypassing an interrupted task.
- A new Codex Core endpoint, installed Core artifact, latest-turn CAS, or a second
  app-server process.
- Sending the full monitor report as prompt input, authenticating a supplied
  thread ID as the MCP caller, remote/TCP delivery, raw shell predicates, or
  arbitrary command execution.
- Treating any wake, command terminal, worker delivery, or Git attestation as task
  success, lead acceptance, merge authorization, or user acceptance.

## Decisions

### 1. Compose the existing queue instead of patching Codex Core

The plugin uses one dedicated host-local app-server connection initialized with
`experimentalApi: true`. Thread delivery composes:

1. read-only capability and exact-target preflight;
2. `thread/queue/add` with one bounded pointer and stable
   `clientUserMessageId=deliveryId`;
3. a pre-resume queue snapshot when the exact stored target is not loaded; and
4. `thread/resume` for that exact target.

The queue remains responsible for FIFO ordering and for dispatch after an active
turn ends. Automatic FIFO drain is triggered by enqueue on a loaded idle thread,
normal turn completion, or resume. The plugin never calls `thread/queue/start`:
that method can bypass earlier items or start user-authored work.

Resume/load adds no plugin-authored input beyond the persisted pointer, but it
releases the existing FIFO head. Therefore an unconditional resume-to-deliver may
start user-authored items already ahead of the pointer and cascade through later
items until the FIFO drains or a turn interrupts/fails. Before resume, the plugin
records the observed item count and identities ahead of the pointer. Explicit
thread-monitor registration authorizes this same-thread FIFO release and its
material model-spend boundary; it does not authorize reordering or steering.

Alternative: add `thread/deliver` with durable idempotency and
`queued -> claimed -> recorded` Core state. That is the stronger long-term
design, but it expands the change into a Core migration and installed-binary
rollout. V1 instead exposes the current queue's crash boundary truthfully.

Alternative: `turn/startIfIdle`. Rejected because changed or busy history would
discard an event that should be handled later.

### 2. Use two explicit delivery kinds, with thread delivery as the default

`wait_for_event(thread_id, condition, expires_in_seconds, idempotency_key,
rearm_of?)` selects `ThreadDelivery`. It accepts a loaded or durably stored local
thread whether its goal is null, active, paused, or blocked; it neither reads goal
status for admission nor mutates a goal. The call arms promptly and returns
`next_action=end_current_turn`.

`defer_goal_until_event(...)` selects `GoalDelivery`. Active goals use the
existing watcher-first pause protocol; paused goals use stable two-read capture.
`wake_me_up` and `wake_me_up_defer` remain thin compatibility aliases for one
migration period. No interface creates a goal or silently switches delivery kind.

An additive versioned delivery envelope records:

- delivery kind and schema epoch;
- exact thread ID;
- stable monitor-scoped delivery ID;
- for thread delivery, pointer digest and app-server capability identity;
- for goal delivery, the existing exact `TargetGuard` and pause facts; and
- admission attempt, queue receipt, reconciliation, cancellation, and terminal
  delivery facts.

A legacy row with no delivery tag and a valid target guard decodes as
`GoalDelivery` without database rewrite. Idempotent monitor registration includes
the complete immutable delivery envelope.

### 3. Keep the prompt tiny and the durable evidence in the plugin ledger

The queue input is a bounded, provenance-labelled pointer equivalent to:

`[monitor <monitor-id> fired; delivery <delivery-id>] Inspect monitor status once
and continue from its witness. This pointer is not a success claim.`

The stable delivery ID is also the client user-message ID. The pointer carries no
raw command, log tail, publish token, Git credentials, or full event report.
Status remains the authority for wake reason, witness, evidence class, evaluation
count, terminal-event/Git attestation, and explicit acceptance flags.

A replayed pointer therefore asks for the same immutable status and cannot create
a second trigger claim. This gives semantic idempotency at the monitor layer, not
transport exactly-once.

### 4. Separate observation, admission, and recording state

Condition evaluation and one durable trigger claim remain common. After a
`ThreadDelivery` claim, the ledger progresses through these facts:

`unattempted -> admission_in_progress -> queue_accepted -> recorded`

Terminal alternatives are `delivery_rejected`, `delivery_uncertain`,
`cancelled`, and `delivery_modified`. Only one plugin
`thread/queue/add` request is permitted. The attempt is durably consumed before
the request. A transport-uncertain response or recovered
`admission_in_progress` state first enters the same delivery-ID reconciliation
ladder below and is never re-added.

After a conclusive queue ACK, the receipt records queue item identity/order and
the daemon drives resume as the only explicit queue-drain operation.
Reconciliation checks, in order:

1. exact thread history for `clientUserMessageId=deliveryId` and the expected
   pointer digest;
2. the shared queue for the accepted item and expected pointer digest; then
3. if both are absent, a persisted 60-second online reconciliation window.

App-server downtime pauses that window. If the expected history or queue row does
not reappear after the online window, the terminal result is
`delivery_uncertain` with `absence_kind=unresolved_absence` and observed
possible causes such as user deletion, app-server crash, archive, or storage
failure. The daemon does not re-enqueue because current APIs cannot distinguish
those cases. A history item with changed content or a queue edit becomes
`delivery_modified`; user-owned text is not restored.

The implementation does not promise at-most-once pointer recording: the Codex
queue itself can replay across its pre-delete crash window. Status explicitly
distinguishes the plugin's single admission attempt from observed pointer count.

### 5. Respect shared queue and task lifecycle ownership

Earlier queued user items remain ahead of the monitor pointer. User reorder is
accepted and reported. Queue-full registration does not matter until firing;
queue-full delivery becomes a typed `delivery_rejected` receipt and no alternate
path is attempted.

An active regular turn is never steered. The pointer remains queued until the
app-server's normal idle lifecycle drains it. An interrupted task can leave queue
processing paused; status reports `stalled_interrupted` and the plugin does not
force a release. Pending trigger work and earlier FIFO entries may delay the
pointer indefinitely.

Cancellation before admission wins in the plugin ledger. After queue acceptance,
the plugin uses the exact queue-item removal operation once. Per-thread app-server
serialization decides removal versus dispatch. Conclusive removal records
`cancelled`; a matching history item means `cancellation_too_late`; absence
from both queue and history enters the same reconciliation/uncertainty rule.

Archived, deleted, ephemeral, unsupported spawned-subagent, cross-host, and
unresolvable targets fail with typed outcomes and are never forked or retargeted.
If a target is archived after queue acceptance and exact removal is unavailable,
the result enters unresolved-absence reconciliation rather than claiming
cancellation.

### 6. Preflight experimental capability before arming

Registration verifies a healthy local daemon and a dedicated app-server
connection whose initialize response enables the expected experimental queue API.
It performs a read-only queue/list and thread-read compatibility probe for the
exact target, records Codex version/response-shape identity and a plugin delivery
epoch, and fails before arming when the capability is absent or malformed.
`thread.can_accept_direct_input` is the authoritative current field for rejecting
unsupported spawned-subagent targets.

The check is repeated before admission. Capability loss after the monitor fired is
recorded as `delivery_capability_unavailable`; it never falls back to goals, Core
CAS, Desktop coordination, or CLI resume. Downgrade preflight refuses while a
nonterminal thread delivery or matching queued pointer exists.

### 7. Goal delivery remains an isolated legacy branch

`GoalDelivery` retains the loaded, idle, exact paused-goal preflight, one durable
activation attempt, returned-marker comparison, and fail-closed uncertainty.
Heuristic-continuation authorization applies only to this goal-status branch.

Explicit `ThreadDelivery` registration authorizes delivery of the pointer for any
typed wake reason, including heuristic evidence, expiry, or irrecoverable observer
failure. It never upgrades that evidence to success. The delivery kinds cannot
share one monitor, switch after arm, or fall back to one another.

### 8. Classify every affected terminal write

| Write site | Disposition | Durable receipt |
|---|---|---|
| Invalid/ambiguous registration | Correctable; no row | validation result |
| Daemon/queue capability absent before arm | Fail closed; no row | capability identity/error |
| Condition satisfaction | Wake-eligible | one claim, witness, `condition` |
| Thread-delivery expiry | Wake-eligible | one claim, `expired` |
| Irrecoverable observer failure | Wake-eligible | one claim, `observer_failed` |
| Transient/unknown observation | Retry observation | evidence/count; remains armed |
| Cancellation before admission | Operator stop | `cancelled`; no queue request |
| Queue admission ACK | Delivery in progress | item ID/order and one attempt |
| Queue full/unsupported target/rejection | Fail closed | `delivery_rejected` reason |
| Admission response/crash uncertainty | Reconcile, never re-add | history/queue/window facts |
| Queue item present | Retry reconciliation | queue position/content digest |
| Matching history item present | Delivery complete | `recorded`, turn/message facts |
| Queue/history absent after online window | Ambiguous terminal | `delivery_uncertain:unresolved_absence` |
| Queue/history content changed | User-owned terminal | `delivery_modified` |
| Interrupted target retains item | Inspectable stall | `stalled_interrupted` |
| Cancellation loses to dispatch | Too late | history receipt or uncertainty |
| Archive blocks post-ACK removal | Ambiguous terminal | unresolved-absence facts |
| Existing goal pause/activation sites | Existing fail-closed policy | existing goal outcomes |
| Legacy row decode | Compatibility; no write | derived `delivery_kind=goal` |
| Terminal-event publication/attestation | Existing pending-change policy | immutable redacted event report |

## Risks / Trade-offs

- **[Current queue can lose or duplicate a pointer around a crash]** -> Use one
  stable delivery ID, reconcile queue/history, make duplicates semantically
  harmless, report ambiguity, and do not advertise exactly-once.
- **[User deletion, crash loss, archive, and storage failure can be
  observationally identical]** -> Report unresolved absence and observed possible
  causes; never overwrite user queue intent.
- **[Experimental API drifts]** -> Preflight response shapes before arm and again
  before admission; fail closed with version/epoch receipts.
- **[Queue backlog or interruption delays wake]** -> Show position/stall in status,
  actively resume unloaded targets, and never claim a delivery deadline.
- **[Resume can release user items ahead of the pointer]** -> Snapshot and expose
  the pre-resume FIFO, document the billed cascade, preserve order, and require
  explicit thread-monitor registration.
- **[A pointer starts a billed model turn]** -> Require explicit bounded
  registration, use one tiny message, and include this material-spend boundary in
  operator guidance.
- **[Mixed binaries misread new rows]** -> Add a delivery epoch and refuse
  downgrade while thread delivery is nonterminal or queued.

## Migration Plan

1. Freeze and sync `add-terminal-and-worker-delivery-events` before integrating
   shared files; preserve its strict source regression baseline and immutable
   evidence contract.
2. Record the `turn/startIfIdle` Core draft as superseded, retain its exact patch
   as a historical fallback artifact, and verify the installed Core remains
   unchanged; retire only task-owned draft edits after exact review.
3. Add delivery persistence/fixtures, capability preflight, queue adapter,
   admission/reconciliation state machine, public APIs, and guidance in that order.
4. Run focused tests, full source regressions, compile/validation, and exact
   failure-set comparison before replacing the installed plugin.
5. Cache-bust/install the verified plugin, restart only the proven daemon lock
   owner, and verify loaded source/epoch parity.
6. Use disposable tasks to verify loaded-idle delivery, active-turn FIFO delivery,
   unloaded resume-to-deliver, stable delivery-ID history correlation, queue
   delete/reorder visibility, interrupted stall, cancellation, daemon/app-server
   restart, and command/worker terminal events. Do not claim exactly-once from
   these smokes.

Rollback disables new registration first and refuses downgrade while a
nonterminal thread delivery or matching queue pointer remains. It preserves
completed redacted rows and all legacy goal rows, restores the previous verified
plugin cache, and restarts only the proven daemon lock owner. No Core artifact is
installed or rolled back by this change.

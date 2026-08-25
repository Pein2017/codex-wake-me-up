## ADDED Requirements

### Requirement: Deliver a fired thread monitor through the durable task queue

For `ThreadDelivery`, the system SHALL make at most one plugin admission attempt
to place one bounded, self-identifying pointer in the exact target thread's
existing durable FIFO. The pointer MUST carry the stable monitor-scoped delivery
identifier as its client user-message identifier, MUST direct the task to inspect
the durable monitor status, and MUST state that delivery is not a success claim.
It MUST NOT contain the full wake report, raw commands, publish tokens, credentials,
or unbounded evidence.

The system MUST NOT steer an active turn. A loaded active target SHALL retain the
pointer for a separate later FIFO turn. For a durably stored unloaded target, the
system SHALL first record the observed queue item count and identities ahead of
the pointer, then request host-local resume/load for that exact thread. Resume MAY
release user-authored items already ahead of the pointer and cascade until the
queue drains or a turn interrupts/fails; explicit thread-monitor registration
SHALL authorize that same-thread FIFO release and its model-spend boundary. The
system MUST preserve FIFO order and MUST NOT call `thread/queue/start`.
It MUST NOT use goal creation or activation, ordinary `turn/start`,
`turn/steer`, history injection, Desktop coordination messages, or
`codex exec resume` as a fallback.

#### Scenario: Loaded idle thread receives the pointer
- **WHEN** a claimed thread monitor is admitted while the exact target is loaded
  and idle
- **THEN** its queue item is eligible to start one separate task turn and status
  records queue admission independently from history recording

#### Scenario: Active thread is not steered
- **WHEN** a claimed thread monitor is admitted while the exact target has an
  active regular turn
- **THEN** the pointer remains queued behind existing work and starts only through
  the normal later FIFO lifecycle

#### Scenario: Stored unloaded thread is resumed for delivery
- **WHEN** the queue accepts a pointer for an exact durably stored target that is
  not loaded
- **THEN** the system records the items ahead, requests local resume/load without a
  second Core writer, and does not depend on the user reopening the task, subject
  to the shared-queue and interrupted-stall rules of this specification

#### Scenario: User items precede the pointer at resume
- **WHEN** the pre-resume snapshot finds user-authored queue items ahead of the
  monitor pointer
- **THEN** status records that billed-work boundary and resume preserves their FIFO
  priority rather than starting the pointer directly

#### Scenario: Queue admission rejects
- **WHEN** the queue is full, the target is unsupported, or admission conclusively
  rejects
- **THEN** the monitor records `delivery_rejected` with the reason and attempts no
  alternate delivery path

### Requirement: Reconcile queue delivery without overstating reliability

The system SHALL distinguish trigger claim, plugin admission attempt, queue
acceptance, queue presence, durable history recording, and terminal delivery
outcome. It MUST durably consume the one admission attempt before sending. An
uncertain admission response or recovered in-progress admission MUST enter the
same delivery-ID reconciliation below without an automatic second add.

After queue acceptance, the system SHALL reconcile the stable delivery identifier
and expected pointer digest against exact thread history and the shared queue. If
both are absent, it SHALL wait through a persisted 60-second window while the
app-server is online. Continued absence MUST become
`delivery_uncertain` with `absence_kind=unresolved_absence` and observed
possible causes; it MUST NOT be blindly re-enqueued. A changed queued or recorded
pointer SHALL become
`delivery_modified`. The system MUST expose any observed pointer count and MUST
NOT claim exactly-once recording because the underlying queue can replay or lose a
pointer around dispatch-time crashes.

#### Scenario: Matching pointer reaches durable history
- **WHEN** exact thread history contains the stable delivery identifier and
  expected pointer digest
- **THEN** the monitor records `recorded` with the observed message/turn facts

#### Scenario: App-server restarts before queue dispatch
- **WHEN** an accepted queue row survives app-server replacement
- **THEN** reconciliation retains `queue_accepted` and resume/queue processing may
  continue without a second queue-add attempt

#### Scenario: Queue and history are both absent
- **WHEN** neither surface contains the accepted delivery after 60 seconds of
  online reconciliation
- **THEN** the system records unresolved-absence uncertainty, including observed
  user-delete, crash, archive, or storage-failure facts when available, and does
  not recreate the item

#### Scenario: Queue replays the pointer
- **WHEN** history contains more than one pointer with the stable delivery ID after
  a dispatch-time crash
- **THEN** status exposes the observed count while every pointer refers to the same
  immutable monitor report and no second trigger claim is created

### Requirement: Respect shared queue and cancellation ownership

Thread delivery SHALL treat the target FIFO as shared user-owned state. It MUST
preserve prior queue order, MUST accept and report user reorder, edit, and delete
actions without restoring user-owned text, and MUST expose queue position,
interrupted stalls, and earlier pending work when observable. It SHALL NOT promise
a delivery deadline.

Cancellation before admission MUST prevent queue input. After conclusive queue
acceptance, cancellation SHALL make at most one exact queue-item removal request.
A conclusive removal SHALL record `cancelled`; matching history SHALL record
`cancellation_too_late`; absence from both queue and history SHALL use the same
bounded reconciliation and uncertainty rule. If archive or target-state change
prevents removal after acceptance, the system MUST NOT claim cancellation.

#### Scenario: User reorders the shared queue
- **WHEN** a user moves the accepted pointer behind or ahead of other queued input
- **THEN** the system reports its observed position and does not restore the
  original order

#### Scenario: Interrupted target stalls queue processing
- **WHEN** the exact target is interrupted and the accepted pointer remains queued
- **THEN** status reports `stalled_interrupted` and the system neither steers nor
  claims that delivery completed

#### Scenario: Cancellation wins before dispatch
- **WHEN** exact item removal commits before the queue starts the pointer
- **THEN** the monitor records `cancelled` and no matching history item is
  intentionally created

#### Scenario: Queue edit changes the pointer
- **WHEN** the queue or history contains the stable delivery ID with a different
  pointer digest
- **THEN** the system records `delivery_modified` and does not overwrite it

### Requirement: Verify thread-delivery capability before arming

The daemon and app-server preflight SHALL advertise the plugin delivery epoch,
loaded source identity, Codex version, experimental queue availability, and the
expected read-only queue/thread response shapes. `ThreadDelivery` registration
MUST fail before arming unless the healthy lock-owning daemon and dedicated local
app-server connection pass that preflight for the exact target. A legacy
PID/timestamp-only heartbeat or non-experimental connection MUST NOT be accepted.
The capability MUST be checked again before queue admission.

#### Scenario: Experimental queue is unavailable
- **WHEN** the installed app-server rejects or omits the required queue capability
- **THEN** registration fails before arm without creating a goal or choosing a
  fallback continuation path

#### Scenario: Capability disappears after arm
- **WHEN** preflight passed at registration but the exact capability is unavailable
  when the monitor fires
- **THEN** the system records `delivery_capability_unavailable` and makes no queue
  or goal request

#### Scenario: Downgrade sees pending thread delivery
- **WHEN** compatibility preflight targets an older delivery epoch while a
  nonterminal thread delivery or matching pointer remains queued
- **THEN** it refuses downgrade before source/cache replacement

## MODIFIED Requirements

### Requirement: Register a typed, one-shot monitor

The system SHALL let an operator register one monitor with an exact local thread
identity, exactly one tagged delivery contract, a typed condition expression, a
bounded expiry, and an optional idempotency key. `ThreadDelivery` SHALL require a
loaded or durably stored local thread and SHALL NOT require or mutate a goal.
`GoalDelivery` SHALL require the existing exact paused-goal guard. The system MUST
reject an absent, ambiguous, multi-kind, cross-host, or otherwise unsafe target or
condition and MUST NOT create a goal to satisfy registration.

The same idempotency key and semantically identical request MUST return the
original monitor; reuse with a changed target, delivery envelope, condition, or
policy MUST be rejected.

#### Scenario: A valid thread monitor is armed
- **WHEN** `wait_for_event` names an exact loaded or stored local thread and a
  supported condition
- **THEN** the system returns a durable `ThreadDelivery` monitor without creating,
  pausing, or activating a goal

#### Scenario: A valid goal monitor is armed
- **WHEN** the explicit goal operation names a loaded target with its current
  paused-goal guard and a supported condition
- **THEN** the system returns a durable `GoalDelivery` monitor without activating
  the target immediately

#### Scenario: A main thread arms its own paused goal
- **WHEN** a loaded target owns a paused goal but is currently active because its
  own main thread is registering explicit goal delivery
- **THEN** the system captures that guard without requiring idleness at registration

#### Scenario: A reused idempotency key has different semantics
- **WHEN** an existing key is reused with a changed target, delivery kind, guard,
  condition, or policy
- **THEN** the system rejects the request and leaves the original monitor unchanged

#### Scenario: A process identity cannot be captured
- **WHEN** registration includes a PID-exit leaf whose PID is absent or cannot be
  uniquely identified on the current boot
- **THEN** the system rejects registration rather than treating it as complete

### Requirement: Require explicit authorization for heuristic continuation

For `GoalDelivery`, the system MUST decide authorization from the actual true-leaf
witness at trigger claim time. It MUST NOT activate a non-deferred goal from time,
GPU, PID, tmux, log-pattern, thread-idle, or heartbeat-stale evidence unless the
registration explicitly authorizes heuristic/liveness continuation. A confirmed
deferred goal with an unauthorized heuristic witness SHALL follow the existing
`unauthorized_evidence` guarded wake and MUST NOT be presented as authorized or
successful. Successful `receipt_success` or explicitly reserved
`command_terminal`/`worker_terminal` evidence MAY authorize goal handling under
the existing policy, while retaining their distinct success/acceptance classes.

An explicit `ThreadDelivery` registration SHALL itself authorize delivery of the
bounded pointer for any typed wake reason, including heuristic evidence, expiry,
or irrecoverable observer failure. It MUST retain the witness and MUST NOT upgrade
heuristic, command-terminal, worker-terminal, or Git evidence to task success or
lead acceptance. Only successful `receipt_success` may carry
`task_success: true`; all terminal-event witnesses MUST carry
`lead_accepted: false`.
The trusted witness classification SHALL distinguish command termination, worker
terminal failure, valid delivery candidate, invalid delivery, and heuristic stall.

#### Scenario: Heuristic witness lacks opt-in on non-deferred goal delivery
- **WHEN** non-deferred `GoalDelivery` fires only from heuristic evidence without
  authorization, successful receipt, or bound terminal event
- **THEN** it records the witness and terminates without activating the goal

#### Scenario: Heuristic witness lacks opt-in on deferred goal delivery
- **WHEN** confirmed deferred `GoalDelivery` fires only from unauthorized
  heuristic evidence
- **THEN** it uses the guarded `unauthorized_evidence` wake with no success claim

#### Scenario: A receipt-or-time goal condition is satisfied by time
- **WHEN** non-deferred `GoalDelivery` is satisfied by its time leaf without
  heuristic authorization or successful receipt
- **THEN** it terminates without activation and records the time-only witness

#### Scenario: Combined receipt and liveness goal condition succeeds
- **WHEN** explicit goal delivery requires both successful receipt and PID exit
- **THEN** it may activate only after both are true and the goal guard remains valid

#### Scenario: Failed command event reaches thread delivery
- **WHEN** a bound command terminal reports failure for `ThreadDelivery`
- **THEN** the pointer may be delivered while status preserves failure evidence and
  makes no task-success claim

#### Scenario: Worker candidate reaches thread delivery
- **WHEN** a bound worker event reports a delivered candidate
- **THEN** the pointer may be delivered for review while lead acceptance remains
  false

#### Scenario: Stale heartbeat reaches thread delivery
- **WHEN** `heartbeat_stale` is the only true witness for `ThreadDelivery`
- **THEN** the pointer may be delivered with heuristic classification and no claim
  that the producer failed or stopped

### Requirement: Persist and recover monitor lifecycle state

The system SHALL durably record immutable registration, tagged delivery and guard,
evidence, trigger claim, wake reason, plugin admission or goal activation attempt,
queue/history observations, and terminal outcome. After daemon restart it MUST
rebuild eligible monitors and resume observation or delivery reconciliation.
Every monitor MUST obtain at most one durable trigger claim and consume at most one
plugin attempt of its selected delivery kind before sending that attempt.

An in-progress goal activation recovered after a crash MUST become uncertain
rather than be sent again. An in-progress thread queue-add admission MUST first
run the normal history-then-queue-then-online-window reconciliation by its stable
delivery ID and MUST NOT issue another queue-add in any branch. A conclusively
accepted thread queue item MAY continue through resume and reconciliation without
another add. Existing pre-change goal rows MUST decode as `GoalDelivery` without
database rewrite or loss of prior facts.

#### Scenario: Daemon restarts while a monitor is armed
- **WHEN** the daemon restarts before expiry
- **THEN** it restores and re-evaluates the monitor before deciding to trigger

#### Scenario: Monitor has already been claimed
- **WHEN** two evaluations observe a satisfiable condition
- **THEN** only the durable claim winner may enter the selected delivery path

#### Scenario: Thread monitor expires before a usable trigger
- **WHEN** an armed `ThreadDelivery` reaches expiry before another trigger
- **THEN** it claims one `expired` wake for pointer delivery

#### Scenario: Non-deferred goal monitor expires
- **WHEN** non-deferred `GoalDelivery` reaches expiry before a trigger
- **THEN** it becomes terminal-expired without goal activation

#### Scenario: Deferred goal monitor expires
- **WHEN** confirmed deferred `GoalDelivery` reaches expiry
- **THEN** it follows the existing guarded deferred terminal-wake policy

#### Scenario: Old row is recovered
- **WHEN** persistence contains a pre-change row with a non-null target guard
- **THEN** it is interpreted as `GoalDelivery` with its historical facts intact

### Requirement: Continue only the guarded loaded paused goal

This requirement SHALL apply only to `GoalDelivery`. The system SHALL preflight
continuation only when the target is loaded, idle, and owns the same captured
paused goal. It MUST make no more than one goal-status continuation attempt. An
unloaded, busy, changed, non-paused, transport-failed, or uncertain target MUST
produce the existing non-firing terminal outcome without retry, compensation, goal
creation, or a new target turn.

Because the local goal protocol has no expected-goal compare-and-set, the system
MUST compare the returned marker with the captured guard after its one request.
A mismatch MUST record `mis-targeted-activation` with both markers and MUST NOT
trigger compensation or retry.

#### Scenario: Guard remains valid at activation
- **WHEN** claimed `GoalDelivery` finds the loaded idle target with the captured
  paused goal
- **THEN** it makes one continuation attempt and records the outcome

#### Scenario: Target has been unloaded
- **WHEN** claimed `GoalDelivery` finds its target unloaded
- **THEN** it records `unloaded-target` without changing the target

#### Scenario: Activation result is uncertain
- **WHEN** the one goal continuation result is inconclusive
- **THEN** it records uncertainty and does not retry

#### Scenario: Goal changes during the protocol race window
- **WHEN** the returned marker differs from the captured paused-goal guard
- **THEN** it records `mis-targeted-activation` and makes no compensating write or
  retry

### Requirement: Provide inspectable operator controls

The system SHALL provide local controls to register, inspect, list, and cancel a
monitor. Inspection MUST expose delivery kind, redacted target/guard, condition,
evidence class, claim, admission/activation attempt, queue/history observation,
terminal outcome, timestamps, and daemon supervision. Cancellation before the
selected attempt MUST prevent future delivery; later cancellation MUST report its
queue-removal, too-late, or uncertainty result.

#### Scenario: Operator inspects a monitor
- **WHEN** status is requested by monitor identifier
- **THEN** it returns durable registration, selected delivery, evidence, queue or
  goal facts, and lifecycle/terminal state

#### Scenario: Operator cancels before the attempt
- **WHEN** cancellation wins while the monitor is registering, armed, or claimed
  but before its selected attempt
- **THEN** it records `cancelled` and sends no later queue or goal request

#### Scenario: Armed monitor is unsupervised
- **WHEN** an armed monitor has no compatible live daemon heartbeat
- **THEN** status reports it as unsupervised rather than healthy

### Requirement: Keep the control plane host-local and constrained

The system MUST use only the existing local Codex control transport for queue or
goal delivery, MUST NOT expose it through TCP, and MUST NOT accept raw shell
commands as conditions. A thread target MUST resolve to one exact local loaded or
durably stored task. The system MUST reject a remote, deleted, ephemeral, or
unsupported spawned-subagent target without forking or retargeting it.
The supported trust boundary is one same-user local host: any local MCP client
with access to the plugin MAY name any stored local thread, and the plugin MUST
describe that targeting as best-effort rather than authenticate it as the caller's
current task. Every pointer MUST identify its monitor and delivery provenance.

#### Scenario: Cross-host target is requested
- **WHEN** a supplied thread cannot be resolved as an exact local loaded or stored
  task
- **THEN** registration rejects without contacting a remote control plane

#### Scenario: Raw shell condition is requested
- **WHEN** a condition contains a shell command or untyped executable expression
- **THEN** the system rejects it without execution

### Requirement: Deliver a self-describing wake report

For every fired monitor, durable status SHALL include wake reason, satisfying
witness or failure detail, bounded log journal when applicable, arming/firing
timestamps, evaluation count, and selected delivery facts. For terminal events it
SHALL include redacted reservation/producer identity, event kind/status, bounded
command or worker evidence, heartbeat facts, Git attestation, and explicit success
and acceptance flags. The queued pointer SHALL identify the monitor but SHALL NOT
duplicate this report or mutate a goal objective or other user-owned text.
The report MUST distinguish task success, command termination, worker delivery
candidate, invalid delivery, and heuristic stall.

#### Scenario: Woken task reads context once
- **WHEN** thread or goal delivery starts a later turn and the task requests status
  once with the monitor ID
- **THEN** the response contains the wake reason, witness, wait statistics, and
  delivery receipt without further discovery

#### Scenario: Lead wakes for worker delivery
- **WHEN** a worker-terminal witness caused pointer or goal delivery
- **THEN** status contains producer identity, candidate commit, changed-path
  evidence, attestation, and an explicit not-accepted marker

#### Scenario: Lead wakes for command failure
- **WHEN** a command-terminal witness reports a failed or signaled command
- **THEN** status contains bounded exit evidence and declared logs/artifacts without
  claiming task success

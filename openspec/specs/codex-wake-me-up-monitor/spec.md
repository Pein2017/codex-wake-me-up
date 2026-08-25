# codex-wake-me-up-monitor Specification

## Purpose
Provide host-local, durable monitoring of explicit external conditions with
thread-primary queue delivery and optional guarded delivery to an existing goal.
## Requirements
### Requirement: Register a typed, one-shot monitor

The system SHALL let an operator register one monitor with one exact local
origin thread, exactly one tagged delivery contract, a typed condition
expression, a bounded expiry, and an optional idempotency key. The MCP
`wait_for_event` operation SHALL derive its origin thread exclusively from the
trusted caller metadata supplied by Codex and SHALL NOT expose a model-supplied
thread identity. The CLI SHALL retain an explicit local origin-thread argument.
`ThreadDelivery` SHALL resolve that origin to either the same directly
deliverable thread or, for a spawned V2 subagent, its root main-thread. It SHALL
NOT require or mutate a goal. `GoalDelivery` SHALL require the existing exact
paused-goal guard. The system MUST reject an absent trusted MCP identity,
ambiguous identity, multi-kind delivery, cross-host target, malformed ancestry,
or otherwise unsafe target or condition and MUST NOT create a goal to satisfy
registration.

The same idempotency key and semantically identical request MUST return the
original monitor; reuse with a changed origin, delivery envelope, condition, or
policy MUST be rejected.

#### Scenario: A main thread arms its own thread monitor
- **WHEN** `wait_for_event` receives a trusted caller identity for an exact
  loaded or stored root main-thread and a supported condition
- **THEN** the system returns a durable `ThreadDelivery` monitor targeted to
  that same root without creating, pausing, or activating a goal

#### Scenario: A spawned subagent arms a thread monitor
- **WHEN** `wait_for_event` receives a trusted caller identity for an exact
  spawned V2 subagent with a valid local ancestry and a supported condition
- **THEN** the system returns a durable `ThreadDelivery` monitor whose origin is
  the subagent and whose queue target is its root main-thread

#### Scenario: Trusted MCP identity is absent
- **WHEN** an MCP registration has no valid Codex-supplied caller identity
- **THEN** the system rejects before arming and does not accept a replacement
  identity from model arguments

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
- **WHEN** an existing key is reused with a changed origin, target, delivery kind,
  guard, condition, or policy
- **THEN** the system rejects the request and leaves the original monitor unchanged

#### Scenario: A process identity cannot be captured
- **WHEN** registration includes a PID-exit leaf whose PID is absent or cannot be
  uniquely identified on the current boot
- **THEN** the system rejects registration rather than treating it as complete

#### Scenario: A process already terminated before registration
- **WHEN** registration includes a PID-exit leaf whose captured Linux process
  state is `Z`, `X`, or `x`
- **THEN** the system rejects registration so an event that preceded arming does
  not produce an immediate completion wake

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

### Requirement: Evaluate explicit condition evidence conservatively

The system SHALL support absolute or relative time, stable GPU utilization,
tmux pane or session termination, PID termination, durable completion receipt,
and typed `all` or `any` composition. A leaf condition MUST produce `true`,
`false`, or `unknown`; `unknown` MUST NOT satisfy either a leaf or a composed
condition. GPU utilization MUST be represented as heuristic resource-state
evidence, while PID and tmux conditions MUST be represented as liveness
evidence only. A durable receipt reporting success MUST be represented as
task-success evidence. A relative time condition MUST be persisted as an
absolute deadline that remains evaluable after a daemon restart.

#### Scenario: Stable GPU condition reaches its observation window
- **WHEN** every selected GPU remains at or below the registered utilization
  threshold for the registered stability duration
- **THEN** the GPU leaf becomes true and records its sampled resource-state
  evidence

#### Scenario: An observer becomes unavailable
- **WHEN** a condition observer cannot obtain or validate its required host
  evidence
- **THEN** that leaf is unknown and the monitor does not fire on the basis of
  that observation

#### Scenario: A receipt reports success
- **WHEN** a receipt with the monitor's expected identity is durably published
  with a successful terminal status
- **THEN** the receipt leaf becomes true and records task-success evidence

#### Scenario: A captured process becomes a zombie
- **WHEN** the same captured boot ID, PID start time, and UID remain present but
  the Linux process state becomes `Z`, `X`, or `x`
- **THEN** the PID-termination leaf becomes true with
  `process_terminated` liveness evidence without waiting for a parent to reap the
  `/proc` record

#### Scenario: A PID is reused by a terminal process
- **WHEN** the observed PID has a terminal process state but its captured boot
  identity, start time, or UID differs
- **THEN** identity mismatch takes precedence and the leaf is fatal `unknown`,
  never completion evidence for the original process

#### Scenario: A tmux pane is dead but still addressable
- **WHEN** tmux continues to resolve the captured pane or session target after its
  pane process terminates
- **THEN** `tmux_exit` remains false because it observes target removal, not the
  pane process lifecycle

### Requirement: Observe a reserved command-terminal event

The system SHALL support a `command_terminal` condition leaf naming one bound
command reservation. The leaf SHALL remain false until the reservation has an
immutable command-terminal event, then become true for every terminal status,
including failure, cancellation, and signal termination. The witness SHALL
carry bounded command identity, status, exit or signal evidence, timing, and
declared log or artifact paths. It MUST NOT classify command termination as
whole-task success.

#### Scenario: Command succeeds
- **WHEN** the bound reservation publishes a valid `succeeded` terminal event
- **THEN** the leaf becomes true with exit code and bounded execution evidence
  in its witness

#### Scenario: Command fails
- **WHEN** the bound reservation publishes a valid `failed`, `cancelled`, or
  `signaled` terminal event
- **THEN** the leaf becomes true immediately with the failure evidence and does
  not wait for monitor expiry

### Requirement: Observe a reserved worker-terminal event

The system SHALL support a `worker_terminal` condition leaf naming one bound
worker reservation. The leaf SHALL become true for `delivered`, `blocked`,
`failed`, and `cancelled` outcomes. Its witness SHALL carry the producer
identity, declared outcome, and, for a delivery, the candidate commit and
bounded Git attestation. Worker termination and delivery SHALL remain candidate
evidence and MUST NOT be represented as lead acceptance.

#### Scenario: Worker delivers a valid candidate
- **WHEN** the bound worker event is `delivered` and Git attestation is valid
- **THEN** the leaf becomes true with the candidate commit and changed-path
  evidence for lead review

#### Scenario: Worker delivery is invalid
- **WHEN** the worker event is terminal but Git attestation reports a missing,
  baseline-mismatched, or out-of-scope candidate
- **THEN** the leaf still becomes true with an invalid-delivery witness so the
  lead can handle it instead of remaining asleep

#### Scenario: Worker ends without a commit
- **WHEN** the bound worker publishes `blocked`, `failed`, or `cancelled`
- **THEN** the leaf becomes true with its bounded reason and no invented review
  target

### Requirement: Observe a stale producer heartbeat

The system SHALL support a `heartbeat_stale` condition leaf naming one bound
terminal-event reservation and a positive staleness duration. The leaf SHALL be
false while accepted producer heartbeats advance within the duration, true when
the host-observed heartbeat age reaches the duration before terminal
publication, and false once a terminal event exists. It SHALL be classified as
heuristic liveness evidence and MUST NOT claim the producer failed or stopped.

#### Scenario: Producer stops advancing its heartbeat
- **WHEN** no accepted heartbeat advances for the configured duration and no
  terminal event exists
- **THEN** the leaf becomes true with last-sequence and host-observed age
  evidence

#### Scenario: Producer publishes terminal event before staleness
- **WHEN** the reservation has an immutable terminal event
- **THEN** the heartbeat-stale leaf does not independently satisfy the monitor

#### Scenario: Heartbeat evidence is unavailable
- **WHEN** the reservation never received the heartbeat required by its monitor
  contract or its identity cannot be validated
- **THEN** the leaf is `unknown`, not evidence of a stall

### Requirement: Linearize event evaluation and the trigger claim

For a monitor containing terminal-event leaves, the system SHALL reload the
still-armed monitor and its bound reservation, evaluate transaction-current
event/heartbeat facts with a post-lock host time, persist condition/evidence/
witness/evaluation count, and optionally claim the trigger in one short SQLite
transaction. Host, app-server, filesystem, and Git I/O MUST remain outside that
transaction. The first committed terminal publication or expiry/claim SHALL
determine the one durable wake reason and later contenders MUST NOT create a
second claim or activation.

#### Scenario: Terminal publication wins against heartbeat staleness
- **WHEN** a terminal publication commits before a competing heartbeat-stale
  evaluation obtains the claim transaction
- **THEN** the transaction observes the terminal event and can claim only the
  terminal condition, not the no-longer-true stale heartbeat

#### Scenario: Terminal publication races monitor expiry
- **WHEN** terminal publication and an expiry claim contend
- **THEN** the first committed transaction determines the single wake: a stored
  terminal event wins as `condition`, while an already committed expiry claim
  rejects later producer publication

#### Scenario: Bound reservation row is logically corrupt or missing
- **WHEN** an event leaf cannot resolve a logically valid bound reservation
- **THEN** evaluation returns fatal `unknown`; a confirmed deferred monitor
  performs its one `observer_failed` guarded wake, while a legacy monitor
  terminates `observer_failed` without activation

### Requirement: Verify daemon event capability before arming or pausing

The daemon heartbeat/readiness contract SHALL advertise a condition/event
schema capability epoch and loaded source identity. Registering an event monitor
MUST fail before arming, and deferring one MUST fail before pausing, unless the
lock-owning healthy daemon advertises the exact required event capability. A
legacy PID/timestamp-only heartbeat MUST NOT be treated as event-capable.

#### Scenario: Legacy daemon is still healthy
- **WHEN** an event registration or defer observes a fresh legacy daemon
  heartbeat without the required capability epoch
- **THEN** the operation fails closed before monitor arm or goal pause and no
  activation is attempted

#### Scenario: Operator preflights a daemon downgrade
- **WHEN** the current event-capable binary runs its compatibility check with
  the target older epoch while a nonterminal monitor contains an event leaf,
  including when its bound reservation row is missing or corrupt
- **THEN** the check refuses the downgrade before source/cache replacement;
  an already-restored older binary is not claimed to contain this newer guard,
  while completed terminal event history alone does not block rollback

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
commands as conditions. An origin and its resolved delivery target MUST each
resolve to one exact local loaded or durably stored task. The system MUST reject
a remote, deleted, ephemeral, or unsupported target without forking it or
inventing another thread. The MCP trust boundary SHALL be the exact caller
identity supplied out of band by Codex. Only an exact spawned V2 caller MAY be
retargeted, and only by following its validated parent chain to the root
main-thread. Explicit CLI and legacy goal operations remain same-user,
host-local targeting surfaces. Every pointer MUST identify its monitor and
delivery provenance.

#### Scenario: Cross-host target is requested
- **WHEN** an origin or resolved target cannot be resolved as an exact local
  loaded or stored task
- **THEN** registration rejects without contacting a remote control plane

#### Scenario: Raw shell condition is requested
- **WHEN** a condition contains a shell command or untyped executable expression
- **THEN** the system rejects it without execution

#### Scenario: Arbitrary retargeting is requested
- **WHEN** a caller is not an exact spawned V2 subagent or its supplied identity
  is not the trusted MCP caller identity
- **THEN** the system does not reinterpret it as another thread

### Requirement: Route spawned-subagent wakes to the root main-thread

For a `ThreadDelivery` whose origin is a spawned V2 subagent, the system SHALL
recognize only the exact `thread_spawn` subagent source, read its exact local
ancestry through every `parentThreadId` to the topmost root, and SHALL use only
that root as the queue target. Review, compact, memory-consolidation, and other
non-spawn delegate sub-sessions MUST NOT be reinterpreted as V2 workers or
retargeted through their parent and SHALL fail closed when they are not ordinary
root targets. The system MUST reject a missing, malformed, cyclic, or
over-bounded spawn ancestry before arming. It MUST verify that the root accepts
direct input and supports the required queue capability. It SHALL persist the
origin thread, actual delivery target, and resolved ancestry as delivery
provenance while preserving the root target as the owner of all queue admission
and reconciliation operations.

Any `thread_idle` self-wait guard SHALL compare against the actual delivery
target. A spawned child MAY wait for its own turn to end because the root is the
wake target, but it MUST NOT wait for the root whose wake would make that same
root active.

The delivered pointer SHALL wake the root only. The plugin MUST NOT call a
subagent follow-up operation, resume or recreate the subagent, infer acceptance,
or decide whether the root should inspect, continue, discard, or replace the
originating worker. Those decisions belong to the awakened root main-thread.

#### Scenario: A depth-one subagent event fires
- **WHEN** a monitor originating from a spawned child fires after a valid root
  ancestry was frozen at registration
- **THEN** one pointer is admitted for the root and no child continuation is
  attempted

#### Scenario: A review sub-session requests a monitor
- **WHEN** a one-shot review delegate has a non-`thread_spawn` subagent source
  and a parent thread
- **THEN** registration rejects rather than relaying its wake to that parent

#### Scenario: A child waits for its own turn completion
- **WHEN** a spawned child arms `thread_idle` for its own origin thread and the
  actual delivery target is the root
- **THEN** registration permits the condition while a `thread_idle` condition
  naming the root is rejected

#### Scenario: A depth-two subagent event fires
- **WHEN** a monitor originates from a spawned grandchild whose parent is also a
  spawned V2 subagent
- **THEN** the system traverses both parent links and targets the topmost root,
  not the intermediate parent

#### Scenario: An ancestry cycle is observed
- **WHEN** an origin or ancestor repeats before a root is reached
- **THEN** registration rejects without arming or queue delivery

#### Scenario: The root receives the wake pointer
- **WHEN** the root's normal FIFO starts the delivered pointer
- **THEN** the pointer reports the monitor and origin provenance and leaves all
  follow-up judgment to the root

### Requirement: Observe log content read-only with captured file identity

The system SHALL support a `log_pattern` condition leaf that scans one local
log file against a bounded set of named, length-capped regular expressions.
At preparation it SHALL capture the file's identity (device and inode) and
current size, and SHALL scan only bytes appended after arming. A missing file
at preparation is permitted and recorded; the leaf remains false until the
file exists. Rotation, truncation, or identity replacement SHALL make the
leaf `unknown`, never `true`. The observer SHALL read the file read-only
within a bounded per-poll budget, SHALL never execute observed content, and
SHALL classify every match as heuristic evidence. Matched lines SHALL be
appended to a bounded per-monitor journal with explicit drop counters when
caps are exceeded.

#### Scenario: A named pattern matches an appended line
- **WHEN** a line appended after arming matches any named pattern
- **THEN** the leaf is true and its witness records the matching pattern
  names and the bounded matched lines

#### Scenario: The log file is rotated or replaced
- **WHEN** the observed path no longer has the captured device and inode, or
  its size shrinks below the scanned offset
- **THEN** the leaf reports `unknown` with the identity mismatch in evidence
  and never reports completion from the replacement file

#### Scenario: Pattern evaluation exceeds its budget
- **WHEN** scanning or matching exceeds the configured read or wall-clock
  budget for one evaluation
- **THEN** the leaf reports `unknown` with a budget-exceeded evidence record
  and other monitors' evaluation is not delayed

### Requirement: Observe another local thread's turn completion

The system SHALL support a `thread_idle` condition leaf naming another
locally loaded Codex thread, observed only through the existing local
app-server read path. At preparation it SHALL capture the child's identity
and runtime status, and SHALL reject a child already idle at preparation
unless the registration explicitly accepts an already-idle child. The armed
leaf SHALL be true only once the child is observed idle after arming; an
unloaded or missing child SHALL be `unknown`, never `true`. The witness
SHALL record the child's last observed runtime status, goal status, and any
available usage snapshot. The leaf SHALL be classified as heuristic
evidence: an ended turn is not task success.

#### Scenario: Child thread completes its turn
- **WHEN** the armed leaf observes the captured child loaded and idle
- **THEN** the leaf is true with the child's status and usage snapshot in
  its witness

#### Scenario: Child already idle at registration
- **WHEN** the named child is already idle at preparation and the
  registration does not accept an already-idle child
- **THEN** the registration is rejected with a distinct error directing the
  caller to handle the child's result in the current turn

#### Scenario: Child thread disappears
- **WHEN** the captured child becomes unloaded or unknown to the local
  app-server
- **THEN** the leaf reports `unknown` with the observation in evidence and
  never treats disappearance as completion

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

### Requirement: Record re-arm lineage

Registration and defer operations SHALL accept an optional lineage reference
naming a prior monitor. The system SHALL validate that the referenced
monitor exists and is terminal, store the reference, and surface the chain
in status and list output. Lineage SHALL be archival only: no captured
guard, condition, authorization, or state is inherited, and every safety
check runs fresh. The system MUST NOT re-arm any monitor automatically.

#### Scenario: Agent re-defers after handling a wake
- **WHEN** a new defer request names a fired monitor as its lineage
  reference
- **THEN** the new monitor records the chain and passes every registration
  check as if unrelated

#### Scenario: Lineage reference is invalid
- **WHEN** a lineage reference names an unknown or non-terminal monitor
- **THEN** the registration is rejected without side effects

# codex-goal-self-defer Specification

## Purpose
Define optional, guarded delivery to an existing Codex goal while durable
thread-primary event monitoring remains available without any goal.
## Requirements
### Requirement: Best-effort active-goal deferral

The plugin SHALL expose `defer_goal_until_event` for a caller that explicitly
chooses `GoalDelivery` and supplies an exact target task ID, typed condition,
bounded expiry, optional heuristic-continuation authorization, and idempotency key.
For an active goal it SHALL run the existing best-effort pause protocol; for a
paused goal it SHALL capture the guard without another pause. It SHALL reject an
unloaded target, null or blocked goal, changed guard, or invalid input. It MUST NOT
claim the supplied task ID is authenticated as the caller and MUST NOT create a
goal.

#### Scenario: Eligible active goal is deferred
- **WHEN** the explicit target is locally loaded with one active goal and valid
  typed condition
- **THEN** the plugin returns a durable receipt only after confirming that same goal
  paused, with an instruction to end the current turn

#### Scenario: Target is not eligible for defer
- **WHEN** the target is unloaded, has no goal, has a blocked goal, or changes guard
  during admission
- **THEN** the operation rejects or records the existing conservative outcome
  without creating or compensating a goal

#### Scenario: Eligible paused goal is attached
- **WHEN** the explicit target is locally loaded with the same paused goal
- **THEN** the plugin arms `GoalDelivery` without sending a pause and instructs
  the caller to end the current turn

### Requirement: Watcher-first and pause-once ordering

The plugin SHALL establish its same-host watcher before changing an active goal.
It SHALL durably record pause-in-progress before the one pause request, send at
most one paused-status update, and re-observe the same goal before arming. When the
goal is already paused, it SHALL verify watcher readiness and a stable two-read
guard without consuming a pause attempt.

#### Scenario: Watcher is unavailable
- **WHEN** watcher readiness cannot be established before active-goal pause or
  paused-goal arm
- **THEN** the operation records unavailable supervision and sends no pause

#### Scenario: Pause is conclusively confirmed
- **WHEN** the pause response and subsequent observation show the captured goal
  paused
- **THEN** the plugin arms exactly one monitor for that guard

#### Scenario: Goal was already paused
- **WHEN** two admission observations show the same captured paused goal
- **THEN** the plugin arms it without issuing a redundant goal-status write

### Requirement: Fail-closed pause uncertainty
The plugin SHALL never retry a pause after a crash, timeout, disconnect, or
other inconclusive pause result. It SHALL record a distinct terminal outcome
for pause uncertainty, rejection, or a changed returned goal marker and SHALL
not send a compensating goal update.

#### Scenario: Pause delivery becomes uncertain
- **WHEN** the process restarts while a pause was in progress or the local
  control plane returns an inconclusive pause result
- **THEN** the monitor reaches a visible terminal uncertainty outcome and no
  additional pause or activation request is sent automatically

#### Scenario: Pause affects a different goal marker
- **WHEN** the pause response or confirmation does not match the captured goal
  marker
- **THEN** the monitor records a mis-targeted-pause outcome and makes no
  compensating or retry request

### Requirement: Active-turn continuation barrier
For a deferred monitor, the plugin SHALL not evaluate or claim a wake condition
while the target runtime is active. It SHALL retain the armed deferred monitor
until the target is idle and still owns the exact captured paused goal, then
use the guarded one-shot continuation policy.

#### Scenario: Condition is true before the agent ends its turn
- **WHEN** a deferred monitor's condition is already true but the target task
  runtime is active
- **THEN** the monitor remains armed and does not activate the goal

#### Scenario: Idle deferred target reaches an authorized condition
- **WHEN** the target becomes idle with the same captured paused goal and the
  wake condition is authorized by the established policy
- **THEN** the plugin performs at most one guarded continuation attempt

### Requirement: Native guarded goal continuation
After the plugin has conclusively changed the captured paused goal to active,
it SHALL rely on the installed local Codex goal runtime to continue an idle
active goal. It MUST NOT add `thread/resume`, `turn/start`, or a synthetic user
message to create a second continuation path. The plugin SHALL retain its
single durable active-status request and never retry an uncertain activation.

#### Scenario: Active goal continues through the native runtime
- **WHEN** a matching paused and idle target reaches an authorized wake
  condition and the active-status update is conclusively confirmed
- **THEN** the plugin makes no further task-control request and the installed
  local goal runtime is eligible to start the next goal turn

#### Scenario: Active-status delivery becomes uncertain
- **WHEN** the process restarts while the active-status request is in progress
  or the local control plane returns an inconclusive activation result
- **THEN** the monitor reaches the existing visible activation-uncertain
  outcome and no further task-control request is sent automatically

### Requirement: Agent-facing automatic-defer guidance

The packaged skill SHALL present durable event monitoring as the primary behavior
and goal delivery as an optional compatibility path. For any exact local task,
including `goal=null`, active, paused, or blocked, it SHALL permit
`wait_for_event` when the verified queue capability is available, report the
detached monitor receipt, and end the current turn without calling `create_goal`.
It SHALL use `defer_goal_until_event` only when the agent explicitly needs to
pause and later reactivate an eligible active or paused goal.

The guidance SHALL retain the 15-minute threshold, no-independent-work test,
typed evidence and failure-signature requirements, bounded expiry, one status call
after delivery, lineage rules, material billed-turn warning, and the boundary that
a wake is not success. If thread-delivery capability is unavailable, it SHALL
report that fact rather than create a goal or choose another continuation path.

#### Scenario: Long dependent wait without a goal
- **WHEN** an agent has a same-host job expected to block for at least 15 minutes,
  no useful independent work, and `goal=null`
- **THEN** the skill permits one bounded `wait_for_event`, forbids goal creation,
  and directs the agent to end the current turn

#### Scenario: Long dependent wait with an existing goal
- **WHEN** the same wait occurs in a task with an active, paused, or blocked goal
- **THEN** the skill defaults to `wait_for_event` and uses explicit goal defer
  only when goal pause/reactivation semantics are actually desired and eligible

#### Scenario: Agent resumes after a wake
- **WHEN** a queue pointer or guarded goal wake starts a later turn
- **THEN** the skill directs exactly one status read, action based on the witness,
  and re-registration only for another bounded wait

#### Scenario: Log watch omits failure signatures
- **WHEN** a proposed `log_pattern` matches success but omits actionable failure
  signatures
- **THEN** the skill requires the failure alternation before arming

#### Scenario: Queue capability is unavailable
- **WHEN** the installed runtime fails thread-delivery preflight
- **THEN** the skill reports `ThreadDelivery` unavailable without creating a goal,
  invoking a compatibility alias, or starting a CLI-resume process

### Requirement: Preserve explicit goal-delivery compatibility APIs

The plugin SHALL expose `defer_goal_until_event` as the canonical optional
`GoalDelivery` operation and SHALL retain `wake_me_up` and
`wake_me_up_defer` as documented compatibility aliases for one migration period.
`wake_me_up` MUST retain paused-goal admission and `wake_me_up_defer` MUST
retain active-goal watcher-first pause semantics. Aliases MUST preserve
idempotency, guards, evidence, expiry, and at-most-once goal activation and MUST
NOT create a goal or select `ThreadDelivery` implicitly.

#### Scenario: Existing paused-goal caller uses compatibility alias
- **WHEN** a caller invokes `wake_me_up` with its existing valid payload
- **THEN** it maps to paused `GoalDelivery` without changing guarded behavior

#### Scenario: Existing active-goal caller uses defer alias
- **WHEN** a caller invokes `wake_me_up_defer` with its existing valid payload
- **THEN** it maps to active `GoalDelivery` with watcher-first pause

#### Scenario: Alias sees no goal
- **WHEN** either compatibility alias targets a thread whose goal is null
- **THEN** it rejects without creating a goal and directs the caller to
  `wait_for_event`

### Requirement: Deferred terminal wake policy

For a deferred monitor whose own pause was conclusively confirmed (it reached
the armed state), the system SHALL treat expiry, condition satisfaction
without continuation authorization, and irrecoverable observer failure as
wake-eligible facts rather than terminal stranded outcomes. Each SHALL be
consumed through the existing single durable trigger claim and guarded
activation path, with a durable `wake_reason` of `expired`,
`unauthorized_evidence`, or `observer_failed` (`condition` for an authorized
condition wake). The system MUST NOT add a second activation path, MUST apply
the idle barrier and pre-activation guard re-read to every wake reason, and
MUST leave every pre-arm, guard-violating, cancelled, or
activation-uncertain state fail-closed exactly as currently specified.

#### Scenario: Deferred monitor expires while armed
- **WHEN** an armed deferred monitor with a confirmed pause reaches its
  expiry and the target is later observed idle with the same captured paused
  goal
- **THEN** the system consumes its single guarded activation with wake
  reason `expired` and records that reason in the terminal receipt

#### Scenario: Heuristic evidence fires without authorization
- **WHEN** an armed deferred monitor's condition is satisfied only by
  heuristic leaves and the registration did not authorize heuristic
  continuation
- **THEN** the system wakes the target through the same guarded path with
  wake reason `unauthorized_evidence`, retains the witness, and makes no
  claim of task success

#### Scenario: Observer identity irrecoverably fails
- **WHEN** an armed deferred monitor's observer reports an irrecoverable
  identity violation that can never become true
- **THEN** the system wakes the target through the same guarded path with
  wake reason `observer_failed` and records the failure detail

#### Scenario: Guard no longer matches at a deadline wake
- **WHEN** an armed deferred monitor passes its expiry but the pre-activation
  re-read shows a changed, unloaded, or non-matching target
- **THEN** the system records the existing conservative terminal outcome and
  sends no activation request

#### Scenario: Daemon restart discovers an expired deferred monitor
- **WHEN** the daemon starts and finds an armed or already-claimed deferred
  monitor whose expiry passed
- **THEN** an armed monitor is claim-eligible with wake reason `expired` and
  a claimed monitor continues its wake under the same guarded path, rather
  than either becoming a stranded terminal expiry

#### Scenario: Transient observation failure neither strands nor wakes
- **WHEN** an untyped or transient failure (for example a local control-plane
  read error) occurs while observing an armed deferred monitor
- **THEN** the monitor records the evidence and remains armed with expiry as
  the backstop, neither reaching a stranded terminal state nor consuming the
  single wake

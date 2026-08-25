## ADDED Requirements

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

## MODIFIED Requirements

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

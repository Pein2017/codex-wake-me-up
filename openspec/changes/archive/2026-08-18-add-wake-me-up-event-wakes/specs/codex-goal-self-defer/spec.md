## Purpose

Extend the self-defer contract so a goal the plugin itself paused is never
stranded paused by the plugin's own bookkeeping: every post-arm terminal fact
of a healthy deferred monitor becomes a guarded wake with a durable reason.

## ADDED Requirements

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

## MODIFIED Requirements

### Requirement: Agent-facing automatic-defer guidance

The packaged skill SHALL instruct an agent to choose self-defer only after it
has launched a same-host job expected to block for at least 15 minutes and
has no useful independent work. The skill SHALL direct it to use the defer
operation, report the monitor receipt, and end its turn; it SHALL retain the
existing heuristic-evidence and fail-closed constraints. The skill SHALL
state that expiry itself wakes a deferred goal, so a separate
`any(condition, time)` deadline backstop is unnecessary; that every
`log_pattern` watch MUST include the failure signatures the agent would act
on, because a success-only watch is silent through a crashloop; that a wake
is not a success claim and the witness decides; and that the first post-wake
action is exactly one status call followed by acting on the wake report,
optionally re-deferring with a recorded lineage reference for a
per-occurrence loop.

#### Scenario: Long dependent wait
- **WHEN** an agent has a known same-host job expected to take at least 15
  minutes and all remaining work depends on it
- **THEN** the skill permits one bounded best-effort defer request instead of
  sleeping or running a polling loop

#### Scenario: Agent resumes after a wake
- **WHEN** a deferred goal returns to active after its monitor fired
- **THEN** the skill directs the agent to read the wake report once, act on
  the recorded reason and witness, and re-defer with a lineage reference only
  if a further bounded wait is genuinely required

#### Scenario: Log watch omits failure signatures
- **WHEN** an agent proposes a `log_pattern` condition matching only a
  success line
- **THEN** the skill directs it to widen the alternation with the failure
  signatures it would act on before arming

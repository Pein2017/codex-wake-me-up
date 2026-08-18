## Purpose

Provide a conservative host-local way for a Codex agent to defer a loaded
active goal while an external same-host job runs, then continue it at most once
only after the existing guarded wake-up conditions are satisfied.

## ADDED Requirements

### Requirement: Best-effort active-goal deferral
The plugin SHALL expose a distinct best-effort defer operation that accepts an
explicit target task ID, a typed wake condition, a bounded expiry, optional
heuristic-continuation authorization, and an idempotency key. It SHALL reject
an unloaded target, a target without an active goal, or invalid monitor input.
The operation SHALL describe the supplied task ID as a best-effort target and
MUST NOT claim to authenticate it as the caller's current task.

#### Scenario: Eligible active goal is deferred
- **WHEN** the explicit target is locally loaded with one active goal and the
  typed condition is valid
- **THEN** the plugin records a guarded defer intent and returns a durable
  monitor receipt only after the target is confirmed paused with the same goal
  marker, including an instruction for the agent to end its current turn

#### Scenario: Target is not eligible for defer
- **WHEN** the explicit target is unloaded, has no goal, or has a goal whose
  status is not active
- **THEN** the defer operation rejects it without changing that goal

### Requirement: Watcher-first and pause-once ordering
The plugin SHALL establish that its same-host watcher is ready before it sends
the one pause request. It SHALL durably record a pause-in-progress state before
that request, send at most one goal-status update to paused, and re-observe the
captured goal marker before arming the monitor.

#### Scenario: Watcher is unavailable
- **WHEN** the watcher cannot be positively established before a defer pause
- **THEN** the operation records an unavailable-supervision outcome and does
  not send a pause request

#### Scenario: Pause is conclusively confirmed
- **WHEN** the pause response and a subsequent observation both show the same
  captured goal marker in paused status
- **THEN** the plugin arms exactly one wake-up monitor for that guard

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
The packaged skill SHALL instruct an agent to choose self-defer only after it
has launched a same-host job expected to block for at least 15 minutes and has
no useful independent work. The skill SHALL direct it to use the defer
operation, report the monitor receipt, and end its turn; it SHALL retain the
existing heuristic-evidence and fail-closed constraints.

#### Scenario: Long dependent wait
- **WHEN** an agent has a known same-host job expected to take at least 15
  minutes and all remaining work depends on it
- **THEN** the skill permits one bounded best-effort defer request instead of
  sleeping or running a polling loop

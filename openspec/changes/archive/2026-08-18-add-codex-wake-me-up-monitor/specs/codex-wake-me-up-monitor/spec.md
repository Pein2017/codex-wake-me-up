## Purpose

Provide a conservative, host-local way to wait for explicit external work
conditions and continue one exact paused Codex goal without requiring an
operator to watch a terminal or manually return to the task.

## ADDED Requirements

### Requirement: Register a typed, one-shot monitor

The system SHALL let an operator register a monitor with an exact target
thread identity, a captured paused-goal guard, a typed condition expression,
an expiry, and an optional idempotency key. The system MUST reject a request
whose target or condition cannot be safely identified at registration time.
The target MUST be loaded and own a paused goal when it is captured, but it
need not be idle until a continuation is about to be attempted. The same
idempotency key and semantically identical request MUST return the original
monitor; the same key with different semantics MUST be rejected.

#### Scenario: A valid monitor is armed
- **WHEN** an operator registers a loaded target with its current paused goal
  and a supported condition that can be observed on the host
- **THEN** the system returns a durable monitor identifier and an `armed`
  lifecycle state without activating the target immediately

#### Scenario: A main thread arms its own paused goal
- **WHEN** a loaded target owns a paused goal but is currently active because
  its own main thread is registering the monitor
- **THEN** the system captures the paused-goal guard and arms the monitor
  without requiring the target to be idle at registration

#### Scenario: A reused idempotency key has different semantics
- **WHEN** an operator submits a registration using an existing idempotency key
  but changes the target, goal guard, condition, or activation policy
- **THEN** the system rejects the request and leaves the original monitor
  unchanged

#### Scenario: A process identity cannot be captured
- **WHEN** an operator registers a PID-exit condition for a PID that is absent
  or cannot be uniquely identified on the current boot
- **THEN** the system rejects the registration rather than treating the PID as
  already complete

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

### Requirement: Require explicit authorization for heuristic continuation

The system MUST decide continuation authorization from the actual true-leaf
witness that satisfied the condition at trigger claim time. It MUST NOT
activate a goal from a time, GPU, PID, or tmux witness unless the registration
explicitly authorizes heuristic/liveness-based continuation. A witness that
contains a successful receipt-success leaf may authorize continuation without
that heuristic opt-in. The system MUST retain the witness evidence in the
terminal receipt.

#### Scenario: Heuristic witness lacks opt-in
- **WHEN** a condition is satisfied by time, GPU, PID, or tmux evidence and
  its true-leaf witness contains no successful receipt while the registration
  does not authorize that evidence for activation
- **THEN** the system records the satisfied condition but terminates without
  activating the target

#### Scenario: A receipt-or-time condition is satisfied by time
- **WHEN** an `any(receipt-success, time)` condition is satisfied by its time
  leaf while the receipt is not successful and heuristic continuation is not
  authorized
- **THEN** the system terminates without activating the target and records the
  time-only witness

#### Scenario: Combined receipt and liveness condition succeeds
- **WHEN** a registration explicitly requires both a successful receipt and a
  PID-exit condition
- **THEN** the system may activate only after both leaves are true and the
  target guard remains valid

### Requirement: Persist and recover monitor lifecycle state

The system SHALL durably record a monitor's immutable registration, current
evidence, trigger claim, activation attempt, and terminal outcome in a
host-local runtime state root. After a daemon restart, the system MUST rebuild
eligible monitors from durable state and re-observe their conditions. A
monitor MUST have at most one durable trigger claim and at most one activation
attempt. Before a continuation request is sent, the system MUST durably commit
that its single activation attempt has been consumed; recovery of that state
MUST produce an uncertain terminal outcome rather than a second request.

#### Scenario: Daemon restarts while a monitor is armed
- **WHEN** the daemon stops and later starts before the monitor expires
- **THEN** it restores the monitor from durable state and re-evaluates the
  condition before deciding whether to trigger

#### Scenario: Monitor has already been claimed
- **WHEN** two evaluations observe a satisfiable condition for the same
  monitor
- **THEN** only the evaluation that obtains the durable trigger claim may make
  an activation attempt

#### Scenario: Monitor expires before a usable trigger
- **WHEN** the expiry is reached before the monitor obtains a trigger claim
- **THEN** the monitor becomes `expired` and never activates the target

### Requirement: Continue only the guarded loaded paused goal

The system SHALL preflight continuation only if the target is still loaded on
the local host, has runtime status `idle`, and owns the same captured paused
goal. It MUST make no more than one continuation attempt. If the target is
unloaded, not idle, has any goal status other than `paused`, has a different
goal, the control transport fails, or the result is uncertain, the system MUST
record a non-firing terminal outcome and MUST NOT retry automatically or
create a new target turn.

The local Codex control protocol does not provide an expected-goal
compare-and-set. The system MUST therefore compare the returned goal marker
with the captured guard after its one permitted request. If they differ, it
MUST record `mis-targeted-activation` with both markers, MUST NOT make a
compensating goal write, and MUST NOT retry.

#### Scenario: Guard remains valid at activation
- **WHEN** a monitor has a trigger claim and the target is loaded, idle, and
  still owns the captured paused goal
- **THEN** the system makes one continuation attempt for that goal and records
  the resulting outcome

#### Scenario: Target has been unloaded
- **WHEN** a monitor reaches a trigger claim but its target is not loaded on
  the local host
- **THEN** the system records an `unloaded-target` non-firing outcome without
  resuming or otherwise changing the target

#### Scenario: Activation result is uncertain
- **WHEN** the local Codex control transport cannot conclusively report the
  outcome of the one permitted continuation attempt
- **THEN** the system records an uncertain non-firing outcome and does not
  retry the attempt

#### Scenario: Goal changes during the protocol race window
- **WHEN** the target passes the preflight guard but the returned goal marker
  after the one continuation request differs from the captured paused-goal
  guard
- **THEN** the system records `mis-targeted-activation` with both markers and
  makes neither a compensating write nor a retry

### Requirement: Provide inspectable operator controls

The system SHALL provide local controls to register, inspect, and cancel a
monitor. Inspection MUST expose the target identity, guarded goal identity,
condition summary, evidence classification, lifecycle state, terminal outcome,
timestamps, and daemon liveness/heartbeat. Cancellation while a monitor is
registering, armed, or claimed but before its activation attempt MUST durably
prevent future activation. An armed monitor without a live daemon heartbeat
MUST be reported as unsupervised rather than healthy.

#### Scenario: Operator inspects a monitor
- **WHEN** an operator requests monitor status by identifier
- **THEN** the system returns its durable registration, latest evidence, and
  lifecycle or terminal state

#### Scenario: Operator cancels a claimed monitor
- **WHEN** an operator cancels a monitor while it is armed or claimed but
  before it has made an activation attempt
- **THEN** the system records `cancelled` and no later observation activates
  the target

### Requirement: Keep the control plane host-local and constrained

The system MUST use only the existing local Codex control transport for
continuation, MUST NOT expose that transport through a TCP listener, and MUST
NOT accept an arbitrary shell command as a condition. It MUST reject an
attempt to target a thread owned by a different host or to evaluate a raw shell
predicate.

#### Scenario: Cross-host target is requested
- **WHEN** an operator requests a target that cannot be resolved as a locally
  loaded Codex thread
- **THEN** the system rejects the registration without contacting a remote
  control plane

#### Scenario: Raw shell condition is requested
- **WHEN** an operator submits a condition containing a shell command or
  untyped executable expression
- **THEN** the system rejects the request without executing it

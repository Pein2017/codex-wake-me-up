## MODIFIED Requirements

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

## ADDED Requirements

### Requirement: Route spawned-subagent wakes to the root main-thread

For a `ThreadDelivery` whose origin is a spawned V2 subagent, the system SHALL
recognize only the exact `thread_spawn` subagent source, read its exact local
ancestry through every `parentThreadId` to the topmost root, and SHALL use only
that root as the queue target. Review, compact, memory-consolidation, and other
non-spawn delegate sub-sessions MUST NOT be reinterpreted as V2 workers or
retargeted through their parent and SHALL fail closed when they are not ordinary
root targets. The system MUST reject a missing, malformed, cyclic, or
over-bounded spawn ancestry
before arming. It MUST verify that the root accepts direct input and supports the
required queue capability. It SHALL persist the origin thread, actual delivery
target, and resolved ancestry as delivery provenance while preserving the root
target as the owner of all queue admission and reconciliation operations.

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

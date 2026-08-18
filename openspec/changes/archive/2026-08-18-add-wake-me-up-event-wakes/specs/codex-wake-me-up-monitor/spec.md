## Purpose

Extend the monitor contract with two event-carrying condition leaves, a
self-describing wake report, and re-arm lineage, and scope the existing
expiry and heuristic-authorization outcomes so deferred monitors can follow
the deferred terminal-wake policy.

## ADDED Requirements

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

For every fired monitor the terminal receipt SHALL include the wake reason,
the satisfying witness or failure detail, the bounded matched-line journal
tail when a `log_pattern` leaf exists, arming and firing timestamps, and the
evaluation count. The status operation SHALL return this report so one call
fully re-orients a woken agent. The system MUST NOT mutate the goal
objective or any user-owned text to carry wake context.

#### Scenario: Woken agent reads its context in one call
- **WHEN** an agent's goal returns to active after its monitor fired and the
  agent calls the status operation once with the monitor ID
- **THEN** the response contains the wake reason, witness, journal tail, and
  wait statistics without requiring further discovery calls

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

## MODIFIED Requirements

### Requirement: Require explicit authorization for heuristic continuation

The system MUST decide continuation authorization from the actual true-leaf
witness that satisfied the condition at trigger claim time. For a
non-deferred monitor it MUST NOT activate a goal from a time, GPU, PID,
tmux, log-pattern, or thread-idle witness unless the registration explicitly
authorizes heuristic/liveness-based continuation. For a deferred monitor
with a confirmed pause, an unauthorized heuristic witness SHALL follow the
deferred terminal-wake policy: the wake proceeds through the guarded path
labeled `unauthorized_evidence` and MUST NOT be presented as an authorized
or successful outcome. A witness that contains a successful receipt-success
leaf may authorize continuation without the heuristic opt-in. The system
MUST retain the witness evidence in the terminal receipt.

#### Scenario: Heuristic witness lacks opt-in
- **WHEN** a non-deferred monitor's condition is satisfied by heuristic
  evidence, its witness contains no successful receipt, and the registration
  does not authorize that evidence for activation
- **THEN** the system records the satisfied condition but terminates without
  activating the target

#### Scenario: Heuristic witness lacks opt-in on a deferred monitor
- **WHEN** a deferred monitor with a confirmed pause is satisfied only by
  heuristic evidence without authorization
- **THEN** the system performs the guarded wake with the
  `unauthorized_evidence` reason and the retained witness, making no success
  claim

#### Scenario: A receipt-or-time condition is satisfied by time
- **WHEN** a non-deferred monitor's `any(receipt-success, time)` condition is
  satisfied by its time leaf while the receipt is not successful and heuristic
  continuation is not authorized
- **THEN** the system terminates without activating the target and records the
  time-only witness

#### Scenario: Combined receipt and liveness condition succeeds
- **WHEN** a registration explicitly requires both a successful receipt and a
  PID-exit condition
- **THEN** the system may activate only after both leaves are true and the
  target guard remains valid

### Requirement: Persist and recover monitor lifecycle state

The system SHALL durably record a monitor's immutable registration, current
evidence, trigger claim, wake reason, activation attempt, and terminal
outcome in a host-local runtime state root. After a daemon restart, the
system MUST rebuild eligible monitors from durable state and re-observe
their conditions, applying the deferred terminal-wake policy to armed and
claimed deferred monitors found past expiry (an armed monitor becomes
claim-eligible with reason `expired`; a claimed monitor continues its wake). A monitor MUST have at most one durable
trigger claim and at most one activation attempt regardless of wake reason.
Before a continuation request is sent, the system MUST durably commit that
its single activation attempt has been consumed; recovery of that state MUST
produce an uncertain terminal outcome rather than a second request.

#### Scenario: Daemon restarts while a monitor is armed
- **WHEN** the daemon stops and later starts before the monitor expires
- **THEN** it restores the monitor from durable state and re-evaluates the
  condition before deciding whether to trigger

#### Scenario: Monitor has already been claimed
- **WHEN** two evaluations observe a satisfiable condition for the same
  monitor
- **THEN** only the evaluation that obtains the durable trigger claim may
  make an activation attempt

#### Scenario: Monitor expires before a usable trigger
- **WHEN** a non-deferred monitor's expiry is reached before it obtains a
  trigger claim
- **THEN** the monitor becomes `expired` and never activates the target

#### Scenario: Deferred monitor expires before a usable trigger
- **WHEN** a deferred monitor with a confirmed pause reaches expiry before
  any trigger claim
- **THEN** its expiry is consumed as an `expired` wake under the deferred
  terminal-wake policy instead of a stranded terminal state

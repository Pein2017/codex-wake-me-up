## ADDED Requirements

### Requirement: Bound primary pre-arm app-server reads

The system SHALL enforce one documented 25-second wall-clock budget over the
primary app-server target-resolution and condition-observation reads performed
before a monitor row is created. If that read budget expires, registration MUST
return a typed `monitor_created=false` result naming the failed read stage, a
bounded timeout reason, and whether a read-only retry remains safe. The
existing at-most-one transport retry MAY occur only while time remains;
definitive read errors MUST NOT be retried, and the budget MUST NOT cover
synthetic synchronous identity capture, queue admission, or any write after row
creation. Identity capture MUST retain its own existing per-observer bounds and
MUST NOT create a row after the read deadline has expired.

#### Scenario: Target resolution exceeds the read budget

- **WHEN** the app-server target-resolution read does not finish within the
  documented read budget
- **THEN** registration returns a timeout at the target-resolution stage with
  `monitor_created=false`, `safe_to_retry=true`, and no ledger row, trigger
  binding, or queue item

#### Scenario: Condition observation exceeds the remaining read budget

- **WHEN** target resolution succeeds but an app-server condition observation
  consumes the remaining read budget
- **THEN** registration returns a timeout at the condition-observation stage
  with `monitor_created=false` and leaves no durable monitor or delivery side
  effect

#### Scenario: A transient read retry still fits the budget

- **WHEN** the first read fails with a retryable transport error and the
  second read can complete before the same read budget expires
- **THEN** registration makes at most two read attempts, can arm normally, and
  reports the retry as part of the registration receipt or audit status

#### Scenario: No retry is safe after a definitive or exhausted failure

- **WHEN** a read returns a definitive rejection, or the one allowed retry has
  already been attempted, or no read budget remains
- **THEN** registration fails closed without another read and reports the
  failure stage and retry disposition without creating a monitor

#### Scenario: Synchronous identity capture overruns the read deadline

- **WHEN** bounded Git, tmux, PID, or condition-shape capture finishes after
  the app-server read deadline
- **THEN** registration fails rowlessly before ledger creation and does not
  claim that an arbitrary synchronous callback was interruptibly bounded

### Requirement: Make observation expiry and current-monitor state explicit

An armed registration receipt SHALL describe `expires_at` as the observation
expiry only. Its compact next action and adjacent receipt fields MUST
distinguish condition satisfaction, notification recording, target-status
consumption, and lead acceptance, and MUST NOT present observation expiry as a
delivery or task-success deadline. The trusted
current-task monitor query SHALL expose a compact condition binding, delivery
state, supervision state, and existing lifecycle timestamps for each matching
monitor, including non-terminal admitted or stale work. Existing rows MUST be
preserved; the query MUST NOT auto-clean, replay, quarantine, or declare old
work successful.

#### Scenario: Armed receipt explains the expiry semantics

- **WHEN** a monitor reaches `armed`
- **THEN** its human-readable next action identifies observation expiry at the
  recorded time, and its compact notation/receipt context keeps notification,
  target-status consumption, task completion, and acceptance separate

#### Scenario: Current-task query shows what is waiting

- **WHEN** the trusted caller asks for monitors targeting its current task
- **THEN** the response includes each monitor's condition type/binding,
  delivery state, supervision state, and available creation, admission,
  reconciliation, and expiry timestamps

#### Scenario: Stale or admitted work remains diagnosable

- **WHEN** a matching monitor is `claimed`, `queue_accepted`,
  `delivery_uncertain`, or otherwise non-terminal after its target becomes
  unavailable
- **THEN** the query preserves the row and reports its last known delivery or
  reconciliation facts without replaying delivery or treating it as success

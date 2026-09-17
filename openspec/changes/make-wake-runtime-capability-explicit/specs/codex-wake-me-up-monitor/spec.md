## ADDED Requirements

### Requirement: Report runtime capabilities without changing monitor state

The system SHALL provide a read-only capability report for the current plugin,
lock-owning daemon, and local Codex control connection. The report SHALL
separately classify required native-worker observation as `available`,
`unavailable`, or `indeterminate`, retain a bounded structured reason, and
state the required Core method when it is unavailable. It MUST NOT arm,
cancel, retarget, resume, enqueue, or otherwise mutate a monitor or task.
Native-worker registration MUST fail closed with the same compact structured
unavailable reason when that required method is unavailable. It MUST NOT fall
back to `thread_idle` or any weaker condition.

#### Scenario: Installed Core lacks native-worker observation
- **WHEN** the Core rejects the required native-worker observation method as
  unsupported
- **THEN** the report returns `unavailable`, names the required method, omits
  the unbounded Core method catalogue, and no monitor is created

#### Scenario: Capability report cannot contact the local control connection
- **WHEN** the read-only capability observation has a typed transport failure
- **THEN** the report returns `indeterminate` with a bounded transport reason
  and makes no monitor or delivery mutation

#### Scenario: Native worker is requested while unavailable
- **WHEN** a trusted caller registers `native_worker_terminal` and the required
  Core method is unavailable
- **THEN** registration fails closed with `monitor_created=false`,
  `required_capability=thread/agent/observe`, and no substituted condition

### Requirement: Make pre-arm registration failures and recovery inspectable

Before a monitor row is created, the registration operation SHALL identify
target resolution and condition preparation failures with a stable stage,
`monitor_created=false`, and a bounded error classification. It SHALL expose
whether a retry was safe. A typed local app-server transport failure during a
read-only target-resolution or preparation observation MAY be retried once
within the same registration request, only while no ledger row, event binding,
delivery admission, or task mutation exists. All other failures, any exhausted
retry, and every failure after row creation SHALL remain fail-closed and MUST
NOT resend a queue admission or synthesize a monitor.

#### Scenario: Target read recovers before registration
- **WHEN** the first read-only target-resolution observation has a typed
  transport failure and the one retry succeeds before monitor creation
- **THEN** registration can return its ordinary armed receipt and exactly one
  monitor is created

#### Scenario: Target read exhausts its bounded retry
- **WHEN** both allowed read-only target-resolution observations have typed
  transport failures before monitor creation
- **THEN** the response reports the resolution stage, `monitor_created=false`,
  `safe_to_retry=true`, and no monitor or queue admission exists

#### Scenario: Condition preparation has a definitive failure
- **WHEN** condition preparation returns a malformed or unsupported response
- **THEN** registration reports `monitor_created=false` with
  `safe_to_retry=false` and creates no monitor

#### Scenario: Queue admission has begun
- **WHEN** a monitor has consumed or may have consumed its queue admission
  attempt
- **THEN** this recovery rule does not enqueue a second pointer and existing
  delivery reconciliation remains authoritative

### Requirement: Record trusted delivery-target status consumption distinctly

The system SHALL record `target_status_observed` only when the exact trusted
MCP caller identity equals a monitor's frozen thread-delivery target and reads
that monitor's status. The status response SHALL distinguish condition
satisfied, pointer recorded or uncertain, trusted-target status observation,
and task or lead acceptance. A recorded status observation MUST NOT imply that
the pointer was understood, that work succeeded, or that a lead accepted it.

#### Scenario: Intended target reads a delivered monitor
- **WHEN** the trusted target task requests that monitor's status after a
  thread-delivery receipt exists
- **THEN** the monitor records target-status observation while preserving its
  separate delivery and acceptance fields

#### Scenario: Another task inspects the monitor
- **WHEN** a caller other than the frozen delivery target requests status
- **THEN** the status is returned without recording target-status observation

### Requirement: Provide a scoped ordinary monitor surface

The system SHALL provide a current-task monitor query that derives its task
scope solely from trusted MCP caller metadata and returns only monitors whose
frozen thread-delivery target is that task. Normal registration, status, and
cancel responses SHALL remain the ordinary usage surface. An armed registration
receipt SHALL additionally include one concise human-readable summary naming
the bounded condition, target, expiry, and the fact that a wake is not task
acceptance.

#### Scenario: Target lists its current monitors
- **WHEN** a trusted task requests its current monitors
- **THEN** the response contains only monitors bound to that task and their
compact wait state

#### Scenario: Caller identity is absent
- **WHEN** the current-monitor query lacks one trusted caller identity
- **THEN** it fails closed without exposing another task's monitor list

## MODIFIED Requirements

### Requirement: Observe another local thread's turn completion

The system SHALL support a `thread_idle` condition leaf naming another locally
loaded Codex thread, observed only through the existing local app-server read
path. At preparation it SHALL capture the child's identity and runtime status,
and SHALL reject a child already idle at preparation unless the registration
explicitly accepts an already-idle child. The armed leaf SHALL be true only
once the child is observed idle after arming; an unloaded or missing child SHALL
be `unknown`, never `true`. The witness SHALL record the child's last observed
runtime status and any available usage snapshot. Observation for this condition
MUST NOT request or record goal state merely to determine turn completion. The
leaf SHALL be classified as heuristic evidence: an ended turn is not task
success.

#### Scenario: Child thread completes its turn
- **WHEN** the armed leaf observes the captured child loaded and idle
- **THEN** the leaf is true with the child's status and usage snapshot in its
  witness without an additional goal read

#### Scenario: Child already idle at registration
- **WHEN** the named child is already idle at preparation and the registration
  does not accept an already-idle child
- **THEN** the registration is rejected with a distinct error directing the
  caller to handle the child's result in the current turn

#### Scenario: Child thread disappears
- **WHEN** the captured child becomes unloaded or unknown to the local
  app-server
- **THEN** the leaf reports `unknown` with the observation in evidence and
  never treats disappearance as completion

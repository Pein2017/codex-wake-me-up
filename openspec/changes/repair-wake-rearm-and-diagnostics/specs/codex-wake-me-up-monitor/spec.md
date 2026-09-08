## MODIFIED Requirements

### Requirement: Record re-arm lineage

Registration and defer operations SHALL accept an optional lineage reference
naming a prior monitor whose single trigger and selected delivery attempt can no
longer be repeated. A terminal monitor satisfies this rule. A `ThreadDelivery`
monitor in `queue_accepted` also satisfies it because its trigger claim and queue-add
attempt are durably consumed, even while history reconciliation remains pending.

The system SHALL store the reference and surface the chain in status and list
output. Lineage SHALL be archival only: no captured guard, condition,
authorization, state, success claim, or delivery settlement is inherited, and
every safety check runs fresh. Accepting lineage MUST NOT make `queue_accepted`
terminal, stop its reconciliation, resend its pointer, mark it successful, or
re-arm any monitor automatically.

#### Scenario: Agent re-defers after handling a wake
- **WHEN** a new defer request names a fired monitor as its lineage reference
- **THEN** the new monitor records the chain and passes every registration check
  as if unrelated

#### Scenario: Consumed pointer remains under reconciliation
- **WHEN** a new registration names a `queue_accepted` thread monitor whose pointer
  was delivered and inspected while history reconciliation is still incomplete
- **THEN** the new monitor may record that lineage while the prior monitor remains
  `queue_accepted`, makes no task-success claim, and receives no second queue-add

#### Scenario: Lineage reference is invalid
- **WHEN** a lineage reference names an unknown monitor or one whose trigger or
  selected delivery attempt could still occur
- **THEN** the registration is rejected without side effects

## ADDED Requirements

### Requirement: Diagnose bounded registration failures

When thread-monitor registration fails at a daemon-readiness or app-server stage,
the system SHALL identify the failed stage. A delivery-daemon readiness failure
SHALL additionally report one bounded current reason that distinguishes missing or
malformed heartbeat, retiring owner, stale heartbeat, dead process, capability
epoch mismatch, loaded-source mismatch, and lock-owner mismatch when observable.

Diagnostics MUST remain read-only, disclose no credentials or full transcript,
and MUST NOT lengthen the readiness or app-server timeout, retry an uncertain
request, choose a fallback delivery path, or claim a historical root cause that
current evidence does not establish.

#### Scenario: Delivery daemon cannot become ready
- **WHEN** bounded readiness ends without an exact delivery-capable lock owner
- **THEN** registration rejects before arming with stage `delivery_daemon_readiness`
  and the current bounded reason

#### Scenario: Target resolution times out
- **WHEN** the app-server times out while resolving the origin and delivery target
- **THEN** registration reports stage `resolve_delivery_target` and the exact
  app-server method failure without increasing or retrying the request

#### Scenario: Condition preparation cannot read a child
- **WHEN** target resolution succeeded but a thread-backed condition observation
  fails before monitor creation
- **THEN** registration reports stage `prepare_condition` and creates no monitor row

### Requirement: Bind native worker settlement to one host invocation

A native-worker condition SHALL accept a public canonical task name only when the
local Codex host can resolve it under the trusted caller root to one exact current
invocation identity and report that invocation's native lifecycle status. The
frozen invocation identity, not the reusable task name alone, SHALL govern every
later observation. Existing `any`/`all` composition and root queue delivery SHALL
remain the only composition and wake path.

Core `interrupted`, `completed`, and `failed` persisted-turn statuses
SHALL settle the condition with distinct outcome evidence; each is settlement only
and MUST NOT claim task success or lead acceptance. `bindPending` SHALL reject before
arming because no exact invocation exists yet, while `running` remains nonterminal.
Invocation mismatch, disappearance without retained terminal status, or an
unavailable host binding SHALL fail closed and MUST NOT be approximated from generic
thread idleness, unloading, a cooperative publisher, or a completion message.

#### Scenario: Current Core has no native binding surface
- **WHEN** the plugin cannot resolve a canonical task name to exact native lifecycle
  status through a host-owned public interface
- **THEN** native-worker registration remains unavailable and creates no monitor
  rather than advertising approximate support

#### Scenario: Exact native invocation settles
- **WHEN** the host reports `interrupted`, `completed`, or `failed` for the exact
  invocation frozen at registration
- **THEN** the condition settles once with that distinct outcome and may feed the
  existing composed monitor without asserting task success or lead acceptance

#### Scenario: Task name is reused
- **WHEN** the same canonical task name resolves to a different invocation after
  registration
- **THEN** the condition fails closed on the identity mismatch and never observes
  the replacement as the original worker

### Requirement: Recover transient claimed-delivery preflight

Before queue admission, the system SHALL distinguish a typed app-server transport
failure from a conclusive rejection, unsupported capability, wrong runtime root,
unsafe permission, invalid stored envelope, or malformed protocol response. A
transport failure during a claimed monitor's read-only delivery preflight SHALL
preserve `claimed`, `unattempted`, a null `admission_attempted_at`, and a null queue
receipt. The existing daemon cadence SHALL retry only that read-only preflight.

Condition expiry SHALL remain the observation deadline and MAY produce the one
claim; it SHALL NOT become a post-claim delivery deadline. A claimed preflight MAY
therefore remain pending until transport recovers or the user cancels it. A
conclusive preflight failure SHALL remain terminal. Once admission is durably begun,
the system MUST NOT retry queue-add after any transport-uncertain response and SHALL
use existing reconciliation instead.

#### Scenario: Read timeout recovers before admission
- **WHEN** a claimed monitor receives one or more typed transport failures during
  read-only delivery preflight and a later daemon pass receives a valid capability
  response
- **THEN** it begins admission once, queues the original pointer exactly once, and
  retains no pre-timeout admission timestamp or queue receipt

#### Scenario: Claimed preflight remains pending past trigger expiry
- **WHEN** read-only preflight transport remains unavailable after the monitor's
  condition expiry timestamp
- **THEN** the already claimed pointer remains pending on the existing daemon
  cadence because expiry is not a delivery deadline

#### Scenario: User cancels before transport recovers
- **WHEN** the user cancels a claimed monitor while read-only preflight is pending
- **THEN** it becomes cancelled and no later daemon pass attempts queue-add

#### Scenario: Preflight is conclusively denied
- **WHEN** read-only preflight receives a server rejection, wrong-root or permission
  failure, unsupported capability, or malformed protocol response
- **THEN** delivery becomes terminally unavailable before queue admission

#### Scenario: Queue-add response times out
- **WHEN** transport becomes uncertain only after `admission_attempted_at` is set
- **THEN** the system reconciles the consumed admission and never issues a second
  queue-add

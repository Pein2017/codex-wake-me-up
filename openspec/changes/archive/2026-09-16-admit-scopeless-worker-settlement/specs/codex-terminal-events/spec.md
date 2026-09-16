## MODIFIED Requirements

### Requirement: Reserve a terminal event before producer launch

The system SHALL let an operator reserve one typed terminal event before the
corresponding command or worker starts. A reservation MUST name exactly one
event kind (`command_terminal` or `worker_terminal`), have a bounded expiry,
carry an optional idempotency key, and return an opaque publish token. Creating
a reservation MUST NOT pause, activate, or otherwise modify any Codex goal.

A worker reservation MUST identify one producer task. It MAY additionally
declare delivery scope, but repository, canonical worktree, baseline commit,
and allowed repository-relative path prefixes MUST then be present together.
Omitting the complete delivery scope declares a settlement-only producer that
cannot publish a delivered candidate.

#### Scenario: Command reservation is created before launch
- **WHEN** an operator reserves a `command_terminal` event with a valid expiry
- **THEN** the system returns a durable reservation ID and publish token without
  creating a monitor or changing a goal

#### Scenario: Worker reservation captures delivery scope
- **WHEN** an operator reserves a scoped `worker_terminal` event
- **THEN** the request identifies the producer task, canonical repository and
  worktree, baseline commit, and allowed repository-relative path prefixes

#### Scenario: Worker reservation omits delivery scope
- **WHEN** an operator reserves a `worker_terminal` event with a producer task
  and none of the delivery-scope fields
- **THEN** the system records a settlement-only reservation without inventing a
  repository, worktree, baseline, or allowed path set

#### Scenario: Worker reservation partially declares delivery scope
- **WHEN** a worker reservation supplies only some delivery-scope fields
- **THEN** reservation is rejected without issuing a publish capability

#### Scenario: Idempotent reservation is repeated
- **WHEN** the same idempotency key is reused with semantically identical
  reservation input
- **THEN** the system returns the original reservation ID and token fingerprint
  but does not reconstruct or reissue the raw publish token

#### Scenario: Initial publish capability is lost
- **WHEN** the caller loses the one creation response containing the raw publish
  token
- **THEN** the caller must cancel or allow expiry and allocate a new reservation;
  an idempotent retry cannot recover the bearer capability

#### Scenario: Idempotency semantics change
- **WHEN** an existing idempotency key is reused with a different event kind,
  producer identity, expiry, delivery scope, or path scope
- **THEN** the request is rejected and the original reservation remains
  unchanged

### Requirement: Publish a worker-terminal event with an optional delivery

The holder of a worker reservation's publish token SHALL be able to publish
exactly one bounded worker-terminal event with outcome `delivered`, `completed`,
`blocked`, `failed`, `cancelled`, or `settlement_uncertain`. A `delivered` event
MUST name exactly one candidate Git commit and MUST be rejected unless the
reservation captured complete delivery scope. A `completed` event MUST NOT name
a candidate commit and represents only that the producer turn ended normally.
Blocked, failed, cancelled, and uncertain outcomes MUST carry a bounded reason
and MUST NOT claim a candidate commit. `settlement_uncertain` MUST preserve the
producer's bounded failure classification showing why a reliable result could
not be established. Every worker-terminal event is handling evidence, not task
success or lead acceptance.

#### Scenario: Worker delivers a candidate commit
- **WHEN** the named worker publishes `delivered` with one full commit object ID
  against a scoped reservation
- **THEN** the event records that commit as a candidate review target and starts
  Git delivery attestation

#### Scenario: Scopeless worker completes normally
- **WHEN** the named worker publishes `completed` without a candidate commit
- **THEN** the event becomes terminal with explicit `task_success=false` and
  `lead_accepted=false` and no Git attestation is attempted

#### Scenario: Scoped worker completes without a delivery
- **WHEN** a scoped worker publishes `completed` without a candidate commit
- **THEN** the event becomes terminal without inventing a delivery or running
  Git attestation

#### Scenario: Worker stops without a delivery
- **WHEN** the worker publishes `blocked`, `failed`, or `cancelled` with a
  bounded reason
- **THEN** the event becomes terminal without inventing a commit or acceptance
  result

#### Scenario: Worker settlement cannot be validated
- **WHEN** the producer adapter reaches a terminal result whose validity or
  settlement cannot be established
- **THEN** it may publish `settlement_uncertain` with bounded classification and
  explicit `task_success=false` and `lead_accepted=false`

#### Scenario: Delivered outcome omits its commit
- **WHEN** a worker publishes `delivered` without exactly one full commit object
  ID
- **THEN** publication is rejected and no terminal event is recorded

#### Scenario: Scopeless worker claims a delivery
- **WHEN** a worker publishes `delivered` against a reservation without delivery
  scope
- **THEN** publication is rejected and no terminal event or attestation is
  recorded

#### Scenario: Completed worker claims a candidate
- **WHEN** a worker publishes `completed` with a candidate commit
- **THEN** publication is rejected and the reservation remains unpublished

## ADDED Requirements

### Requirement: Preflight a private worker publisher descriptor

The command-line interface SHALL let an existing launcher validate one
current-user-owned mode-0600 worker publisher descriptor against the selected
runtime root and an expected producer task before model work begins. Validation
MUST verify descriptor schema and kind, reservation identity, bearer-token
fingerprint, live reservation lifecycle, and exact producer identity without
publishing, binding, or exposing the bearer.

#### Scenario: Launcher validates its reserved worker event
- **WHEN** a launcher supplies a valid descriptor and the exact producer task
  frozen in the live reservation
- **THEN** the CLI returns a redacted compatible receipt without changing the
  reservation or monitor state

#### Scenario: Descriptor belongs to another producer
- **WHEN** the expected producer task differs from the reservation's producer
  identity
- **THEN** preflight fails without exposing the actual bearer or allowing model
  work to start under the wrong event

#### Scenario: Descriptor bearer is stale or incorrect
- **WHEN** descriptor schema and reservation ID are valid but its bearer does
  not match the frozen publish-token fingerprint
- **THEN** preflight fails without consuming or changing the reservation

### Requirement: Publish worker settlement through a private descriptor

The command-line interface SHALL let an existing producer publish one worker
terminal envelope using a current-user-owned mode-0600 publisher descriptor and
a current-user-owned mode-0600 event payload. The bearer token MUST be read from
the descriptor and MUST NOT appear in command arguments, output, diagnostics,
or retained logs. Descriptor and payload validation MUST occur before terminal
state changes.

#### Scenario: Existing producer publishes completed settlement
- **WHEN** a producer supplies a valid worker descriptor and a matching
  `completed` event payload
- **THEN** the CLI publishes through the descriptor's frozen reservation and
  returns only redacted terminal status

#### Scenario: Descriptor or event identity differs
- **WHEN** descriptor kind, producer identity, reservation identity, or event
  kind does not match the frozen reservation
- **THEN** publication fails closed without changing the reservation

#### Scenario: Descriptor publication is retried identically
- **WHEN** an already accepted terminal envelope is published again through the
  same descriptor with identical semantics
- **THEN** the operation returns the original immutable event idempotently

#### Scenario: Private input is unsafe
- **WHEN** either input is not a current-user-owned regular mode-0600 file or
  exceeds the bounded payload size
- **THEN** the CLI rejects it without exposing or consuming the bearer token

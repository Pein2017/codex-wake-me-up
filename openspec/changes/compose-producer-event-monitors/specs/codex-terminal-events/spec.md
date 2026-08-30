## MODIFIED Requirements

### Requirement: Bind one reservation to one monitor condition

The system SHALL bind each live reservation to exactly one compatible terminal
condition leaf. Binding MUST validate every reservation kind and MUST be durable
before the monitor can be armed. A bound reservation MUST NOT be rebound to a
second monitor, including after cancellation or terminal completion of the
first monitor.

One monitor MAY reference one or several distinct reservations through the
existing typed `all`/`any` AST. For each referenced reservation it MUST contain
exactly one terminal leaf matching that reservation kind and MAY contain one or
more `heartbeat_stale` leaves naming the same reservation. The monitor insert or
idempotent reuse and the complete reservation set MUST bind in one atomic
transaction. Duplicate terminal leaves, a heartbeat without its matching
terminal leaf, or any missing, extra, expired, incompatible, already-bound, or
otherwise invalid member MUST reject and roll back the entire registration.

An `any` condition permanently consumes every reservation it binds, including
branches that did not win the trigger. Cancellation, expiry, or terminal
completion MUST NOT make any member reusable.

#### Scenario: Compatible reservation is bound
- **WHEN** a monitor registers a terminal leaf naming an unexpired, unbound
  compatible reservation
- **THEN** the reservation is durably bound to that monitor and cannot be used
  by another monitor

#### Scenario: Several reservations bind atomically
- **WHEN** a monitor registers a valid condition naming several unexpired,
  unbound compatible reservations
- **THEN** the monitor and complete binding set commit together before the
  monitor becomes armed

#### Scenario: One member of a set cannot bind
- **WHEN** any reservation in a proposed multi-reservation condition is expired,
  incompatible, missing, or already bound
- **THEN** registration leaves no new monitor and binds none of the other
  reservations

#### Scenario: Producer finishes before binding
- **WHEN** valid terminal events were published while reservations were still
  unbound and a compatible monitor's binding transaction commits before every
  reservation deadline
- **THEN** the bound condition observes the stored terminal events without
  losing or duplicating them

#### Scenario: Bind races a reservation deadline
- **WHEN** a multi-reservation binding and any member's expiry contend
- **THEN** the first committed transaction determines the result: a committed
  complete binding remains valid, while a committed expiry leaves no partial
  monitor or partial binding

#### Scenario: Condition duplicates a terminal leaf
- **WHEN** one monitor contains more than one terminal leaf for the same
  reservation or a heartbeat leaf with no matching terminal leaf
- **THEN** registration is rejected without inserting the monitor or binding
  any reservation

#### Scenario: Reservation kind does not match condition
- **WHEN** a `worker_terminal` condition names a command reservation, or a
  `command_terminal` condition names a worker reservation
- **THEN** binding is rejected without changing any reservation or monitor

#### Scenario: Losing any branch is reused
- **WHEN** a second monitor attempts to bind a reservation consumed by a prior
  `any` condition but not selected as its witness
- **THEN** the request is rejected because the reservation remains permanently
  bound to the first monitor

### Requirement: Publish a worker-terminal event with an optional delivery

The holder of a worker reservation's publish token SHALL be able to publish
exactly one bounded worker-terminal event with outcome `delivered`, `blocked`,
`failed`, `cancelled`, or `settlement_uncertain`. A `delivered` event MUST name
exactly one candidate Git commit. Other outcomes MUST carry a bounded reason and
MUST NOT claim a candidate commit. `settlement_uncertain` MUST preserve the
producer's bounded failure classification showing why a reliable result could
not be established. A worker-terminal event is evidence that the named producer
ended or became terminally uncertain and declared an outcome; it is not task
success or lead acceptance.

#### Scenario: Worker delivers a candidate commit
- **WHEN** the named worker publishes `delivered` with one full commit object ID
- **THEN** the event records that commit as a candidate review target and starts
  Git delivery attestation

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

## ADDED Requirements

### Requirement: Return a monitor-ready condition projection

Terminal reservation creation SHALL return a compact non-secret
`monitor_condition` containing exactly the compatible terminal leaf type and
reservation ID. Idempotent reservation inspection or replay MAY return the same
projection without reconstructing the publish token. The projection MUST NOT
contain a publisher descriptor path, publish token, repository credential,
producer text, or any authority to target another Codex task.

The projection is a convenience for an existing launcher and lead; it MUST NOT
launch a producer, bind or arm a monitor, authorize a billed wake, or create a
second event transport.

#### Scenario: Command reservation returns its condition
- **WHEN** a `command_terminal` reservation is created
- **THEN** the response includes
  `{"type":"command_terminal","reservation_id":"..."}` as its
  `monitor_condition`

#### Scenario: Worker reservation is replayed idempotently
- **WHEN** a worker reservation is inspected or replayed after its one raw
  publish token response
- **THEN** the same non-secret worker condition remains available without the
  bearer capability

#### Scenario: Launcher consumes a condition projection
- **WHEN** an external launcher accepts the private publisher descriptor and
  returns the supplied condition to its caller
- **THEN** this plugin can bind that condition without reading the launcher's
  private runtime state

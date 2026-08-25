## ADDED Requirements

### Requirement: Observe a reserved command-terminal event

The system SHALL support a `command_terminal` condition leaf naming one bound
command reservation. The leaf SHALL remain false until the reservation has an
immutable command-terminal event, then become true for every terminal status,
including failure, cancellation, and signal termination. The witness SHALL
carry bounded command identity, status, exit or signal evidence, timing, and
declared log or artifact paths. It MUST NOT classify command termination as
whole-task success.

#### Scenario: Command succeeds
- **WHEN** the bound reservation publishes a valid `succeeded` terminal event
- **THEN** the leaf becomes true with exit code and bounded execution evidence
  in its witness

#### Scenario: Command fails
- **WHEN** the bound reservation publishes a valid `failed`, `cancelled`, or
  `signaled` terminal event
- **THEN** the leaf becomes true immediately with the failure evidence and does
  not wait for monitor expiry

### Requirement: Observe a reserved worker-terminal event

The system SHALL support a `worker_terminal` condition leaf naming one bound
worker reservation. The leaf SHALL become true for `delivered`, `blocked`,
`failed`, and `cancelled` outcomes. Its witness SHALL carry the producer
identity, declared outcome, and, for a delivery, the candidate commit and
bounded Git attestation. Worker termination and delivery SHALL remain candidate
evidence and MUST NOT be represented as lead acceptance.

#### Scenario: Worker delivers a valid candidate
- **WHEN** the bound worker event is `delivered` and Git attestation is valid
- **THEN** the leaf becomes true with the candidate commit and changed-path
  evidence for lead review

#### Scenario: Worker delivery is invalid
- **WHEN** the worker event is terminal but Git attestation reports a missing,
  baseline-mismatched, or out-of-scope candidate
- **THEN** the leaf still becomes true with an invalid-delivery witness so the
  lead can handle it instead of remaining asleep

#### Scenario: Worker ends without a commit
- **WHEN** the bound worker publishes `blocked`, `failed`, or `cancelled`
- **THEN** the leaf becomes true with its bounded reason and no invented review
  target

### Requirement: Observe a stale producer heartbeat

The system SHALL support a `heartbeat_stale` condition leaf naming one bound
terminal-event reservation and a positive staleness duration. The leaf SHALL be
false while accepted producer heartbeats advance within the duration, true when
the host-observed heartbeat age reaches the duration before terminal
publication, and false once a terminal event exists. It SHALL be classified as
heuristic liveness evidence and MUST NOT claim the producer failed or stopped.

#### Scenario: Producer stops advancing its heartbeat
- **WHEN** no accepted heartbeat advances for the configured duration and no
  terminal event exists
- **THEN** the leaf becomes true with last-sequence and host-observed age
  evidence

#### Scenario: Producer publishes terminal event before staleness
- **WHEN** the reservation has an immutable terminal event
- **THEN** the heartbeat-stale leaf does not independently satisfy the monitor

#### Scenario: Heartbeat evidence is unavailable
- **WHEN** the reservation never received the heartbeat required by its monitor
  contract or its identity cannot be validated
- **THEN** the leaf is `unknown`, not evidence of a stall

### Requirement: Linearize event evaluation and the trigger claim

For a monitor containing terminal-event leaves, the system SHALL reload the
still-armed monitor and its bound reservation, evaluate transaction-current
event/heartbeat facts with a post-lock host time, persist condition/evidence/
witness/evaluation count, and optionally claim the trigger in one short SQLite
transaction. Host, app-server, filesystem, and Git I/O MUST remain outside that
transaction. The first committed terminal publication or expiry/claim SHALL
determine the one durable wake reason and later contenders MUST NOT create a
second claim or activation.

#### Scenario: Terminal publication wins against heartbeat staleness
- **WHEN** a terminal publication commits before a competing heartbeat-stale
  evaluation obtains the claim transaction
- **THEN** the transaction observes the terminal event and can claim only the
  terminal condition, not the no-longer-true stale heartbeat

#### Scenario: Terminal publication races monitor expiry
- **WHEN** terminal publication and an expiry claim contend
- **THEN** the first committed transaction determines the single wake: a stored
  terminal event wins as `condition`, while an already committed expiry claim
  rejects later producer publication

#### Scenario: Bound reservation row is logically corrupt or missing
- **WHEN** an event leaf cannot resolve a logically valid bound reservation
- **THEN** evaluation returns fatal `unknown`; a confirmed deferred monitor
  performs its one `observer_failed` guarded wake, while a legacy monitor
  terminates `observer_failed` without activation

### Requirement: Verify daemon event capability before arming or pausing

The daemon heartbeat/readiness contract SHALL advertise a condition/event
schema capability epoch and loaded source identity. Registering an event monitor
MUST fail before arming, and deferring one MUST fail before pausing, unless the
lock-owning healthy daemon advertises the exact required event capability. A
legacy PID/timestamp-only heartbeat MUST NOT be treated as event-capable.

#### Scenario: Legacy daemon is still healthy
- **WHEN** an event registration or defer observes a fresh legacy daemon
  heartbeat without the required capability epoch
- **THEN** the operation fails closed before monitor arm or goal pause and no
  activation is attempted

#### Scenario: Operator preflights a daemon downgrade
- **WHEN** the current event-capable binary runs its compatibility check with
  the target older epoch while a nonterminal monitor contains an event leaf,
  including when its bound reservation row is missing or corrupt
- **THEN** the check refuses the downgrade before source/cache replacement;
  an already-restored older binary is not claimed to contain this newer guard,
  while completed terminal event history alone does not block rollback

## MODIFIED Requirements

### Requirement: Require explicit authorization for heuristic continuation

The system MUST decide continuation authorization from the actual true-leaf
witness that satisfied the condition at trigger claim time. For a non-deferred
monitor it MUST NOT activate a goal from a time, GPU, PID, tmux, log-pattern,
thread-idle, or heartbeat-stale witness unless the registration explicitly
authorizes heuristic/liveness-based continuation. For a deferred monitor with
a confirmed pause, an unauthorized heuristic witness SHALL follow the deferred
terminal-wake policy: the wake proceeds through the guarded path labeled
`unauthorized_evidence` and MUST NOT be presented as an authorized or successful
outcome. A witness containing a successful `receipt_success` leaf MAY authorize
continuation as task-success evidence without the heuristic opt-in. A true
`command_terminal` or `worker_terminal` leaf MAY authorize continuation without
the heuristic opt-in because the operator explicitly reserved and bound that
event kind; its witness MUST remain terminal or delivery-candidate evidence and
MUST NOT be presented as task success, lead acceptance, or user acceptance. The
system MUST retain the satisfying witness evidence in the terminal receipt.

The trusted witness classification SHALL distinguish command termination,
worker terminal failure, valid delivery candidate, and invalid delivery. Only a
successful `receipt_success` witness may use the `task_success` classification;
all terminal-event witnesses SHALL carry `task_success: false` and
`lead_accepted: false`.

#### Scenario: Heuristic witness lacks opt-in
- **WHEN** a non-deferred monitor's condition is satisfied by heuristic
  evidence, its witness contains no successful receipt or bound terminal event,
  and the registration does not authorize that evidence for activation
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

#### Scenario: Failed command terminal event authorizes handling
- **WHEN** a bound `command_terminal` leaf becomes true with a failed command
  outcome
- **THEN** the monitor may perform its one guarded continuation without the
  heuristic opt-in and the wake report makes no task-success claim

#### Scenario: Worker delivery authorizes lead review
- **WHEN** a bound `worker_terminal` leaf becomes true with a delivered
  candidate
- **THEN** the monitor may perform its one guarded continuation for lead review
  while keeping lead acceptance unset

#### Scenario: Stale heartbeat remains heuristic
- **WHEN** a `heartbeat_stale` leaf is the only true witness and heuristic
  continuation was not authorized
- **THEN** it follows the same non-deferred or deferred unauthorized-evidence
  policy as other heuristic leaves

### Requirement: Deliver a self-describing wake report

For every fired monitor the terminal receipt SHALL include the wake reason, the
satisfying witness or failure detail, the bounded matched-line journal tail when
a `log_pattern` leaf exists, arming and firing timestamps, and the evaluation
count. For terminal-event witnesses it SHALL additionally include the
reservation and producer identity, event kind, terminal status, bounded command
evidence or worker outcome, last heartbeat facts, and any Git delivery
attestation. The status operation SHALL return this report so one call fully
re-orients a woken agent. The report MUST distinguish task success, command
termination, worker delivery candidate, invalid delivery, and heuristic stall;
it MUST NOT infer lead acceptance. The system MUST NOT mutate the goal objective
or any user-owned text to carry wake context.

#### Scenario: Woken agent reads its context in one call
- **WHEN** an agent's goal returns to active after its monitor fired and the
  agent calls the status operation once with the monitor ID
- **THEN** the response contains the wake reason, witness, journal tail, and
  wait statistics without requiring further discovery calls

#### Scenario: Lead wakes for worker delivery
- **WHEN** a worker-terminal witness caused the guarded continuation
- **THEN** the same status response contains producer identity, candidate
  commit, changed-path evidence, attestation status, and an explicit
  not-accepted marker

#### Scenario: Lead wakes for command failure
- **WHEN** a command-terminal witness reports a failed or signaled command
- **THEN** the same status response contains the bounded exit evidence and
  declared logs or artifacts without claiming task success

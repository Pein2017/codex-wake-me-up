# codex-terminal-events Specification

## Purpose

Provide a host-local, durable producer-to-lead event contract for commands and
delegated workers so terminal outcomes can wake one guarded Codex goal without
turning process exit, Git activity, or worker self-report into task acceptance.

## Requirements

### Requirement: Reserve a terminal event before producer launch

The system SHALL let an operator reserve one typed terminal event before the
corresponding command or worker starts. A reservation MUST name exactly one
event kind (`command_terminal` or `worker_terminal`), have a bounded expiry,
carry an optional idempotency key, and return an opaque publish token. Creating
a reservation MUST NOT pause, activate, or otherwise modify any Codex goal.

#### Scenario: Command reservation is created before launch
- **WHEN** an operator reserves a `command_terminal` event with a valid expiry
- **THEN** the system returns a durable reservation ID and publish token without
  creating a monitor or changing a goal

#### Scenario: Worker reservation captures delivery scope
- **WHEN** an operator reserves a `worker_terminal` event
- **THEN** the request identifies the producer task, canonical repository and
  worktree, baseline commit, and allowed repository-relative path prefixes

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
  producer identity, expiry, repository scope, or path scope
- **THEN** the request is rejected and the original reservation remains
  unchanged

### Requirement: Bind one reservation to one monitor condition

The system SHALL bind a live reservation to exactly one compatible terminal
condition leaf. Binding MUST validate the reservation kind and MUST be durable
before the monitor can be armed. A bound reservation MUST NOT be rebound to a
second monitor, including after cancellation or terminal completion of the
first monitor.

One monitor MAY reference at most one distinct reservation. It MUST contain
exactly one terminal leaf matching that reservation kind and MAY contain one or
more `heartbeat_stale` leaves naming the same reservation. Binding the monitor
insert or idempotent reuse and that reservation MUST be one atomic transaction;
conditions naming multiple distinct reservations or duplicate terminal leaves
MUST be rejected.

#### Scenario: Compatible reservation is bound
- **WHEN** a monitor registers a `command_terminal` leaf naming an unexpired,
  unbound command reservation
- **THEN** the reservation is durably bound to that monitor and cannot be used
  by another monitor

#### Scenario: Producer finishes before binding
- **WHEN** a valid terminal event was published while its reservation was still
  unbound and a compatible monitor's binding transaction commits before the
  reservation deadline
- **THEN** the bound condition observes the stored terminal event without
  losing or duplicating it

#### Scenario: Bind races the reservation deadline
- **WHEN** binding and reservation expiry contend
- **THEN** the first committed transaction determines the result: a committed
  binding remains valid, while a committed expiry leaves no orphan monitor and
  cannot later bind

#### Scenario: Condition names more than one reservation
- **WHEN** one monitor condition names multiple distinct event reservations or
  contains duplicate terminal leaves
- **THEN** registration is rejected without inserting the monitor or binding
  any reservation

#### Scenario: Reservation kind does not match condition
- **WHEN** a `worker_terminal` condition names a command reservation, or a
  `command_terminal` condition names a worker reservation
- **THEN** binding is rejected without changing the reservation or monitor

#### Scenario: Reservation reuse is attempted
- **WHEN** any second monitor attempts to bind a reservation that was already
  bound
- **THEN** the request is rejected even if the first monitor is cancelled,
  expired, or terminal

### Requirement: Expire and cancel reservations safely

An unbound reservation SHALL become unusable at its expiry. The system SHALL
provide a cancellation operation for an unbound reservation, and cancellation
MUST prevent later heartbeat, terminal publication, or monitor binding. Expiry
or cancellation of a reservation MUST NOT change any goal. The reservation
expiry is a pre-bind deadline: once binding commits, the expiry no longer
invalidates producer writes by time alone. After binding, heartbeat or terminal
publication is accepted only while the monitor has not durably claimed, failed,
cancelled, expired, or begun activation. A bound reservation is never reusable.

#### Scenario: Unbound reservation expires
- **WHEN** the reservation expiry passes before monitor binding
- **THEN** later binding and publication requests are rejected and no goal is
  changed

#### Scenario: Operator cancels an unused reservation
- **WHEN** the operator cancels a live unbound reservation
- **THEN** the reservation becomes terminal-cancelled and its token can no
  longer publish or bind an event

#### Scenario: Bound monitor ends before producer publication
- **WHEN** a bound monitor durably claims, fails, is cancelled, expires, or
  begins activation before a producer publishes its terminal event
- **THEN** later heartbeat and terminal publication are rejected while the
  reservation remains permanently bound for inspection

### Requirement: Publish bounded producer heartbeats

The system SHALL let the holder of a valid publish token record bounded,
monotonically sequenced heartbeats before terminal publication. It MUST record
the host-observed receive time, retain only bounded heartbeat evidence, and
reject token mismatch, non-increasing sequence numbers, or any heartbeat after
a terminal event. Heartbeats SHALL be liveness evidence only.

#### Scenario: Producer heartbeat advances
- **WHEN** a valid producer publishes a heartbeat with a sequence greater than
  the reservation's last accepted sequence
- **THEN** the system durably records the new sequence and host-observed time

#### Scenario: Duplicate heartbeat is retried
- **WHEN** the producer repeats the most recently accepted heartbeat with
  identical semantics
- **THEN** the operation is idempotent and does not append duplicate evidence

#### Scenario: Heartbeat arrives after terminal publication
- **WHEN** a heartbeat is submitted after the reservation has a terminal event
- **THEN** the heartbeat is rejected and the terminal event remains unchanged

### Requirement: Publish exactly one command-terminal event

The holder of a command reservation's publish token SHALL be able to publish
exactly one bounded command-terminal event. The event MUST identify a safe
operator label and command digest rather than requiring raw command arguments,
and MUST report one terminal status: `succeeded`, `failed`, `cancelled`, or
`signaled`. A succeeded or failed status MUST carry an exit code; a signaled
status MUST carry a signal identity. The system SHALL stamp its own receive
time and MAY retain bounded local log and artifact paths supplied by the
producer.

#### Scenario: Successful command publishes terminal evidence
- **WHEN** a valid producer publishes `succeeded` with exit code zero and the
  required command identity
- **THEN** the reservation records one immutable command-terminal event

#### Scenario: Failed command publishes terminal evidence
- **WHEN** a valid producer publishes `failed` with a nonzero exit code
- **THEN** the same terminal event path becomes observable immediately without
  waiting for expiry or pretending the command succeeded

#### Scenario: Terminal retry is identical
- **WHEN** the producer retries the same terminal publication with identical
  semantics
- **THEN** the operation returns the original event idempotently

#### Scenario: Terminal event is rewritten
- **WHEN** a producer attempts to change the status, identity, exit evidence,
  paths, or timestamps after terminal publication
- **THEN** the request is rejected and the original terminal event remains
  immutable

### Requirement: Publish a worker-terminal event with an optional delivery

The holder of a worker reservation's publish token SHALL be able to publish
exactly one bounded worker-terminal event with outcome `delivered`, `blocked`,
`failed`, or `cancelled`. A `delivered` event MUST name exactly one candidate
Git commit. Other outcomes MUST carry a bounded reason and MUST NOT claim a
candidate commit. A worker-terminal event is evidence that the named producer
ended and declared an outcome; it is not lead acceptance.

#### Scenario: Worker delivers a candidate commit
- **WHEN** the named worker publishes `delivered` with one full commit object ID
- **THEN** the event records that commit as a candidate review target and starts
  Git delivery attestation

#### Scenario: Worker stops without a delivery
- **WHEN** the worker publishes `blocked`, `failed`, or `cancelled` with a
  bounded reason
- **THEN** the event becomes terminal without inventing a commit or acceptance
  result

#### Scenario: Delivered outcome omits its commit
- **WHEN** a worker publishes `delivered` without exactly one full commit object
  ID
- **THEN** publication is rejected and no terminal event is recorded

### Requirement: Attest Git delivery read-only against the frozen scope

For a delivered worker event, the system SHALL perform a bounded, read-only Git
attestation against the repository, worktree, baseline commit, and allowed path
prefixes frozen in the reservation. It SHALL report whether the candidate is a
commit in the captured repository, descends from the baseline, and changes only
allowed paths. It MUST NOT update a ref, index, worktree, branch, merge state, or
remote. An invalid attestation SHALL remain an actionable terminal worker event
that wakes the lead with an invalid-delivery report; it MUST NOT be converted
into acceptance or silently stranded.

The adapter MUST scrub inherited Git repository/config/object-alternate/index/
namespace environment, disable replacement objects, use exact-or-slash-
descendant path-prefix matching, and revalidate the captured worktree/common-dir
identity. It MUST fully consume the NUL-delimited path set within a 5-second and
8-MiB scan ceiling before reporting `valid`, complete count, or complete digest.
Timeout, ceiling exhaustion, malformed output, or an identity change MUST report
`attestation_error`; a partial scan MUST NOT report `valid`.

#### Scenario: Candidate commit matches frozen delivery scope
- **WHEN** the candidate exists in the captured repository, has the baseline as
  an ancestor, and changes only allowed paths
- **THEN** the event reports a valid candidate plus bounded changed-path and
  commit metadata for lead review

#### Scenario: Candidate commit is missing or from another repository
- **WHEN** the candidate cannot be resolved as a commit in the captured
  repository
- **THEN** attestation reports `invalid_commit`, the lead is still eligible to
  wake, and no Git state is changed

#### Scenario: Candidate is not based on the delegated baseline
- **WHEN** the candidate commit does not descend from the captured baseline
- **THEN** attestation reports `baseline_mismatch` and does not rewrite or
  compensate any branch

#### Scenario: Candidate changes an out-of-scope path
- **WHEN** the baseline-to-candidate diff contains a path outside every frozen
  allowed prefix
- **THEN** attestation reports `out_of_scope` with bounded path evidence and
  leaves the candidate unaccepted

#### Scenario: Git path scan exceeds its ceiling
- **WHEN** the complete baseline-to-candidate path stream cannot be consumed
  within the declared time or byte ceiling
- **THEN** attestation reports `attestation_error` without a partial valid
  result, complete-set digest, or Git mutation

#### Scenario: Identical terminal publication is retried
- **WHEN** an identical producer envelope is retried after its terminal event
  and attestation were stored
- **THEN** the system returns the original immutable event and attestation
  without rerunning Git or comparing a newly derived attestation

### Requirement: Keep terminal publication separate from acceptance

Command-terminal and worker-terminal events SHALL authorize only the requested
guarded wake. A command exit code, worker declaration, valid Git attestation,
or worker-created commit MUST NOT be labeled task success, lead acceptance, or
user acceptance unless separate evidence establishes that decision. The system
MUST NOT automatically review, merge, cherry-pick, revert, stage, commit, or
push in response to any terminal event.

#### Scenario: Valid worker commit wakes the lead
- **WHEN** a valid delivered worker event satisfies its bound monitor
- **THEN** the wake report names a candidate review target and requires the lead
  to perform independent acceptance before any integration

#### Scenario: Command exits successfully
- **WHEN** a `command_terminal` event reports exit code zero
- **THEN** the event proves only the bounded command outcome and is not promoted
  to whole-task success

### Requirement: Persist and inspect terminal-event lifecycle

The system SHALL durably retain reservation identity, binding, bounded
heartbeats, immutable terminal publication, Git attestation, expiry,
cancellation, and idempotency outcomes under the existing host-local runtime
root. Status inspection MUST expose these facts without exposing the publish
token. Recovery after daemon restart MUST preserve single binding and single
terminal publication.

Terminal-event presence, permanent `bound_monitor_id`, cancellation, and
pre-bind expiry SHALL be persisted as orthogonal facts. The public lifecycle is
derived deterministically from those facts rather than requiring one state
value to erase a prepublished terminal event when it later binds or expires.

#### Scenario: Daemon restarts after terminal publication
- **WHEN** the daemon restarts after an event was durably published but before
  its monitor consumed it
- **THEN** recovery exposes the same terminal event to the same bound monitor
  without accepting a second publication

#### Scenario: Operator inspects a reservation
- **WHEN** an operator requests reservation status
- **THEN** the response includes kind, lifecycle, binding, last heartbeat,
  terminal outcome, and bounded attestation while omitting the publish token

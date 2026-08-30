## MODIFIED Requirements

### Requirement: Observe a reserved worker-terminal event

The system SHALL support a `worker_terminal` condition leaf naming one bound
worker reservation. The leaf SHALL become true for `delivered`, `blocked`,
`failed`, `cancelled`, and `settlement_uncertain` outcomes. Its witness SHALL
carry the producer identity, declared outcome, and, for a delivery, the
candidate commit and bounded Git attestation. Worker termination, uncertain
settlement, and delivery SHALL remain candidate or failure evidence and MUST
NOT be represented as lead acceptance or task success.

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
- **WHEN** the bound worker publishes `blocked`, `failed`, `cancelled`, or
  `settlement_uncertain`
- **THEN** the leaf becomes true with its bounded reason and no invented review
  target, task success, or lead acceptance

### Requirement: Linearize event evaluation and the trigger claim

For a monitor containing terminal-event leaves, the system SHALL reload the
still-armed monitor and the complete set of reservations named by its condition,
evaluate transaction-current event/heartbeat facts with a post-lock host time,
persist condition/evidence/witness/evaluation count, and optionally claim the
trigger in one short SQLite transaction. Host, app-server, filesystem, and Git
I/O MUST remain outside that transaction. The first committed terminal
publication or expiry/claim SHALL determine the one durable wake reason and
later contenders MUST NOT create a second claim, queue admission, or activation.

The transaction MUST verify that stored reservation identities and kinds equal
the complete set derived from the condition. A missing, extra, duplicate,
malformed, or mismatched binding MUST become fatal `unknown`; the evaluator
MUST NOT silently omit a member or select a different reservation.

#### Scenario: Several terminal events satisfy an all condition
- **WHEN** one armed monitor contains `all` over several bound terminal
  reservations and every leaf becomes terminal
- **THEN** one transaction observes the complete snapshot, claims the monitor
  once, and permits at most one delivery admission

#### Scenario: One terminal event satisfies an any condition
- **WHEN** one armed monitor contains `any` over several bound terminal
  reservations and one leaf becomes true
- **THEN** the monitor claims once and all losing reservations remain permanently
  bound and inspectable rather than becoming reusable

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

#### Scenario: Bound reservation set is logically corrupt
- **WHEN** an event leaf cannot resolve the exact complete set of logically valid
  bound reservations
- **THEN** evaluation returns fatal `unknown`; a confirmed deferred or thread
  monitor performs its one `observer_failed` wake, while a legacy monitor
  terminates `observer_failed` without activation

### Requirement: Deliver a self-describing wake report

For every fired monitor, the default MCP `wake_me_up_status` decision view SHALL
include wake reason, satisfying compact witness or observer failure detail,
arming/firing/wait statistics, evaluation count, selected delivery identity and
final classification, abnormal non-recorded diagnostics, re-arm chain when
present, and bounded journal evidence. It SHALL be sufficient for the woken task
to decide its next step after exactly one status call and MUST NOT require a
second detail operation.

The decision view SHALL omit nulls, empty containers, full capability snapshots,
full queue receipts, normal recorded reconciliation history, duplicate
`delivery_outcome`, and defaults without safety meaning. Its journal SHALL keep
only a bounded newest set of distinct consecutive runs represented as
`{"line":"...","repeat_count":N}` plus an explicit dropped count; the durable
audit journal SHALL remain unchanged.

For one or several terminal events the decision view SHALL include each relevant
redacted reservation and producer identity, event kind/status, bounded command
or worker evidence, heartbeat facts, candidate and Git attestation, failure
evidence, and explicit success and acceptance flags. A legacy one-event monitor
SHALL retain its singular terminal-event projection; a multi-event monitor
SHALL return one bounded plural projection without duplicating the same event in
both forms. The queued pointer SHALL identify the monitor, direct exactly one
`view="decision"` read, state that it is not success, and SHALL NOT duplicate
this report or mutate a goal objective or other user-owned text. Publish tokens,
credentials, raw command arguments, commit messages, and sensitive user text
MUST remain absent.

`wake_me_up_status(..., view="audit")` SHALL retain the full durable status and
complete multi-reservation evidence. One call SHALL select exactly one view.

#### Scenario: Woken task reads context once
- **WHEN** thread or goal delivery starts a later turn and the task requests
  `wake_me_up_status` once with the monitor ID and `view="decision"`
- **THEN** the response contains the wake reason, witness, wait statistics, and
  delivery receipt without further discovery

#### Scenario: Operator requests full audit status
- **WHEN** status is requested once with `view="audit"`
- **THEN** it returns the complete durable status fields without changing the
  model-facing decision projection

#### Scenario: Lead wakes for several worker results
- **WHEN** a composed worker-terminal condition causes pointer or goal delivery
- **THEN** status contains each bounded producer result and any candidate,
  attestation, failure, or uncertainty evidence with explicit false acceptance
  and success flags

#### Scenario: Lead wakes for command failure
- **WHEN** a command-terminal witness reports a failed or signaled command
- **THEN** status contains bounded exit evidence and declared logs/artifacts
  without claiming task success

## ADDED Requirements

### Requirement: Observe one exact Git ref change read-only

The system SHALL support a one-shot `git_ref_change` condition naming one
absolute local Git worktree root and either literal `HEAD` or one full direct
`refs/...` name. At registration it MUST capture the canonical worktree root,
Git common directory, object format, exact ref, direct baseline object ID,
peeled baseline commit, and, for `HEAD`, its symbolic target or detached state.
It MUST reject an absent, unborn, non-commit, revision-expression, non-root,
remote, or otherwise unprovable binding before arming.

Each evaluation MUST revalidate the captured repository identity before
classifying the ref. An unchanged binding SHALL remain false. A changed binding
SHALL become true with exactly one bounded classification: `fast_forward`,
`ref_rewrite`, `ref_deleted`, or `head_retarget`. Repository replacement,
object-format change, malformed stored identity, an unresolvable now-present
ref, Git timeout/error, or an identity race SHALL be fatal `unknown` and follow
the existing observer-failure wake policy.

The witness MAY include canonical local paths, ref names, object format, full
object IDs, ancestry relation, and a bounded count of coalesced commits. It MUST
NOT include commit messages, authors, configuration, remotes, raw Git stderr,
credentials, or caller text. Git ref movement SHALL remain heuristic progress
evidence and MUST NOT authorize continuation or imply worker, command, task, or
lead success.

#### Scenario: Watched branch advances
- **WHEN** the captured ref moves from its baseline commit to a descendant
  commit
- **THEN** the leaf becomes true as `fast_forward` with bounded old/new object
  identity and no commit message

#### Scenario: Watched branch is rewritten
- **WHEN** the captured ref moves to a commit that does not descend from the
  baseline, or its direct object changes without changing the peeled commit
- **THEN** the leaf becomes true as `ref_rewrite` without claiming a new commit
  was safely produced

#### Scenario: HEAD changes target
- **WHEN** captured `HEAD` switches symbolic ref or between symbolic and detached
  state, including when its commit object remains the same
- **THEN** the leaf becomes true as `head_retarget` with old/new target identity

#### Scenario: Watched ref disappears
- **WHEN** the exact captured repository remains valid but the watched direct ref
  no longer exists
- **THEN** the leaf becomes true as `ref_deleted`

#### Scenario: Repository identity becomes uncertain
- **WHEN** the worktree/common-directory/object-format binding cannot be
  revalidated or a Git observation fails
- **THEN** the leaf becomes fatal `unknown` and the wake report identifies an
  observer failure rather than a ref change or success

### Requirement: Arm asynchronous wakes without owning producer launch

The primary MCP operation SHALL arm and return immediately rather than joining
the producer. It MUST NOT execute a caller-supplied shell command, discover a
producer by scanning unrelated processes, install Git hooks, scrape another
plugin's private state, or treat a tmux container as the producer outcome.

The model-facing guidance and terminal reservation receipt SHALL distinguish a
synchronous join from asynchronous monitoring. “Do not wait” SHALL be described
as skipping a foreground join; when later continuation is required for a long
durable producer, the caller still arms the returned typed condition, requires
an `armed` receipt, and ends its turn. No monitor SHALL be armed implicitly when
the caller has not requested or authorized a later wake.

#### Scenario: Existing process is monitored
- **WHEN** a caller already has the exact producer PID or typed terminal
  reservation and requests a later wake
- **THEN** it can arm that condition without the plugin launching or searching
  for the producer

#### Scenario: Caller declines automatic wake
- **WHEN** the caller explicitly says not to wake or continue the task later
- **THEN** guidance does not reinterpret the launch as permission to arm a
  billed continuation

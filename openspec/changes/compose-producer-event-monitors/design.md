## Context

See [proposal.md](proposal.md) for motivation. The current condition AST already
supports `all` and `any`, but `event_condition_binding()` collapses the tree to
one `(reservation_id, kind)` pair. Registration passes that pair to the ledger,
the event table makes `bound_monitor_id` unique, evaluation accepts one event
snapshot, and status retrieves one event per monitor. These assumptions reject
the natural condition for an asynchronous agent batch even though the monitor's
claim and delivery state machines are already one-shot.

The current terminal reservation is otherwise the right producer boundary: the
plugin creates a private single-use publication capability before launch, while
the existing command wrapper or worker harness remains responsible for running
and settling its producer. The current Git code also already owns canonical
worktree discovery, object-format checks, bounded subprocess execution, and
read-only ancestry verification for worker delivery attestation.

## Goals / Non-Goals

**Goals:**

- Reuse the existing condition AST to bind and evaluate several reserved
  terminal events under one monitor and one trigger claim.
- Preserve exact reservation identity, transaction-current evaluation,
  idempotency, restart recovery, one-shot delivery, and fail-closed diagnostics.
- Add one narrow read-only Git-ref observer without hooks or history scanning.
- Make terminal reservation output directly usable as a monitor condition and
  make synchronous join versus asynchronous arm explicit to the model.
- Preserve complete audit evidence and a bounded single-call decision report.

**Non-Goals:**

- No new monitor, delivery, reconciliation, or activation states.
- No shell launcher, process discovery, tmux job semantics, Git hook,
  process-group/cgroup observer, recurring subscription, or automatic re-arm.
- No outcome-filter language, quorum operator, callback scheduler, generic
  producer-handle framework, or second status/details call.
- No reading or editing HarnessDock private state. A launcher integration is a
  separate producer-side consumer of this repo's public descriptor/condition
  contract.
- No installation, cache replacement, daemon restart, commit, or push in the
  source implementation phase without separate authorization.

## Decisions

### 1. Keep one arming operation and the existing condition AST

`wait_for_event` remains the only primary arm operation. Terminal reservation
responses add the exact compatible condition as a small `monitor_condition`
projection, and documentation consistently describes this operation as an
asynchronous arm that returns immediately. The plugin does not add a second MCP
alias or a launch-and-wait tool.

The first implementation supports multi-reservation composition through the
existing `all` and `any`. It does not add batch-specific policy syntax. For the
accepted agent-benchmark policy, each ordinary or uncertain worker settlement
is terminal, so `all` produces one wake after every member settles. A caller
that deliberately chooses `any` accepts that the first true member wins.

Alternative: create `agent_batch`, `completion_handle`, or `all_settled`
frameworks. Rejected because the existing AST already expresses the required
composition once event binding is plural.

### 2. Derive a complete, canonical event binding set from the AST

Replace the singular internal binding helper with a plural helper that returns
an ordered canonical tuple of distinct `(reservation_id, event_kind)` values.
For every reservation the tree must contain exactly one matching
`command_terminal` or `worker_terminal` leaf. Any `heartbeat_stale` leaf must
name one of those same reservations. Duplicate terminal leaves and heartbeat-
only reservation references remain invalid.

Canonical ordering makes semantic/idempotency comparisons deterministic; AST
evaluation order remains the caller's stored order. An `any` monitor binds and
permanently consumes every member, including losing branches. Releasing a loser
would let another monitor race against evidence already used by the first and
is therefore out of scope.

### 3. Remove only monitor-side uniqueness from event persistence

Each event reservation row continues to contain one permanent
`bound_monitor_id`; that preserves the rule that a reservation has exactly one
consumer. The schema migration removes the uniqueness constraint that currently
prevents two rows from naming the same monitor and replaces it with a normal
lookup index on `bound_monitor_id`.

Registration passes the complete binding set into the existing monitor creation
transaction. The transaction validates every row and either inserts/reuses the
monitor and binds all members, or rolls back every change. An idempotent replay
must derive the same semantic condition and exact binding set.

The migration rebuilds only the event-reservation table inside SQLite, copies
all existing columns and rows byte-for-byte at the JSON/value level, restores
indexes, and validates that every legacy row still has at most one monitor
binding. Existing one-event monitors require no data rewrite beyond the table
copy. `EVENT_CAPABILITY_EPOCH` advances so an older daemon cannot supervise a
runtime containing active multi-event monitors.

Alternative: introduce a monitor-reservation join table. Rejected because the
existing row already owns a permanent single consumer; removing the reverse
uniqueness is the smaller representation.

### 4. Snapshot and evaluate all bound events in the claim transaction

Plural ledger lookup returns every reservation bound to the monitor. During
event evaluation, the transaction derives the expected binding set from the
stored condition, loads all bound rows, validates exact identity/kind equality,
and builds a `reservation_id -> redacted status` map. A missing, extra,
duplicate, malformed, or mismatched row produces fatal `unknown`.

`evaluate_event_condition_tree()` selects the correct immutable snapshot at
each event leaf and continues to combine values with the existing tri-state
operators. External PID/log/thread/Git observations remain outside the SQLite
transaction and enter through their existing path-indexed evaluation map. The
current `state=ARMED` plus `evaluation_count` compare-and-set remains the only
claim gate, so multiple publications cannot create a second wake or delivery.

### 5. Preserve singular status and add a bounded plural projection

Legacy one-event monitors retain their existing singular terminal-event fields.
Multi-event monitors expose `terminal_events` in deterministic reservation
order; they do not duplicate those events in the singular field. Audit status
retains complete reservation lifecycle and attestation facts.

Decision status includes only the members needed to explain the satisfied
condition or observer failure, plus bounded abnormal evidence for the other
bound members when it changes the next decision. It preserves producer,
candidate, attestation, failure, heartbeat, `task_success=false`, and
`lead_accepted=false` facts while excluding publish tokens, descriptors, raw
commands, credentials, commit messages, and user text. One pointer still asks
for exactly one decision-status call.

### 6. Represent terminal settlement uncertainty explicitly

`settlement_uncertain` becomes a validated `worker_terminal` outcome. It carries
one bounded fixed-classification reason, carries no candidate commit, becomes a
true terminal leaf, and is always reported with explicit false success and
acceptance flags. This lets an adapter terminate honestly after a rejected or
unverifiable driver result instead of leaving the monitor asleep or inventing a
definite worker failure.

Identical publication remains idempotent and any rewrite remains rejected. Older
producers need not emit the new value and retain their existing outcomes.

Alternative: map uncertainty to `failed`. Rejected because it collapses “the
worker failed” and “the adapter cannot establish settlement,” which require
different lead decisions.

### 7. Add Git ref observation at the existing Git trust boundary

`git_ref_change` accepts an absolute canonical worktree root plus literal
`HEAD` or a validated full direct `refs/...` name. Git-specific capture and
comparison live beside the existing attestation helpers; `conditions.py`
remains the AST adapter.

Registration captures worktree/common-directory identity, object format,
direct baseline OID, peeled commit, and `HEAD` symbolic target or detached
state. Each sample revalidates repository identity before resolving the exact
direct ref. A changed descendant is `fast_forward`; a non-descendant or changed
direct object with the same peeled commit is `ref_rewrite`; a missing ref in the
same verified repository is `ref_deleted`; and a `HEAD` target change takes
precedence as `head_retarget`. Identity, parse, timeout, or post-read race errors
are fatal observer failures, not ref changes.

The adapter uses the existing bounded, scrubbed Git subprocess path and never
invokes history/log output. Several commits between daemon samples are
coalesced into one ref movement; it does not invent an exact intervening-commit
count that the direct-ref observation cannot prove. The leaf is heuristic and
is deliberately absent from authoritative continuation evidence.

Alternative: install `post-commit` hooks. Rejected because hooks mutate each
repository, can be bypassed, do not naturally cover ref rewrites, and create a
second producer transport.

### 8. Keep producer integration explicit and outside private runtimes

Reservation creation returns both the private descriptor (only when requested
and only on first creation) and a separate non-secret `monitor_condition`.
Existing launchers may accept the descriptor and return that condition in their
own spawn receipt. They publish their terminal result through the existing
adapter; the monitor never scans their process table or private ledger.

This repo updates the README and skill to show the reserve -> launch -> arm
sequence and to state that “no synchronous wait” does not mean “no later wake.”
It documents the expected HarnessDock integration seam but does not modify that
plugin. A native worker without a host adapter may publish from its brief, with
heartbeat/expiry as the explicit failure boundary; a later host callback can
consume the same descriptor without changing this contract.

## Terminal-State and Failure Receipts

| Write or observation | Disposition | Durable/observable receipt |
|---|---|---|
| Multi-binding validation fails before commit | Fail closed; no retry inside registration | No monitor row and no newly bound reservation |
| Complete multi-binding commits | Armed/eligible | Monitor semantic condition plus every reservation's permanent `bound_monitor_id` |
| Command/worker terminal publication commits | Wake-eligible immutable evidence | Per-reservation terminal event and publication timestamp |
| `settlement_uncertain` commits | Wake-eligible terminal uncertainty | Bounded classification with false success/acceptance flags |
| Conflicting terminal rewrite | Rejected; never compensating | Original immutable event plus conflict error |
| Multi-row snapshot differs from stored AST | Fatal observer failure | Condition evidence naming missing/extra/mismatched binding class |
| Git ref classification is true | Wake-eligible heuristic evidence | Captured/current OIDs, ref identity, and bounded classification |
| Git identity or observation cannot be proven | Fatal observer failure | Fixed Git observer error class; no fabricated ref movement |
| Existing expiry, cancellation, queue admission, reconciliation, or delivery terminal write | Unchanged | Existing monitor/delivery state and decision/audit status |

## Risks / Trade-offs

- [An `any` monitor strands unused producer capabilities] -> Make permanent
  consumption explicit in spec, receipt, and tests; callers allocate fresh
  reservations for later monitoring.
- [SQLite table rebuild is served by an older daemon] -> Bump the event
  capability epoch, enforce compatibility before arming, and require separately
  authorized daemon replacement before live use.
- [Several Git subprocesses lengthen a serial daemon pass] -> Reuse bounded
  fixed-output commands, avoid path/history scans, and document that this is a
  small-number one-shot observer rather than a high-cardinality subscription.
- [A ref moves several times between samples] -> Report one bounded coalesced
  movement without an unproven exact count; do not promise one billed wake per
  commit.
- [External launchers do not yet publish terminal evidence] -> Keep PID/log and
  heartbeat/expiry fallbacks explicit; do not scrape private launcher files.
- [Plural status increases model payload] -> Return only decision-bearing
  members in decision view and keep complete evidence in audit status.

## Migration Plan

1. Add focused characterization/RED tests for the current multi-reservation
   rejection, single-binding schema, unsupported Git condition, and missing
   uncertainty outcome.
2. Implement and test the event-table migration and capability-epoch guard,
   then plural binding, snapshot evaluation, concurrency, and status projection.
3. Implement `settlement_uncertain`, the monitor-ready reservation projection,
   and producer adapter/documentation updates.
4. Implement the Git capture/evaluation adapter and fail-closed identity tests.
5. Run focused suites, full `pytest -q`, Ruff, skill validation, strict OpenSpec
   validation, JSON/diff checks, and payload sensitivity checks.
6. Stop at source acceptance. Installation, cache replacement, daemon restart,
   and any HarnessDock-side change require separate authorization and their own
   live compatibility receipt.

Rollback after a future authorized installation first refuses while any active
multi-event monitor requires the newer event epoch. Once compatibility permits,
restore the previous source/cache and exact daemon using the existing guarded
rollback procedure; the migrated event rows remain representable as ordinary
single-bound rows only when no multi-event monitor remains active.

## Context

See [proposal.md](proposal.md) for motivation and the delta specs for the
observable contract. Today a `receipt_success` leaf is created as part of a
monitor and stores a monitor-scoped receipt path/token. That ordering works
when a producer can be configured after monitor registration, but not for the
common sequence “prepare producer → launch it → defer the current goal”: the
producer needs its publish capability before it starts, while defer must be the
last action of the active Codex turn.

The monitor ledger is one SQLite database in WAL/FULL mode. Monitor creation,
condition preparation, evaluation, and the one guarded activation already have
durable compare-and-set boundaries. The new event lifecycle must reuse those
boundaries without adding a second goal-write path or treating producer output
as acceptance.

The Git use case sharpens the ownership boundary. A lead does not need a broad
watch on every `.git` mutation; it needs one explicit event saying that one
delegated worker ended and, if it delivered, which immutable commit is the
candidate review target. Git facts can attest that candidate, but only the lead
can review and accept it.

## Goals / Non-Goals

**Goals:**

- Let a caller allocate a publish capability before launching a command or
  worker, then bind it exactly once during monitor registration or defer.
- Wake on success and failure terminal events without model-level polling.
- Reuse one event transport for command and worker producers while retaining
  distinct payload validation and claims.
- Make a worker-created commit an exact, bounded candidate-delivery receipt for
  lead review.
- Preserve current monitor, defer, activation, recovery, and legacy receipt
  behavior.

**Non-Goals:**

- Execute commands, accept raw shell predicates, or become a job scheduler.
- Continuously watch all Git refs, worktree dirt, filesystem changes, or commits
  made outside a reserved worker delivery.
- Automatically review, accept, merge, cherry-pick, revert, commit, push, or
  change a worker or lead branch.
- Treat exit zero, worker self-report, or Git attestation as whole-task success.
- Add cross-host transport, TCP, a second activation API, or automatic re-arm.

## Decisions

### 1. Use a separate reservation lifecycle, not a new monitor phase

Add an `event_reservations` table beside `monitors`. Its externally meaningful
states are `reserved`, `bound`, `terminal`, `cancelled`, and `expired`.
Reservation state never drives a goal write directly; a compatible condition
leaf reads it through the existing monitor evaluation and claim path.

The table records immutable semantics, event kind, idempotency key, a hash of
the publish token, expiry, optional frozen Git scope, bound monitor ID, last
heartbeat sequence/host time, terminal event, attestation, and timestamps. The
raw token is returned only at creation and never appears in status. An
idempotent retry returns the original reservation ID and token fingerprint, not
the bearer; losing the creation response requires a new reservation. Existing
monitor rows and state enums remain unchanged.

Binding and monitor creation occur in one SQLite transaction. One monitor may
name at most one distinct reservation: exactly one matching terminal leaf and
optional heartbeat-stale leaves may share that ID. The transaction either
creates/reuses the semantic monitor and binds that reservation, or changes
neither; duplicate terminal leaves or distinct event IDs are rejected. Binding
is permanent: a pause failure,
monitor cancellation, expiry, or activation failure does not make the
reservation reusable. A retry allocates a new reservation, which avoids two
monitors racing to consume one producer event.

Alternative considered: add `RESERVED` and `EVENT_READY` to `MonitorState`.
Rejected because a reservation has no target goal and is intentionally created
before monitor ownership exists; mixing it into the monitor state machine would
weaken the existing one-target/one-activation proof.

### 2. Allow publication before binding and make defer the final caller action

The intended flow is:

1. Reserve a `command_terminal` or `worker_terminal` event.
2. Give the returned publish capability to the command wrapper or worker brief.
3. Launch the command or worker using the existing harness.
4. Register/defer a monitor whose terminal leaf names the reservation.
5. End the Codex turn immediately after a successful defer.

A fast producer may publish between steps 3 and 4. The event is retained on the
unbound reservation; a binding transaction committed before the pre-bind
deadline makes it immediately observable. Once bound, time alone does not
expire the producer token, but monitor claim/failure/cancellation/expiry or
activation start rejects later producer writes. The deferred idle barrier still
prevents an already-true event from
waking the turn that is currently performing defer.

Alternative considered: a `launch_and_defer(command)` tool. Rejected because it
would make this plugin execute arbitrary commands, duplicate shell ownership,
and materially expand the security surface.

### 3. Share the durable envelope, not the terminal semantics

Both producer kinds use the same reservation, token validation, heartbeat,
idempotency, persistence, binding, and status machinery. Payload validation is
kind-specific:

- `command_terminal` accepts `succeeded`, `failed`, `cancelled`, or `signaled`,
  plus a safe label, command digest, exit/signal evidence, producer timestamps,
  and bounded local path references. It never stores raw command arguments.
- `worker_terminal` accepts `delivered`, `blocked`, `failed`, or `cancelled`,
  plus producer task identity and a bounded reason. `delivered` requires one
  full commit object ID and runs Git attestation.

The event JSON budget is 64 KiB; default reversible sub-budgets are 64 path
references, 4 KiB per string, and one retained heartbeat payload plus counters.
Truncation is explicit in evidence. The service stamps host receive times, so
producer timestamps are never used for expiry, staleness, or ordering.

This is why `command_terminal` is not equivalent to the Claude-style worker
commit trigger by itself. Wrapping `git commit` proves only that one process
reported an exit status. `worker_terminal` additionally binds producer
identity, delegated baseline, worktree, allowed paths, candidate OID, and the
acceptance boundary. Both still use the same underlying event pipe.

Alternative considered: represent every event as generic
`command_terminal(outputs={...})`. Rejected because caller-specific conventions
would become hidden policy, and an exit-zero Git command could be mistaken for
a valid worker delivery.

### 4. Attest Git once at worker-terminal publication; never watch refs

A worker reservation captures the canonical worktree root, Git common-dir
identity, object format, baseline commit, producer task identity, and normalized
repository-relative allowed prefixes. Absolute paths, `..`, `.git`, option-like
revisions, empty baselines, and implicit unrestricted scope are rejected.

On `delivered`, a fixed read-only Git adapter validates the full hexadecimal
candidate OID, resolves it as a commit in the captured repository, checks that
the baseline is an ancestor, and computes a NUL-delimited baseline-to-candidate
path list. The adapter invokes `git` directly with fixed argument vectors and
validated revisions; it never invokes a shell. It scrubs inherited repository,
config, index, object-alternate, and namespace environment; disables replacement
objects; uses exact-or-slash-descendant prefix matching; and revalidates the
captured common-dir identity. Attestation is computed before the SQLite
terminal-event transaction and stored atomically with the immutable event.
Commit/path evidence is bounded to 256 paths and 64 KiB. The adapter fully
consumes the NUL-delimited path set within 5 seconds and 8 MiB before reporting
a complete count/digest. Ceiling exhaustion, malformed output, or identity
change returns `attestation_error`; partial scans never report `valid`.

Attestation results are `valid`, `invalid_commit`, `baseline_mismatch`,
`out_of_scope`, or `attestation_error`. Every result is wake-eligible because
the worker is terminal and the lead must handle a bad delivery rather than
sleep until expiry. Only `valid` identifies a reviewable candidate; none is
acceptance. A transient Git adapter error is stored as `attestation_error`
instead of rejecting the producer's terminal fact.

Terminal idempotency compares only the immutable producer envelope. The first
winning transaction stores its derived attestation; an identical retry returns
that stored event and attestation without rerunning Git, so repository drift
cannot turn an identical producer retry into a conflict.

Alternative considered: watch `HEAD`, refs, or `.git` mtimes. Rejected because
unrelated commits, rebases, index writes, maintenance, and other worktrees are
indistinguishable from a delegated delivery, while a commit object plus explicit
producer event is immutable and owner-scoped.

### 5. Read event state through condition evaluation and retain one claim path

Condition preparation validates reservation existence/kind but leaves binding
to the monitor-create transaction. During reconciliation, host/app-server/
filesystem observations are collected first. A short Ledger-owned
`BEGIN IMMEDIATE` transaction then reloads the armed monitor and its bound
reservation, evaluates transaction-current event/heartbeat facts using a
post-lock host time, and atomically stores latch state, evidence, witness,
evaluation count, and the optional one claim. SQLite connections never escape
the ledger and Git I/O never occurs under the lock. Inside that transaction an
event resolver supplies the reservation snapshot to the condition evaluator:

- `command_terminal` is true for any immutable command terminal status.
- `worker_terminal` is true for every worker terminal outcome, including an
  invalid Git delivery.
- `heartbeat_stale` is false while monotonic heartbeats advance, true after the
  configured host-time window, unknown if the required initial heartbeat or
  reservation identity is unavailable, and false after terminal publication.

`witness_authorizes_continuation` gains a third path: successful
`receipt_success`, explicit bound terminal event, or the existing heuristic
opt-in. A terminal event is authorized because the caller separately reserved
and bound its exact kind, but its evidence class remains
`command_terminal`/`worker_delivery_candidate`, not `task_success`.
`heartbeat_stale` remains heuristic and follows the existing
`unauthorized_evidence` policy.

No new `WakeReason` is needed. Authorized terminal events use `condition`; the
witness and report carry the finer event kind/status. Expiry,
`unauthorized_evidence`, and `observer_failed` remain unchanged.

### 6. Keep producer APIs narrow and token-safe

Add MCP/service operations to reserve, inspect, cancel, heartbeat, and publish
terminal events. The CLI mirrors them with event-specific subcommands and a
JSON payload file option so bounded event data need not appear in process-list
arguments. An optional mode-0600 publisher descriptor under the existing
runtime root may carry the reservation ID/token for shell or worker harnesses;
status and logs expose only the reservation ID and token fingerprint.

Token comparison uses a persisted salted digest and constant-time comparison.
An identical terminal retry is idempotent; a conflicting rewrite is rejected.
The existing monitor-scoped `wake_me_up_publish_receipt` remains unchanged for
compatibility.

### 7. Make heartbeats anomaly triggers, not progress streams

Only the latest accepted heartbeat payload, sequence, host-observed time, total
count, and rejected-count are retained. The daemon does not deliver heartbeat
turns to Codex. A caller composes `any(command_terminal, heartbeat_stale)` or
`any(worker_terminal, heartbeat_stale)` when stall intervention is desired.
Success/failure remains the terminal event; a stale heartbeat says only that
the producer stopped advancing under the declared contract.

Alternative considered: wake on every progress event. Rejected because it
recreates the model re-entry and compaction cost this change exists to remove.

### 8. Extend the existing wake report rather than add a second callback

`wake_me_up_status` remains the single post-wake orientation call. Its existing
outcome gains a bounded `terminal_event` section containing reservation ID,
producer identity, kind/status, command evidence or worker reason, last
heartbeat summary, candidate OID, Git attestation, and an explicit
`lead_accepted: false`. The report never carries the publish token or raw
command.

HarnessDock and native subagent adapters publish the same worker event. They do
not implement their own wake mechanism. For a native child that cannot publish,
the existing `thread_idle` condition remains a heuristic fallback; for a worker
that commits, explicit publication is the preferred review trigger.

### 9. Classify every new terminal or failure write

| Write site | Durable result | Disposition | Observable receipt | Required regression |
|---|---|---|---|---|
| Unbound reservation expiry | `expired` | Justified fail-closed; no goal exists | reservation status with expiry | expired reservation cannot bind or publish |
| Unbound reservation cancellation | `cancelled` | Justified operator stop | cancellation timestamp | cancelled token cannot heartbeat/publish |
| Compatible monitor creation | `bound` | Retryable only through idempotent same request | bound monitor ID | concurrent double-bind admits one monitor |
| Kind mismatch or reused binding | no write | Retryable with correct/new reservation | conflict response | original reservation remains unchanged |
| Valid heartbeat | bound/reserved row update | Retryable/idempotent | last sequence/host time/count | duplicate same heartbeat does not append |
| Invalid heartbeat/token | no write | Retryable with correct input | validation/conflict response | terminal/binding state unchanged |
| Command terminal publish | immutable `terminal` | Wake-eligible after binding | command status and exit/signal evidence | success and failure both satisfy leaf once |
| Worker terminal without delivery | immutable `terminal` | Wake-eligible after binding | outcome and bounded reason | blocked/failed/cancelled wake with no commit |
| Worker delivery, valid attestation | immutable `terminal` | Wake-eligible candidate | OID, paths, `valid`, not-accepted marker | exact candidate wakes once |
| Worker delivery, invalid/error attestation | immutable `terminal` | Wake-eligible intervention | OID plus invalid/error attestation | bad candidate wakes rather than strands |
| Conflicting second terminal publish | no write | Justified immutable conflict | original event identity | original terminal event is preserved |
| Bound reservation missing/corrupt at evaluation | existing monitor `observer_failed` policy | Deferred: wake via `observer_failed`; legacy: fail-closed terminal | fatal evidence in monitor status | no duplicate claim or activation |
| Heartbeat stale | no reservation terminal write; monitor claim only | Wake-eligible only under existing heuristic policy | heartbeat age/sequence witness | authorized vs unauthorized paths preserved |
| Monitor/defer pause failure after binding | existing monitor terminal state; reservation stays bound | Justified fail-closed; never rebind | monitor failure plus binding | producer event cannot activate another goal |
| Monitor cancellation/expiry after binding | existing monitor policy; reservation stays bound | Existing cancellation/expiry semantics | monitor and event status | no rebind or second activation |
| Daemon restart with published event | no semantic rewrite | Retryable recovery | same immutable event | one terminal event produces at most one claim |

Existing monitor terminal writers remain governed by the current design. Event
conditions use the transaction-current evaluate-and-claim ledger seam, then
converge on the existing single activation attempt. Legacy conditions retain
their existing path unless compatibility tests justify a safe unification.

### 10. Gate mixed-version daemons with a capability epoch

The daemon heartbeat gains an event-condition schema epoch and loaded source
identity. Event registration verifies that epoch and the advertising PID's
kernel-reported ownership of `daemon.lock` before arming; event defer verifies
both before any goal pause. A fresh legacy `{pid, at}` heartbeat is not
event-capable. Before restoring older source/cache, the operator runs the
current binary's `event-compatibility-check` with the target epoch; it refuses
while any nonterminal monitor contains an event leaf, deriving that need from
the stored condition independently of whether its reservation row survives.
Completed terminal event history does not require the evaluator and does not
block rollback. An older binary cannot retroactively contain this guard, so the
contract does not claim self-protection after source replacement.

## Risks / Trade-offs

- **[Producer with a valid token can lie]** → Treat command and worker payloads
  as producer evidence, independently attest Git facts, and preserve lead review
  as the acceptance boundary.
- **[Git CLI latency delays publication]** → Use fixed read-only commands,
  strict output/time budgets, and convert adapter failure into a wake-eligible
  `attestation_error` rather than stranding the lead.
- **[Reservation created but never bound]** → Bound its lifetime and allow
  explicit cancellation; it cannot affect a goal.
- **[Bound event outlives a failed defer]** → Keep the permanent binding and
  expose the event for forensics; never reassign it to another goal.
- **[Heartbeat cadence is misconfigured]** → Keep staleness heuristic, require a
  declared initial heartbeat contract, and use monitor expiry as the final wake
  backstop for deferred goals.
- **[Extra status payload increases context]** → Enforce JSON/path budgets,
  hashes/counts/truncation, and keep one status response as the only payload
  carrier.
- **[Installed daemon serves old code after upgrade]** → Retain the existing
  `/proc`/`PYTHONPATH` verification and daemon restart gate.

## Migration Plan

1. Add the reservation table and service operations additively; load all
   existing monitor rows unchanged and keep `receipt_success` behavior intact.
2. Add reservation/unit/recovery tests, then condition leaves and authorization
   tests, then Git-worktree and producer CLI tests.
3. Update README and skill guidance only after the executable contract passes.
4. Run strict OpenSpec validation, the complete test suite with failure-set diff,
   compile checks, and a source CLI/MCP smoke.
5. Reinstall only after explicit authorization. Verify the installed cache path,
   stop/restart the exact lock-holding daemon, and re-read its cmdline and
   `PYTHONPATH`.
6. Use disposable paused goals and inert objectives for one live
   reserve → publish → bind/defer → wake smoke. Do not target a user task.

Rollback first runs the current binary's compatibility check for the target
epoch. It then restores the previous source and plugin version, reinstalls, and
restarts the exact daemon. Before rollback, cancel or allow completion of every
nonterminal event-based monitor; the check remains fail-closed if such a
monitor's reservation row is missing or corrupt. The prior implementation
cannot evaluate the new leaves and cannot enforce the new preflight. The
additive reservation table and completed terminal event history may remain as
forensic data and are never replayed by the old code.

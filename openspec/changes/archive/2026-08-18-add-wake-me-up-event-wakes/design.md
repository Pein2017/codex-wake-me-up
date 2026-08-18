## Context

See [proposal.md](proposal.md) for the gap table and decision points. The two
shipped changes ([add-codex-wake-me-up-monitor](../2026-08-18-add-codex-wake-me-up-monitor/design.md),
[add-best-effort-goal-self-defer](../2026-08-18-add-best-effort-goal-self-defer/design.md))
established the durable ledger, typed tri-state conditions, the deferred
pause-once flow, the idle barrier, and the one-activation guarantee. This
change does not touch that machinery's guarantees; it changes *which durable
facts are allowed to consume the one activation* and *what evidence a wake
carries*.

The reference contract is Claude Code's Monitor/background-task event model:
a watch is armed once, the harness owns the wait, every terminal state of the
watch (event, timeout, failure) produces a notification, and the notification
carries the matched payload so the woken agent does not re-discover state.
The analog here is constrained by the Codex app-server protocol: there is no
channel to inject an event into a turn, so "notification" can only mean "the
goal returns to `active` and the first post-wake tool call reads the wake
report."

## Goals / Non-Goals

**Goals:**

- Guarantee that a conclusively self-deferred goal is never stranded paused
  past its expiry by the plugin's own bookkeeping.
- Make a wake self-describing: reason, witness, payload lines, wait
  statistics, all behind one status call.
- Detect job failure from log content minutes-to-hours before pid/tmux exit
  or expiry would, without shell predicates.
- Replace `wait_agent`-style long-yield polling on a subagent thread with a
  token-free deferred wait.
- Keep re-armed waits cheap and traceable.

**Non-Goals:**

- Multi-shot schedules, automatic re-arm, or stream monitors that deliver
  events into an active turn (no protocol channel; would reintroduce
  unbounded token cost).
- A second continuation path (`turn/start`, `thread/resume`, synthetic user
  messages) — the native-guarded-continuation requirement stands.
- Raw shell predicates or arbitrary file writes by the daemon.
- Treating any new condition as task-success evidence; receipts remain the
  only authoritative success.
- Changing legacy (non-deferred) monitor behavior.

## Decisions

### Route every deferred wake through the existing claim machinery

Deferred monitors gain a durable `wake_reason` field with values
`condition`, `expired`, `unauthorized_evidence`, and `observer_failed`.
Instead of adding a second activation path, the three newly wake-eligible
facts are converted into claim-eligible facts on the *same* path:

```text
ARMED --(idle barrier + authorized condition)---------------> CLAIMED[condition]
ARMED --(idle barrier + expiry reached)---------------------> CLAIMED[expired]
ARMED --(idle barrier + satisfied, no authorization)--------> CLAIMED[unauthorized_evidence]
ARMED --(idle barrier + irrecoverable observer identity)----> CLAIMED[observer_failed]
CLAIMED -> ACTIVATING -> FIRED | existing conservative terminals
```

Consequences that must hold and be tested:

- "At most one activation request" stays trivially true: whatever the reason,
  the wake consumes the single durable claim and the single activation.
- The idle barrier and the pre-activation guard re-read apply unchanged to
  every reason. A deadline that passes while the target is still running its
  turn waits for idle, exactly as a true condition does. Consequence,
  observed live in the 5.3 smoke: a target stuck in a non-idle runtime state
  (e.g. `systemError`) holds an expired monitor armed indefinitely — the
  wake-at-latest-by-expiry guarantee carries an "observed idle again"
  precondition, and status showing `armed` past `expires_at` is the honest
  signal. The app-server also exposes goal states beyond `active`/`paused`
  (`blocked` was observed); every eligibility and guard check treats them as
  ineligible, which fails closed.
- For deferred monitors, `EXPIRED` and `SATISFIED_REQUIRES_AUTHORIZATION`
  stop being reachable terminal states from `ARMED`; they remain terminal for
  legacy monitors and for deferred monitors whose activation preflight fails
  (an expired-then-guard-changed monitor still ends `SUPERSEDED`, not woken).
- `FIRED` outcome records carry `wake_reason`; a deadline wake is
  distinguishable from a condition wake forever after.
- `recover_after_daemon_start` must apply the same policy: an armed deferred
  monitor found past its expiry after a daemon crash is claim-eligible with
  reason `expired`, not terminal-expired. Otherwise a daemon restart
  re-creates the stranding this change removes.

Wake eligibility is a strict partition. Wake-eligible: deferred mode AND this
monitor's own pause was conclusively confirmed (it reached `ARMED`) AND the
guard still matches at the pre-activation re-read. Fail-closed, unchanged:
`DEFER_ABANDONED`, `PAUSE_UNCERTAIN`, `PAUSE_REJECTED`, `MIS_TARGETED_PAUSE`,
`DAEMON_UNAVAILABLE`, `SUPERSEDED`, `UNLOADED_TARGET`, `CANCELLED`,
`MIS_TARGETED_ACTIVATION`, `ACTIVATION_UNCERTAIN`, `ACTIVATION_FAILED`.
Those are exactly the states where a compensating write could be the
duplicate or mis-targeted write v1 guards against, or where a human
explicitly took over.

`allow_heuristic_continuation` is retained and keeps selecting the claim
strength, not the wake itself: flag on → `condition`; flag off with only
heuristic true leaves → `unauthorized_evidence`. The skill states plainly
that a wake is not a success claim; the witness is. (Decision D2 offers the
deadline-only alternative; recommended against because holding a task paused
for hours after its evidence fired adds wall-clock cost and no safety — an
agent that would misread evidence misreads it with the flag set too.)

Alternative considered: a separate "restore" write path for non-condition
wakes. Rejected: two irreversible RPC phases, two crash-recovery analyses,
and the guarantee "at most one goal-status write after arming" would no
longer be a single-table CAS fact.

### Classify every deferred terminal write; three error paths must not re-strand

The stranding bug this change removes can re-enter through error paths, not
just the happy path. Review rule for implementation and review: enumerate
every site that writes a terminal state on a *deferred* monitor and classify
it as exactly one of (a) wake via the claim path, (b) stay retryable
(remain `ARMED`/`CLAIMED` with recorded evidence; expiry is the backstop), or
(c) justified fail-closed (guard violation, mis-target, uncertain write,
cancellation). Three existing sites need explicit treatment:

1. **Top-of-loop expiry in `reconcile_once` also hits `CLAIMED` rows.** A
   deferred monitor claimed before a daemon crash, discovered after its
   expiry passed, must not be written terminal-`EXPIRED` — that re-creates
   the stranding after restart. The unconditional expiry write is legacy-only;
   deferred rows follow the wake policy for both `ARMED` and `CLAIMED`.
2. **`_record_unexpected_failure` maps `ARMED` to terminal
   `OBSERVER_FAILED`.** For deferred monitors, only *typed* irrecoverable
   identity violations (`Evaluation.fatal`) wake with reason
   `observer_failed`. An *untyped* exception — e.g. a transient app-server
   socket error inside the idle preflight — must neither strand (terminal)
   nor burn the single wake (claim): record the evidence and stay `ARMED`;
   expiry is the backstop. Legacy behavior is unchanged.
3. **`_finish_claim` preflight failure maps `CLAIMED` to terminal
   `ACTIVATION_FAILED`.** Before `begin_activation` no write has been sent,
   so a transient read failure on a deferred `CLAIMED` row is safely
   retryable: record evidence, stay `CLAIMED`, retry next tick. After
   `begin_activation`, uncertainty stays terminal for both modes — that is
   the uncertain-write bucket and must not be touched.

A fourth site, the `deferred_idle_barrier_missing` write (deferred `ARMED`
row whose idle barrier flag is unset), is classified (c): it is unreachable
through the public API (defer always sets the barrier) and guards a
corrupted row, which must not be woken on unvalidated state; it stays
terminal and appears in the state-machine diff.

Each of the three carries a dedicated regression test (see tasks): daemon
restart past expiry while `CLAIMED`; transient app-server error leaves a
deferred monitor `ARMED`; preflight error leaves a deferred monitor
`CLAIMED`.

### Add a `log_pattern` leaf: read-only, identity-guarded, payload-bearing

```json
{
  "type": "log_pattern",
  "path": "/abs/path/to/train.log",
  "patterns": [
    {"name": "ready", "regex": "Ready in [0-9.]+s"},
    {"name": "crash", "regex": "Traceback|CUDA out of memory|Killed"}
  ]
}
```

Semantics, mirroring the identity doctrine of `pid_exit`/`tmux_exit`:

- At preparation the observer captures the file's device+inode and current
  size; only bytes appended after arming are scanned (EOF-at-arm baseline).
  A missing file at arm is allowed (the job may not have created it yet) and
  recorded; the leaf stays `false` until it appears.
- Rotation, truncation, or inode replacement makes the leaf `unknown`, never
  `true`: a replaced file is a different observation target, exactly as a
  restarted tmux server is. The evidence names the mismatch.
- The daemon reads the file `O_RDONLY` with a durable resume offset and a
  bounded per-poll read budget (default 4 MiB, an implementation-owned
  reversible detail); it never executes content and never writes outside the
  runtime root. This preserves the no-shell-predicate doctrine: the AST gains
  a data pattern, not a program.
- Patterns are compiled with `re`, individually length-capped (default 512
  chars) and count-capped (default 8). Because `re` cannot be interrupted
  mid-match, a wall-clock check between lines does not bound one pathological
  line: the matcher receives at most the first 4 KiB of each line (noted in
  evidence when truncated). That per-line bound, not the wall-clock guard, is
  what keeps one hostile line from delaying every other monitor.
- The leaf is `true` when any named pattern matches a scanned line. The
  witness records which names matched; matched lines append to a bounded
  per-monitor journal (defaults: last 50 matched lines, 16 KiB total, both
  reversible), which the wake report returns. The journal is evidence
  payload, not an event stream — no line is ever delivered mid-turn.
- Evidence class: heuristic. A "ready"-pattern match is a liveness fact about
  text in a file; only a receipt is task success.

The skill imports the Monitor coverage rule verbatim in spirit: a
`log_pattern` watch MUST include the failure signatures the caller would act
on (`Traceback|OOM|Killed|FAILED`), because a watch that matches only the
success line stays silent through a crashloop, and silence is
indistinguishable from still-running. `pid_exit`/`gpu_stable` compose with it
(`any`) to cover hangs and silent exits.

Alternative considered: a generic `file_exists`/`file_quiet` family.
Rejected for now: `log_pattern` with an existence-tolerant baseline covers
the observed need; each extra leaf is another identity-capture analysis.

### Add a `thread_idle` leaf for subagent and auxiliary-thread waits

```json
{"type": "thread_idle", "thread_id": "<child-thread-id>"}
```

- Observed through the existing `AppServerClient.read_observation` path on
  the same local socket; read-only, host-local, no new protocol surface.
- At preparation the observer captures the child's identity and current
  runtime status. Registration **rejects** a child already idle at arm unless
  `accept_already_idle: true` is set: an already-finished child should be
  handled in the current turn, not deferred on (this is the
  event-fired-before-arming trap; rejection converts it into an immediate,
  visible signal instead of an expiry-length stall or a premature wake).
- Armed semantics are edge-shaped: the leaf is `true` once the child has been
  observed idle after arming (having been active at or after arm, or
  `accept_already_idle` was set). An unloaded or missing child is `unknown`,
  never `true` — disappearance is not completion, exactly as for tmux.
- The witness records the child's last observed runtime status, goal status,
  and (when a goal exists) its token/time usage snapshot, so the parent's
  wake report already says what the child consumed.
- Evidence class: heuristic. Idle means the turn ended, not that the child
  succeeded; the parent must read the child's actual output. Composition via
  existing `all`/`any` covers "all three subagents finished" and "child done
  or deadline".

Why this beats the status quo: today a parent either burns `wait_agent`/
long-yield poll turns or ends its goal and relies on the user to resume. With
`thread_idle` the parent defers at zero token cost and the daemon's local
socket reads (already the cheapest observation in the system) own the wait.

Alternative considered: watching the child's turn counter for strict
transition detection via `includeTurns`. Rejected: heavier reads on every
poll for a guarantee the arm-time rejection already provides.

### Make the wake self-describing

The terminal outcome of every fired monitor records: `wake_reason`, the
satisfying witness (or failure detail for `observer_failed`), the journal
tail, `armed_at`/`fired_at`, evaluation count, and an estimated
avoided-poll-turn count (wait duration divided by the retrospective's
observed poll cadence — additive, low priority, but it makes fleet cost
visible where it was invisible). `wake_me_up_status` already returns outcome
records; no new tool is needed. The stored condition embeds the journal, so
status and registration responses MUST elide it to counters
(`{lines_stored, dropped}`): `outcome.journal_tail` (bounded, default last 20
lines) is the sole line carrier. The report applies to legacy fired monitors
too (their reason is always `condition`): the preserved legacy contract is
the state machine, not the outcome payload, which gains only additive
fields. Post-wake context cost is the product
metric; returning the journal twice would spend it. The skill's post-wake contract: exactly one
`wake_me_up_status` call, then act on the witness — no log re-reading when
the journal already answers, no speculative re-verification of a receipt
wake.

Rejected alternative: amending the goal objective to carry wake context. The
objective is user-owned text and part of the guard marker; mutating it would
both overwrite user intent and break the guard equality this plugin's safety
rests on.

### Record re-arm lineage instead of multi-shot monitors

`wake_me_up` and `wake_me_up_defer` accept an optional `rearm_of:
<monitor_id>`. It is validated (the referenced monitor must exist and be
terminal), stored, and surfaced by `status`/`list` as a chain. Semantics are
purely archival: no state is inherited, every safety check runs fresh. The
skill documents the per-occurrence loop — wake, handle the event, optionally
re-defer with `rearm_of` and a fresh idempotency key — as the supported
answer to "notify me every time," and the lineage makes the loop's cost and
history auditable. Automatic re-arm stays rejected: each arm is a
consent-carrying tool call by an agent that just saw the previous wake's
evidence.

## Risks / Trade-offs

- **This reverses v1's "expiry never changes goal status"** → gated on
  decision D1; scoped strictly to deferred monitors whose own confirmed
  pause created the paused state; every wake still passes the idle barrier,
  guard re-read, and single-activation CAS. Legacy monitors keep v1 behavior
  exactly.
- **Python `re` catastrophic backtracking on hostile patterns** → length and
  count caps, per-poll read budget, and a per-evaluation wall-clock guard
  (implementation detail; on breach the leaf reports `unknown` with a
  `pattern_budget_exceeded` evidence record rather than stalling the daemon
  loop for every other monitor).
- **Log files on network filesystems** (stale attrs, rewritten inodes) →
  identity doctrine already degrades to `unknown`; the skill recommends
  local paths.
- **Journal growth** → hard line/byte caps with drop counters in evidence
  ("N earlier matches dropped"), matching the no-silent-caps rule.
- **A premature `thread_idle` wake if a child is momentarily idle between
  turns** → the child is a goal-less or goal-owning thread whose runtime
  blips idle; the witness carries the child snapshot, the parent can re-defer
  with `rearm_of` at the cost of one short turn. Accepted: strictly cheaper
  than today's polling, and rejection-at-arm removes the worst case.
- **State-machine complexity** → the claim-reason routing adds one enum, not
  new phases; the frozen state diagram diff is a review artifact in tasks.
- **Ledger migration** → additive columns/values with defaults; legacy rows
  and non-deferred behavior byte-for-byte unaffected; focused migration
  tests over v0.1 rows.

## Migration Plan

1. Land spec deltas (this change) and obtain the user's ruling on D1–D3.
2. Additive ledger schema (`wake_reason`, `rearm_of`, journal storage) with
   migration tests over existing rows; extend models and condition
   preparation.
3. Implement claim-reason routing including the recovery path; new leaves
   with focused tests (identity capture, rotation, budgets, edge semantics,
   already-idle rejection); wake report assembly.
4. Update the skill (coverage rule, post-wake contract, re-defer loop,
   remove the `any(condition, time)` backstop recommendation).
5. Frozen state-machine diff review; disposable local app-server smoke: one
   `log_pattern` defer with a synthetic log, one `thread_idle` defer on a
   scratch child thread, one forced-expiry wake, one daemon-restart-then-
   expiry-wake recovery.
6. Bump the plugin cachebuster and reinstall through the existing local
   marketplace; roll back by reinstalling the previous cachebuster. Restart
   the runtime-lock-holding daemon as part of any install or rollback — a
   daemon keeps running the code it was launched from, so a stale-cache
   daemon silently applies the old semantics to new monitors. Terminal
   records remain forensic evidence and are never replayed.

## Implementation Notes (for the executing worker)

Integration points, so implementation does not re-derive them. Budgets and
caps named here are reversible implementation defaults, not contract.

- `models.py`: add `WakeReason` StrEnum (`condition`, `expired`,
  `unauthorized_evidence`, `observer_failed`). No new monitor states: the
  wake policy re-routes facts into `CLAIMED`, it does not add phases.
- `ledger.py` (additive migration via the existing `PRAGMA table_info`
  pattern; v0.1 rows must load unchanged): columns `wake_reason TEXT`,
  `rearm_of TEXT`, `evaluation_count INTEGER NOT NULL DEFAULT 0`,
  `armed_at REAL`. `arm`/`arm_deferred` set `armed_at`;
  `update_evaluation` increments `evaluation_count`; `claim` accepts and
  persists a wake reason.
- `conditions.py`: two new leaves, both *latching* (once true, stay true in
  stored state) — consumed log bytes and an observed idle edge cannot be
  re-observed, and non-latching leaves would flap under `all(...)`.
  `ObserverContext` gains `thread_observations: dict[str, Mapping | None]`
  filled by the service; condition evaluation stays synchronous and does no
  app-server I/O itself. Provide an AST walker returning `thread_idle`
  targets (works on raw and prepared forms). Defaults: ≤8 patterns,
  ≤512 chars each, 4 MiB read budget per poll, 4 KiB per-line match bound,
  journal ≤50 lines / 16 KiB with drop counters, stored matched lines
  truncated for storage.
- `service.py`: `register`/`defer` accept `rearm_of` (validated: exists and
  terminal) inside `semantic` so idempotent replays must repeat it; both
  reject a `thread_idle` leaf naming the monitor's own target thread; both
  pre-read each `thread_idle` child through the already-open app-server
  session and inject summaries into the observer context before
  `prepare_condition`. `reconcile_once` fetches child observations (deferred:
  in the same session as the idle preflight; legacy monitors with
  `thread_idle` leaves fetch too) and passes the populated context into
  evaluation. Deferred routing per the wake policy and the three error-path
  dispositions above. `_finish_claim`: skip the authorization gate and the
  `expired_before_activation` cut for deferred rows, discriminating on
  `mode == deferred` and never on the stored wake reason — a deferred row
  claimed by v0.1 code migrates with a `NULL` reason and must not re-enter
  the gate (that would resurrect the stranding on daemon restart). A `NULL`
  reason affects only the `FIRED` label, reconstructed from the witness. `recover_after_daemon_start` needs no new pass:
  `ARMED`/`CLAIMED` rows survive restart and the first `reconcile_once`
  applies the policy — the restart tests assert exactly this.
- `mcp_server.py`: `rearm_of: str | None = None` on both registration tools;
  keep tool descriptions short.
- `skills/wake-me-up/SKILL.md` and module `README.md`: expiry now wakes
  (drop the `any(condition, time)` backstop guidance); log-watch coverage
  rule with failure signatures; wake ≠ success — the witness decides; first
  post-wake action is exactly one `wake_me_up_status` call; re-defer loop
  with `rearm_of`; `thread_idle` for subagent waits.
- Estimated avoided-poll-turn count: wait duration divided by the repo's
  180 s long-poll yield floor.
- Runtime: run tests with `conda run -n ms python -m pytest`; note the base
  suite state per the benchmark-verification doctrine (diff failure sets,
  never compare pass counts). Re-run
  `openspec validate add-wake-me-up-event-wakes --strict` after any doc
  edit.

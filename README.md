# Codex Wake Me Up

`codex-wake-me-up` is a source-owned, host-local, one-shot monitor for one exact
Codex task. It observes a typed external condition on the same host, records
durable evidence, and makes at most one delivery attempt through an explicitly
selected delivery kind.

The primary `wait_for_event` path is goal-independent. Codex binds registration
to the calling task through trusted MCP metadata. A root caller queues one
bounded, self-identifying pointer in its own durable FIFO; a spawned V2 subagent
routes that pointer to its topmost root main-thread. The full wake evidence and
origin stay in monitor status. The plugin wakes the root only: it never resumes,
recreates, follows up, or accepts the subagent. `defer_goal_until_event` is the
optional legacy path for callers that explicitly want to pause and later
reactivate an eligible goal.

Its purpose is to remove the token cost of waiting. An agent that would
otherwise poll, sleep, or burn long-yield turns instead arms one monitor and
ends its turn; the daemon owns the wait.

## Contract and authority

- `openspec/specs/` owns the normative behavior contract:
  [`codex-wake-me-up-monitor`](openspec/specs/codex-wake-me-up-monitor/spec.md)
  (registration, conditions, lifecycle, wake report, lineage) and
  [`codex-goal-self-defer`](openspec/specs/codex-goal-self-defer/spec.md)
  (the best-effort self-defer flow and its terminal-wake policy).
- `openspec/changes/archive/` is history and forensic evidence — never current
  authority. It holds each shipped change's proposal, design, tasks, and its
  verification record, including the frozen state-machine diff and the receipts
  from the live app-server smoke.
- This README is the operator surface. `skills/wake-me-up/SKILL.md` is the
  packaged agent-facing guidance.

## Design stance

It is intentionally conservative:

- It never opens a TCP listener, runs a raw shell predicate, targets another
  host, steers an active turn, or starts a second app-server/Core writer.
- Thread delivery composes only the experimental per-thread queue plus exact
  `thread/resume` for a stored unloaded target. It never calls
  `thread/queue/start`, ordinary `turn/start`, steer, inject, Desktop task
  messaging, or `codex exec resume`.
- Primary MCP registration takes its task UUID only from Codex `_meta.threadId`.
  A spawned V2 caller is resolved through its exact `parentThreadId` chain to
  the topmost root. The root receives the wake and owns every later decision;
  no intermediate agent or child turn is started by the plugin.
- A thread monitor consumes its sole queue-add attempt before transport. Queue
  ACK, queue presence, durable history recording, and terminal uncertainty are
  distinct facts. The underlying queue is not exactly-once and the plugin never
  blindly re-adds after an uncertain response.
- A receipt with status `success` is the only authoritative task-success
  evidence. Time, GPU utilization, PID exit, tmux exit, log content, and
  another thread's idleness are resource/liveness signals, and continuing a
  goal from them requires `allow_heuristic_continuation: true`.
- Goal delivery still requires the target to be locally loaded, `idle`, and own
  the captured paused goal immediately before its one activation attempt.
- At most one durable trigger claim and one activation request per monitor,
  whatever caused the wake.
- `wake_me_up_defer` is **best effort**: a generic MCP tool receives a supplied
  task ID, not an authenticated binding to its calling Codex task. Use it only
  for the current task; it refuses uncertainty rather than retrying a pause or
  compensating for one.

## Source setup

```bash
cd codex-wake-me-up
python -m pip install -e .
codex-wake-me-up doctor --thread-id <loaded-thread-id>
```

The source plugin manifest is `.codex-plugin/plugin.json`; installation is an
explicit operator decision. The daemon stores only runtime state under
`$CODEX_HOME/runtime/codex-wake-me-up/`.

## MCP tools

`wait_for_event` is the primary operation. It has no model-supplied `thread_id`;
Codex supplies the exact caller UUID out of band. It works regardless of whether
the caller's goal is null, active, paused, or blocked:

```json
{
  "condition": {"type": "time", "after_seconds": 1800},
  "expires_in_seconds": 7200,
  "idempotency_key": "after-training-idle-v1"
}
```

For a spawned V2 subagent, registration records the child as
`origin_thread_id` and the topmost root as the delivery `thread_id`. When the
event fires, only the root FIFO is woken. The root inspects status and decides
whether any child follow-up is useful. The CLI `wait-for-event` command remains
an explicit same-host targeting surface for operators.

Registration authorizes the later billed task turn and, for a stored unloaded
target, an unconditional exact-thread resume that can release user-authored FIFO
items ahead of the monitor pointer. Their identities and count are persisted
before resume. The pointer itself is small, names the origin task UUID, and tells
the root to inspect status once; it is not a success claim.

`defer_goal_until_event` takes the same monitor fields plus the legacy heuristic
authorization for an explicit active or paused goal. `wake_me_up` and
`wake_me_up_defer` remain compatibility aliases for paused and active goal
delivery respectively; neither alias creates a goal or selects thread delivery.

`wake_me_up_status` returns the captured guard, evidence, outcome, timestamps,
and `supervision` (`healthy` or `unsupervised`). `wake_me_up_cancel` cancels a
registering, armed, or claimed monitor; it cannot undo an activation already
recorded as `activating`, nor compensate for a possibly delivered pause.
`wake_me_up_publish_receipt` writes a monitor's receipt atomically.

The MCP request timeout is only a control-call bound. Once registration returns
an `armed` durable receipt, the detached daemon owns observation and delivery
until the condition, expiry, cancellation, or a typed failure terminates it.
Blocking an MCP/CLI call for the whole wait would couple monitor lifetime to a
client transport and is not a delivery architecture.

## Terminal events and worker delivery

For a command or delegated worker that can publish a bounded terminal event,
the operator/agent workflow is:

1. Reserve one expiring, single-use event capability before starting the
   producer.
2. Hand the publish capability to the existing command wrapper or worker
   harness. The plugin does not launch commands, execute raw shell predicates,
   or replace the harness.
3. Launch the command or worker through that existing harness.
4. Bind the reservation while registering a monitor (or while performing the
   existing watcher-first defer). A producer may publish before binding; the
   reservation retains the event until it is bound or expires.
5. End the lead's turn after a successful bind/defer. The lead is not billed a
   continuation merely for reserving or binding; any resulting guarded wake is
   a real continuation and may spend tokens or other model budget.
6. The producer may send bounded heartbeats and then publish exactly one
   terminal event through the handed capability. Publication is producer-owned;
   it is not a command launched by the plugin.
7. When the monitor wakes, the lead makes one status call and independently
   reviews any worker candidate before deciding whether to integrate it.

The frozen producer surfaces are:

- CLI: `event-reserve --payload`, `event-status --reservation-id`,
  `event-cancel --reservation-id`, `event-heartbeat --payload`, and
  `event-publish --payload`.
- MCP: `wake_me_up_event_reserve`, `wake_me_up_event_status`,
  `wake_me_up_event_cancel`, `wake_me_up_event_heartbeat`, and
  `wake_me_up_event_publish`.

Reserve, heartbeat, and publish accept only a path to a current-user-owned,
mode-`0600`, regular JSON file of at most 64 KiB; symlinks are refused. Never
put a publish token in CLI arguments. A command reserve payload can be as small
as:

```json
{
  "kind": "command_terminal",
  "expires_in_seconds": 7200,
  "idempotency_key": "train-command-20260820",
  "producer_identity": "existing-shell-wrapper",
  "publisher_descriptor_path": "/private/runtime/train-command.publisher.json"
}
```

The first creation response is the only response containing the raw token;
idempotent replay returns the same reservation identity and fingerprint but
cannot reissue the bearer. Capture the first response or use the optional
mode-`0600` publisher descriptor. A heartbeat/publish payload wraps that
capability rather than passing it on the command line:

```json
{
  "reservation_id": "<reservation-id>",
  "publish_token": "<publish-token>",
  "terminal_event": {
    "kind": "command_terminal",
    "status": "succeeded",
    "command_label": "focused-test",
    "command_digest": "<64-lowercase-hex>",
    "exit_code": 0
  }
}
```

Native subagents and HarnessDock workers use the same `worker_terminal`
envelope through `publish_worker_terminal_from_descriptor`; this is a narrow
publisher adapter, not another watcher, callback scheduler, or wake claimant.

`command_terminal` and `worker_terminal` are distinct condition leaves and
claims:

- A `command_terminal` event reports one command outcome: `succeeded`,
  `failed`, `cancelled`, or `signaled`, with bounded execution evidence. Even
  exit code 0 proves only that bounded command process outcome.
- A `worker_terminal` event reports `delivered`, `blocked`, `failed`, or
  `cancelled`. `delivered` names one candidate commit and receives read-only
  attestation against the reservation's repository, worktree, baseline, and
  allowed paths. The candidate is a delivery for independent lead review, not
  acceptance. In particular, `git commit` exit 0 is not delivery acceptance.

Every command terminal outcome wakes when bound, including succeeded, failed,
cancelled, and signaled. Every worker terminal outcome also wakes when bound,
including blocked, failed, cancelled, and a delivered event whose candidate is
missing, baseline-
mismatched, out of scope, or otherwise has an invalid/error attestation. An
invalid delivery must wake the lead so it can handle the evidence; it must not
be silently converted to success or stranded until expiry. The plugin never
reviews, stages, commits, merges, cherry-picks, reverts, pushes, or watches Git
refs, worktrees, or unrelated commits.

Heartbeats are bounded liveness hints, not progress turns. `heartbeat_stale`
is heuristic: it says that accepted heartbeats stopped advancing under the
declared contract, not that the producer failed. It is false after terminal
publication and unknown when the required initial heartbeat or reservation
identity is unavailable. Apply the existing heuristic authorization policy to
it.

After the lead wakes, make exactly one status call for the monitor. It carries
the terminal outcome, producer identity, candidate and bounded attestation (if
any), heartbeat summary, and an explicit not-accepted marker. The lead then
independently reviews the candidate and decides whether any integration is
warranted. A terminal event authorizes the guarded wake, not task success,
lead acceptance, or user acceptance.

Cancellation or expiry of an unbound reservation is terminal and prevents
later binding or publication; a bound reservation remains bound and is never
reassigned after monitor cancellation, expiry, pause failure, or activation
failure. Event and monitor lifecycle still converge on one durable trigger
claim and at most one guarded wake attempt per monitor, with no retry or
automatic re-arm. The existing cancellation, expiry, fail-closed, and legacy
`receipt_success`/defer rules remain in force.

Publish tokens and raw command arguments are never placed in status or logs;
use bounded, redacted evidence and token fingerprints only. Cancellation,
expiry, and daemon restart are recovery boundaries, not permission to reuse a
capability or publish a second terminal event.

No install, plugin cutover, daemon restart, live app-server smoke, paid/live
continuation, or material command/worker/model spend is implied by this
documentation. Those actions require their existing explicit operator
authorization and verification boundaries.

Rollback is producer-side: stop creating or publishing terminal-event
reservations and keep using the legacy monitor conditions. Existing legacy
rows remain readable and functional; the additive event ledger is retained for
forensic inspection and must not be deleted during rollback.

## Conditions

All conditions are typed objects, combined with `all` or `any`.

```json
{"type": "time", "after_seconds": 600}
```

```json
{"type": "gpu_stable", "devices": [0, 1], "max_utilization_percent": 5, "stable_for_seconds": 180}
```

```json
{"type": "pid_exit", "pid": 12345}
```

```json
{"type": "tmux_exit", "target_kind": "pane", "target": "%12", "socket": "/tmp/tmux-1000/default"}
```

For tmux the monitor captures the original server PID and pane/session ID; a
missing or restarted server is `unknown`, not completion. For PIDs it captures
the boot ID, start time, and UID, so PID reuse or a reboot is also `unknown`.
The same captured process in Linux state `Z`, `X`, or `x` is terminated even
while its `/proc` record awaits reaping; registration rejects a PID that is
already terminal. PID and tmux leaves remain liveness evidence, never task
success. Pass the tmux socket explicitly: the environment fallback belongs to
the registering service, and a dead pane that remains addressable is not a
satisfied `tmux_exit` target-removal condition.

### `log_pattern`

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

Tails one local file read-only. It captures the file's device, inode, and size
at arming and scans only bytes appended afterwards, so pre-existing content
never matches. A missing file is tolerated — the job may not have created it
yet — and is scanned from byte 0 once it appears. Rotation, truncation, or an
inode replacement reports `unknown`, never completion, because a replaced file
is a different observation target.

A watch MUST include the failure signatures you would act on. One that matches
only the success line stays silent through a crashloop, and silence is
indistinguishable from still-running. Matched lines accumulate in a bounded
journal that becomes the wake payload.

Reversible budgets: at most 8 patterns of 512 characters, 4 MiB read per poll,
the first 4 KiB of each line offered to the matcher, and a journal of 50 lines
/ 16 KiB with explicit drop counters. Exceeding a read or wall-clock budget
reports `unknown` with a `pattern_budget_exceeded` record — the scan still
advances its offset — rather than delaying every other monitor.

### `thread_idle`

```json
{"type": "thread_idle", "thread_id": "<child-thread-id>"}
```

Waits for another locally loaded Codex thread — typically a subagent the caller
launched — to end its turn, through the same local app-server read path. This
replaces long-yield polling on a child.

A child already idle at registration is **rejected**: that result belongs in
the current turn, not behind an expiry-length wait. Pass
`accept_already_idle: true` to override. An unloaded or missing child is
`unknown`, never `true` — disappearance is not completion. The witness carries
the child's runtime status, goal status, and usage snapshot. An ended turn is
not task success; read the child's actual output.

### `receipt_success`

```json
{"type": "all", "children": [{"type": "receipt_success"}, {"type": "pid_exit", "pid": 12345}]}
```

A monitor containing `receipt_success` gets a receipt path and random token in
its response. A job wrapper publishes one atomically:

```bash
codex-wake-me-up receipt --monitor-id <id> --token <token> --status success
```

This receipt is monitor-bound. An ordinary producer JSON that happens to carry
`exit_code: 0` is not visible to `receipt_success`; use a pre-reserved
`command_terminal` event, or observe that ordinary file with `log_pattern`
including both success and failure signatures.

`any(receipt_success, time)` does not bypass the heuristic policy. For a legacy
monitor a time-only witness ends as `satisfied_requires_authorization` unless
the operator opted in; for a deferred monitor the same witness wakes through
the guarded path labelled `unauthorized_evidence`, which is honest about making
no success claim. A true receipt lets either continue without the flag.

## Deferred waits

After launching a same-host job expected to block for at least 15 minutes, an
agent with no useful independent work arms a watcher and exits its turn in one
request:

```json
{
  "thread_id": "<current-loaded-thread-id>",
  "condition": {
    "type": "tmux_exit",
    "target_kind": "pane",
    "target": "%12",
    "socket": "/tmp/tmux-1000/default"
  },
  "expires_in_seconds": 14400,
  "allow_heuristic_continuation": true,
  "idempotency_key": "training-pane-defer-v1"
}
```

The service records intent, establishes a same-host daemon heartbeat, commits
one paused-status request, and arms only after a matching re-read. If watcher
readiness, pause delivery, or the returned goal marker is uncertain, it stops
with a visible terminal receipt rather than retrying. A deferred monitor waits
for the target runtime to be `idle` before evaluating anything, so an
already-true condition cannot wake the ongoing turn.

### Expiry wakes a deferred goal

Expiry, satisfaction without continuation authorization, and an irrecoverable
observer identity are all consumed through the *same* single guarded
activation, each recording a durable `wake_reason`:

| `wake_reason` | Meaning |
| --- | --- |
| `condition` | The condition was true and its witness authorized continuation. |
| `expired` | The deadline arrived first. No claim is made about the job. |
| `unauthorized_evidence` | Heuristic evidence fired without an explicit opt-in. Not a success claim. |
| `observer_failed` | The observer's captured identity can never become true again. |

Because expiry itself wakes, an `any(condition, time)` deadline backstop is
unnecessary and counterproductive: it spends the single wake earlier and loses
the reason. This applies to deferred monitors only — a legacy `wake_me_up`
monitor still ends `expired` or `satisfied_requires_authorization` as before,
and never has its goal changed by a deadline.

The guarantee is precisely: **an armed deferred monitor is woken at the latest
at its expiry, provided the daemon lives, the captured guard still matches, and
the target is observed `idle` at least once after expiry.** That third
condition is real, not theoretical — see [Known limits](#known-limits).

Guard violations, mis-targeting, cancellation, and any uncertainty after the
activation request stay fail-closed and terminal. A transient local read error
neither strands the goal nor burns its wake: the monitor records the evidence,
stays retryable, and falls back on expiry.

### The wake report

Every fired monitor's terminal receipt is self-describing, so one
`wake_me_up_status` call fully re-orients the woken agent: the wake reason, the
satisfying witness (or failure detail), the bounded matched-line
`journal_tail`, `armed_at`/`fired_at`, waited seconds, evaluation count, and an
estimate of how many polling turns the wait avoided at the 180-second
long-poll floor.

The stored condition keeps its own journal, but `status` and registration
responses elide it to `{lines_stored, dropped}`: `outcome.journal_tail` is the
sole line carrier, so post-wake context is spent once rather than twice.

### Re-arm lineage

Both registration tools accept an optional `rearm_of: <monitor-id>` naming the
terminal monitor a new one succeeds. It is validated (the reference must exist
and be terminal), stored, and surfaced as a `rearm_chain` in `status` and
`list`. Lineage is archival only: no guard, condition, authorization, or state
is inherited, and every safety check runs fresh.

Nothing re-arms automatically — each arm is a consent-carrying call by an agent
that just saw the previous wake's evidence. This wake → handle → re-defer loop
is the supported answer to "notify me every time"; multi-shot schedules are a
non-goal.

## Operations

An armed registration starts a detached same-host daemon owning a file lock and
heartbeat. If it is absent after a reboot or failure, `status` reports
`unsupervised` rather than claiming the monitor is healthy.

```bash
codex-wake-me-up doctor --thread-id <loaded-thread-id>
codex-wake-me-up list
codex-wake-me-up daemon --once     # one recovery + one reconcile pass, then exit
```

`reconcile` is diagnostic only; do not run it beside the daemon unless you have
verified that no daemon owns the runtime lock.

### Install and rollback

The plugin runs from an installed cache under `$CODEX_HOME/plugins/cache/`, and
its daemon holds a lock keyed to the runtime root. **After any install or
rollback, re-read the lock-holding daemon's `/proc` cmdline and `PYTHONPATH`,
then restart it.** A surviving daemon from the previous version keeps serving
new monitors with old code, which silently produces old behavior under a new
version label — and because a reinstall replaces the previous cache directory,
that daemon ends up executing from a path that no longer exists.

The installer copies the source tree **verbatim**: it honours neither
`.gitignore` nor `.codexignore` (verified 2026-08-18 on Codex Desktop 0.147.0),
so `openspec/`, `tests/`, `.serena/`, and Python caches all ship, about 2.9 MB
in total. This is inert rather than harmful — an installed plugin reads only
what its manifest declares, `skills/` and `.mcp.json` — but do not expect an
ignore file to shrink the package. `.codexignore` records the intended
exclusions in case the installer gains support.

Reverting the cachebuster string alone is not a code rollback: the marketplace
installs from the source tree, so a rollback is (1) restore the source to the
previous commit, (2) restore the previous version string, (3) reinstall,
(4) restart the lock-holding daemon. Terminal monitor rows are forensic
evidence and are never replayed, so no ledger rollback is required.

Before step (1), run the compatibility preflight from the still-current source:

```bash
codex-wake-me-up event-compatibility-check --supported-event-epoch 0
```

Replace `0` with the target source's declared epoch. The check refuses while a
nonterminal event monitor still requires the current evaluator, even if its
reservation row is missing or corrupt. Readiness separately requires Linux to
report the heartbeat PID as the kernel owner of `daemon.lock`; a matching PID
written into the file is insufficient. An older binary cannot contain a guard
added by this version, so running the preflight before source/cache replacement
is mandatory; completed event history does not block rollback and the additive
event table remains intact.

## Known limits

- **Thread queue delivery is not exactly-once.** Core can lose a pointer after
  queue deletion but before durable history, or replay it around a dispatch-time
  crash. The stable delivery ID makes duplicates refer to one immutable monitor;
  history then queue then 60 seconds of online absence reconciliation produces
  `recorded`, `delivery_modified`, or truthful `delivery_uncertain`. User delete,
  crash, archive, and storage loss can remain observationally indistinguishable.
- **The queue is shared user state.** Its observed capacity is 100. Existing
  items retain FIFO priority; users can edit, reorder, or delete the pointer; an
  interrupted task can stall dispatch indefinitely. The plugin reports those
  facts and never restores user text or promises a delivery deadline.
- **Stored-target resume can spend model budget.** Explicit thread registration
  authorizes exact-thread FIFO release. Resume may start user-authored items ahead
  of the pointer and cascade until the queue drains or a turn interrupts.

- **Read-to-write race.** The app-server protocol has no expected-goal
  compare-and-set. If a user replaces the goal after preflight but before the
  one request, the monitor records `mis_targeted_activation` with both goal
  markers, makes no compensating write, and never retries. Use a receipt and
  avoid editing the target goal near its trigger when the strongest guarantee
  matters.
- **A target stuck non-idle is never woken.** Every activation path requires
  the target thread to be observed `idle`. A thread parked in a non-idle state
  such as `systemError` never opens the idle barrier, so its armed deferred
  monitor sits past expiry and its goal stays paused. This is the same
  stranding the deferred-wake policy removes, reached through a stuck target
  rather than the plugin's own bookkeeping.
- **`goal.status` includes `blocked`.** Behavior fails closed on it — the guard
  accepts only `paused` and defer eligibility only `active` — but it is part of
  the real status vocabulary.
- **An `active` goal is a work order.** On a host running Codex Desktop, the
  goal runtime picks up an active goal on an idle thread and starts a real,
  billed turn to pursue it. Anything scripting against the local app-server
  should create fixture goals `paused`, use inert objective text, or budget the
  induced turns. A synthetic thread also cannot reach the `idle` state required
  for activation once a goal is attached, so wake paths cannot be exercised on
  one.

# Codex Wake Me Up

`codex-wake-me-up` is a source-owned, host-local, one-shot monitor for a loaded
Codex goal. It observes a typed external condition on the same host, records
durable evidence, and makes at most one guarded
`thread/goal/set(status: "active")` request.

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

- It never opens a TCP listener, runs a raw shell predicate, starts a new turn,
  reloads an unloaded task, or targets another host.
- Continuation is the native guarded goal-status write only. There is no
  `turn/start`, no `thread/resume`, and no synthetic user message.
- A receipt with status `success` is the only authoritative task-success
  evidence. Time, GPU utilization, PID exit, tmux exit, log content, and
  another thread's idleness are resource/liveness signals, and continuing a
  goal from them requires `allow_heuristic_continuation: true`.
- The target must be locally loaded, `idle`, and still own the captured goal
  immediately before activation. An activation attempt is never retried.
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

`wake_me_up` captures a target's already-paused goal and arms a monitor:

```json
{
  "thread_id": "<loaded-thread-id>",
  "condition": {"type": "time", "after_seconds": 1800},
  "expires_in_seconds": 7200,
  "allow_heuristic_continuation": true,
  "idempotency_key": "after-training-idle-v1"
}
```

`wake_me_up_defer` takes the same fields for a loaded **active** goal and runs
the watcher-first, pause-once flow below. It is a separate tool so existing
`wake_me_up` callers keep the explicit paused-goal precondition.

`wake_me_up_status` returns the captured guard, evidence, outcome, timestamps,
and `supervision` (`healthy` or `unsupervised`). `wake_me_up_cancel` cancels a
registering, armed, or claimed monitor; it cannot undo an activation already
recorded as `activating`, nor compensate for a possibly delivered pause.
`wake_me_up_publish_receipt` writes a monitor's receipt atomically.

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
  "condition": {"type": "tmux_exit", "target_kind": "pane", "target": "%12"},
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

## Known limits

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

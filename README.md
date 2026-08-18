# Codex Wake Me Up

`codex-wake-me-up` is a source-owned, host-local one-shot monitor for a
loaded Codex goal. It observes a typed external condition on the same SSH host,
records durable evidence, and makes at most one guarded
`thread/goal/set(status: "active")` request.

It is intentionally conservative:

- It never opens a TCP listener, runs a raw shell predicate, starts a new turn,
  reloads an unloaded task, or targets another host.
- GPU utilization, tmux target exit, PID exit, log content, another thread's
  idle turn, and time are resource/liveness evidence—not proof that a job
  succeeded. They require `allow_heuristic_continuation: true` before they can
  continue a goal.
- A receipt with status `success` is the initial authoritative task-success
  condition.
- The target must be locally loaded, `idle`, and still own the captured paused
  goal immediately before activation. The runtime never retries an activation
  attempt.
- `wake_me_up_defer` is **best effort**: a generic MCP tool receives a supplied
  task ID, not an authenticated binding to its calling Codex task. It must be
  used only for the current task, and it refuses uncertainty rather than
  retrying a pause or compensating for one.

The current Codex app-server protocol has no expected-goal compare-and-set.
There is therefore a tiny read-to-write race: if a user replaces the goal
after preflight but before the one request, the monitor records
`mis_targeted_activation` with both goal markers. It makes no compensating
write and never retries. This source module implements the explicitly selected
best-effort automatic policy; use a receipt and avoid editing the target goal
near its trigger when the strongest guarantee matters.

## Automatic defer for a long dependent wait

After launching a same-host job expected to block for at least 15 minutes, an
agent with no useful independent work can arm a watcher and exit its turn in
one request:

```json
{
  "thread_id": "<current-loaded-thread-id>",
  "condition": {"type": "tmux_exit", "target_kind": "pane", "target": "%12"},
  "expires_in_seconds": 14400,
  "allow_heuristic_continuation": true,
  "idempotency_key": "training-pane-defer-v1"
}
```

Call this through `wake_me_up_defer`, then report the monitor receipt and end
the turn. The service records intent, establishes a same-host daemon heartbeat,
commits one paused-status request, and arms only after a matching re-read. If
watcher readiness, pause delivery, or the returned goal marker is uncertain,
it stops with a visible terminal receipt: it does not retry or change the goal
again. A deferred monitor also waits for the target runtime to be `idle` before
it evaluates a condition, so an immediately true condition cannot wake the
ongoing turn.

For a goal the user already paused, use `wake_me_up` instead. The legacy tool
does not pause a goal itself.

## Expiry wakes a deferred goal

A deferred monitor that reached `armed` carries one guarantee: its goal is
woken at the latest at its expiry, for as long as the daemon lives and the
captured guard still matches. Expiry, condition satisfaction without
continuation authorization, and an irrecoverable observer identity are all
consumed through the *same* single guarded activation, each recording a
durable `wake_reason`:

| `wake_reason` | Meaning |
| --- | --- |
| `condition` | The condition was true and its witness authorized continuation. |
| `expired` | The deadline arrived first. No claim is made about the job. |
| `unauthorized_evidence` | Heuristic evidence fired without an explicit opt-in. Not a success claim. |
| `observer_failed` | The observer's captured identity can never become true again. |

Because expiry itself wakes, an `any(condition, time)` deadline backstop is
unnecessary and counterproductive—it spends the single wake earlier and loses
the reason. This applies to deferred monitors only: a legacy `wake_me_up`
monitor still ends `expired` or `satisfied_requires_authorization` exactly as
before, and never has its goal changed by a deadline.

Guard violations, mis-targeting, cancellation, and any uncertainty after the
activation request remain fail-closed and terminal. A transient local read
error neither strands the goal nor burns its wake: the monitor records the
evidence, stays retryable, and falls back on expiry.

## The wake report

Every fired monitor's terminal receipt is self-describing, so one
`wake_me_up_status` call fully re-orients the woken agent: the wake reason,
the satisfying witness (or the failure detail), the bounded matched-line
`journal_tail`, `armed_at`/`fired_at`, the waited seconds, the evaluation
count, and an estimate of how many polling turns the wait avoided at the
180-second long-poll floor.

The stored condition keeps its own matched-line journal, but `status` and
registration responses elide it to `{lines_stored, dropped}`:
`outcome.journal_tail` is the sole line carrier, so post-wake context is spent
once rather than twice.

## Re-arm lineage

Both registration tools accept an optional `rearm_of: <monitor-id>` naming the
terminal monitor a new one succeeds. It is validated (the reference must exist
and be terminal), stored, and surfaced as a `rearm_chain` in `status` and
`list`. Lineage is archival only: no guard, condition, authorization, or state
is inherited, and every safety check runs fresh. Nothing re-arms
automatically—each arm is a consent-carrying call by an agent that just saw
the previous wake's evidence. This documented wake → handle → re-defer loop is
the supported answer to "notify me every time"; multi-shot schedules are a
non-goal.

## Source setup

To use its CLI from the source tree, install its declared dependencies in the
intended Python environment:

```bash
cd /data/CoordExp/codex-wake-me-up
python -m pip install -e .
codex-wake-me-up doctor --thread-id <loaded-thread-id>
```

Its source plugin manifest is `.codex-plugin/plugin.json`; normal plugin
installation remains an explicit operator decision. The daemon stores only
runtime state under `$CODEX_HOME/runtime/codex-wake-me-up/`.

## MCP tools

`wake_me_up` captures the target's current paused goal and arms a monitor:

```json
{
  "thread_id": "<loaded-thread-id>",
  "condition": {"type": "time", "after_seconds": 1800},
  "expires_in_seconds": 7200,
  "allow_heuristic_continuation": true,
  "idempotency_key": "after-training-idle-v1"
}
```

`wake_me_up_defer` accepts the same monitor fields for a loaded active goal and
performs the watcher-first, pause-once flow above. It is intentionally a
separate tool so existing `wake_me_up` callers keep the explicit paused-goal
precondition.

`wake_me_up_status` returns the captured guard, evidence, outcome, timestamps,
and `supervision` (`healthy` or `unsupervised`). `wake_me_up_cancel` can cancel
an uncommitted/armed monitor; it cannot undo an activation attempt already
recorded as `activating`, or compensate for a possibly delivered pause.

## Conditions

All conditions are typed objects. Combine them with `all` or `any`:

```json
{"type": "time", "after_seconds": 600}
```

```json
{
  "type": "gpu_stable",
  "devices": [0, 1],
  "max_utilization_percent": 5,
  "stable_for_seconds": 180
}
```

```json
{"type": "pid_exit", "pid": 12345}
```

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

`log_pattern` tails one local file read-only. It captures the file's device,
inode, and size at arming and scans only bytes appended afterwards, so
pre-existing content never matches. A missing file is tolerated (the job may
not have created it yet) and scanned from byte 0 once it appears. Rotation,
truncation, or an inode replacement reports `unknown`—never completion—because
a replaced file is a different observation target. A watch MUST include the
failure signatures you would act on: one that matches only the success line
stays silent through a crashloop. Matched lines accumulate in a bounded
per-monitor journal that becomes the wake payload.

Reversible implementation budgets: at most 8 patterns of 512 characters each,
4 MiB read per poll, the first 4 KiB of each line offered to the matcher, and
a journal of 50 lines / 16 KiB with explicit drop counters. Exceeding a read
or wall-clock budget reports `unknown` with a `pattern_budget_exceeded`
record rather than delaying every other monitor.

```json
{"type": "thread_idle", "thread_id": "<child-thread-id>"}
```

`thread_idle` waits for another locally loaded Codex thread—typically a
subagent the caller launched—to end its turn, observed through the same local
app-server read path. A child that is already idle at registration is
rejected: that result belongs in the current turn, not behind an
expiry-length wait. Pass `accept_already_idle: true` to override. An unloaded
or missing child is `unknown`, never `true`: disappearance is not completion.
The witness carries the child's runtime status, goal status, and usage
snapshot. An ended turn is not task success—read the child's actual output.

```json
{
  "type": "tmux_exit",
  "target_kind": "pane",
  "target": "%12",
  "socket": "/tmp/tmux-1000/default"
}
```

For tmux, the monitor captures the original server PID and pane/session ID.
A missing or restarted server is `unknown`, not completion. For PIDs, it
captures the boot ID, PID start time, and UID; PID reuse or a reboot is also
`unknown`.

```json
{
  "type": "all",
  "children": [
    {"type": "receipt_success"},
    {"type": "pid_exit", "pid": 12345}
  ]
}
```

When a monitor includes `receipt_success`, its response includes a receipt
path and random token. A job wrapper can publish one atomically:

```bash
codex-wake-me-up receipt \
  --monitor-id <monitor-id> \
  --token <receipt-token> \
  --status success
```

`any(receipt_success, time)` does not bypass the heuristic policy. For a
legacy monitor, a time-only witness ends as `satisfied_requires_authorization`
unless the operator explicitly opted in; for a deferred monitor the same
witness wakes the goal through the guarded path labelled
`unauthorized_evidence`, which is honest about making no success claim. If
receipt success is true, either can continue without that flag.

## Operations

An armed MCP registration starts a detached same-host daemon. It owns a local
file lock and heartbeat. If it is absent after a reboot or failure, `status`
reports `unsupervised` rather than claiming the monitor is healthy.

```bash
codex-wake-me-up doctor --thread-id <loaded-thread-id>
codex-wake-me-up list
codex-wake-me-up daemon --once
```

`reconcile` is a diagnostic-only manual evaluation path; do not run it beside
the daemon unless you have explicitly verified that no daemon owns the runtime
lock.

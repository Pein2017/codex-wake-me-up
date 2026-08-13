# Codex Wake Me Up

`codex-wake-me-up` is a source-owned, host-local one-shot monitor for a
loaded Codex goal. It observes a typed external condition on the same SSH host,
records durable evidence, and makes at most one guarded
`thread/goal/set(status: "active")` request.

It is intentionally conservative:

- It never opens a TCP listener, runs a raw shell predicate, starts a new turn,
  reloads an unloaded task, or targets another host.
- GPU utilization, tmux target exit, PID exit, and time are resource/liveness
  evidence—not proof that a job succeeded. They require
  `allow_heuristic_continuation: true` before they can continue a goal.
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

`any(receipt_success, time)` does not bypass the heuristic policy: if only the
time leaf is true, it ends as `satisfied_requires_authorization` unless the
operator explicitly opted in. If receipt success is true, it can continue
without that flag.

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

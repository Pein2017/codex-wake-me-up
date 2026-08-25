---
name: wake-me-up
description: Use when a same-host GPU, tmux, PID, log-producing job, or local Codex thread will block the current task for at least 15 minutes and no useful independent work remains, especially when a root or subagent should become idle until an event.
---

# Wake Me Up

Use this when external work is about to launch or is already running, it is
expected to block for at least 15 minutes, and every remaining step depends on
it. Reserve a producer terminal event before launch when the harness supports
one; otherwise launch first and capture the strongest observable same-host
witness. Do useful independent work before ending the turn.

## Let root and subagents become idle

`armed` is the handoff barrier. The caller may end its turn only after the
registration response identifies the monitor, condition, expiry, origin, and
root delivery target. A failed or ambiguous registration is not a monitor: stay
active long enough to report the failure, and do not assume a later wake.

For a subagent that launched an independent long job:

1. Launch the job in tmux, a background worker, or another durable local
   process, and capture its exact PID, log, receipt, or thread identity. A
   foreground shell/tool call is not suspended by ending the model turn.
2. From that subagent, call `wait_for_event` once. Trusted MCP metadata binds
   the origin automatically, and the plugin resolves the topmost root as the
   sole wake target.
3. After an `armed` response, the subagent reports the monitor receipt and ends
   its turn. If root is inside `wait_agent`, that completion wakes the wait;
   root records the receipt and also ends its turn. Root MUST NOT issue another
   `wait_agent` merely to cover the monitored interval.
4. The external job and daemon continue while root and intermediate subagents
   are idle. A no-subscriber idle root may be unloaded from memory without
   recursively cancelling a live child or deleting durable task history.
5. When the condition fires, the plugin queues one pointer to root only. Root
   calls `wake_me_up_status` once, inspects the real result, and decides whether
   to continue itself, call `followup_task` for an idle subagent, or stop.

If the long work is itself an active Codex child turn, its parent or root may
instead monitor `thread_idle` after that child is confirmed active. Ending the
parent or root turn does not end the active child. Child idle is only a
lifecycle witness, never proof that the work succeeded.

Keep these lifecycle boundaries distinct:

| Action | Monitor | Active subagent or external job |
| --- | --- | --- |
| Root/subagent ends its current turn | Continues | Continues |
| Idle root is unloaded while app-server stays alive | Continues | Live child/job continues |
| `interrupt_agent`, `close_agent`, or explicit shutdown targets the worker | Continues unless cancelled separately | Targeted work stops |
| Codex/app-server process exits | Host daemon may remain, but cannot deliver until service returns | In-flight Codex turns do not keep computing; independently launched host jobs may continue |

This is dormant orchestration, not nested waiting: model turns go idle and the
daemon owns observation. Never describe the flow as "monitor wakes the child,
then child releases root's wait"; a subagent-originated wake targets root, and
root alone chooses any child continuation.

1. Call `wait_for_event` once with a stable `idempotency_key`, one typed
   condition, and bounded expiry. Do not supply a task ID: Codex binds the call
   to the current task through trusted MCP metadata. This is the default whether
   the task goal is null, active, paused, or blocked. Never call `create_goal` or
   mutate a goal to satisfy monitoring.

   ```json
   {
     "condition": {"type": "time", "after_seconds": 900},
     "expires_in_seconds": 3600,
     "idempotency_key": "job-wait-v1"
   }
   ```
2. A root caller targets its own FIFO. A spawned V2 subagent is resolved through
   its exact parent chain and targets only the topmost root main-thread. Status
   records both origin and delivery target. The plugin wakes the root only; it
   never calls `followup_task`, resumes or recreates the child, or decides what
   the root should do. The root owns that judgment.
3. Use `defer_goal_until_event` only when an eligible active or paused goal
   explicitly needs pause/reactivation semantics. `wake_me_up` and
   `wake_me_up_defer` are legacy goal aliases, never fallbacks for unavailable
   thread delivery. No operation creates a goal.
4. Choose the strongest condition that is actually bindable for this producer:
   a pre-reserved `command_terminal`/`worker_terminal` event; a monitor-bound
   `receipt_success`; the producer's own completion log or ordinary receipt via
   `log_pattern`; `any(log_pattern, pid_exit(actual producer))`; and only then a
   bare `pid_exit`. Use `tmux_exit` only to observe removal of the exact tmux
   target, not as proof that its pane process terminated. Thread delivery may
   carry any typed witness without upgrading it to success. Goal delivery still
   requires deliberate heuristic authorization.
5. Report the monitor ID, condition, delivery kind, and expiry, then end the
   current turn. The daemon owns observation after the prompt registration
   response. A 30-second MCP timeout is not the condition lifetime.

If trusted caller identity, ancestry, experimental queue capability,
exact-target read, daemon source/epoch, or root direct-input preflight fails,
report `ThreadDelivery`
unavailable. Do not create a goal, choose a legacy alias, start a CLI-resume
process, or substitute ordinary turn start, steer, inject, Desktop messaging,
`thread/queue/start`, automatic subagent follow-up, or another app-server/Core
writer.

The pointer queue is shared user state and not exactly-once. The plugin makes
one queue-add attempt, then reconciles exact history, exact queue, and 60 seconds
of online absence. User edit/delete/reorder, interruption, queue capacity, crash,
archive, and storage ambiguity remain visible; never re-add blindly or claim a
delivery deadline. A relayed pointer names its child origin so the root can
distinguish it before the status read.

## Producer terminal events

When the existing command or worker harness supports a terminal-event
publisher, reserve its expiring, single-use capability before launch, hand the
capability to that harness, launch through the harness, and then bind it in the
monitor registration or in `wake_me_up_defer`. A fast producer may publish
before binding; if binding commits before the pre-bind deadline, it retains
that immutable event for the monitor lifecycle. End the lead turn immediately
after a successful bind/defer. The producer may send bounded heartbeats and
then publish exactly one terminal event; after the wake, make one status call
and independently review any worker candidate.

Use the frozen MCP operations `wake_me_up_event_reserve`,
`wake_me_up_event_status`, `wake_me_up_event_cancel`,
`wake_me_up_event_heartbeat`, and `wake_me_up_event_publish`. Reserve,
heartbeat, and publish take only a path to a current-user-owned mode-`0600`
regular JSON file of at most 64 KiB; never place the token in a CLI/MCP
argument. The equivalent CLI commands are `event-reserve --payload`,
`event-status --reservation-id`, `event-cancel --reservation-id`,
`event-heartbeat --payload`, and `event-publish --payload`.

The raw publish token is available only on first creation. Capture that first
response or request a mode-`0600` `publisher_descriptor_path`; an idempotent
retry returns identity and fingerprint, never the bearer again. Native
subagents and HarnessDock workers may call the reference
`publish_worker_terminal_from_descriptor` adapter with the same
`worker_terminal` envelope. The plugin hands this capability to an existing
harness; it never launches commands, runs raw shell predicates, becomes a job
scheduler, or adds another wake claimant.

Keep the two condition leaves and their meanings separate:

- `command_terminal` covers `succeeded`, `failed`, `cancelled`, and `signaled`.
  Exit code zero is only a bounded command process outcome.
- `worker_terminal` covers `delivered`, `blocked`, `failed`, and `cancelled`.
  `delivered` carries one candidate commit for review. `git commit` exit 0 is
  not delivery acceptance, and even valid Git attestation is not lead or task
  acceptance.

All worker terminal outcomes wake when bound. Invalid, missing, baseline-
mismatched, out-of-scope, or errored delivery attestation still wakes the lead
with the failure evidence; never treat it as success or wait for expiry. The
plugin performs no Git watcher, merge, cherry-pick, revert, stage, commit, or
push operation.

`receipt_success` is also plugin-bound: registration creates its monitor path
and random token, which the producer must receive before it can publish. An
ordinary job JSON containing `exit_code` is not that receipt. If an already-
running producer will create or append an ordinary receipt or log, monitor it
with `log_pattern` and include both success and failure signatures.

Use `heartbeat_stale` only as heuristic liveness evidence. It means accepted
heartbeats stopped advancing under the declared interval, not that a worker
failed. It is not a progress stream and is unknown when the required initial
heartbeat or identity is unavailable. The normal heuristic authorization and
`unauthorized_evidence` rules still apply.

After a wake pointer or guarded goal activation, call `wake_me_up_status`
exactly once. Independently review the
returned worker candidate and attestation before deciding whether to integrate
anything. A terminal event authorizes the guarded wake, never acceptance.

Cancellation or expiry makes an unbound reservation unusable; a bound
reservation cannot be rebound or reused after monitor cancellation, expiry,
pause failure, activation failure, or daemon restart. Preserve the exact
at-most-once claim/activation behavior and do not retry or automatically
re-arm. Keep publish tokens, raw command arguments, and sensitive status data
redacted; use bounded evidence and fingerprints.

Reserving/binding does not authorize installation, plugin cutover, daemon
restart, live or paid continuation, or material command/worker/model spend.
Those remain explicit operator actions with their existing verification and
restart boundaries.

Before an authorized rollback to an older event epoch, run the still-current
binary's `event-compatibility-check --supported-event-epoch <target>` and stop
if it refuses. A nonterminal event monitor remains a refusal even when its
reservation row is missing or corrupt; completed event history alone does not.
Never assume the older binary can enforce a compatibility guard that did not
yet exist.

## Expiry itself wakes a deferred goal

A deferred monitor that armed successfully carries one guarantee: the goal is
woken at the latest at its expiry, as long as the daemon lives, the target
guard still holds, and the target is observed idle at least once after expiry.
That last condition is what the idle barrier waits for: a thread stuck in a
non-idle state (for example `systemError`) is never woken, so treat a monitor
still `armed` well past its expiry as a stuck target, not a slow one.

Do **not** wrap a condition in `any(condition, time)` as a deadline backstop —
that only spends the single wake earlier and loses the reason. The terminal
receipt records why it woke: `condition`, `expired`, `unauthorized_evidence`,
or `observer_failed`.

## Cover failure signatures in every log watch

A `log_pattern` watch MUST include the failure signatures you would act on,
not only the success line. A success-only watch stays silent through a
crashloop, and silence is indistinguishable from still-running.

```json
{
  "type": "log_pattern",
  "path": "/abs/path/to/train.log",
  "patterns": [
    {"name": "ready", "regex": "Ready in [0-9.]+s"},
    {"name": "crash", "regex": "Traceback|CUDA out of memory|Killed|FAILED"}
  ]
}
```

Only bytes appended after arming are scanned, and a rotated or truncated file
becomes `unknown`, never a completion. Compose with `any(log_pattern,
pid_exit)` so a silent exit is covered too, and target the actual producer PID
rather than a tmux wrapper shell when that distinction is available. Prefer
local paths.

Typed composition always uses `children`:

```json
{
  "type": "any",
  "children": [
    {
      "type": "log_pattern",
      "path": "/abs/path/to/job-receipt.json",
      "patterns": [
        {"name": "success", "regex": "\"exit_code\"\\s*:\\s*0"},
        {"name": "failure", "regex": "\"exit_code\"\\s*:\\s*[1-9][0-9]*|Traceback|Killed|FAILED"}
      ]
    },
    {"type": "pid_exit", "pid": 12345}
  ]
}
```

`pid_exit` is liveness evidence. It treats the same captured process in Linux
state `Z`, `X`, or `x` as terminated even while `/proc/<pid>` remains until a
parent reaps it; stopped or sleeping states remain alive. Registration rejects
a process that has already terminated. This witness still does not prove a
successful command, complete process tree, or valid artifact.

Always pass an absolute tmux socket explicitly. The `$TMUX` fallback belongs to
the registering service environment, not necessarily the caller's terminal.
`tmux_exit` becomes true only after the captured pane/session target is no
longer addressable. A pane can be dead yet remain addressable, so do not rank
this condition above producer evidence or a corrected producer `pid_exit`.

## Wait on another local Codex thread

`{"type": "thread_idle", "thread_id": "<child-thread-id>"}` waits for a
locally loaded thread — typically a subagent you just launched — to end its
turn. This replaces long-yield polling entirely. A child that is already idle
at registration is rejected: handle its result in the current turn instead.
Idle means the turn ended, not that the child succeeded; read its actual
output. Compose with `all(...)` to wait for several children.

## A wake is not a success claim

The witness decides, not the fact that you woke. Only a `receipt_success`
witness is authoritative task success; a log line, an idle child, an exited
PID, a freed GPU, and a reached deadline are all heuristic. A wake labelled
`unauthorized_evidence` means the evidence fired without your explicit
authorization — verify before acting on it as completion.

## After a wake: exactly one status call

Call `wake_me_up_status` once with the monitor ID and act on what it returns.
The report carries the wake reason, the satisfying witness or failure detail,
the matched-line `journal_tail`, and the wait statistics. Do not re-read the
log when the journal already answers, and do not re-verify a receipt wake.

For a per-occurrence loop, re-register after handling the event: a fresh
`wait_for_event` (or explicit goal defer when needed) with a new
`idempotency_key` and `rearm_of` set to the
monitor that just fired. Lineage is archival only — every safety check runs
fresh — and it makes the loop's cost auditable. There is no multi-shot
schedule and nothing re-arms automatically.

Fail closed: if caller identity, ancestry, capability, target eligibility,
watcher readiness, queue
admission, pause delivery, or goal identity is uncertain, do not retry,
compensate, switch delivery kinds, or arm automatically. Never steer an active
turn or target another task. A subagent-originated wake goes to root only; root
decides whether and how to continue the child.

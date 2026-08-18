---
name: wake-me-up
description: When a same-host GPU, tmux, PID, or log-producing job — or another local Codex thread — will block the current Codex task for at least 15 minutes and no useful independent work remains, arm one guarded host-local monitor instead of sleeping or polling. Prefer automatic best-effort self-defer with `wake_me_up_defer`; use `wake_me_up` only for an already paused goal.
---

# Wake Me Up

Use this only after the external job is launched, its same-host identity is
known, it is expected to block for at least 15 minutes, and every remaining
step depends on it. Do useful independent work first.

1. If the current goal is active, call `wake_me_up_defer` once with its task
   ID. It first records intent, verifies its host-local watcher is ready,
   pauses the captured goal, and arms only after confirming the same paused
   goal. This is best-effort targeting: generic MCP cannot authenticate that a
   supplied task ID is the caller, so never use another task's ID.
2. If the goal is already paused, call `wake_me_up` once instead. Do not use
   raw app-server commands, `turn/start`, a sleep loop, or a polling loop.
3. Choose one typed condition: prefer `receipt_success`; otherwise use
   `log_pattern`, `thread_idle`, `tmux_exit`, `pid_exit`, `gpu_stable`, or
   `time`. Everything except a successful receipt is heuristic, so set
   `allow_heuristic_continuation: true` deliberately.
4. Always use a bounded `expires_in_seconds` and stable `idempotency_key`.
   Report the monitor ID, condition, and expiry, then end the turn. The daemon
   owns waiting and will make at most one guarded continuation attempt.

## Expiry itself wakes a deferred goal

A deferred monitor that armed successfully carries one guarantee: the goal is
woken at the latest at its expiry, as long as the daemon lives and the target
guard still holds. Do **not** wrap a condition in `any(condition, time)` as a
deadline backstop — that only spends the single wake earlier and loses the
reason. The terminal receipt records why it woke: `condition`, `expired`,
`unauthorized_evidence`, or `observer_failed`.

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
pid_exit)` so a silent exit or a hang is covered too. Prefer local paths.

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

For a per-occurrence loop, re-defer after handling the event: a fresh
`wake_me_up_defer` with a new `idempotency_key` and `rearm_of` set to the
monitor that just fired. Lineage is archival only — every safety check runs
fresh — and it makes the loop's cost auditable. There is no multi-shot
schedule and nothing re-arms automatically.

Fail closed: if target eligibility, watcher readiness, pause delivery, or goal
identity is uncertain, do not retry, compensate, or arm automatically. A
deferred monitor holds while its task is still active, so an already-true
condition cannot wake it before this turn ends. Never call `turn/start` or
target another task.

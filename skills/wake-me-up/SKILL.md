---
name: wake-me-up
description: When a same-host GPU, tmux, or PID job will block the current Codex task for at least 15 minutes and no useful independent work remains, arm one guarded host-local monitor instead of sleeping or polling. Prefer automatic best-effort self-defer with `wake_me_up_defer`; use `wake_me_up` only for an already paused goal.
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
   `tmux_exit`, `pid_exit`, `gpu_stable`, or `time`. Tmux/PID/GPU/time are
   heuristic, so set `allow_heuristic_continuation: true` deliberately.
4. Always use a bounded `expires_in_seconds` and stable `idempotency_key`.
   Report the monitor ID, condition, and expiry, then end the turn. The daemon
   owns waiting and will make at most one guarded continuation attempt.

Fail closed: if target eligibility, watcher readiness, pause delivery, or goal
identity is uncertain, do not retry, compensate, or arm automatically. A
deferred monitor holds while its task is still active, so an already-true
condition cannot wake it before this turn ends. Never call `turn/start` or
target another task.

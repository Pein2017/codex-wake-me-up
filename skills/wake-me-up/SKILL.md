---
name: wake-me-up
description: Use when an ordinary native Codex worker should hand off asynchronously, or when same-host work will block the current task for at least 15 minutes and no useful independent work remains.
---

# Wake Me Up

Use this for an ordinary native Codex worker that should hand off asynchronously,
or when other same-host work will block for at least 15 minutes and no useful
independent work remains. The producer must keep running after the model turn ends:
use a native worker, tmux, a durable worker, another active Codex thread, or another
host-local process with a stable witness.

## Ordinary lifecycle

1. An existing launcher owns the durable producer. Before launch, reserve a
   terminal event when available; keep its private publisher descriptor with
   that launcher and retain the returned non-secret `monitor_condition`. A
   foreground tool call does not become durable merely because the turn ends.
2. Choose the strongest condition the producer can bind:
   `native_worker_terminal` for an ordinary spawned Codex worker;
   `command_terminal` or cooperative `worker_terminal`; monitor-bound `receipt_success`;
   the producer's success-and-failure log or receipt through `log_pattern`;
   `any(log_pattern, pid_exit(actual producer))`; then bare `pid_exit`.
   `tmux_exit`, GPU state, deadlines, and thread idle are weaker lifecycle or
   resource witnesses.
3. Launch through that existing launcher, then call goal-independent
   `wait_for_event` once with its returned typed condition, a bounded expiry,
   and a stable `idempotency_key`. Do not provide a task ID; trusted MCP metadata
   binds the current task and resolves a spawned child to its root delivery
   target. This arms a later wake; it does not join the producer.

```json
{
  "condition": {
    "type": "any",
    "children": [
      {
        "type": "log_pattern",
        "path": "/abs/path/to/job.log",
        "patterns": [
          {"name": "success", "regex": "DONE"},
          {"name": "failure", "regex": "Traceback|Killed|FAILED"}
        ]
      },
      {"type": "pid_exit", "pid": 12345}
    ]
  },
  "expires_in_seconds": 7200,
  "idempotency_key": "job-wait-v1"
}
```

4. Treat only a clear `armed` receipt as the handoff barrier. It identifies the
   monitor, compact condition binding, expiry, origin, root delivery target,
   delivery kind, supervision, and next action. If registration fails or is
   ambiguous, report it and remain active; do not assume a later wake.
5. After `armed`, report the monitor ID and end the current turn. “Do not wait”
   means do not synchronously join; it does not mean omit the asynchronous arm.
   Do not sleep, poll, or issue a long tool wait for the monitored interval; the
   daemon owns observation.
6. A delivered pointer is bounded routing data, not a result or success claim.
   Call `wake_me_up_status(monitor_id, view="decision")` exactly once. That one
   response contains the witness or observer failure, wait statistics, selected
   delivery facts, abnormal delivery diagnostics, and terminal-event evidence.
   Decide from it without calling an additional details endpoint.

For an ordinary spawned Codex worker, use
`{"type":"native_worker_terminal","task_name":"/root/worker"}` with the
canonical name returned by spawn. Core freezes its exact child thread and turn
invocation; completed, failed, and interrupted all wake as settlement,
never success. `bindPending` does not arm; remain active until an exact invocation
is observable. A later follow-up is a new invocation and needs a fresh monitor.

For a cooperative launcher or HarnessDock worker, reserve a `worker_terminal`
event first. A scoped worker may publish `delivered`; a settlement-only worker may
publish `completed` with no candidate, attestation, success, or acceptance. The
launcher preflights its private mode-0600 descriptor before work and later publishes
a separate mode-0600 event file; never put its bearer in arguments or output. A
worker that exits before this final publish is not observed as settled and waits
for expiry.

## Condition rules

- Every `log_pattern` must include both success and failure signatures. Only
  bytes appended after arming are scanned. Rotation, truncation, or replacement
  is observer failure, never completion.
- Point `pid_exit` at the actual producer, not a tmux wrapper when they differ.
  tmux is only a shell around that PID. PID and tmux exit prove liveness only; a
  zombie counts as terminated after its captured identity still matches.
- Pass an absolute tmux socket. `tmux_exit` observes removal of the captured
  pane/session target, not merely a dead pane process.
- Only a successful monitor-bound `receipt_success` is authoritative task
  success. A log match, deadline, idle child, exited PID, quiet GPU, command
  termination, or worker candidate is not whole-task or lead success.
- Wake reason may be `condition`, `expired`, `unauthorized_evidence`, or
  `observer_failed`. The wake itself never upgrades the evidence.
- `git_ref_change` is a one-shot, read-only local-ref observer. Its movement is
  progress evidence, not success; its details are in the terminal-events
  reference.

## Fixed boundaries

- Monitoring is one-shot. One trigger claim permits at most one queue admission
  attempt. Never retry delivery blindly, automatically re-arm, or reuse an old
  `idempotency_key` for changed semantics.
- Never create or mutate a goal to enable monitoring. Do not fall back to a
  legacy goal alias, CLI resume, steer, inject, ordinary turn start, Desktop
  messaging, a second app-server/Core writer, or automatic subagent follow-up.
- Do not install or switch plugins, restart a daemon, launch paid continuation,
  or authorize material producer/model spend without separate permission.
- Do not scrape a launcher's private runtime, scan processes to discover one,
  add shell/tmux adapters, or automatically re-arm after a wake.
- For a new occurrence after handling the wake, an agent may explicitly call a
  fresh `wait_for_event` with a new key and `rearm_of`; a prior thread monitor
  may already be `queue_accepted`, but remains nonterminal and is never resent.
  Nothing re-arms itself.
- Use one composed `all` monitor to wake after every worker settles. For
  continuing first-settlement control, arm independent single-leaf monitors;
  retain or cancel survivors explicitly. Typed `any` consumes losers.

## Read details only when needed

- Producer terminal events, private publish descriptors, heartbeat, cancellation,
  and rollback: [references/terminal-events.md](references/terminal-events.md)
- Queue admission, reconciliation, modified/uncertain delivery, and cancellation:
  [references/queue-reconciliation.md](references/queue-reconciliation.md)
- Spawned-subagent root routing, native settlement, and `thread_idle`:
  [references/subagents-and-thread-idle.md](references/subagents-and-thread-idle.md)
- Explicit legacy paused-goal delivery and its expiry/guard semantics:
  [references/legacy-goal-delivery.md](references/legacy-goal-delivery.md)

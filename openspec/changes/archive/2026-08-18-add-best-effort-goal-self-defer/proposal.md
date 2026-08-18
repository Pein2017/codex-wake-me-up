## Why

An agent that has launched a same-host GPU, tmux, or PID job currently has to
wait for a user to pause its goal before it can arm `codex-wake-me-up`. That
leaves an avoidable interactive step precisely when the agent has no useful
independent work. The existing monitor already has a conservative one-shot
activation path; this change adds a bounded way to defer into that path.

## What Changes

- Add a separate `wake_me_up_defer` MCP operation for an explicitly supplied,
  locally loaded active goal. It is documented as **best effort**, not as a
  proof that the supplied task is the caller's current task.
- Make the defer operation persist intent, prove the same-host watcher is
  ready, pause the captured goal once, re-observe the exact paused guard, and
  only then arm the existing one-shot continuation monitor.
- Add explicit fail-closed terminal outcomes for unavailable supervision,
  ambiguous pause delivery, pause rejection, and a changed goal guard. A
  restart never retries a pause whose delivery is uncertain.
- Hold a deferred monitor while the target runtime is still active so a
  condition that is already true cannot re-activate and promptlessly resume
  the goal before the current turn ends.
- Verify the existing guarded active-status update against the installed Codex
  goal runtime, which natively starts an idle active goal's continuation turn;
  do not add a second resume or synthetic turn-start path.
- Update the packaged skill and operator documentation so an agent may choose
  automatic defer only after a same-host wait of at least 15 minutes and only
  when no useful independent work remains.

## Capabilities

### New Capabilities

- `codex-goal-self-defer`: Durable, guarded best-effort deferral of an active
  host-local Codex goal into a typed one-shot wake-up monitor.

### Modified Capabilities

- None.

## Impact

- Affected source: `codex-wake-me-up/src/codex_wake_me_up/`, its MCP surface,
  durable SQLite ledger, daemon reconciliation, tests, README, and packaged
  `wake-me-up` skill.
- The change uses the existing experimental local Codex App Server
  `thread/goal/set` boundary only. It adds no scheduler, network listener,
  remote continuation, raw shell predicate, or `turn/start` behavior.
- Existing `wake_me_up` behavior remains available for a goal that is already
  paused.

## Why

Long-running GPU work on an SSH host often outlives the main Codex turn that
launched or is waiting on it. Today an operator must remember to return to the
right thread, determine whether the external work really reached its intended
terminal state, and manually reactivate the paused goal. That is fragile for
tmux- and PID-owned work, and it cannot be solved by a Codex lifecycle hook
because the external event does not itself produce a Codex tool event.

This change introduces a host-local, one-shot monitor that lets a main Codex
thread register an explicit future condition and conservatively continue its
own paused goal when the condition is satisfied. It makes the monitored fact,
the target identity, and the once-only outcome durable and inspectable.

## What Changes

- Add a standalone `codex-wake-me-up` module under the repository root with a
  durable monitor daemon, typed condition evaluation, a local Codex app-server
  actuator, and a small MCP/CLI control surface.
- Let a caller register a one-shot monitor against an exact target thread and
  a captured paused-goal guard. Initial condition leaves support absolute or
  relative time, GPU-utilization stability, tmux pane/session termination, and
  PID termination; durable job receipts are included as the authoritative
  completion path for a later or combined condition.
- Make condition evidence explicit. GPU utilization is a resource-state
  heuristic; PID and tmux predicates prove only their stated process/liveness
  fact; receipt success is the only initial task-success fact. Heuristic
  conditions require an explicit opt-in before they may activate a goal.
- Persist monitor registration, observer evidence, trigger claim, and terminal
  outcome under shared `CODEX_HOME` runtime state. A daemon restart rebuilds
  from durable state rather than trusting a missed filesystem or polling wake.
- Preflight activation only when a target is still loaded, idle, and guarded by
  the same paused goal. The v1 policy is conservative at-most-once: an
  unloaded target, changed/active goal, app-server error, unknown observer
  state, or uncertain activation produces an inspectable non-firing outcome
  rather than a retry or a new turn. Because the current app-server protocol
  has no expected-goal compare-and-set, the narrow read-to-write race is
  explicitly detected from the response and recorded as
  `mis-targeted-activation`, without a retry or compensating write.
- Provide focused unit tests plus a disposable-session smoke that proves a
  registered time or synthetic terminal condition can activate one test goal
  without touching an existing user session.

## Capabilities

### New Capabilities

- `codex-wake-me-up-monitor`: Durable, typed, one-shot monitoring and guarded
  continuation of an exact Codex main-thread goal on its owning host.

### Modified Capabilities

- None.

## Impact

- New source tree: `codex-wake-me-up/`, including its daemon, app-server
  adapter, MCP/CLI entrypoints, and focused tests.
- New runtime-only state root:
  `$CODEX_HOME/runtime/codex-wake-me-up/`; it is not a training, inference, or
  research artifact and must remain out of Git.
- Uses the existing host-local Codex app-server Unix control transport; it does
  not expose the control plane on TCP, modify Codex core, install a plugin into
  the active desktop session, or alter existing sessions during normal tests.
- Introduces a new optional local process for operators who arm monitors. It
  does not require GPUs, change model/data behavior, or alter existing
  CoordExp-Swift compatibility contracts.

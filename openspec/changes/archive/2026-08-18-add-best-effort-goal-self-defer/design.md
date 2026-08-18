## Context

See [proposal.md](proposal.md) and the
[`codex-goal-self-defer` specification](specs/codex-goal-self-defer/spec.md).
The current plugin can arm only a goal that is already paused. Its durable
ledger and daemon already make activation at most once, but daemon launch is
fire-and-forget and there is no pause lifecycle. The experimental local App
Server accepts a goal-status update but exposes neither an expected-goal CAS
nor an authenticated generic-MCP binding from a tool request to a Codex thread
identity.

## Goals / Non-Goals

**Goals:**

- Add a separate best-effort self-defer surface without weakening the existing
  paused-goal registration path.
- Keep pause and activation independently crash-conscious and observable.
- Prevent a condition already true at registration from waking a task before
  the agent has ended its current turn.

**Non-Goals:**

- Prove that a model-supplied task ID is the calling task, alter Codex core, or
  provide strict current-task authentication.
- Schedule work remotely, run a generic shell hook, use TCP, reload a task,
  start a turn, or automatically repair a paused goal after a failed defer.
- Treat GPU/PID/tmux liveness as task-success evidence, or change the existing
  receipt/heuristic authorization policy.

## Decisions

### Add a separate deferred-monitor mode

The MCP surface will add `wake_me_up_defer` rather than overload
`wake_me_up`. The existing operation retains its prerequisite—an already
paused goal—while the new operation requires a locally loaded active goal and
explicitly labels the target ID best effort. A durable mode flag on a monitor
distinguishes the activation barrier from legacy registrations and participates
in idempotency semantics.

Alternative considered: infer defer intent from a new optional parameter on
`wake_me_up`. That would make the existing safe precondition ambiguous and
could alter behavior for callers already using it.

### Persist an irreversible phase before every control-plane send

The deferred flow is:

```text
DEFER_INTENT
  --(watcher positively ready)--> PAUSING
  --(one pause request; matching response and re-observation)--> ARMED
  --(idle barrier + authorized condition)--> CLAIMED -> ACTIVATING -> terminal
```

`DEFER_INTENT` records the captured active goal and prepared condition before
any local control-plane action. `PAUSING` is committed before the one exact
`thread/goal/set({threadId, status: "paused"})` send. Any daemon restart sees
`DEFER_INTENT` as abandoned and `PAUSING` as pause-uncertain; neither state is
replayed. This mirrors the existing `ACTIVATING` no-retry rule.

Alternative considered: on restart, re-observe paused state and arm a monitor.
That cannot distinguish this operation's pause from a concurrent user or
agent action, so it silently expands authority after a crash.

### Use paused rather than blocked to defer the current turn

The defer action records `paused`, not `blocked`. Both statuses suppress normal
active-goal continuation, but setting either status does not interrupt a
currently executing Codex turn. The only App Server primitive that can force a
turn to stop is interruption, which could race the just-completed defer tool
result and create a less diagnosable lifecycle. The agent instead receives its
successful monitor receipt and ends the turn normally, without sleeping or
polling.

Alternative considered: set `blocked` to force exit. It is a durable
goal-status signal, not a safe process-termination primitive, so it offers no
stronger exit guarantee and would blur a task's semantic reason for being
blocked.

### Require positive daemon readiness before pause

The runtime helper will provide a bounded start-and-readiness check over the
existing local lock/heartbeat, with injectable seams for deterministic tests.
The defer operation waits only for this bounded local readiness confirmation;
if it cannot obtain one, it writes a terminal unavailable-supervision receipt
and sends no pause. Existing paused-goal registration keeps its historical
start behavior unless independently updated.

Alternative considered: retain fire-and-forget `Popen` before pause. A process
could exit immediately or never acquire its lock, leaving a paused task with no
watcher.

### Make confirmation stricter than pause response alone

The App Server adapter will expose a narrow guarded pause action, analogous to
the existing activation action. The service validates the returned marker and
then performs a fresh observation. Only a loaded, paused, exact captured
marker becomes `ARMED`. Response failure, mismatch, or wrong status becomes a
specific terminal record without compensation.

The App Server lacks an expected-goal CAS, so there remains a tiny read-to-send
race. It is made visible through the stored observations and never repaired by
another write.

### Use the native active-goal continuation path exactly once

The current local Codex goal runtime applies the runtime effect of
`thread/goal/set(status: "active")`: for an idle loaded task it invokes its
own `continue_if_idle` path and starts a goal-continuation turn with an
internal steering item. The monitor already makes this update only after an
exact paused-goal preflight and commits `ACTIVATING` before the one send.
Therefore no second task-control request is necessary or safe. The disposable
smoke must observe a new turn on the installed 0.147.0 runtime, rather than
infer it solely from the goal status.

Alternative considered: add `thread/resume` after setting the goal active.
That API can also emit the idle-goal lifecycle in current Codex, creating an
unnecessary second continuation trigger and a second irreversible RPC phase.
The selected path is the single existing status-only active update; it never
calls `thread/resume`, `turn/start`, or injects a synthetic user message.

### Gate deferred monitors before condition evaluation

Daemon reconciliation will preflight a deferred armed monitor before evaluating
its condition. Matching paused + active runtime is a no-op; matching paused +
idle proceeds through ordinary condition evaluation and the existing guarded
activation path. An unloaded or different target reaches the same conservative
terminal handling used by current monitors. This protects the normal agent
tool-call/turn-end ordering without changing behavior of legacy monitors.

Alternative considered: claim on a true condition and defer only activation.
That can turn a monitor terminal while the originating turn is still active,
making it unavailable when the task actually becomes idle.

### Keep cancellation and expiry non-compensating

Cancellation before the pause request and after arming works through normal
ledger CAS semantics. A pause in progress is intentionally not cancelled or
compensated because the write may already be delivered. Expiry terminates the
watcher only; it never changes goal status. In all such cases a user may choose
the next action after reading the durable receipt.

## Risks / Trade-offs

- **A supplied task ID can be misused by a model** → strict loaded-goal marker
  checks, one local host, explicit best-effort wording, and no claim of
  caller/task binding.
- **A local pause succeeds but confirmation is lost** → terminal
  `pause_uncertain` leaves the goal untouched thereafter; manual inspection is
  required rather than a potentially duplicate write.
- **Another App Server process shares the same runtime** → the plugin uses the
  target host's managed control socket and one local monitor daemon, but the
  App Server's active-goal continuation is process-sensitive; a smoke verifies
  the installed topology rather than claiming cross-process global exclusion.
- **Daemon readiness can race after heartbeat observation** → bounded positive
  check reduces the window but cannot prove future liveness; status continues
  to expose supervision state.
- **Schema migration hits existing monitor ledgers** → additive defaults must
  preserve legacy monitor behavior and focused migration tests cover old rows.
- **Polls consume small host resources** → only requested typed observers are
  polled, and the active-turn barrier avoids needless evaluation until idle.

## Migration Plan

1. Add additive ledger fields/states and preserve all existing paused-goal
   monitor rows as legacy mode.
2. Run focused unit tests, static/plugin validation, and a disposable local
   App Server smoke using only a newly created test task.
3. Review the frozen state-machine diff independently, repair accepted issues,
   then bump the plugin cachebuster and reinstall through the existing local
   marketplace.
4. Roll back by reinstalling the previous marketplace cachebuster; terminal
   runtime records remain as forensic evidence and are not replayed.

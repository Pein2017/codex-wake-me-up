## Context

See `proposal.md` for the operator problem and
`specs/codex-wake-me-up-monitor/spec.md` for the behavior contract. Codex
currently has an experimental app-server control plane on a user-local Unix
socket, but no native monitor that connects an external terminal/GPU event to
a paused goal. A Codex lifecycle hook cannot observe a job that finishes while
Codex is idle.

The implementation must preserve the target's identity as far as the current
protocol permits and avoid a duplicate continuation after a crash or an
ambiguous local control-plane response. The current app-server does not expose
an expected-goal compare-and-set, so the irreducible read-to-write race is
detected and reported rather than claimed away. It is also running beside
shared GPU work: monitoring must poll cheaply, create no GPU load, and never
kill or otherwise alter the observed workload.

## Goals / Non-Goals

**Goals:**

- Deliver a source-owned local plugin/module at `codex-wake-me-up/` whose MCP
  tools can arm, inspect, and cancel durable monitors.
- Keep the monitor daemon and its facts on the target session's owning SSH
  host, using only its local Unix app-server control socket.
- Make heuristic versus task-success evidence visible and require explicit
  authorization before heuristic evidence can change a goal.
- Make the one permitted activation attempt crash-safe and observable.

**Non-Goals:**

- Restarting, loading, migrating, or waking a Codex task on another host.
- Declaring a GPU becoming idle, a tmux target disappearing, or a PID exiting
  to be proof that a training job succeeded.
- Arbitrary shell hooks, remote agents, TCP listeners, multi-shot schedules,
  or generic job scheduling.
- Changing Codex core, the active Desktop plugin installation, or the named
  real target session during development tests.

## Decisions

### Use a small Python source module with a plugin-facing MCP process

`codex-wake-me-up/` will be a Python package plus a Codex plugin manifest.
It will expose `wake_me_up`, `wake_me_up_status`, and `wake_me_up_cancel` via
a stdio MCP server, and a matching CLI for `doctor`, `list`, `reconcile`, and
receipt publication. The MCP process registers work in durable storage and
ensures the host-local daemon is running; it does not remain responsible for
the wait.

Python is selected because the current `ms` environment already provides a
Unix-socket WebSocket client and an MCP server library, which keeps the
control-plane adapter small and independently testable. Runtime dependencies
will be declared by the module; no dependency is borrowed from the unrelated
`cc-plugin-codex` checkout. The manifest is source-owned but will not be
installed into the active Codex app as part of this change.

Alternative considered: a Node-only daemon and custom stdio MCP protocol.
That would avoid Python dependencies but would require implementing a Unix
WebSocket client and MCP transport manually, increasing the most
correctness-sensitive surface.

### Use SQLite as the durable monitor ledger, with one polling daemon

The runtime root is the canonical real path of
`$CODEX_HOME/runtime/codex-wake-me-up/` and contains a SQLite ledger, a daemon
PID/lock, heartbeat, and receipts. The MCP process, CLI, and
daemon resolve `CODEX_HOME` once (environment value or the standard fallback),
canonicalize it with `realpath`, and persist the result. They fail closed if it
does not match the app-server's initialized host root. Monitor registration
writes an immutable canonical JSON specification plus a captured target guard.
Evidence, state transitions, trigger claim, and activation receipt are written
in transactions. The daemon selects due monitors and polls only their typed
observers; it never scans unrelated processes.

The MCP process starts the daemon detached in its own session/process group,
with inherited standard streams closed. The daemon holds a process lock and
updates a heartbeat. `status` and `list` label an armed monitor `unsupervised`
when no valid lock/heartbeat is present; they do not imply that it will wake
the target. On startup a daemon reloads armed monitors. It converts a durable
`activating` state left by a crash into an uncertain terminal outcome because
an activation could already have reached Codex; it never sends that request
again. A durable `claimed` state without a recorded attempt can proceed only
after a fresh target-guard check and a transaction that reasserts it was not
cancelled.

The ledger enables WAL and `synchronous=FULL`. Its transition to `activating`,
including an immutable pre-send target/goal snapshot (`createdAt`, objective,
token budget, `updatedAt`, tokens used, and time used), is durably committed
before the app-server request is sent. Recovery retains that snapshot for
forensics and any conclusive post-crash comparison, but never retries an
activation it cannot prove was not sent. This is intentionally stronger than
process-crash-only journaling because a lost pre-send record could otherwise
permit a second request after host failure.

Alternative considered: a transient in-memory timer/fs-watch service. It
cannot establish once-only behavior after daemon death and can miss external
events between observing and subscribing.

### Treat condition evaluation as typed three-valued evidence

The canonical condition AST has leaves `time`, `gpu_stable`, `pid_exit`,
`tmux_exit`, and `receipt_success`, composed with `all` and `any`. Each poll
produces `true`, `false`, or `unknown` plus an evidence record. `all` is true
only when all children are true; `any` is true when any child is true; unknown
never becomes true by composition. Transient observation errors remain armed
with unknown evidence; irrecoverable identity violations produce
`observer_failed`.

- `time` converts a relative duration into a stored absolute UTC deadline;
  monotonic time is only an in-process scheduling aid and is never the
  persisted source of truth.
- `gpu_stable` calls the vendor query utility only for the requested device
  indices, and records sampled utilization, threshold, and uninterrupted
  stable interval. Missing/invalid samples reset the stable interval.
- `pid_exit` captures boot ID, process start time, and UID at registration.
  A missing PID is true only on the same boot; PID reuse or boot mismatch is
  unknown/failed rather than completion.
- `tmux_exit` captures server socket, server PID, target kind, and target ID.
  It is true only when the original server is still reachable and the captured
  pane/session is gone. A missing/restarted server is unknown, not success.
- `receipt_success` verifies an atomically published JSON receipt against the
  monitor ID and unguessable per-monitor receipt token. This is the only v1
  task-success evidence.

Raw shell predicates are intentionally not represented in the AST. Receipt
publication is an explicit local CLI/MCP action so a job wrapper can emit an
authoritative terminal fact without giving the daemon arbitrary code
execution.

Alternative considered: treating `nvidia-smi` zero utilization or a tmux pane
exit as success. Those events do not distinguish a healthy task from a crash,
shell handoff, another process, or a new job, so they are intentionally only
heuristic/liveness facts.

### Close registration and activation races with two target observations

At registration, the service reads the target guard from the local app server,
captures process/tmux identities, persists `registering`, and reads the guard
again. It changes to `armed` only if both observations name the same loaded
target with the same paused goal. The target does not need to be idle at this
point, so a main thread can arm its own paused goal while it is still handling
the registration tool call. The goal guard is the thread ID plus the paused
goal's immutable marker (`createdAt`, objective, and token budget), not just
the word `paused`.

When the condition is true, an SQLite compare-and-set claim changes the monitor
from `armed` to `claimed`. A cancellation compare-and-set can win while the
monitor is `registering`, `armed`, or `claimed`. The transaction that changes
`claimed` to `activating` reasserts that cancellation has not won. Before that
transaction, the daemon re-reads target state and accepts only
`ThreadStatus == idle` and `goal.status == paused` with the captured marker.

Only then does it issue exactly one request with the exact wire payload
`{"threadId": "<id>", "status": "active"}`. In particular, it omits
`objective`, `tokenBudget`, and every other optional field: an explicit
`tokenBudget: null` would clear an operator's budget. It does not call
`turn/start`.

The protocol has no expected-goal/version compare-and-set, so a target may
change in the tiny interval after preflight. The response is compared with the
captured marker. A matching active goal records `fired`; a different marker
records `mis-targeted-activation` with both markers, no compensating write,
and no retry. A missing/ambiguous response records uncertainty without retry.

Alternative considered: invoking `codex exec resume <session>`. That starts a
separate CLI path and does not preserve the live Desktop thread/paused-goal
guard as directly as the owning host's app-server control plane.

### Keep the app-server actuator narrow and version-checked

The adapter communicates through the canonical
`$CODEX_HOME/app-server-control/app-server-control.sock` using a Unix-domain
WebSocket. At startup, `doctor` checks the socket is local and accessible to
the current user, performs the app-server `initialize` / `initialized`
handshake, requests opt-out from high-volume notifications, and proves that
the goals feature is live with a read-only `thread/goal/get` for the target.
It uses `thread/read` as the authoritative loaded/idle check and does not
infer loaded state from a partial `thread/loaded/list` page. The adapter
matches responses by request ID and exposes a tiny interface (`read_observation`,
`activate_guarded_goal`) so unit tests use a fake and do not need a live Codex
server.

No TCP URL, remote control API, database mutation, or private-session file
write is used. An unavailable or incompatible control socket prevents arming
and produces a visible diagnostic; it never falls back to another transport.

### Make activation evidence policy explicit

The registration request includes `allow_heuristic_continuation`, defaulting
to false. A condition whose actual true-leaf witness contains no successful
receipt reaches a terminal `satisfied_requires_authorization` state without a
goal change unless that flag is true. A receipt-success leaf in the satisfying
witness may authorize activation itself. For a composed condition, the service
retains the full leaf evidence and its selected satisfying witness. Thus,
`any(receipt_success, time)` cannot activate on time alone without the flag,
but can activate when receipt success is the witness.

This preserves the useful “wake me after this appears idle” workflow while
making its inferential limitation visible in status and audit records.

## State Model

```
REGISTERING --(same second guard)--> ARMED --(condition true)--> CLAIMED
     |                                  |                         |
     +--(guard changed/restart)--> SUPERSEDED   +--(expiry)--> EXPIRED     +--(preflight changed/expiry)--> SUPERSEDED | UNLOADED_TARGET | EXPIRED
                                                                |
                                                                +--(no authorized witness)--> SATISFIED_REQUIRES_AUTHORIZATION
                                                                |
                                                                +--(pre-send transaction)--> ACTIVATING
                                                                                              |--(matching response)--> FIRED
                                                                                              |--(different marker)--> MIS_TARGETED_ACTIVATION
                                                                                              +--(ambiguous/error)--> ACTIVATION_UNCERTAIN

REGISTERING | ARMED | CLAIMED --(cancel before pre-send transaction)--> CANCELLED
ARMED --(irrecoverable observer identity failure)--> OBSERVER_FAILED
```

`ACTIVATING` is fully synchronized before sending the app-server request.
Therefore a crash after the write consumes the one attempt and recovers as
`ACTIVATION_UNCERTAIN`, preserving at-most-once semantics.

## Risks / Trade-offs

- [Experimental app-server protocol changes] → isolate it in one adapter,
  run `doctor`, pin only the required methods, and fail closed on mismatch.
- [A host reboot or PID reuse resembles exit] → bind PID identity to boot ID
  and start time; mismatch is unknown, never true.
- [GPU zero utilization is unrelated to the intended job] → classify it as a
  heuristic and require an explicit continuation opt-in.
- [tmux server restart loses target identity] → require the captured server
  identity to remain available; otherwise do not satisfy the condition.
- [A daemon crash follows an activation send] → durable pre-send state means
  the daemon reports uncertainty rather than retrying.
- [SQLite path is shared by tools and daemon] → use short transactions,
  WAL with `synchronous=FULL`, schema initialization checks, an in-process
  ledger mutex, and a single daemon lock.
- [The app-server has no goal compare-and-set] → preflight immediately before
  the one request, compare its returned marker, and expose a distinct
  `mis-targeted-activation` outcome. The operator selected this best-effort
  automatic policy rather than notification-only behavior; no compensating
  write is safe.
- [A detached daemon stops or is never restarted] → write a heartbeat and
  expose an `unsupervised` status rather than representing an unobserved armed
  monitor as healthy.
- [A malformed receipt is injected] → use a monitor-specific random token,
  atomic write/rename, restrictive runtime-root permissions, and schema
  validation.

## Migration Plan

1. Add the standalone source module, tests, and source plugin manifest under
   `codex-wake-me-up/`; add its runtime directory to Git ignores if needed.
2. Validate the manifest and run focused unit tests with fake observers and a
   fake app-server adapter.
3. Run `doctor` against the current local socket, then use a newly created,
   disposable Codex session with a paused benign goal, a short time monitor,
   and explicit heuristic-continuation authorization. Never use the named live
   user target in this smoke.
4. Do not install or enable the plugin globally. Operators can later install
   the reviewed source plugin deliberately; rollback is stopping the daemon
   and deleting only its runtime state directory after confirming no monitor
   remains armed.

## Open Questions

- None that changes the initial behavior or task breakdown. Later work can
  add scheduler/service-manager integration, new typed observers, or a
  multi-host controller as separate compatibility changes.

## Context

See `proposal.md` for motivation. The daemon currently owns `daemon.lock`,
recovers the durable ledger, and reconciles forever. `Ledger.list(
include_terminal=False)` already supplies the authoritative outstanding-work
predicate, while `daemon-heartbeat.json` plus the kernel-reported lock owner is
the existing readiness proof.

The unsafe boundary is the final empty-ledger check. A registration can observe
the old heartbeat, while the old daemon can decide to exit, and a replacement
process started before `daemon.lock` is released will immediately lose the
lock. Retirement therefore needs both an advertised accepting state and
single-flight replacement startup; an extra empty check alone is insufficient.

## Goals / Non-Goals

**Goals:**

- Retire the lock-owning daemon after 60 seconds with no durable non-terminal
  monitor, using the existing polling loop and ledger.
- Make a committed retirement ineligible for every daemon readiness check.
- Serialize on-demand replacement so concurrent callers or a retiring lock
  owner cannot cause a fork storm or a lost replacement.
- Preserve every stored monitor, reservation, receipt, and terminal outcome.

**Non-Goals:**

- Reclaim MCP frontends, Codex tasks, HarnessDock workers, Serena, or app-server
  processes.
- Change public MCP/CLI schemas, monitor semantics, delivery rules, or the
  existing distinction between readiness-gated registration and legacy
  post-arm best-effort startup.
- Add a configurable policy surface, a scheduler, a second daemon, or a new
  dependency.

## Decisions

### 1. Use the existing ledger as the only residency lease

After each reconciliation, the daemon checks `Ledger.list(
include_terminal=False)`. Any returned row resets the idle deadline. An empty
result starts or continues one monotonic 60-second grace; `--once` retains its
current single-pass behavior and does not wait through the grace.

This intentionally counts all non-terminal phases, not only `armed`. A
`registering`, pause, claim, queue, or cancellation phase is still work whose
recovery owner must remain resident. No separate lease table or timer process is
needed.

**Alternative considered:** derive residency from caller/MCP/task liveness.
Rejected because callers may legitimately disappear while detached monitors
remain authoritative.

### 2. Publish an explicit accepting bit before the final retirement check

The heartbeat gains `accepting_work: true|false`. Normal loop heartbeats write
`true`. At the end of an uninterrupted idle grace the lock owner atomically
writes `false`, then performs one final durable non-terminal query while still
holding `daemon.lock`:

- if work exists, it writes `true`, clears the idle deadline, and resumes;
- if no work exists, it returns from the loop and releases `daemon.lock`.

All base, event, and delivery readiness predicates reject an explicit `false`
even while the PID and lock remain live. A heartbeat from an older installed
daemon has no field and remains compatible with its previous behavior; rollout
restarts that daemon so the new retirement policy actually takes effect.

The final query protects work committed before the retirement marker. Work
committed after it is protected by the caller rejecting that generation and
joining the replacement-start path.

**Alternative considered:** delete or stale the heartbeat before exit.
Rejected because an explicit state is atomic and distinguishable from file or
clock failure.

### 3. Single-flight replacement startup with one advisory start lock

Add one runtime-root `daemon-start.lock`, implemented with the same stdlib
`fcntl.flock` pattern already used by the daemon and defer protocol. The default
starter holds this lock across a bounded start attempt:

1. recheck for an accepting healthy daemon;
2. if a retiring generation still owns `daemon.lock`, wait within the existing
   readiness bound for it to relinquish the lock;
3. recheck, spawn at most one detached daemon only when no eligible owner
   remains, and keep the start lock until base readiness succeeds or the bound
   expires.

Concurrent callers serialize behind that attempt and recheck rather than
spawning duplicate children. Event and delivery callers then apply their
existing stronger epoch/source/lock predicates. The injected readiness seams
remain so the boundary can be tested without wall-clock sleeps.

This also protects the legacy post-arm `daemon_starter` call: if it lands after
the old daemon's final empty check, it waits out that exact retiring owner and
starts one replacement instead of launching a child that loses `daemon.lock`.
A genuinely unavailable daemon retains the existing fail-closed or
`unsupervised` behavior appropriate to the caller; retirement does not invent a
success receipt.

**Alternatives considered:** retry `Popen` from every caller, or introduce a
resident supervisor. The former can fork-storm and the latter defeats resource
reclamation.

### 4. Do not write monitor state during retirement

Retirement is a control-plane lifecycle transition, not a monitor outcome. The
affected terminal-state writers remain:

| Site | Classification | Observable receipt and check |
| --- | --- | --- |
| Event registration second readiness gate | justified fail-closed, not retryable activation | existing `daemon_unavailable` row with `event_daemon_mismatch_before_arm`; regression test proves no arm |
| Thread-delivery second readiness gate | justified fail-closed, not retryable activation | existing `daemon_unavailable` row with `delivery_daemon_mismatch_before_arm`; regression test proves no arm |
| Deferred pre-pause readiness gate | justified fail-closed, not retryable activation | existing `daemon_unavailable` row with `daemon_unavailable_before_pause`; regression test proves no pause |
| Idle retirement | no terminal writer and not wake-eligible | `accepting_work=false`, subsequent lock release/process exit, unchanged ledger rows; daemon-loop test |
| Legacy post-arm startup failure | no terminal writer; existing observable `unsupervised` state | status remains `unsupervised`; starter race test proves ordinary retirement does not create this failure |

No path added by this change is wake-eligible or retries an activation. The
existing daemon still owns all reconciliation terminal writes after a
replacement becomes ready.

## Risks / Trade-offs

- **Registration lands after the final ledger query** -> its readiness/starter
  path rejects the retiring heartbeat and joins serialized replacement startup.
- **Several callers notice retirement together** -> `daemon-start.lock` permits
  one spawn attempt and forces later callers to recheck.
- **A retiring daemon fails to release its lock** -> startup remains bounded and
  callers preserve their existing fail-closed/unsupervised evidence rather than
  killing an unproven process.
- **Short monitor bursts restart the daemon** -> the fixed 60-second grace
  absorbs nearby work without adding a permanent policy/configuration surface.
- **Old installed daemon ignores the new policy** -> installation verification
  restarts the exact lock owner; no database migration or capability-epoch bump
  is required.

## Migration Plan

1. Run the focused heartbeat, idle-loop, and startup-race tests, followed by the
   full suite.
2. Install the source-owned plugin through the existing replacement workflow.
3. Restart only the exact Wake daemon owning the runtime root, then verify its
   command line, `PYTHONPATH`, source identity, accepting heartbeat, and kernel
   lock ownership.
4. Arm and settle one disposable monitor; observe `accepting_work=false` and
   exact daemon exit after the grace, then register a second monitor and verify
   one on-demand replacement.

Rollback requires reinstalling the previous plugin and restarting the exact
lock-owning daemon. The previous binary ignores the additional heartbeat field;
the ledger needs no rollback.

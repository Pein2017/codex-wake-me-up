# Verification Evidence

Recorded 2026-08-18 against `54cee558e` (implementation) on the live local
Codex app-server (Codex Desktop 0.147.0, `CODEX_HOME=/data/CoordExp/.codex`).

## Fixed implementation checks

- `conda run -n ms python -m pytest`: 104 passed / 0 failed. Pre-change
  baseline at `9d5153499` was 52 passed / 0 failed; the failure-set diff is
  empty (∅ → ∅) and every addition is a new test.
- `openspec validate add-wake-me-up-event-wakes --strict`: passed.
- `.codex-plugin/plugin.json` parses; `skills/`, `.mcp.json`, and the
  `composerIcon`/`logo` assets all resolve.
- MCP façade, two independent receipts:
  - In-process introspection (`FastMCP.list_tools()` on the source tree)
    returned exactly `wake_me_up`, `wake_me_up_cancel`, `wake_me_up_defer`,
    `wake_me_up_publish_receipt`, `wake_me_up_status`, with `rearm_of` on both
    registration tools.
  - A **real stdio MCP client** spawned the plugin manifest's own entry point
    — `command: python`, `args: ["./scripts/mcp_server.py"]`, `cwd:` plugin
    root, exactly as `.mcp.json` declares — and completed the handshake
    (server `Codex Wake Me Up`, protocol `2025-11-25`). `tools/list` returned
    the same exact five-tool set. `rearm_of` is present and optional
    (`[{"type":"string"},{"type":"null"}]`) on both registration tools, and
    `idempotency_key` is required on `wake_me_up_defer` but not on
    `wake_me_up`. This exercises the installed-plugin launch route, not just
    the importable module, so a broken script bootstrap could not pass
    unnoticed.
- `codex-wake-me-up doctor`: passed against the local Unix control socket. It
  performed no goal lookup because no `--thread-id` was supplied.

## Execution environment and the stale-daemon finding

A daemon was already running and holding `daemon.lock`. Before terminating it,
its identity was read from `/proc`:

- pid `3322387`, started 2026-08-08 17:50:43 (elapsed 9d 13h),
- cmdline `/root/miniconda3/envs/ms/bin/python -m codex_wake_me_up.daemon
  --runtime-root /data/CoordExp/.codex/runtime/codex-wake-me-up`,
- `PYTHONPATH=/data/CoordExp/.codex/plugins/cache/coordexp-local/codex-wake-me-up/0.1.0+codex.20260808174817/src`.

That is the **installed v0.1 plugin**, not this change. Left running, it would
have owned the lock and processed new monitors with pre-change semantics — a
deferred expiry would have been written as terminal `EXPIRED`, making the
stranding this change removes look like a fresh regression. It was terminated
(`kill 3322387`, exit confirmed); this was safe because all 8 pre-existing
ledger rows were terminal (7 `fired`, 1 `superseded`), so no monitor lost
supervision. **Operational consequence for task 5.4: reinstalling the plugin
must also restart this daemon, otherwise the old cachebuster's daemon keeps
serving new monitors.**

The daemon used for this smoke was started by the production readiness path
(`ensure_daemon_ready` → `ensure_daemon`) and verified to run this change:
pid `4147919`, `PYTHONPATH=/data/CoordExp/codex-wake-me-up/src`.

The defer flow was driven from Python via `MonitorService.defer(...)` with the
real `AppServerClient` — the CLI exposes no defer command, so this is the
closest production-shaped entry point, identical to the code path the
`wake_me_up_defer` MCP tool handler calls, one layer below the stdio
transport. It is not MCP-stdio-driven, and no result below depends on a fake
or mock.

## Targets

Disposable threads created solely for this smoke:

| Thread | Role |
| --- | --- |
| `01a013b5-2477-7741-b3f1-36d165354a99` | defer target ("scratch A") |
| `01a013bf-eb8b-7cb3-b4a8-6bf7f491a4d4` | runtime-status probe ("scratch C") |
| `01a013c1-335b-7bf1-9bd2-0ec6ece18048` | fork of scratch A, used as an idle `thread_idle` child |

The real loaded thread `019fd043-5abb-7eb1-997c-d11a8316281a` was never passed
to any mutating call. The harness carried an assertion that refused its thread
ID on every app-server call; the assertion fired once during a read-only probe
and correctly aborted that read.

**These threads turned out not to be inert fixtures — this smoke's own goals
drove them.** `thread/start` yields an idle thread, but attaching an *active*
goal hands it to the Codex Desktop goal runtime, which starts a turn to pursue
that goal. Every "foreign" turn observed was induced this way. Three
independent signals establish it:

1. **Timing.** Each induced turn began at the exact second a goal was created
   or re-activated by this smoke:

   | Goal write by this smoke | Induced turn start |
   | --- | --- |
   | `01a013b5` goal created 07:11:34 | 07:11:34 |
   | `01a013b5` goal re-created 07:18:00 | 07:18:00 |
   | `01a013bf` goal created 07:22:19 | 07:22:23 |
   | `01a013b5` goal re-created 07:24:42 | 07:24:42 |

2. **No user message.** Every induced turn's item list contains no
   `userMessage` — the agent simply began working. The only turn carrying a
   `userMessage` is the one approved turn this smoke sent itself.

3. **The openings quote the objective text.** The 07:11:34 turn opens by
   putting "the `wake-me-up smoke` goal" onto the repository — the objective
   then set was `wake-me-up smoke scratch goal`. The 07:18:00 turn opens
   "based on the *new* goal, scoping to `event-wakes`" — the objective had
   just been rewritten to `wake-me-up event-wakes smoke (disposable)`. The
   `01a013bf` turn opens by checking "the active Codex goal state" against the
   objective `status probe`.

No external agent was involved, and no third party's work was disturbed. This
also fully explains the earlier `idle → active, permanently` mystery: an
active goal on this host **is a goal that gets run**, so the thread stays busy
while the runtime pursues it (retrying, and reporting `serverOverloaded` when
the model is at capacity).

Read as evidence, the induced turns are a positive result: they demonstrate
the downstream half of a wake — the local goal runtime does pick up an
`active` goal on an idle thread and start a turn. That is the continuation a
wake ultimately buys. The caveat is stated exactly: these turns were induced
by a direct `thread/goal/set`, not by a monitor activation, because no monitor
in this smoke ever fired.

### Fixture-induced turns and unintended cost

Recorded for honest accounting. Setting fixture goals caused real model spend
that was neither intended nor pre-approved:

| Thread | Turn start | Status | Items |
| --- | --- | --- | --- |
| `01a013b5-…` | 07:11:34 | completed | 11 |
| `01a013b5-…` | 07:18:00 | completed | 12 |
| `01a013bf-…` | 07:22:23 | completed | 12 |
| `01a013b5-…` | 07:24:42 | failed (`serverOverloaded`) | 0 |
| `01a013b5-…` | 07:25:34 | failed (`serverOverloaded`) | 1 |

Five induced turns: three completed, two failed at capacity. Separately, the
one **approved** turn (07:43:22) produced `SMOKE_OK` but was itself recorded
`failed / serverOverloaded`. The two turns visible on the fork
`01a013c1-335b-…` are timestamped *before* the fork existed (07:23:43) and are
inherited copies of the parent's history, not new spend.

The lesson for any future smoke of this plugin: **an `active` goal is not an
inert fixture — it is a work order.** A goal-bearing fixture should be created
`paused`, or the objective should be inert text, or the runtime should be
understood as part of the fixture's cost.

### Pre-deletion ledger

Recorded before deletion, because deletion is irreversible. The user
authorised closing the threads this session started; the user's own thread
`019fd043-5abb-7eb1-997c-d11a8316281a` was explicitly excluded and a hard
assertion kept it out of the delete set.

| Thread | Created | Final thread status | Final goal status | Turns (all induced or approved) |
| --- | --- | --- | --- | --- |
| `01a013b5-2477-…` | 07:10:33 | `systemError` | `blocked` | 5: 07:11:34 completed (11), 07:18:00 completed (12), 07:24:42 failed (0), 07:25:34 failed (1), 07:43:22 failed (2, the approved turn) |
| `01a013bf-eb8b-…` | 07:22:19 | `notLoaded` | none | 1: 07:22:23 completed (12) |
| `01a013c1-335b-…` | 07:23:43 (fork) | `notLoaded` | none | 2 inherited from the fork parent, pre-dating the fork: 07:11:34 completed (11), 07:18:00 interrupted (11) |
| `01a013c1-3635-…` | 07:26:50 | `notLoaded` | none | 0 (observed empty, no preview, while still loaded) |
| `01a013c1-3636-…` | 07:26:50 | `notLoaded` | none | 0 (same) |
| `01a013c1-3726-…` | 07:26:50 | `notLoaded` | none | 0 (same) |

Net new model spend caused by this smoke: **five induced turns** (three
completed, two failed at capacity) plus **one approved turn** (produced
`SMOKE_OK`, recorded failed at capacity). The fork's two turns are inherited
copies and are not new spend. The three `01a013c1-36xx` threads never ran a
turn.

## PASS — verified against the live app-server

| # | Behavior | Evidence |
| --- | --- | --- |
| 1 | Real defer protocol completes | intent → readiness → pause → confirmation → `armed`, `next_action: end_current_turn`. Scratch A's goal genuinely moved `active` → `paused` via one `thread/goal/set`. |
| 2 | Journal elided in registration response | `condition.journal == {"lines_stored": 0, "dropped": 0}`; no `lines` key. |
| 3 | `log_pattern` captures real file identity | captured `{"device": 160, "inode": 200301695}` matched the real file's `st_dev`/`st_ino`; `offset: 41` equals the file's EOF-at-arm size; `missing_at_arm: false`. |
| 4 | Guard re-read fail-closes on a live goal change | While monitor `dbd109d6-7a16-47ad-9785-a9a1d1e0b612` was armed, the goal was replaced out-of-band. The running daemon detected the mismatch and wrote `superseded` (`deferred_evaluation_guard_changed`) with **no activation request**. Unplanned but decisive: the pre-activation guard works against the real control plane. |
| 5 | `thread_idle` rejects self-wait | `thread_idle must not name the monitor's own target thread`. |
| 6 | `thread_idle` rejects an already-idle child | Against the real idle child `01a013c1-335b-…`: `is already idle at registration; handle its result in the current turn, or set accept_already_idle…`. This is the spec scenario "Child already idle at registration". |
| 7 | `rearm_of` rejects an unknown reference | `rearm_of names an unknown monitor: no-such-monitor`. |
| 8 | `rearm_of` rejects a non-terminal reference | Against live armed monitor `604c63ab-…`: `rearm_of must name a terminal monitor; a live monitor cannot be a lineage parent`. |
| 9 | `rearm_of` accepted and surfaced | Monitor `8724b5c1-…` recorded `rearm_of: dbd109d6-…` and `rearm_chain: ["dbd109d6-…"]`, surfaced identically in `status` and `list`. |
| 10 | `cancel` reaches a terminal state | armed → `cancelled`, no activation. |
| 11 | No thread leak from the client | 3 connect+read cycles and ~3 daemon poll cycles created zero new threads. |

## BLOCKED — the four wake paths of task 5.3

None of the four wake paths could be executed, for one environmental reason,
and no substitute was used.

**Finding: creating a goal makes a never-run thread permanently non-idle.**
On this app-server build, a thread created with `thread/start` reports
`status.type == "idle"`. The moment a goal is attached with
`thread/goal/set {objective}`, the status becomes
`{"type": "active", "activeFlags": []}` with zero turns, and it never returns
to `idle` — not after pausing the goal, not after clearing the goal, and not
after 12+ seconds of settling. Isolated on a fresh thread:

```
at thread/start   : {"type": "idle"}
+2s, before goal  : {"type": "idle"}
right after goal  : {"type": "active", "activeFlags": []}
+8s after goal    : {"type": "active", "activeFlags": []}
```

Every activation path requires `runtime_status == "idle"`: the deferred idle
barrier (`_deferred_target_is_idle`) and the pre-activation preflight
(`TargetObservation.activation_eligible`), for legacy and deferred monitors
alike. A synthetic disposable thread therefore cannot satisfy the
precondition, so no monitor armed against one can ever fire. Observed
directly: the smoke-1 monitor sat `armed` with `evaluation_count: 0` — the
barrier never opened, so the condition was never even scanned.

Escape hatches ruled out without spending anything:

- `thread/fork` of a goal-bearing thread yields an idle thread but **no goal**;
  attaching a goal re-triggers the same flip.
- Clearing and re-setting the goal does not reset the status.

**This is a fixture limitation, not a product defect.** In production the
deferring agent's own thread is genuinely running a turn when it defers, and
returns to `idle` when that turn ends. The 7 historical `fired` monitors in
this ledger — each of which passed the same `runtime == "idle"` activation
gate against real threads — are the standing evidence that the gate opens in
real use.

Completing the four wake paths requires making a scratch thread realistic,
which means running at least one real turn (`turn/start`) on it. That is a
model-token cost on the user's account, and material cost is user-owned.

**One turn was approved and spent; it did not unblock the paths.** The turn
(`Reply with exactly: SMOKE_OK. Do not run any commands and do not use any
tools.`) did produce `SMOKE_OK` as its final answer, but the turn was recorded
`failed` with `codexErrorInfo: serverOverloaded` ("Selected model is at
capacity"), as were two neighbouring turns in the same window. The thread
settled at `status: systemError` and its goal moved to `blocked`, so it is
still not a `(loaded ∧ idle ∧ goal present)` target. Only the one approved
turn was spent; no further turn was run. The paths still outstanding:

- `log_pattern` defer, success path
- `log_pattern` defer, crash path
- `thread_idle` defer on a scratch child
- forced expiry wake
- daemon-restart-then-expiry-wake recovery

## Product observations raised by real execution

Neither is a defect in this change; both are gaps between the documents and
the live control plane, surfaced for a ruling.

1. **`goal.status` can be `blocked`.** Observed directly after the failed
   turn. No spec or design text in this change or its two predecessors
   mentions that value; they reason over `active` and `paused` only. Current
   behavior is safe by construction — `TargetObservation.matches_guard`
   accepts only `paused` and `_defer_eligible` only `active`, so a `blocked`
   goal fails closed — but the documented status vocabulary is incomplete.

2. **A target stuck non-idle defeats the "woken at the latest at expiry"
   guarantee.** `_deferred_target_is_idle` returns false for any
   `runtime_status != "idle"`, so a thread parked in `systemError` (or any
   long-lived non-idle state) never opens the idle barrier; its armed
   deferred monitor stays `ARMED` past expiry indefinitely and its goal stays
   paused. Observed: monitor `dbd109d6-…` sat `armed` with
   `evaluation_count: 0`. This is the same stranding this change removes,
   reached through a different door — a stuck target rather than the plugin's
   own bookkeeping. `proposal.md` states the guarantee holds "as long as the
   daemon lives and the guard holds"; in practice there is a third implicit
   precondition: **the target must reach `idle` at least once after expiry**.
   Whether to state that in the spec, or to treat a stuck target as
   wake-eligible, is a scope decision for the change owner.

## Scope limitation recorded by ruling

The `thread_idle` active → idle **edge** was deliberately not purchased. Per
the 2026-08-18 ruling: the edge and the `accept_already_idle` path traverse the
same `read_observation` → `runtime_status` comparison, a paid turn would only
re-confirm that a running thread's status string is not `idle`, and the
implementation treats every non-`idle` string as busy (the fail-safe
direction). The already-idle rejection above is the real receipt for the
arm-time guard. This limitation is recorded rather than described as full
`thread_idle` coverage.

## Residual state

- Monitors created by this smoke: `dbd109d6-…` (`superseded`),
  `8724b5c1-…` (`cancelled`), `604c63ab-…` (`cancelled`). **No non-terminal
  monitor remains.** Terminal rows are retained as forensic receipts and are
  never replayed.
- Three empty threads (`01a013c1-3635-…`, `01a013c1-3636-…`,
  `01a013c1-3726-…`, all `source=vscode`, 0 turns, no preview) appeared during
  the session and could **not** be attributed to this smoke; connecting and
  daemon polling were both proven not to create threads. They were left
  untouched rather than deleted on a guess.
- **Threads cleaned up under user authorisation.** `01a013b5-…`,
  `01a013bf-…`, and `01a013c1-335b-…` were deleted successfully. The three
  `01a013c1-36xx` threads returned `no rollout found for thread id …`: they
  never persisted a rollout, so there was nothing to delete, and they are gone
  from the loaded set. Deletion ran only after asserting that no non-terminal
  monitor existed, and behind a hard assertion excluding the user's thread.
- The user's thread `019fd043-5abb-7eb1-997c-d11a8316281a` is untouched and
  still loaded — verified immediately after the deletion pass.
- Monitor rows created by this smoke remain as forensic receipts
  (`dbd109d6-…` superseded, `8724b5c1-…` cancelled, `604c63ab-…` cancelled).
  No non-terminal monitor remains.

## Task 5.4 — install through the local marketplace

Approved after review of `state-machine-diff.md`.

- **Old cachebuster (rollback target): `0.1.0+codex.20260808174817`.**
  New cachebuster: `0.1.0+codex.20260818080356`, written to
  `.codex-plugin/plugin.json`.
- Reinstalled through the local marketplace: `plugin/install` with
  `pluginName: codex-wake-me-up` and
  `marketplacePath: /data/CoordExp/.agents/plugins/marketplace.json`
  (marketplace `coordexp-local`, source `/data/CoordExp/codex-wake-me-up`).
  `plugin/list` then reported `localVersion: 0.1.0+codex.20260818080356`,
  `installed: true`, `enabled: true`.
- Installed content verified, not assumed: `diff -rq --exclude=__pycache__`
  between the installed `src/` and the working tree reports **no differences**
  (only `.pyc` artifacts differed before the exclusion). `WakeReason` is
  present, `service.py` carries `deferred_preflight_retryable`, and the
  installed `SKILL.md` carries the expiry-wakes section and `rearm_of`.
- **Daemon handover, per the updated task wording.** The previous daemon's
  identity was re-read from `/proc` before terminating it (pid `4147919`,
  `PYTHONPATH=/data/CoordExp/codex-wake-me-up/src` — the source tree), with an
  assertion that its cmdline was a `codex_wake_me_up.daemon`. After it exited,
  a daemon was started through the **installed** copy's own
  `ensure_daemon`/`ensure_daemon_ready` (the production spawn path, imported
  from the cache and asserted to resolve there). Result: pid `45113`,
  `PYTHONPATH=…/0.1.0+codex.20260818080356/src`, holding `daemon.lock`
  (`pid=45113`) and heartbeating. The installed code now owns the runtime.
- Post-install MCP receipt: a real stdio client launched
  `./scripts/mcp_server.py` with `cwd` set to the **installed cache
  directory**, completed the handshake (protocol `2025-11-25`), and listed the
  same five tools with `rearm_of` on both registration tools. The installed
  plugin's tool surface is live, not merely staged.
- The durable ledger was unaffected by the reinstall: 11 rows, 0 non-terminal.

### Rollback path

Reverting the cachebuster string alone is **not** a code rollback. The
marketplace installs from the source tree, and the previous cache directory
`0.1.0+codex.20260808174817` was replaced in place, so reinstalling under the
old version string would republish current source under an old label. A true
rollback is:

1. restore the source tree to the pre-change commit (`9d5153499`, i.e. revert
   `54cee558e` and `778437c80`) under `codex-wake-me-up/`;
2. set `.codex-plugin/plugin.json` back to `0.1.0+codex.20260808174817`;
3. reinstall through the same marketplace path;
4. re-read `/proc` for the lock-holding daemon, terminate it, and let the
   reinstalled code spawn its replacement — otherwise the new-code daemon
   keeps serving after a code rollback, which is the mirror image of the
   stale-daemon trap found at the start of this smoke.

Terminal monitor rows are forensic evidence and are never replayed, so no
ledger rollback is required.

## Source verification

### RED

- A sanitized `queue_accepted` parent reproduced the existing
  `rearm_of must name a terminal monitor` rejection before the lineage change.
- Staged-diagnostic tests initially failed before the readiness reason helper and
  app-server boundary labels existed.
- The native adapter/condition slice initially reported 10 failures: the client had
  no `observe_native_worker` method and `native_worker_terminal` was unsupported.
- The trusted-root regression then showed that direct explicit-thread registration
  could bind native task names until the current-caller gate was added.
- Cross-source review reproduced rejection of Core's valid 512-character non-ASCII
  error because the client incorrectly bounded UTF-8 bytes instead of characters.
- A sensitivity replay restoring the old catch-all disposition made the sanitized
  claimed-timeout consumer test fail with
  `delivery_capability_unavailable` instead of durable `claimed` state before any
  queue-add.
- Follow-up counterexamples showed that broad `OSError`/`aiohttp.ClientError`
  handling misclassified socket-stat permission denial and HTTP 403 WebSocket
  handshake rejection as retryable transport; both now remain definitive.

### GREEN

- `conda run --no-capture-output -n ms python -m pytest -vv`: 450 passed.
- `conda run --no-capture-output -n ms python -m ruff check src tests`: passed.
- `conda run --no-capture-output -n ms python -m compileall -q src tests`: passed.
- Packaged skill `quick_validate.py skills/wake-me-up`: passed.
- `openspec validate repair-wake-rearm-and-diagnostics --strict`: passed.
- Owned-path `git diff --check` and untracked-file trailing-whitespace scan: clean.

### Core contract replay

Against the uninstalled Core source candidate under
`/data/CoordExp/external/harness/codex/codex-rs`, the following focused tests each
passed:

- app-server protocol JSON-RPC decode for `thread/agent/observe`;
- root-scoped canonical task resolution;
- exact historical `read_turn` lookup;
- public unavailable-root response;
- exact interrupted and failed settlement mapping; and
- rejection of a previous generation's terminal status for a new in-progress turn.
- positive public bind behavior that keeps returning the original completed turn
  after a follow-up starts on the same child.

The generated protocol schema exposes exactly `bindPending`, `running`,
`interrupted`, `completed`, `failed`, `unavailable`, and `mismatch`.

## Acceptance boundary

This is a source candidate only. Core/plugin installation, daemon restart, live
monitor creation, and real root-final native-worker wake were not authorized and
were not performed. Task 4.3 remains open; no live-acceptance claim is made.
The installed September 1 daemon and the already terminal historical delivery-loss
row are also unchanged: this source fix does not migrate, reopen, or requeue it.

## Plugin-only install and daemon switch (2026-09-08)

The user separately authorized the plugin reinstall and exact daemon replacement,
but not a Core install, app-server/session restart, live monitor, replay, or native
acceptance matrix. The prescribed cachebuster helper changed the manifest from
`0.1.0+codex.20260901013840` to `0.1.0+codex.20260908113517`, and
`codex plugin add codex-wake-me-up@coordexp-local --json` installed it at
`/data/CoordExp/.codex/plugins/cache/coordexp-local/codex-wake-me-up/0.1.0+codex.20260908113517`.
The installed package source identity is
`741f22cdbeadcdd62718d46b0e888b50d47e1f62fdd14fe4fd621920c8ddb8dd`,
exactly matching the accepted source checkout. Its `.mcp.json` still launches
`conda run --no-capture-output -n ms python -u ./scripts/mcp_server.py`.

Before installation, SQLite's online backup API captured the committed WAL state
and `quick_check=ok` at
`/data/CoordExp/.codex/backups/codex-wake-me-up/20260908T113454Z/monitors-pre-switch.sqlite3`.
The same private rollback directory contains the complete old installed cache,
the prior source manifest, the pre-switch plugin list, and the sanitized runtime
inventory. It recorded 72 monitor IDs and 5 event reservation IDs.

Immediately before signalling, old PID `1321833` was revalidated by start ticks
`3436466414`, exact cmdline, deleted old-cache cwd, heartbeat PID, empty child
list, lock text, and its exclusive `/proc/locks` flock. It alone received
`SIGTERM`; `pidwait -F` observed exit and the lock released. The installed runtime
entry then started PID `3279711`, start ticks `3500545178`. That PID owns the same
runtime lock, runs from the new installed cache, has the expected `CODEX_HOME` and
`PYTHONPATH`, and advertised event epoch 3, delivery epoch 1, accepting-work true,
and the exact new source identity in a freshly observed heartbeat.

An independent stdio initialize plus `tools/list` through the installed MCP entry
negotiated protocol `2025-06-18` and returned all 12 tools without calling a
monitor tool. The live database remained `quick_check=ok`; all 72 prior monitor
IDs and all 5 prior event IDs remained present, with no state or admission
transition caused by the switch. Historical monitor
`753cf012-9f25-4726-a498-71cc06c555ae` remained exactly
`delivery_capability_unavailable` / `delivery_rejected`, with no admission time
and no queue receipt. It was not replayed or repaired. The final sanitized
installed-runtime and preservation receipt is
`/data/CoordExp/.codex/backups/codex-wake-me-up/20260908T113454Z/post-switch-inventory.json`.

Core was intentionally not installed. The live CLI remained `0.153.4`, and a
read-only `thread/agent/observe` capability probe was rejected as an unknown
method (`-32600`), so `native_worker_terminal` still fails closed rather than
falling back. Task 4.3 therefore remains unchecked and no native live-acceptance
claim is made.

Rollback requires a new explicit operator decision: preserve the current dirty
source first, restore the plugin source from the private old-cache snapshot plus
its saved manifest, reinstall through `codex plugin add`, then revalidate and
`SIGTERM` only the exact current daemon lock owner before starting the restored
installed runtime. Do not restore the ledger backup over the live ledger and do
not replay terminal rows; the database backup is forensic recovery evidence.
# Local runtime continuation authorization (2026-09-08)

User request: "两个实测阻塞是否可以修复和克服? 若可以,则继续推进;
openspec + native-subagents-guidance ... 最终,都需要 install 安装在本地."

Continuation owns task 4.3: reproduce the unsupported native observer and stale
delivery heartbeat, repair or deploy the matching source, install locally, and
verify real native settlement and root wake. Lead owns spec and integrated
activation; `native_observe_install` owns the Core candidate and
`wake_daemon_repair` owns the plugin/daemon candidate. Neither candidate is live
acceptance until the installed caller/consumer path passes. Historical failed
monitors are not replayed. Existing unrelated work and active task state remain
outside the mutation scope.

## Heartbeat repair and local installation

The awaited-reconciliation regression fails against the old heartbeat cadence and
passes with the periodic heartbeat task. The worker reports 451 plugin tests plus
Ruff, compileall, skill and OpenSpec validation passing. Lead inspected the exact
daemon/test diff and independently replayed all six residency tests:
`/tmp/codex-infoflow-20260908/lead-daemon-residency-tests.log`.

Installed `0.1.0+codex.20260908151430` has source identity
`2b9865b7250ec99ab13f86ccffcfe822fde0aed0b8e76c9f07bdd24cb8635e04`.
The exact prior daemon PID 3279711 was replaced by PID 4010804. A 45.07-second
installed-runtime sample observed 25 heartbeat advances, maximum heartbeat age
1.134 seconds, and eight reconciliation timestamp changes. All 72 monitor IDs
and five event reservation IDs were preserved, with state/admission comparisons
and SQLite `quick_check=ok`. Lead freshly confirmed installed/source hashes match,
`daemon_delivery_capable=true`, and unchanged live Core PID 3337483 start ticks
3500676316. This accepts the stale-heartbeat repair, not native wake task 4.3.

Receipts:
- `/tmp/codex-infoflow-20260908/heartbeat-supervisor-freshness.json`
- `/data/CoordExp/.codex/backups/codex-wake-me-up/20260908T152000Z-preinstall/reinstall-receipt.json`

## Compatible Core candidate boundary

The original July source lacks 28 request methods supported by the installed
Desktop Core, including queue admission. It must not replace the running Core.
The isolated candidate at
`/data/CoordExp/external/harness/codex-native-observe-0.153.4` instead uses exact
release tag `rust-v0.153.4`, commit
`3d2ee51ca2d5db578f328aa75e20aa22c0197c9a`, plus the observer patch.
The API comparison retains all 158 installed methods and adds only
`thread/agent/observe`. Protocol, exact invocation, historical turn, and queue
checks report 307 passes. CLI packaging and actual activation remain pending;
the unchanged same-version code-mode host can be retained without a V8 rebuild.
No shared Core restart has occurred and other active task state must be preserved.

## Lifecycle-only root wake check armed

The current task's preexisting MCP process still held the old source identity and
correctly rejected registration with `source_identity_mismatch` before creating a
monitor. The new installed CLI then armed the supported `thread_idle` condition on
the still-active Core package worker `01a08188-6a0e-7a30-8e66-cb013aae3621`, using
the CLI's explicit local target contract rather than claiming trusted MCP binding.
Monitor `a9852dac-e4ee-43ad-a6e2-3bb87be6d206` targets the current root
`01a08055-ae0c-7433-bb93-615f333c0edc`, has healthy supervision and a one-hour
expiry. Receipt:
`/tmp/codex-infoflow-20260908/core153-idle-arm-receipt.json`.
Root now ends its turn. Delivery and actual resumption remain to be observed;
this is not exact native invocation acceptance and does not complete task 4.3.

## Actual root resumption and candidate acceptance

The root received the monitor pointer in a new turn after its prior final reply.
The single decision-status read returned `state=recorded`, `wake_reason=condition`,
and `observed_pointer_count=1`. Its witness is the expected loaded child becoming
idle, classified as `heuristic_thread_lifecycle`. Arming-to-trigger elapsed time
was 267.362 seconds across six daemon evaluations. This establishes actual
root-final wake-to-review for this lifecycle condition; it does not establish
exact native invocation observation or a general exactly-once delivery guarantee.

The completed Core candidate is staged at
`/data/CoordExp/.codex/packages/standalone/releases/0.153.4-native-observe-20260908-x86_64-unknown-linux-gnu`.
Lead verified both binary hashes, reviewed activation/rollback guards, ran the
read-only activation plan successfully, and independently replayed the packaged
WebSocket proxy/observation/queue check in an isolated temporary home. Receipt:
`/tmp/lead-core153-package-5nybv0he/validation/packaged-proxy-smoke.json`.
All 158 installed request methods remain available and only the observer is added.
The candidate is accepted for coordinated activation, not yet active.

Another root (`01a06f6d-2336-7670-a929-8c4d56ed54ba`, "推进实验") was last observed
active. Fresh app listing and a direct `thread/read` did not produce a current
status; the direct call timed out. No shared restart is authorized by inference
about that other task's safety. Obtain the user's coordination decision before
interrupting its potentially active turn. Task 4.3 remains open until activation
and the installed native-worker checks finish.

## Activation attempt stopped; restart transferred to user

The user subsequently authorized restart. The guarded activation attempt sent
SIGTERM to the exact captured Core but it did not exit within 30 seconds; the
helper refused to change the installation pointer. The candidate remains staged,
not active. Evidence: `/tmp/codex-infoflow-20260908/core153-activation-runner.log`
and `core153-activation-runner-result.json` (`activation_exit_code=1`).

The user then explicitly requested that the agent stop handling app-server and
leave restart to the user. The recovery worker was interrupted and further
runtime operations stopped. Maintenance continuation monitor
`07db949b-9ef5-45f7-9d01-35df03483c88` was cancelled while admission remained
unattempted, with no queue receipt. Native activation/acceptance task 4.3 remains
open and is not automatically resumed by this cancelled monitor.

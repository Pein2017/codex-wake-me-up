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

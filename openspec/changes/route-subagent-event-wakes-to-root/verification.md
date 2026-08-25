## Verification receipt

- Change: `route-subagent-event-wakes-to-root`
- Installed version: `0.1.0+codex.20260822101648`
- Installed cache:
  `/data/CoordExp/.codex/plugins/cache/coordexp-local/codex-wake-me-up/0.1.0+codex.20260822101648`
- Lock-owning daemon PID: `965706`
- Loaded source identity:
  `02a72e77d5a5c6dda74500bf6e0c28719bc98c8d843bb6c859758300f3e66b5a`
- Heartbeat epochs: delivery `1`, event `1`
- Readiness: daemon, delivery, and event checks all returned `True`
- Installed `wait_for_event` schema properties: `condition`,
  `expires_in_seconds`, `idempotency_key`, `rearm_of`; `thread_id` and context
  are not model-facing.
- Source/cache byte comparison passed for the changed runtime, skill, and README
  surfaces.

## Acceptance commands

- `conda run -n ms pytest -q`: passed; 324 tests collected.
- `openspec validate route-subagent-event-wakes-to-root --strict`: passed.
- Serena diagnostics for changed Python source and tests: no errors or warnings.
- `git diff --check -- codex-wake-me-up`: passed.
- Plugin validator: passed before and after the cachebuster update.
- Independent Claude Opus 5 xhigh audit: the routing and identity corrections
  converged after one bundled correction round; its final narrow finding was
  resolved by specifying and testing fail-closed behavior for one-shot non-spawn
  delegates instead of expanding their lifecycle.

## Live boundary

No queue item was admitted during installation. The shared runtime ledger had
only terminal monitor states (`cancelled`, `fired`, `superseded`) before each
daemon switch. The original PID `4112580` and intermediate PID `956350` received
`SIGTERM`; the final exact cache daemon acquired the same runtime lock and
emitted the matching heartbeat.

The current Codex task was created before the MCP schema replacement, so a new
Codex task is required to expose the installed tool signature. A real V2 child
to root queue wake remains intentionally unspent in this task; the exact wire
shape and Core prohibition were source-verified, the resolver is covered through
depth two, and the installed daemon is ready for the first new-task use.

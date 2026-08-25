## Why

Multi-Agent V2 exposes a subagent's canonical `AgentPath` to the model but app-server thread APIs require its UUID `ThreadId`; the current MCP contract asks the model to supply that identity and therefore accepts invalid values such as `/root/worker`. Even with the UUID corrected, Codex intentionally rejects direct queue input to spawned V2 subagents, so a subagent cannot safely arm a goal-independent monitor that later wakes itself.

## What Changes

- Bind MCP monitor registration to Codex's out-of-band `_meta.threadId` instead of a model-supplied `thread_id`.
- Preserve direct `ThreadDelivery` for a caller whose own thread accepts direct input.
- When the caller is a spawned V2 subagent, walk its exact local `parentThreadId` chain to the root main-thread and deliver the wake pointer there.
- Record both the originating caller and the actual delivery target so the root can inspect provenance and decide what to do.
- Wake the root only. The plugin MUST NOT call `followup_task`, resume or recreate a subagent, or claim that the root accepted the worker/event result.
- Keep CLI registration explicitly targeted; the MCP self-targeting contract does not turn arbitrary MCP arguments into cross-thread authority.
- Fail closed when trusted MCP identity is absent, the ancestry is malformed or cyclic, or no eligible ancestor exists.

## Capabilities

### New Capabilities

None.

### Modified Capabilities

- `codex-wake-me-up-monitor`: Make MCP registration caller-bound and route unsupported spawned-subagent wake delivery to an eligible root ancestor without automatically continuing the child.

## Impact

- MCP API and packaged skill: `wait_for_event` no longer asks the model for `thread_id`; legacy explicit goal-delivery operations keep their existing target contract.
- App-server adapter and service: caller inspection, ancestry resolution, and origin/delivery provenance become explicit.
- Persistence and status: existing `ThreadDelivery` rows remain readable while new rows record the origin separately from the queue target.
- Runtime cost: a subagent-originated event may start one billed root turn. No child turn is automatically started.
- Codex Core is unchanged; its V2 direct-input prohibition remains authoritative.

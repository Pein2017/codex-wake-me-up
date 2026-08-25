## Context

See `proposal.md` for motivation. Codex supplies the current task UUID to MCP
calls as `_meta.threadId`; a V2 subagent's model-visible agent path is not a
thread UUID. Codex also deliberately rejects direct queue input to V2 spawned
subagents. The existing plugin already owns durable `ThreadDelivery` queue
admission, reconciliation, cancellation, and status, so this change belongs at
the MCP identity boundary and app-server target-resolution seam rather than in
Codex Core.

## Goals / Non-Goals

**Goals:**

- Make primary MCP registration self-bound to Codex's trusted caller identity.
- Resolve a spawned V2 caller to its topmost root before a monitor is armed.
- Preserve the origin and actual queue target as inspectable durable facts.
- Keep queue admission, uncertainty, cancellation, and status semantics on the
  actual root target.

**Non-Goals:**

- Resuming, recreating, messaging, or accepting a subagent automatically.
- Adding a second delivery path, changing Codex Core, or requiring a goal.
- Removing explicit targeting from the CLI or legacy goal-delivery operations.

## Decisions

### 1. The MCP boundary owns caller identity

`wait_for_event` receives a FastMCP context parameter and extracts only
`request_context.meta.threadId`; `thread_id` is removed from the public tool
schema. Missing metadata fails before service invocation. The MCP boundary calls
a dedicated trusted-entry service method whose internal binding is an
identity-only sentinel; explicit CLI JSON cannot manufacture it. This prevents a
model from confusing an agent path with a thread UUID or selecting another task,
and prevents the CLI from claiming MCP-authenticated provenance.

Alternative: keep `thread_id` and validate it. Rejected because it leaves both
the usability failure and cross-thread authority in the model-facing surface.

### 2. App-server reads resolve one frozen root target

The app-server adapter reads the origin. A directly deliverable caller remains
its own target. Only the exact wire shape
`source.subAgent.thread_spawn` identifies a spawned V2 subagent; review, compact,
memory-consolidation, and other one-shot delegate sub-sessions are not
reinterpreted as V2 workers and fail closed when their non-null parent identity
would otherwise invite retargeting. A V2 spawn is followed through
`parentThreadId` until a topmost parentless root is reached. Resolution is
cycle-checked and bounded to 64 nodes. Every node must be an exact local task;
the root must pass the existing queue preflight. The resolved chain is frozen in
the monitor delivery envelope at registration.

Alternative: choose the first ancestor accepting direct input. Rejected because
the user selected root-only policy and an intermediate agent must not become a
second scheduler. Alternative: change Core to accept direct subagent input.
Rejected because Core's prohibition is an intentional lifecycle boundary.

### 3. Origin and target have separate durable roles

The request's semantic identity uses the origin. The self-wait guard uses the
actual delivery target: a child may wait for its own turn to end, but it may not
wait on the root whose wake would make that same root active. The delivery
envelope stores `origin_thread_id`, the actual root `thread_id`, the relay chain,
and queue capability evidence. Existing queue, history, cancellation, and
reconciliation code continues to use `delivery.thread_id`, so no second writer
or fallback path is introduced. Old rows without origin provenance decode with
their target as the implicit origin.

Alternative: put the child UUID only in pointer text. Rejected because status
and idempotency need structured, durable provenance even if pointer text is
edited or deleted.

### 4. Delivery ends at waking the root

The pointer names the origin and tells the root to inspect immutable monitor
status.
No plugin component calls `followup_task`, sends a child message, starts a child
turn, or labels the child result accepted. The root task decides the next action
using its own current context.

## Terminal-state receipts

- Identity or ancestry failure: registration raises before a monitor row is
  armed; tests assert no service/ledger mutation.
- Root capability failure at registration: registration fails before arm through
  the existing preflight receipt.
- Capability loss after arm: existing `delivery_capability_unavailable` remains
  terminal; no alternate target is selected.
- Queue rejection, uncertainty, modification, cancellation, and recording keep
  their existing terminal states and refer to the frozen root target.
- Successful root history observation remains `recorded`; it is delivery
  evidence only, not worker or lead acceptance.

## Risks / Trade-offs

- [A wake consumes a root turn even if the event needs no action] -> Keep the
  pointer bounded and make the root the sole judgment owner.
- [The origin may no longer exist when the root wakes] -> Preserve its UUID and
  ancestry in status; do not attempt recreation.
- [Ancestry changes after registration] -> Freeze the validated target and chain;
  fail closed if the frozen root loses capability at dispatch.
- [Older monitor rows lack origin provenance] -> Decode target-as-origin and add
  new fields without a database migration.
- [Installed daemon may keep old code after cache replacement] -> Restart the
  exact lock-owning daemon and verify `/proc` command, `PYTHONPATH`, heartbeat
  epochs, source identity, and lock ownership.

## Migration Plan

1. Add contract tests and observe the new MCP and ancestry cases fail.
2. Implement caller extraction, bounded root resolution, and delivery provenance.
3. Run focused and full suites plus strict OpenSpec validation.
4. Bump the plugin cache version, reinstall from the local marketplace, restart
   the lock-owning daemon, and verify the loaded cache/source/heartbeat.
5. Roll back by reinstalling the preceding cache version and restarting the
   daemon; existing rows remain readable because provenance fields are additive.

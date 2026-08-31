## Context

See [proposal.md](proposal.md) for motivation. Terminal reservations already
provide a private descriptor, immutable publication, prepublication-before-bind
handling, multi-reservation `all`/`any`, heartbeat evidence, and one-shot root
delivery. The missing representation is a normally completed Agent turn without
a Git candidate. The generic descriptor adapter exists in Python but the CLI
currently accepts only a combined private payload containing the bearer.

The coordinated HarnessDock change consumes only the public descriptor and
terminal envelope. It must not read this plugin's ledger, daemon, queue, or
monitor status.

## Goals / Non-Goals

**Goals:**

- Reuse one terminal-event vocabulary for cooperative native L1 turns and
  HarnessDock Agent turns.
- Keep Git delivery attestation unchanged whenever complete scope is declared.
- Give existing launchers a descriptor-in/event-file-in publication boundary
  without putting tokens in arguments or output.
- Preserve one-shot monitor and acceptance semantics for singular and composed
  waits.

**Non-Goals:**

- No `agent_terminal` event kind, persistent wait group, nickname-to-ThreadId
  resolver, new delivery path, recurring subscription, or automatic re-arm.
- No native or HarnessDock launcher code in this repository.
- No installation, daemon restart, live Agent spend, or loaded-runtime claim in
  source acceptance.

## Decisions

### Reuse `worker_terminal` with optional delivery scope

Worker identity and settlement are common to write and read-only Agent turns;
Git delivery is optional evidence layered on top. Reservation normalization
therefore accepts either the complete current scope tuple or none of it. Partial
scope remains invalid. `delivered` remains valid only for a scoped reservation
and keeps the existing full-commit and attestation path.

Alternative: add `agent_terminal`. Rejected because it would duplicate reserve,
bind, publish, heartbeat, immutability, status, and condition-leaf behavior only
to remove a scope that already lives in semantic JSON.

### Add one non-success `completed` outcome

`completed` means only that the named producer turn ended normally. It never
carries a candidate commit, does not run Git, and always projects false task
success and lead acceptance. Existing `blocked`, `failed`, `cancelled`, and
`settlement_uncertain` retain their required bounded reasons and meanings.

Alternative: misuse `delivered` without a commit or `receipt_success`. Rejected
because each would silently strengthen lifecycle evidence into delivery or task
success.

### Expose descriptor preflight and publication through the CLI

Add one read-only descriptor-preflight command and a descriptor publication form
to the existing event CLI. Preflight reads the private descriptor, checks its
bearer against the ledger fingerprint, and compares the reservation's frozen
producer task with the launcher's expected identity. Publication reads the
descriptor plus a bounded terminal-event payload; the adapter validates worker
kind and event shape, then calls the same `MonitorService.publish_terminal_event`
sink already used by MCP and direct CLI publication. No second terminal writer
or ledger path is introduced.

Both commands print redacted status only. Descriptor, event, and expected
producer are arguments; the token remains inside the descriptor. Preflight is
read-only. Validation or identity failure is fail-closed before the terminal
sink changes state.

### Keep both wait policies as compositions of one-shot monitors

An all-settled wait uses one existing `all` monitor over several reservation
conditions and produces at most one root wake. A caller that needs continuing
first-settlement control arms one independent single-leaf monitor per member;
after a wake it may leave survivors armed or explicitly cancel them. Typed
`any` remains appropriate only when losing branches are intentionally consumed.
No persistent group state is added.

### Advance the event capability epoch without a schema migration

The new outcome and optional scope change semantic validation but not the
ledger columns. Advance `EVENT_CAPABILITY_EPOCH`; startup compatibility must
reject an older daemon that cannot interpret `completed`. Installation and
lock-owner restart remain a separate authorization gate.

### Classify every terminal write site

- MCP private-payload publication and the existing direct CLI form remain
  wake-eligible through `MonitorService.publish_terminal_event`.
- Descriptor CLI publication becomes another validated caller of that same
  sink, not another writer.
- Ledger identical retry remains idempotent; conflicting rewrite is fail-closed.
- Heartbeat, cancellation, reservation expiry, monitor expiry, and delivery
  reconciliation remain non-terminal-publication paths with their existing
  semantics.

## Risks / Trade-offs

- [A cooperative native child exits before publishing] -> The bound monitor
  wakes only at expiry; acceptance must record the missing terminal fact and may
  later justify a host-observed ThreadId integration.
- [Scopeless delivery weakens Git evidence] -> Reject `delivered` without the
  complete frozen scope and never synthesize attestation.
- [Descriptor or payload leaks a bearer] -> Require owned mode-0600 files and
  exclude token and input content from output and diagnostics.
- [Several independent first-settlement monitors fire close together] -> Queue
  admission remains one-shot per monitor; L0 may cancel survivors but must
  budget up to one wake per member.
- [Active Goal immediately restarts L0] -> Ordinary true sleep requires no
  active Goal; explicit legacy goal deferral remains separate.

## Migration Plan

1. Add RED tests for scopeless reservation normalization, `completed`, rejected
   scopeless delivery, decision evidence, descriptor preflight, and descriptor
   CLI publication.
2. Implement the smallest validation and CLI changes through the existing
   service/ledger sink and advance the event capability epoch.
3. Run focused tests, full pytest, strict OpenSpec validation, lint/compile
   checks, and a source-only descriptor publication vertical slice.
4. Let the coordinated HarnessDock source change consume this frozen interface.
5. Stop before install or daemon restart. A separately authorized activation
   must reinstall, restart the exact lock owner, and verify loaded source/epoch.

Rollback removes `completed`, restores mandatory scope, removes the descriptor
CLI form, and restores the prior epoch only after the current binary's
compatibility check permits that target.

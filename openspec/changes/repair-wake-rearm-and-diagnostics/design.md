## Context

See `proposal.md`. The observed re-arm failure named a thread monitor whose state
was `queue_accepted` after its root pointer had been inspected. The state is
intentionally nonterminal because reconciliation can still reach `recorded`,
modified, cancellation, or uncertainty. Two `thread/read` calls later timed out,
but retained evidence does not establish why. A separate readiness failure exposed
only a generic capability message; its historical heartbeat has since been
overwritten.

A later live receipt closes a different issue: after its producer completed and
the condition was claimed, delivery preflight timed out before
`admission_attempted_at` was set. The catch-all preflight handler then wrote the
terminal `delivery_capability_unavailable` state, so no pointer was queued. This
establishes delivery loss from transient preflight handling without establishing
why Core did not answer within ten seconds.

Core currently owns both the canonical `AgentPath` to `ThreadId` mapping and the
native `AgentStatus` watch. Existing app-server thread status, history, and queue
surfaces do not expose equivalent lifecycle evidence.

## Goals / Non-Goals

**Goals:**

- Let a consumed queue delivery parent fresh lineage without changing its lifecycle.
- Make the next registration failure identify the narrow failed stage and current
  readiness reason.
- Recover a claimed delivery from transient read-only preflight transport failure
  without permitting a second queue-add or retrying an uncertain send.
- Freeze the smallest native lifecycle contract and stop at the Core boundary.

**Non-Goals:**

- Retrying or resending queue input after admission, reclassifying delivery as task
  success, or changing reconciliation/cancellation behavior.
- Inflating timeouts or claiming the cause of a `thread/read` timeout is repaired.
- Polling agent idle state, parsing transcripts, adding a launcher-side publisher,
  or building another scheduler, messenger, or composition engine.

## Decisions

### 1. Lineage settlement is narrower than monitor terminality

Re-arm validation accepts the existing terminal set plus `queue_accepted`; the
global terminal set remains unchanged. This is the smallest change that reflects
the consumed trigger and queue-add attempt while preserving daemon residency and
history reconciliation. Adding `queue_accepted` to the terminal vocabulary was
rejected because it would strand reconciliation and alter cancellation behavior.

Receipt: the parent row remains `state=queue_accepted` with its original
`queue_receipt`; the child row stores `rearm_of`. The regression asserts one parent
queue-add and no inherited success flag.

### 2. Diagnostics inspect failure; they do not alter control flow

The service labels the two registration app-server boundaries:
`resolve_delivery_target` and `prepare_condition`. On a failed delivery-daemon gate,
a pure heartbeat/lock inspection reports the first current mismatch in the same
order as readiness validation. It performs no start, wait, write, or retry.

The historical read timeout cannot be root-caused from overwritten runtime state.
Although `thread/read(includeTurns=false)` still consults the persisted ThreadStore,
that source fact alone does not prove the stall. Replacing it with a weaker endpoint
is therefore rejected in this slice.

Receipt: pre-arm failures contain `stage=<stage> reason=<reason>` in the typed error;
tests provide sanitized timeout and heartbeat fixtures.

### 3. Native worker observation requires a Core-owned read seam

The host surface is experimental `thread/agent/observe`, scoped by trusted root
`rootThreadId` plus public canonical `taskName`. Binding omits expected IDs; only a
response with both `childThreadId` and persisted-turn `invocationId` may arm.
`bindPending` therefore rejects before row creation. Every later read supplies both
expected IDs and queries that historical invocation, so reuse of the same task and
child thread cannot silently rebind the monitor.

The frozen status vocabulary is `bindPending`, `running`, `interrupted`,
`completed`, `failed`, `unavailable`, and `mismatch`. Persisted-turn `interrupted`,
`completed`, and `failed` are terminal settlement for the exact invocation; none is
whole-task success or lead acceptance. Core persists shutdown during a turn as
`interrupted`; there is no separate wire-only terminal. `unavailable` and `mismatch`
fail closed. Existing `thread_idle` remains heuristic and cooperative
`worker_terminal` remains a different producer contract. Core owns neither monitor
delivery nor composition.

Receipt: source tests cover exact binding, already-finished facts, all terminal
settlements, disappearance/mismatch, historical invocation reuse, existing `all`,
and one root queue admission. Live acceptance still requires installed Core/plugin
sources and real native children after separate authorization.

### 4. Error-path disposition

Transport classification is typed at the app-server boundary rather than inferred
from error text. Socket absence, connection/write failure, WebSocket closure, and
request timeout are transport failures. Wrong runtime root, unsafe socket ownership
or permissions, server rejection, unsupported target capability, and malformed
protocol data remain definitive failures.

Before admission, a transport failure stores a bounded diagnostic while preserving
`claimed` plus `unattempted`; the existing daemon cadence retries only the read-only
preflight. Condition expiry bounds observation and may itself cause the one claim;
it is not a post-claim delivery deadline. A claimed preflight therefore remains
pending until transport recovers, the user cancels it, or a definitive preflight
failure terminalizes it. After `admission_attempted_at` is set, existing
reconciliation applies and queue-add is never retried.

| Path | Disposition | Durable/observable receipt |
|---|---|---|
| First delivery-daemon gate fails | justified fail-closed before row creation | staged typed error with current reason |
| Target resolution/preflight fails | justified fail-closed before row creation | `resolve_delivery_target` error |
| Condition preparation fails | justified fail-closed before row creation | `prepare_condition` error |
| Second daemon gate loses after row creation | existing terminal `daemon_unavailable`, unchanged | stored `delivery_daemon_mismatch_before_arm` |
| Claimed delivery preflight transport fails | remains `claimed` and retries read-only preflight on the daemon cadence | `preflight_transport_unavailable`; no admission timestamp or queue receipt |
| Claimed delivery preflight is definitively denied or malformed | terminal capability failure before queue-add | `delivery_capability_unavailable` |
| Queue-add response is transport-uncertain | admission remains consumed; reconcile without another add | existing `admission_in_progress` receipt |
| Prior pointer is `queue_accepted` | reconciliation remains live; lineage alone is allowed | parent queue receipt plus child `rearm_of` |
| Native bind is pending or unavailable | justified fail-closed before row creation | `bindPending` or `unavailable` response |
| Exact native invocation disappears or mismatches | existing observer-failure wake path | bounded native identity evidence; no success claim |

## Risks / Trade-offs

- [A readiness reason can change immediately after rejection] -> Label it as the
  current post-failure diagnosis, not the historical cause.
- [Lineage may precede final history recording] -> Preserve the parent's live state
  and never inherit delivery or success facts.
- [Core source and installed runtime can differ] -> Bind source tests to the frozen
  wire schema and keep live acceptance pending until exact-source installation.

## Migration Plan

No persistence migration is needed. In particular, an already terminal historical
delivery-loss row is not reopened or requeued automatically. Source verification
does not activate the installed plugin. Installation, daemon restart, Core changes,
and real native root-final wake remain separate user-authorized acceptance steps.

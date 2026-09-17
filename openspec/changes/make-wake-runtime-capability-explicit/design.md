## Context

See `proposal.md` for the observed user-facing failures. The present service
creates its ledger row only after target delivery resolution and condition
preparation; queue admission is later a separate, durable at-most-once path.
The live daemon has outstanding monitors, so this change must be source-tested
without replacing its running binary.

## Goals / Non-Goals

**Goals:**

- Make the installed Core/native-worker boundary observable and compact.
- Recover one safe pre-arm read failure without crossing any durable side-effect
  boundary.
- Preserve exact execution/result references and distinguish delivery
  consumption from acceptance.

**Non-Goals:**

- Add a scheduler, Core endpoint, fallback condition, second queue add, or
  automatic daemon replacement.
- Claim installed native-worker wake acceptance while the live Core rejects the
  required observation method.

## Decisions

### Capability probe is read-only and tri-state

The app-server adapter will sanitize JSON-RPC rejections into typed bounded
errors and probe only method dispatch for native observation. `unknown variant`
is `unavailable`; a recognised method is `available`; connectivity or ambiguous
shape is `indeterminate`. This avoids the current full method enumeration and
does not use a real task or invoke a fallback. A version allowlist was rejected:
it would drift from the actual installed Core.

### Retry boundary is before the first durable write

`wait_for_event` will wrap only target-resolution and condition-observation
reads in a two-attempt helper for `AppServerTransportError`. A failed retry
becomes a structured pre-arm receipt through the MCP surface; no ledger row,
binding, daemon start, or queue operation is touched. Once `create_or_get`
returns, existing daemon-unavailable and queue reconciliation paths retain
their present terminal/recovery policy. Retrying broader `AppServerError` or
any post-write operation was rejected because it can duplicate irreversible
effects or mask a deterministic incompatibility.

### Thread-idle has a runtime-only read mode

`read_observation(include_goal=False)` retains the identity/runtime validation
but stops before `thread/goal/get`. Only the thread-idle preparation and
reconciliation callers use it; goal delivery and callers that validate goals
keep the existing full observation. This targets the observed long-history
registration cost without changing guard semantics.

### Terminal references remain opaque evidence

`WorkerTerminalEvent` gains bounded strings for publisher invocation and result
location. They are serialized and fingerprinted, with an explicit provenance
label. No filesystem, URI, or report lookup follows them. A generic artifact
resolver was rejected because it adds a trust boundary and a scheduler-like
workflow without improving the wake guarantee.

### Target status observation is a narrow ledger acknowledgement

The MCP status handler uses only its trusted caller identity. The service marks
one additive timestamp if that identity exactly matches the frozen delivery
target and a delivery receipt exists. A current-monitor query applies the same
scope. It neither acknowledges queue receipt nor changes terminal status;
therefore status can never establish acceptance.

### Terminal-write classification

| Site | Classification | Receipt / test |
| --- | --- | --- |
| Pre-arm resolution and preparation | Retryable only for typed read transport failure; otherwise no write | `monitor_created=false`; pre-arm retry tests |
| `create_or_get` then pre-arm readiness loss | Existing justified fail-closed transition | `daemon_unavailable`; existing readiness tests |
| Trigger claim and event publication | Existing wake-eligible single claim | durable claim/event; existing event tests |
| Queue admission and recovery | Existing at-most-one attempt; never retried here | delivery ID/reconciliation; existing thread-delivery tests |
| Target status observation | Additive non-terminal acknowledgement | `target_status_observed_at`; new status tests |

## Risks / Trade-offs

- [Core error wording changes] → classify only the known unsupported-method
  signature; otherwise return `indeterminate` rather than a false positive.
- [A retry increases wait time] → exactly one retry, only before any write, and
  return a retry-safe receipt after exhaustion.
- [Old rows lack acknowledgement metadata] → decode it as absent; no migration
  rewrites historical monitor facts.
- [Source passes but installed daemon is older] → do not restart it; live
  acceptance is limited to read-only capability evidence until an explicit
  coordinated installation.

## Migration Plan

1. Add additive ledger decoding/defaults and focused regressions before changing
   the service path.
2. Implement adapter/service/MCP behavior and update agent/operator guidance.
3. Run focused, full-suite, formatting, compilation, strict OpenSpec, and a
   read-only installed capability probe. Rollback is source rollback with no
   daemon or ledger mutation from this change.

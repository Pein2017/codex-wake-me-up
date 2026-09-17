## Why

The normal `native_worker_terminal` guidance currently recommends a Core read
surface that is absent in the installed environment, while pre-arm `thread/read`
timeouts can reject a request without making clear that no monitor exists. A
worker can also finish with useful result location evidence that is not bound to
the exact execution or carried through the wake report.

## What Changes

- Add a read-only capability report for the installed Core, plugin, and daemon,
  and make unavailable native-worker observation fail closed with a compact,
  structured reason. There is no implicit `thread_idle` fallback.
- Make pre-arm registration failures state whether a monitor was created, which
  stage failed, and whether one bounded retry was safe; retry only typed,
  read-only target-observation transport failures before any ledger row or queue
  admission exists.
- Reduce `thread_idle` observation to its runtime facts so it does not make an
  unrelated goal read, while retaining its heuristic, non-success semantics.
- Extend a worker terminal event with optional bounded publisher-declared
  invocation and result-reference fields, expose them in the wake report, and
  record when the exact trusted delivery target reads status. A status read is
  delivery-consumption evidence, never task or lead acceptance.
- Add a scoped current-monitor view and concise human registration receipt; keep
  normal use centered on register, status, and cancel.

No user-owned material-cost decision, irreversible local behavior, or weakening
of the single-trigger, single-admission, or fail-closed rules is proposed. The
change neither implements an unavailable Core endpoint nor restarts or installs
over a live daemon; real native-worker wake acceptance remains HOLD until that
endpoint is available in an installed Core.

## Capabilities

### New Capabilities

None.

### Modified Capabilities

- `codex-wake-me-up-monitor`: Make runtime capability, pre-arm retry safety,
  scoped inspection, read-consumption, and compact operator-facing semantics
  explicit.
- `codex-terminal-events`: Carry bounded publisher-declared execution and result
  references through terminal evidence without promoting them to acceptance.

## Impact

- `src/codex_wake_me_up/{app_server,service,ledger,mcp_server,terminal_events}.py`
  and their focused tests.
- Packaged wake-me-up skill and README guidance, plus this OpenSpec change.
- Existing SQLite rows receive only additive metadata; no dependency, new
  scheduler, delivery route, Core patch, or daemon restart is introduced.

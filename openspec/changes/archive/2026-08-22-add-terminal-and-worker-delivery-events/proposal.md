## Why

Long-running shell commands and delegated workers still force a Codex lead to
choose between repeated model-level polling and ending its turn without a
reliable completion wake. The existing `receipt_success` leaf can prove only
success after a monitor exists; it cannot reserve a receipt before launch,
wake immediately on failure, or identify the exact Git commit a worker is
delivering for lead review.

## What Changes

- Add an expiring, single-use terminal-event reservation that can be created
  before a command or worker starts, then bound to exactly one monitor without
  pausing or activating any goal during reservation.
- Add typed terminal events for commands and workers. Command events carry a
  bounded execution receipt; worker events distinguish delivered, blocked,
  failed, and cancelled outcomes and may carry one exact Git candidate commit.
- Add `command_terminal`, `worker_terminal`, and `heartbeat_stale` condition
  leaves. Explicit terminal events authorize a wake but do not by themselves
  claim task acceptance; stale heartbeats remain heuristic evidence.
- Attest worker Git deliveries against the reservation's captured repository,
  worktree, and baseline commit, and return the candidate commit plus bounded
  changed-path evidence to the lead. A delivery event never merges,
  cherry-picks, accepts, or otherwise changes Git state.
- Extend the self-describing wake report so a lead can identify the producer,
  terminal status, candidate review target, execution evidence, and any
  attestation failure with one status call.
- Preserve `receipt_success` and every existing condition as compatible
  behavior; this change adds no second activation path and does not weaken the
  one-claim, one-activation, or fail-closed rules.

No material command, worker, or model spend is authorized merely by this
change. Registering a deferred event remains an explicit operator/agent action,
and any resulting goal activation remains a real billed continuation under the
existing guarded wake contract. No irreversible local behavior or weakening of
the standing safety invariants is proposed.

## Capabilities

### New Capabilities

- `codex-terminal-events`: Reserve, publish, validate, and inspect bounded
  command-terminal and worker-terminal events, including optional heartbeats
  and Git candidate-delivery evidence.

### Modified Capabilities

- `codex-wake-me-up-monitor`: Consume terminal-event reservations through new
  typed condition leaves, classify terminal versus heuristic evidence, and
  include terminal and delivery evidence in the one-call wake report.

## Impact

- Runtime and persistence: `ledger.py`, `models.py`, and runtime-root records
  gain a reservation/event owner separate from monitor lifecycle state.
- Observation and wake policy: `conditions.py` and `service.py` gain new typed
  leaves, evidence classes, reservation binding, and bounded Git attestation.
- Producer surfaces: MCP and CLI gain reserve, heartbeat, and terminal-publish
  operations. The existing shell or worker harness remains the command owner;
  the plugin still does not accept or execute arbitrary shell commands.
- Operator and agent guidance: `README.md` and `skills/wake-me-up/SKILL.md` gain
  command and worker event workflows and retain the rule that lead acceptance
  requires independent review.
- Verification: unit, persistence/recovery, Git-worktree, daemon, and
  disposable local app-server tests cover success, failure, invalid delivery,
  early publication, stale heartbeat, expiry, cancellation, and at-most-once
  wake behavior.

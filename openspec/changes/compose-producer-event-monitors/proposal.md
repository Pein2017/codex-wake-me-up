## Why

Long-running jobs and heterogeneous agent batches can already publish durable
terminal evidence, but one monitor can bind only one reservation and launch
receipts do not give the model a uniform condition to arm. This leaves callers
choosing between a blocking foreground join and ending the turn without any
later wake, as happened when one native Codex worker and two HarnessDock workers
were launched asynchronously with no monitor.

## What Changes

- Allow one monitor to bind several distinct terminal-event reservations
  atomically and evaluate them through the existing typed `all`/`any` condition
  AST. Each reservation remains single-consumer, and the monitor remains
  one-shot with at most one queue admission.
- Add a one-shot `git_ref_change` condition that captures an exact local
  worktree, Git common directory, ref, object format, and baseline commit, then
  wakes with bounded classification for advance, rewrite, deletion, retarget,
  or observer failure. It never reports task or worker success.
- Add `settlement_uncertain` as an explicit terminal worker outcome so a
  producer result that cannot be validated does not remain indefinitely
  `working` or get mislabeled as failure certainty.
- Return a compact, non-secret `monitor_condition` projection from terminal
  reservation creation so an existing launcher or harness can pass the private
  publisher descriptor to its producer and the lead can arm the returned
  condition without reconstructing it.
- Clarify the model-facing interaction: “do not wait” skips a synchronous join,
  not an asynchronous arm. After a long durable producer is launched and no
  independent work remains, the lead arms `wait_for_event`, requires an
  `armed` receipt, and ends its turn.
- Preserve launcher ownership. The plugin does not execute arbitrary shell
  commands, scrape another plugin's private ledger, install Git hooks, treat
  tmux as job completion, automatically re-arm, or add a second wake path.

This change does not weaken fail-closed delivery or evidence semantics. A fired
monitor may create one billed Codex turn, but a ref change, PID exit, terminal
event, or uncertain settlement remains evidence rather than a success claim.

## Capabilities

### New Capabilities

None.

### Modified Capabilities

- `codex-wake-me-up-monitor`: Compose several reserved terminal events in one
  monitor, add bounded Git-ref observation, and define the non-blocking arm
  interaction without changing monitor or delivery state machines.
- `codex-terminal-events`: Expose a compact monitor condition from reservation,
  bind several reservations transactionally to one monitor, and preserve
  settlement uncertainty as an explicit terminal worker result.

## Impact

- Condition parsing/evaluation and decision projection:
  `src/codex_wake_me_up/conditions.py` and
  `src/codex_wake_me_up/service.py`.
- Durable event binding, migration, and transactional evaluation:
  `src/codex_wake_me_up/ledger.py`.
- Worker payload validation and producer adapter:
  `src/codex_wake_me_up/terminal_events.py` and
  `src/codex_wake_me_up/worker_delivery_adapter.py`.
- MCP/CLI reservation receipts, packaged skill, README, OpenSpec, and focused
  plus full regression coverage.
- HarnessDock or another launcher may consume the descriptor and returned
  condition through a separate producer-side integration. This repo will not
  edit, inspect, or depend on HarnessDock private state.

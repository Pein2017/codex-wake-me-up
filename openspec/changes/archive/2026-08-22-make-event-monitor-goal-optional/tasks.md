## 1. Freeze authority, baselines, and live capability

- [x] 1.1 Freeze exact ownership and hashes for both active changes and shared
  implementation files; validate `add-terminal-and-worker-delivery-events`
  strictly, record its source failure set/event epoch, and sync its delta specs
  before integrating this change's overlapping requirements.
- [x] 1.2 Run
  `RTK_HOOK_DISABLE=1 conda run -n ms python -m pytest -o addopts= -q`,
  record exact failing-test identities and source/runtime identity, and compare
  final failures to that set rather than to a pass count.
- [x] 1.3 Probe the installed app-server read-only with
  `experimentalApi: true`; record Codex version, control socket, initialize
  response, queue/thread response shapes, exact target classes, queue capacity,
  and whether loaded, active, interrupted, and stored-unloaded semantics match the
  frozen design. Stop on a contract-changing mismatch.
- [x] 1.4 Record the isolated `turn/startIfIdle` Core draft and its exact diff as
  superseded and retain that diff as a historical patch artifact. Do not build or
  install it; after queue acceptance, retire only those task-owned draft edits and
  prove the worktree returned to its pre-draft state.
- [x] 1.5 Freeze authoritative V1 constants: one queue-add attempt, stable
  `clientUserMessageId=deliveryId`, bounded pointer schema/digest, 60 seconds of
  online absence reconciliation, queue capacity 100 as an observed external
  ceiling, and no exactly-once claim.

## 2. Add delivery models and compatible persistence

- [x] 2.1 Add failing tests for exactly-one `ThreadDelivery`/`GoalDelivery`,
  thread admission with null/active/paused/blocked goal states, explicit goal
  admission, ambiguous delivery, unsupported targets, idempotency conflict, and
  the invariant that no path creates a goal.
- [x] 2.2 Add the versioned delivery envelope, stable delivery ID, bounded pointer
  model/digest, capability identity, queue receipt, reconciliation facts, and
  typed delivery outcomes without making `TargetGuard.goal` nullable.
- [x] 2.3 Add an additive ledger migration and transactional methods for delivery
  creation, one admission-attempt consumption, queue ACK, reconciliation,
  cancellation, and terminal outcome; preserve event-reservation binding in the
  same create-or-get transaction.
- [x] 2.4 Add historical fixtures for every pre-change mode/state and prove absent
  delivery tags decode as `GoalDelivery` without database rewrite or loss of
  pause, claim, activation, evidence, expiry, event, or terminal facts.
- [x] 2.5 Add delivery-epoch downgrade checks that refuse while a nonterminal
  thread delivery or matching queued pointer exists and permit completed redacted
  history and legacy goal rows.

## 3. Implement the experimental app-server queue adapter

- [x] 3.1 Add failing adapter tests for experimental initialization, exact target
  read-only preflight including `thread.can_accept_direct_input`, queue
  add/list/update/delete, thread read with turns, and stored-thread resume; cover
  accepted, malformed, rejected, unavailable, and transport-uncertain responses.
- [x] 3.2 Implement one dedicated local connection that enables the experimental
  API, validates Codex version/response shapes, preserves the exact thread ID, and
  exposes typed queue/thread receipts without accepting remote/TCP targets.
- [x] 3.3 Implement one bounded queue-add request carrying the stable delivery ID
  and pointer digest; reject queue full, archived/deleted/ephemeral targets, and
  unsupported spawned-subagent targets using `thread.can_accept_direct_input`
  without retargeting.
- [x] 3.4 Implement safe resume/load with a persisted pre-resume snapshot of items
  ahead of the pointer and their billed-work boundary. Rely only on automatic FIFO
  drain from enqueue, turn completion, or resume; never call
  `thread/queue/start`, ordinary `turn/start`, steer, inject, Desktop messaging,
  CLI resume, or goal mutation. Test active turns remain unsteered, earlier user
  items keep priority, and stored targets use the central app-server only.
- [x] 3.5 Implement exact queue/history inspection and removal with bounded
  payloads and redaction; prove tokens, raw commands, logs, credentials, and full
  event reports never enter the pointer or adapter logs.

## 4. Integrate thread delivery and truthful reconciliation

- [x] 4.1 Route condition satisfaction, expiry, and irrecoverable observer failure
  through the same durable claim and immutable delivery tag; prove transient
  unknown observations remain armed without consuming a claim.
- [x] 4.2 Consume the sole thread admission attempt before queue-add, persist a
  conclusive ACK, and add crash/timeout tests for before-send, uncertain response,
  accepted-before-dispatch, and daemon restart. Every in-progress or uncertain
  queue admission must reconcile history then queue then the online window, with
  no plugin re-add; goal activation uncertainty remains immediately fail-closed.
- [x] 4.3 Implement reconciliation in authority order—exact history, exact queue,
  then the persisted 60-second online window—and test recorded, queued, app-server
  offline, unresolved absence from user deletion/crash/archive/storage failure,
  modified content, and observed duplicate pointer outcomes.
- [x] 4.4 Implement shared-queue status for position, prior items, reorder,
  interrupted stall, pending work, queue full, and user edit/delete. Never restore
  user-owned text or claim a delivery deadline; prove resume can release prior
  user items and records that pre-resume snapshot.
- [x] 4.5 Implement cancellation races before admission and after queue acceptance:
  conclusive removal, history/too-late, and ambiguous absence must each have a
  durable typed receipt and regression test.
- [x] 4.6 Preserve `GoalDelivery` pause/idle/guard/activation behavior and its
  complete prior failure set; prove delivery kinds never switch or fall back to
  one another.
- [x] 4.7 Extend daemon recovery and readiness for delivery epoch, source identity,
  app-server capability drift, armed observation, accepted-queue reconciliation,
  and restart without model-level polling.

## 5. Expose primary event monitoring and update guidance

- [x] 5.1 Implement prompt `wait_for_event` registration for an exact local task
  regardless of goal state, with required idempotency, detached daemon ownership,
  bounded expiry, prompt receipt, and `next_action=end_current_turn`; test MCP
  disconnect before versus after durable arm.
- [x] 5.2 Implement `defer_goal_until_event` as explicit `GoalDelivery`; active
  goals use watcher-first pause, paused goals use stable capture, and
  null/blocked/unloaded goals reject without creation or compensation.
- [x] 5.3 Keep `wake_me_up` and `wake_me_up_defer` as thin compatibility aliases;
  test payload/idempotency mapping and unchanged legacy outcomes.
- [x] 5.4 Extend status, list, cancel, re-arm lineage, CLI controls, and MCP schemas
  for delivery kind, queue receipt, reconciliation, pointer count, terminal
  uncertainty, and one-call self-describing wake reports.
- [x] 5.5 Update README and the packaged skill to make event monitoring primary,
  goals optional, queue delivery billed and not exactly-once, and capability
  failure non-compensating. Retain the 15-minute/no-independent-work threshold,
  failure-signature guidance, one-status-call recovery, and “wake is not success”
  boundary.
- [x] 5.6 Document why the 30-second MCP timeout is not the condition lifetime and
  why blocking MCP/CLI wait, `codex exec resume`, Core CAS, steer, inject, and
  Desktop coordination are rejected delivery architectures.

## 6. Preserve terminal-event and evidence contracts

- [x] 6.1 Re-run command/worker reservation, publication, expiry, cancellation,
  authentication, idempotency, producer identity, token redaction, and
  daemon-restart regressions with `ThreadDelivery` as well as legacy goal
  delivery.
- [x] 6.2 Re-run Git attestation regressions for valid, invalid,
  baseline-mismatch, out-of-scope, ceiling, and attestation-error outcomes; prove
  the pointer and status never expose secrets or claim acceptance.
- [x] 6.3 Add a complete terminal-write table fixture matching `design.md`; every
  retryable, wake-eligible, rejected, queued, recorded, modified, uncertain,
  cancelled, legacy, and goal fail-closed disposition needs its own asserted
  durable receipt.

## 7. Verify source, replace the plugin, and retire the Core draft

- [x] 7.1 Run focused new tests with observed RED or sensitivity evidence, then the
  full frozen pytest command; resolve every newly introduced failing identity.
- [x] 7.2 Run
  `conda run -n ms python -m compileall -q src tests`, strict validation for both
  active changes and all specs, plugin validation, and `git diff --check` limited
  to exact task-owned paths; record source identity and outputs.
- [x] 7.3 Run a source-level app-server smoke proving registration returns promptly
  while daemon observation persists and no goal or second app-server process is
  created.
- [x] 7.4 Cache-bust/install the exact verified plugin, identify and restart only
  the lock-owning old daemon, and prove loaded cache/source/delivery/event epoch
  parity before live delivery.
- [x] 7.5 With disposable exact tasks, verify loaded-idle queue start, active-turn
  separate FIFO delivery, stored-unloaded resume-to-deliver, stable delivery-ID
  history correlation, pre-resume user-item priority and spend, queue
  delete/reorder visibility, interrupted stall, cancellation, daemon restart, and
  app-server restart. Record monitor, queue, thread, message, and turn facts
  without claiming exactly-once.
- [x] 7.6 Run disposable command success/failure and valid/invalid worker-terminal
  event deliveries through the queue, plus the prerequisite's legacy paused-goal
  compatibility smoke; capture evidence/acceptance flags and update its remaining
  live receipt truthfully.
- [x] 7.7 After queue acceptance is complete, remove only the frozen task-owned
  `turn/startIfIdle` draft from its isolated Core worktree after preserving the
  historical patch; verify no unrelated path changed and the installed Codex
  binary was never replaced.

## 8. Sync contracts and report acceptance

- [x] 8.1 Sync the prerequisite delta before this change's delta, update capability
  purposes where required, and run strict validation against the resulting main
  specs without archiving either change.
- [x] 8.2 Record exact source, installed-runtime, daemon, queue/history, failure-set,
  and OpenSpec receipts; distinguish queue admission from recorded delivery and
  list every residual crash-window limitation.
- [x] 8.3 Produce the native depth-2 pilot receipt: topology, model/effort routes,
  L1 acceptance packet, L2 tickets, correction rounds, L0 interventions,
  escalations, wall time, and measured versus inferred token/cost evidence.
- [x] 8.4 Do not claim general availability if capability preflight, installed
  source parity, or any decision-bearing disposable smoke fails. Leave the prior
  plugin loadable and report the typed blocker instead of substituting goal
  creation or another delivery mechanism.

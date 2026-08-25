## 1. Freeze the baseline and event-reservation contract

- [x] 1.1 Run `conda run -n ms pytest`, record the exact pre-change failing-test identities (not only the pass count), and preserve that failure set for the final regression diff.
  - Baseline receipt (2026-08-20 UTC): 104 tests collected across app-server 10, conditions 38, ledger 7, runtime 2, and service 47; `rtk run 'conda run -n ms python -m pytest -q'` exited 0 with an empty failing-test set. At baseline the direct rewrite path misleadingly printed `No tests collected`; the companion RTK hook fix now routes pytest through generic `rtk test`, and final acceptance used the unambiguous direct command with rewrite disabled.
- [x] 1.2 Add typed event/reservation models and strict validators for event kind, lifecycle state, terminal outcome, bounded identifiers, timestamps, sequence numbers, and payload budgets; first add tests that reject unknown enum values, oversized fields, raw command text, and malformed terminal payloads.
- [x] 1.3 Add an additive SQLite migration for durable `event_reservations`, persisting only a salted publish-token digest; cover reserve, inspect, single-use authentication, cancel, expiry, and token-redaction behavior with tests.
- [x] 1.4 Implement one-transaction monitor insert/idempotent reuse plus binding for at most one distinct reservation, with kind/cardinality checks; test shared terminal/heartbeat references, early terminal publication, double binding, distinct-ID and duplicate-terminal rejection, kind mismatch, reuse after terminal state, and bind/cancel/expiry races across separate SQLite connections.
- [x] 1.5 Define daemon-restart recovery for reserved, bound, terminal, cancelled, and expired events, and add restart tests proving no terminal event is lost or delivered twice.

## 2. Implement the producer event lifecycle

- [x] 2.1 Implement monotonic bounded heartbeats authenticated by the reservation token; test valid advancement, duplicate/out-of-order sequences, invalid tokens, post-terminal heartbeats, and payload-budget failures.
- [x] 2.2 Implement exactly-once `command_terminal` publication for `succeeded`, `failed`, `cancelled`, and `signaled`, carrying bounded exit/signal evidence plus a command digest but never raw command text; test idempotent replay and reject conflicting rewrites.
- [x] 2.3 Implement exactly-once `worker_terminal` publication for `delivered`, `blocked`, `failed`, and `cancelled`; require exactly one full Git object ID for `delivered`, forbid it for non-delivery outcomes, and test idempotent replay and conflicting rewrites.
- [x] 2.4 Add reserve, inspect, bind, cancel, heartbeat, and terminal-publish operations to the service layer and expose narrow MCP/CLI adapters using JSON payload files; add source-path tests for each adapter and a mode-`0600` publisher descriptor.
- [x] 2.5 Extend status output with redacted reservation, producer, heartbeat, terminal-event, and Git-attestation fields; prove neither status nor logs expose the raw publish token or raw command.

## 3. Attest worker Git deliveries without taking Git ownership

- [x] 3.1 Add disposable repository/worktree fixtures that freeze the canonical worktree, common Git directory, object format, baseline commit, and allowed path prefixes; test rejection of invalid repositories, detached or changed baselines where disallowed, unsafe path prefixes, and mismatched worktrees.
- [x] 3.2 Implement the read-only Git adapter with fixed direct `git` argv (no shell), scrubbed Git environment, replacement objects disabled, and pre/post repository identity checks: validate a full commit OID, prove the object is a commit and descends from the frozen baseline, and enumerate changed paths with NUL-delimited output.
- [x] 3.3 Classify attestation as `valid`, `invalid_commit`, `baseline_mismatch`, `out_of_scope`, or `attestation_error`; test every class and assert that refs, index, worktree files, configuration, and remotes are unchanged by attestation.
- [x] 3.4 Bound attestation receipts to 256 paths and 64 KiB and the complete scan to 5 seconds/8 MiB; retain total count and deterministic digest only after fully consuming the path set, and test exact boundaries plus over-ceiling `attestation_error` behavior.
- [x] 3.5 Make every terminal worker outcome—including invalid or failed Git attestation—wake-eligible so a bad delivery cannot strand the lead; only `valid` may be labelled a reviewable candidate, and no outcome may be labelled accepted.

## 4. Integrate event leaves into the single wake path

- [x] 4.1 Add and parse the `command_terminal`, `worker_terminal`, and `heartbeat_stale` condition leaves, including `all`/`any` compositions and deterministic tri-state handling for missing or corrupt event records.
- [x] 4.2 Add a Ledger-owned one-transaction event evaluate-and-claim seam that reloads the armed monitor and bound event rows, uses a post-lock host time, and atomically stores evaluation plus optional claim; test terminal publication before binding, terminal-vs-stale and terminal-vs-expiry races across separate connections, and at-most-one continuation.
- [x] 4.3 Update authorization so a correctly bound command/worker terminal event can authorize a condition wake without heuristic opt-in, while legacy heuristic leaves still require opt-in and `receipt_success` keeps its current task-success meaning.
- [x] 4.4 Implement `heartbeat_stale` only as an anomaly heuristic, not progress streaming; test never-heartbeated, advancing, stale, terminal, cancelled, expired, and clock-boundary cases.
- [x] 4.5 Cover the complete failure table: corrupt/missing reservations, defer failure after binding, monitor cancellation/expiry, publisher authentication failure, daemon restart, duplicate delivery, and conflicting terminal writes; verify each case either wakes exactly once or terminates safely with an inspectable reason.
- [x] 4.6 Extend the self-describing wake report with producer/event/Git evidence and an explicit `not accepted` marker; verify the report never claims task success, review success, mergeability, or user acceptance from terminal publication alone.
- [x] 4.7 Add daemon event-capability epoch, loaded-source identity, and kernel lock-owner proof to readiness; reject event registration before arm and event defer before pause against a legacy/mismatched daemon, and expose a current-binary compatibility preflight that refuses an older target epoch while any nonterminal monitor contains an event leaf, independently of reservation-row presence.

## 5. Document and integrate producer adapters

- [x] 5.1 Update the README and skill instructions with the reserve → launch existing shell/harness → bind/defer → terminal publish → lead review flow, including cancellation, expiry, heartbeat limits, and the material-spend/activation boundary.
- [x] 5.2 Document `command_terminal` as process-lifecycle evidence and `worker_terminal` as worker-delivery evidence; state that a successful `git commit` command is insufficient by itself because it lacks worker identity, frozen baseline/scope, candidate attestation, and acceptance semantics.
- [x] 5.3 Add a narrow native-subagent/HarnessDock producer contract or reference adapter that publishes the same `worker_terminal` envelope; do not add a second callback, watcher, scheduler, or wake claimant.
- [x] 5.4 Add end-to-end source tests for command success/failure and worker valid/invalid delivery, proving the existing command or worker harness remains execution owner and the plugin performs no raw-command execution, commit, merge, checkout, reset, push, or ref mutation.

## 6. Verify the change and stop at the live boundary

- [x] 6.1 Run focused new tests, including the frozen concurrency/mixed-daemon/Git-threat cases from the semantic review, then `conda run -n ms pytest`; compare the final failure identities against the recorded baseline and resolve every newly introduced failure rather than accepting a similar pass count.
  - Final source receipt (2026-08-20 UTC): correction regressions were observed RED in three batches—the original four-blocker suite with 7 failures, the lock-owner/downgrade suite with 2 failures, and delivered/non-delivered producer-identity cases with 2 failures—then GREEN; the complete unambiguous command `RTK_HOOK_DISABLE=1 conda run -n ms python -m pytest -o addopts= -q` passed 230 tests with an empty failure set.
- [x] 6.2 Run `conda run -n ms python -m compileall -q src tests`, `openspec validate add-terminal-and-worker-delivery-events --type change --strict`, `openspec validate --all --strict`, and `git diff --check` on the exact task-owned paths.
  - Final receipts: compileall passed; strict change validation passed; strict all-spec validation passed 3/3; tracked-path `git diff --check` and untracked-path trailing-whitespace scan were empty.
- [x] 6.3 Under the superseding queue-first authority from
  `make-event-monitor-goal-optional`, run the installed command success/failure
  and valid/invalid worker matrix through `ThreadDelivery`, retain cancellation,
  expiry, daemon-restart, and app-server-restart receipts, and run one paused-goal
  `GoalDelivery` compatibility row. Do not claim queue exactly-once behavior.
  - Installed receipt (2026-08-21 UTC): command reservations
    `17b6c698-de25-446f-b184-aa585434263d` and
    `76314ca6-a488-49af-817e-0f35d6152973`, and worker reservations
    `49cab4b0-f701-4ef1-90d9-7e8faeabb808` and
    `8bbf7cd2-5c94-408c-b757-4b87949521e4`, each produced one recorded
    ThreadDelivery pointer while preserving `task_success=false` and
    `lead_accepted=false`; valid worker attestation was a reviewable candidate
    and the out-of-scope worker remained an invalid delivery.
  - Cancellation receipts were monitors
    `df96fdab-6d38-4493-bdd4-323a97198994` (before admission) and
    `72cdf4c4-1e1a-44fb-8f5b-3d80ea0cab51` (one conclusive post-ACK queue
    removal). Daemon recovery monitor
    `0d9b9e47-2e21-4904-b38d-00b0e4940715` retained the same delivery/item
    without a second add. Controlled app-server restart monitor
    `ff0a0631-6c97-4a1d-8330-5a21a3ea0077` retained the same queued item and
    was cleaned by one conclusive removal.
  - Expiry receipt: thread `01a0257c-c7f4-7363-881f-8d3db78e7e34`, monitor
    `25b6648d-37a0-46aa-9531-1a1e99794c70`, delivery
    `d28123c9-c0b0-53f1-abba-7747bb787106`, queue item
    `01a0257c-d389-7762-8471-182d23c3797a`, and pointer turn
    `01a0257c-d38b-7082-91ef-7ef0e8ac641b`. The exact installed
    `0.1.0+codex.20260821174100` runtime observed `goal=null`, one expired
    claim, one consumed queue-add attempt, matching history
    `clientUserMessageId`, pointer count one, an empty final queue, no re-add,
    and successful archive.
  - Paused-goal compatibility receipt: reservation
    `f20f323c-a700-476c-bbe0-bb323c0b6410` and monitor
    `107d36f7-35ee-4f9c-9bca-403b71a7952e` used stable two-read capture with
    no pause write and exactly one guarded activation. This single compatibility
    row replaces, rather than expands into, the superseded seven-goal matrix.
- [x] 6.4 Stop before installing, cache-busting, restarting Codex, switching the loaded plugin, launching paid/GPU work, or publishing externally; those actions require separate user authorization and live verification of the loaded cache, daemon path, and `PYTHONPATH`.
- [x] 6.5 Record rollback evidence showing the feature can be disabled by removing producer use while legacy monitors remain readable and functional; do not delete the additive event ledger during rollback.

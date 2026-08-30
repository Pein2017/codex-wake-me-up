## 1. Freeze the behavior at stable seams

- [x] 1.1 Add condition tests that characterize the current single-reservation rejection, then require canonical plural bindings for valid `all`/`any` trees and reject duplicate terminal leaves, heartbeat-only reservations, kind mismatches, and missing or extra bindings.
- [x] 1.2 Add ledger migration and registration tests that require several reservations to bind atomically to one monitor while each reservation remains permanently single-consumer; cover expired, absent, already-bound, and deadline-race members with no partial monitor or binding.
- [x] 1.3 Add concurrency tests proving simultaneous terminal publications, heartbeat staleness, and expiry can produce only one trigger claim and at most one queue admission for multi-event `all` and `any` monitors.
- [x] 1.4 Add service and payload-budget tests for legacy singular versus bounded plural terminal-event status, one-call decision sufficiency, complete audit evidence, secret exclusion, and a sensitivity check that fails if the decision projection falls back to full audit status.
- [x] 1.5 Add RED tests for unsupported `git_ref_change`, missing `monitor_condition`, and rejected `settlement_uncertain`, including each specified fail-closed Git identity/error disposition and explicit false success and acceptance flags.

## 2. Compose terminal reservations transactionally

- [x] 2.1 Replace the singular event-binding helper with the smallest canonical plural projection over the existing AST, preserving caller evaluation order while enforcing exact terminal/heartbeat membership rules.
- [x] 2.2 Migrate the event-reservation table to remove only reverse uniqueness on `bound_monitor_id`, restore a non-unique lookup index, preserve every legacy value, validate legacy rows, and advance `EVENT_CAPABILITY_EPOCH`.
- [x] 2.3 Extend monitor creation and idempotent replay to validate and bind the complete reservation set in one transaction, rolling back every row and the monitor on any invalid member.
- [x] 2.4 Load and validate the exact bound reservation set inside the existing claim transaction, evaluate event leaves from an immutable reservation-status map, and route missing, extra, duplicate, malformed, or mismatched state to fatal `unknown` without changing the claim or delivery state machines.
- [x] 2.5 Preserve one-shot semantics for `any`: keep losing reservations permanently bound and inspectable, and verify later attempts to reuse them fail closed.

## 3. Expose bounded producer and decision evidence

- [x] 3.1 Accept and normalize `settlement_uncertain` as a terminal worker outcome with a bounded fixed classification, no candidate commit, immutable/idempotent publication, and explicit `task_success=false` and `lead_accepted=false` in status.
- [x] 3.2 Return the compatible non-secret `monitor_condition` from command and worker reservation creation and idempotent inspection while keeping the publisher descriptor and token on their existing one-time private path.
- [x] 3.3 Extend audit status with complete deterministic multi-reservation evidence and decision status with only condition-relevant or decision-changing bounded members; retain the legacy singular projection without duplicating it for plural monitors.
- [x] 3.4 Update MCP/CLI schemas and receipts only where required by the plural bindings, `settlement_uncertain`, and `monitor_condition`, preserving one selected status view and all existing credential and user-text redaction.

## 4. Add exact Git-ref observation

- [x] 4.1 Reuse the bounded Git subprocess and repository-identity helpers to capture an absolute canonical worktree, common directory, object format, literal `HEAD` or validated full direct ref, direct OID, peeled commit, and HEAD target/detached state at registration.
- [x] 4.2 Add the `git_ref_change` condition adapter and immutable binding, rejecting absent, unborn, non-commit, revision-expression, non-root, remote, or otherwise unprovable registrations before arming.
- [x] 4.3 Revalidate repository identity on every sample and classify only `fast_forward`, `ref_rewrite`, `ref_deleted`, or `head_retarget`; make parse, timeout, Git, identity, and post-read race failures fatal `unknown` with scrubbed fixed error classes.
- [x] 4.4 Bound the Git witness to ref/repository identity, old/new object IDs, ancestry, and one coalesced-movement fact without an unproven exact commit count; exclude messages, authors, remotes, raw stderr, credentials, caller text, and any success implication.

## 5. Document the interaction and verify source acceptance

- [x] 5.1 Update README, the main wake-me-up skill, its terminal-event reference, and agent metadata to show reserve -> launch -> asynchronous arm -> end turn -> one decision-status read, including multi-producer `all`, PID exit, Git-ref, uncertainty, and the rule that tmux is only a shell around the actual producer.
- [x] 5.2 Document the external launcher seam as descriptor-in/condition-out without reading or modifying HarnessDock private state, executing arbitrary shell, scanning processes, installing hooks, auto-rearming, or implying that wake evidence is success.
- [x] 5.3 Run the focused event, ledger, concurrency, service, payload, terminal-event, worker-adapter, condition, Git-attestation, runtime, and public-API tests in the `ms` environment; demonstrate the load-bearing payload and multi-binding tests fail under a targeted rollback/mutation before restoring GREEN.
- [x] 5.4 Run `conda run -n ms python -m pytest -q`, the repository Ruff command, the packaged-skill validator, `openspec validate compose-producer-event-monitors --strict`, and `git diff --check -- .`; record exact results and any pre-existing failure-set delta.
- [x] 5.5 Stop at verified source artifacts and report the implementation boundary; do not install or replace plugin caches, restart the daemon, change another plugin, commit, or push without separate authorization.

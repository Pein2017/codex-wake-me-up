# Queue-delivery runtime acceptance — 2026-08-21

## Decision

Status: **candidate / generally available within the accepted V1 boundary**.
All decision-bearing installed-runtime smokes passed in a production-shaped,
isolated app-server using the exact installed Codex 0.148.0 binary and plugin
cache. The main Desktop app-server was not restarted or mutated. V1 remains
non-exactly-once and retains the documented queue crash window.

## Frozen and final identity

- Source repository: `/data/CoordExp/codex-wake-me-up`
- Git HEAD: `b274d596ba3ce98b4f5bb64b0247a773637f5d9c`
- Frozen source-closure manifest: `856737bef42ccbc155a99e148e9a85611be45f1ee8d357170a14e0529d2b8f0e`
- Corrected final source-closure manifest: `4287b213b05925690e783bccf87269a83b43e3e3638cc2758c05da59041e60c5`
- Exact manifest command, run from the repository root:
  `find .codex-plugin README.md skills/wake-me-up src tests openspec/specs openspec/changes/add-terminal-and-worker-delivery-events openspec/changes/make-event-monitor-goal-optional -type f ! -path '*/__pycache__/*' ! -name '*.pyc' -print0 | sort -z | xargs -0 sha256sum | sha256sum`
- Final manifest path count: 58 files.
- Frozen tasks hash: `10ef9f51a407682c3a1aa941a6ff2ad3972a6ba86082aed3689d019979d97538`
- Final 42/42 tasks hash: `1fee7c5bc99e167ffb5db441f71a164ad93aa92cc33295fbdef0ad2637f14adb`
- Unchanged proposal/design/monitor/goal hashes:
  `1d95705137425949091846e5f22217a4fd26b03b7dd2d43764fd4beefdcde54e`,
  `1e8628e228c6c0023724ff43ea6806b12c1d639f7d3d869e920a78a8f3e315fb`,
  `986bf21b4db065f9723b36a922205adf6655ad197c2095b0dabb6d876c56c7e2`,
  `aa908e1e2604192317fbc0c9cf8c6eaf922a6909d4ca14722a0ac828095b66b7`.
- One runtime-discovered correction added the missing accepted behavior for
  canonical `defer_goal_until_event` on an already-paused goal: stable two-read
  capture, no pause write, and a durable `paused_guard_confirmed` outcome.
- Corrected loaded source identity: `d4ecb20ca7d1a4d3b624a743dde466499e95e3aa3aa81bb5097f8270b3afadef`.
- Plugin manifest SHA-256: `c651232481282ce4b8cab330da35cdd95494f86193899e540a8f315bd131f0cb`.
- Historical Core patch SHA-256: `75f737e83c6c1b41031d9331074736e4e90598121bc4f5b1cd17e78f33ff63c8`.

## Installation and daemons

- Marketplace/name: `codex-wake-me-up@coordexp-local`; marketplace root
  `/data/CoordExp`; source path `/data/CoordExp/codex-wake-me-up`.
- Previous version/cache: `0.1.0+codex.20260820100342`, preserved under
  `/data/CoordExp/.codex/runtime/codex-wake-me-up/acceptance-backup.lS9vfs/0.1.0+codex.20260820100342`.
- Previous lock owner: PID 661806, whose cwd was the prior deleted cache and
  whose heartbeat/source were recorded before termination.
- First accepted-source install: `0.1.0+codex.20260821171439`, daemon PID
  3965569. It was retired after the paused-goal source defect changed the source
  identity.
- Final version/cache:
  `0.1.0+codex.20260821174100` at
  `/data/CoordExp/.codex/plugins/cache/coordexp-local/codex-wake-me-up/0.1.0+codex.20260821174100`.
- Final main daemon: PID 4112580; cwd is the final cache; `CODEX_HOME` is
  `/data/CoordExp/.codex`; `PYTHONPATH` contains only the final cache `src` path
  twice as inherited by the daemon launcher; heartbeat reports event epoch 1,
  delivery epoch 1, and corrected source identity `d4ecb20c...`.
- Runtime-affecting source/main-cache/isolated-cache manifest over
  `.codex-plugin`, `README.md`, `skills/wake-me-up`, and `src` is exactly
  `e6a48da47cbbc670a59602e2ab377966269e0a6c6b7537ef6c66fdd83e0acdf3`
  on all three roots.
- Isolated home: `/tmp/qdr.RuRbza`; isolated final app-server PID 4126579;
  isolated final plugin daemon PID 4115839; isolated cache is the same final
  version. Initialize returned the exact isolated `codexHome`, its private Unix
  socket, literal `canAcceptDirectInput=true`, and Codex 0.148.0.
- Installed Core stayed `codex-cli 0.148.0`; `/root/.local/bin/codex` SHA-256 is
  `ac2cfed85fb647d61e0150b8548102b330e4799d9d81ad5d354de701edf6b074`.

## Disposable ThreadDelivery matrix

Each monitor made at most one plugin `thread/queue/add` attempt. “Recorded” below
means one observed pointer in history; it is not an exactly-once claim.

- Loaded idle: thread `01a02555-35ac-7f83-8c18-6877c9d33b9c`, monitor
  `7e038f59-7da8-42e2-b31b-236f4fd25e69`, delivery
  `cc50018d-0d1d-59fd-a779-59a9d291b1ad`, item
  `01a02556-2bb8-71a2-9cb6-0055d5142414`; recorded with matching history
  `clientId` and pointer turn `01a02556-2bbb-77f1-bdf3-17d864b74227`.
- Stored/unloaded FIFO: same thread, monitor
  `11f609d8-31b6-4d30-a8c8-d7bd2d935a68`, delivery
  `2daa32a4-c2cc-5c6d-aba5-36b27ff95945`, item
  `01a02557-bc1d-7da3-a256-8977f384c973`. Before exact resume the ledger
  persisted one prior item `01a02557-b0bc-79c2-8a96-e52a4eccbf66`; prior turn
  `01a02557-bcf9-7721-a387-3d0c942737c6` completed before pointer turn
  `01a02557-d374-7273-87ac-5333b36b49c5`.
- Active-race diagnostic (not the acceptance witness): thread
  `01a02559-2bb9-75a1-8f35-05d82c2d62a3`, monitor
  `eb44da79-a831-4248-a79d-ba4f993d775a`, delivery
  `062c72b8-4ae5-586f-af47-cfbd194a77ec`, item
  `01a02559-7d40-7242-a3a8-f04e8d09a660`; admission observed idle and was
  therefore excluded from the active-turn claim.
- Active unsteered FIFO witness: thread
  `01a02559-dd6b-7a12-a278-d21af03bf3f7`, setup turn
  `01a02559-dda4-7ef1-b155-b54862e37689`, monitor
  `aa528d86-7656-4ad7-b8ce-2503f8621445`, delivery
  `6e6305d6-3a4b-5211-83b1-b52efd4cbf11`, item
  `01a02559-e3fd-7071-91a5-655c043b8729`. The setup stayed in progress while
  the pointer was queue position 0 and absent from history; the separate pointer
  turn `01a0255a-0665-7652-b5a1-2942a065ec94` started only afterward.
- Reorder/edit: thread `01a0255b-4a09-72d1-b745-157a08a02b26`, monitor
  `1c5d4291-c8f5-4c36-b4f3-cd8818f9541a`, delivery
  `e19ef774-13cf-5caf-ae7a-c9934dc6c13d`, item
  `01a0255b-4ed9-7b03-b265-3c12f566cd52`. A user item
  `01a0255b-5717-7871-970a-41ae3e1210ce` moved ahead of it; editing the exact
  pointer item produced terminal `delivery_modified`, with no restore/re-add.
- Cancel before admission: monitor `df96fdab-6d38-4493-bdd4-323a97198994`,
  delivery `bb28231d-3be6-57c6-a00b-9570979f9051`; terminal `cancelled`, no
  queue item and no model turn.
- Interrupted stall and post-ACK cancellation: thread
  `01a0255c-1141-73a2-9b5a-52c385e96d42`, monitor
  `72cdf4c4-1e1a-44fb-8f5b-3d80ea0cab51`, delivery
  `2f86d15e-fc6c-5a1e-b29d-26e867e5b9a1`, item
  `01a0255c-1464-7df1-b4df-183754ca5336`; status showed
  `stalled_interrupted`, then one conclusive delete returned `removed`.
- Daemon recovery: thread `01a0255c-ad51-7c92-bac7-9a5801a0b0da`, monitor
  `0d9b9e47-2e21-4904-b38d-00b0e4940715`, delivery
  `5521014c-d03c-5b5b-9a77-cc7d65b02bb9`, item
  `01a0255c-b317-7862-9ccb-9dfca7ef39d1`. After replacement of isolated daemon
  PID 3978909 by PID 4007251, the same item/delivery remained, pointer count was
  one, and no second add occurred; cleanup cancellation removed it.
- App-server crash-window witness: thread
  `01a0255d-3a2e-7081-b40d-2306f1b8c0d9`, monitor
  `18815761-7ffa-42b9-a768-80a2a1b80371`, delivery
  `63b0860b-e3e7-5cba-988d-ea543ba24893`, item
  `01a0255d-43e4-7f61-90b3-7baa09c4cdb9`. Restart during the external queue
  crash window lost the item; after 61.62 online seconds the plugin recorded
  `delivery_uncertain` with zero pointer history and did not re-add.
- Controlled app-server restart: thread
  `01a02568-d39a-7be0-a029-bdb7c0b10261`, monitor
  `ff0a0631-6c97-4a1d-8330-5a21a3ea0077`, delivery
  `37d40c1b-c7d7-588f-9912-9be344e97e2c`, item
  `01a0256b-25a9-73d2-982e-4a43324e9de7`. After interrupt/stall and isolated
  app-server restart from PID 4009467 to PID 4126579, the same queue item and
  delivery survived with no second add; cleanup cancellation removed it.
- Explicit user delete: thread `01a0256d-8ac2-7142-9831-5f0d483b538a`, monitor
  `8ae3a7cb-18ee-455d-85fd-02c5b0d53ca8`, delivery
  `abf4009e-05c3-5b05-a808-1a258863a37c`, item
  `01a0256d-915c-7720-87ff-9b58ee2bb680`. While the setup turn was active the
  pointer was position 0; direct user delete returned `deleted=true`; after
  interrupt and 61.64 online seconds status was `delivery_uncertain`, history
  count zero, queue empty, and no re-add.

## Terminal events and legacy GoalDelivery

- Command success: reservation `17b6c698-de25-446f-b184-aa585434263d`, monitor
  `7994600f-3c78-4807-ab51-6dc4885259ef`, delivery
  `3ddf5157-5323-57f7-8eb5-35a7d66971c4`; recorded one pointer; event succeeded
  with exit 0; `task_success=false`, `lead_accepted=false`.
- Command failure: reservation `76314ca6-a488-49af-817e-0f35d6152973`, monitor
  `d00a5b6d-c8d2-45e2-b3fc-714decb3acd9`, delivery
  `76eddcd2-b946-55fc-9643-28d4cc187e25`; recorded one pointer; event failed
  with exit 2; both acceptance flags remained false.
- Worker valid: reservation `49cab4b0-f701-4ef1-90d9-7e8faeabb808`, monitor
  `330ed5f2-3cad-4129-904d-4c176660c6a7`, delivery
  `8c1867c1-95e1-5654-86b8-8108e46fa963`. Git witness was
  `valid_delivery_candidate`; acceptance flags remained false.
- Worker invalid: reservation `8bbf7cd2-5c94-408c-b757-4b87949521e4`, monitor
  `9aa9630e-9436-46ff-887a-bc37b863c488`, delivery
  `5d2979c2-f3b9-505f-af0e-60b85927c351`. Candidate changed `AGENTS.md` outside
  the allowed prefix, so attestation was `out_of_scope` and witness
  `invalid_delivery`; it still woke and both acceptance flags stayed false.
- Worker evidence repository was reconstructed read-only from bundle
  `/tmp/qdr.RuRbza/workspace/qdr-worker.bundle`, SHA-256
  `fb9644a0c4bb392d6a40db5ee9f9464a10c70bf36f55a8ed265f59d9ab3e6e2a`,
  baseline `f24284d402913ac1e637497982213a3ea4b41125`, candidate
  `b274d596ba3ce98b4f5bb64b0247a773637f5d9c`.
- Canonical paused-goal compatibility: reservation
  `f20f323c-a700-476c-bbe0-bb323c0b6410`, monitor
  `107d36f7-35ee-4f9c-9bca-403b71a7952e`, thread
  `01a02560-c547-76c2-8d0f-4f22e5a2671f`. Admission returned
  `paused_guard_confirmed` with no pause write; the terminal event caused one
  guarded activation, outcome `activation_confirmed`, and turn
  `01a02570-dead-7c20-b2df-a591651fab93` returned `LEGACY_GOAL_DONE`.
  `task_success` and `lead_accepted` remained false. The disposable goal was
  then set complete.
- Two failed pre-bind legacy probes were cleaned honestly: reservation
  `b4fe1b68-1480-4fe2-8a84-6be3a26afcfb` expired, and reservation
  `bc13f790-f495-47be-84f9-6a02ab2d8abf` was cancelled unbound.

The predecessor's former seven-paused-goal wording is superseded by the
Opus-accepted queue-first authority in `make-event-monitor-goal-optional`.
Its task 6.3 now binds the installed command/worker matrix to ThreadDelivery,
reuses cancellation and restart receipts, adds the production-shaped expiry row
below, and retains exactly one paused-goal compatibility row. It does not expand
back into seven legacy goal turns or make a queue exactly-once claim.

## Model-spend and cleanup ledger

There were 23 isolated model-turn records, all on disposable tasks: loaded-idle
pointer (1); cold prior user plus pointer (2); first active diagnostic setup plus
pointer (2); accepted active setup plus pointer (2); edit setup (1); stall setup
(1); daemon-recovery setup (1); crash-window setup plus interrupted recovery turn
(2); command pointers (2); worker pointers (2); controlled restart initial setup,
restart setup, failed delete setup, and its automatically dispatched pointer (4);
explicit-delete setup (1); legacy activation (1); installed expiry pointer (1).
No model action targeted a real user task.

All 14 created disposable threads were archived successfully:
`01a02555-35ac-7f83-8c18-6877c9d33b9c`,
`01a02559-2bb9-75a1-8f35-05d82c2d62a3`,
`01a02559-dd6b-7a12-a278-d21af03bf3f7`,
`01a0255b-4a09-72d1-b745-157a08a02b26`,
`01a0255c-1141-73a2-9b5a-52c385e96d42`,
`01a0255c-ad51-7c92-bac7-9a5801a0b0da`,
`01a0255d-3a2e-7081-b40d-2306f1b8c0d9`,
`01a0255e-8380-7803-8b19-40e7e9cc6bd0`,
`01a0255e-85ba-7922-99a1-8d1d416a53e3`,
`01a02560-c547-76c2-8d0f-4f22e5a2671f`,
`01a02560-c713-7cc2-a9cc-8c9a9b38c949`,
`01a02568-d39a-7be0-a029-bdb7c0b10261`, and
`01a0256d-8ac2-7142-9831-5f0d483b538a`, and
`01a0257c-c7f4-7363-881f-8d3db78e7e34`.

The isolated ledger ended with only terminal states: cancelled 4,
delivery-modified 1, delivery-uncertain 2, fired 1, recorded 10. No matching
pointer or nonterminal disposable delivery remained.

## Source, OpenSpec, Core, and pilot receipts

- The accepted baseline was 302 passing tests with an empty failure set.
- The paused-goal regression was observed RED with the exact old
  `ValidationError`, then GREEN. Final full command passed 303 tests in 3.82s
  after the cachebuster change; an earlier corrected-source run passed 303 in
  3.86s. Final failure set remained empty.
- `compileall`, both strict change validations, `openspec validate --all
  --strict` (5/5), plugin validator, skill quick validator, and exact
  `git diff --check` passed.
- OpenSpec `make-event-monitor-goal-optional` is 42/42 and predecessor
  `add-terminal-and-worker-delivery-events` is 31/31. Neither is archived.
- Core worktree `/data/CoordExp/.worktrees/codex-start-if-idle-turn-guard` stayed
  at HEAD `3ba0f711642a888aec92a611a3f3b2211157ff89`. Before cleanup, reverse-check
  against the retained patch passed and the exact 12 task-owned paths matched.
  Apply-patch-based reversal restored all 11 tracked paths and deleted only the
  one task-owned untracked test. The worktree is now clean; installed Core hash
  and version are unchanged.
- Native depth-2 topology: L0 retained user/source authority; this L1 owned
  installation and acceptance; four read-only L2 routes were
  `runtime_discovery` (Luna/medium), `smoke_mapping` (Terra/high),
  `core_diff_audit` (Luna/medium), and `openspec_receipt` (Luna/medium). L1
  independently replayed decision-bearing receipts. No L2 writer or further
  spawn was used.
- Correction ledger: one permitted source correction; one active-race
  diagnostic superseded by an exact active witness; one app-server crash-window
  loss retained as truthful uncertainty and a second controlled restart used to
  prove persistence; one failed explicit-delete setup superseded by a direct
  `deleted=true` row. No source correction class repeated.
- Measured wall-time basis starts at the pre-install backup receipt timestamp
  `2026-08-21T17:14:39.309742088Z`; the final timestamp is recorded below after
  isolated shutdown. One L0 intervention supplied the recovered exact
  source-closure manifest command. Token/cost were not instrumented, so no
  values are inferred. No GPU or remote work was launched.

## Rollback truth and residual limits

- Rollback artifact is preserved and the old cache can be reinstalled if a later
  regression requires it. Rollback was not executed because final installation
  and live acceptance passed. Additive ledger history was not deleted.
- Queue delivery is not exactly-once. The accepted external queue can lose an
  ACKed pointer if the app-server crashes before its queue persistence boundary;
  the plugin reconciles history, then queue, then 60 online seconds and reports
  `delivery_uncertain` without retrying.
- User edit/delete and app-server loss intentionally converge to the same bounded
  uncertainty class. This is truthful, not a causality claim.
- Pointer turns in the isolated app-server attempted the read-only plugin status
  tool, but its `approvalPolicy=never` caused approval-required failures. The
  acceptance harness read the same durable status externally. Queue admission,
  FIFO dispatch, history correlation, and goal activation remained directly
  observed; this approval-policy behavior is an isolated-client boundary, not a
  queue-delivery failure.
- The predecessor task 6.3 is closed only under the recorded superseding
  queue-first wording; no seven-goal matrix was run or claimed.

Final timestamp: `2026-08-21T17:57:03.250243Z`. Measured wall time from the
pre-install backup receipt through isolated daemon/app-server shutdown:
`2543.941` seconds (42 minutes 23.941 seconds).

## Predecessor closure continuation

The exact final installed cache and Codex 0.148.0 binary were restarted only in
the isolated home `/tmp/qdr.RuRbza`; the main Desktop app-server was untouched
and main plugin daemon PID 4112580 remained unchanged.

Expiry witness: thread `01a0257c-c7f4-7363-881f-8d3db78e7e34`, monitor
`25b6648d-37a0-46aa-9531-1a1e99794c70`, delivery
`d28123c9-c0b0-53f1-abba-7747bb787106`, queue item
`01a0257c-d389-7762-8471-182d23c3797a`, and pointer turn
`01a0257c-d38b-7082-91ef-7ef0e8ac641b`. Preflight observed loaded idle,
`canAcceptDirectInput=true`, queue count zero, and `goal=null`. A far-future
condition with a one-second monitor expiry produced one claim with
`wake_reason=expired`, one consumed plugin queue-add attempt, one matching
history item with `clientUserMessageId=deliveryId`, and terminal `recorded` with
`observed_pointer_count=1`. The final queue was empty, the goal stayed null, no
blind re-add occurred, and the disposable thread was archived. This incurred
exactly one additional isolated model turn.

Continuation cleanup stopped only isolated plugin daemon PID 57018 and isolated
app-server PID 56744; the isolated daemon lock was then independently acquirable.
Main daemon PID 4112580, its cache/source identity, and the main Desktop
app-server remained unchanged.

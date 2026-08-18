## 1. Decision Gate (user-owned)

- [x] 1.1 Obtain rulings on proposal decision points D1–D3 and record them in
  this change before implementation. Done 2026-08-18: user delegated the
  technical rulings with the product goal "no polling tokens, no compaction
  churn"; all three recommended options adopted — see `proposal.md`
  "Rulings (2026-08-18)". Deferred contract: woken at the latest at expiry
  while the daemon lives and the guard holds.

## 2. Ledger and State Machine

- [x] 2.1 Additive ledger schema: `wake_reason`, `rearm_of`,
  `evaluation_count`, `armed_at` via the existing `PRAGMA table_info`
  migration pattern; `claim` accepts a wake reason; `arm`/`arm_deferred`
  stamp `armed_at`; `update_evaluation` increments the counter. Migration
  test opens a raw v0.1-schema database with an existing row and proves it
  loads and behaves unchanged.
- [x] 2.2 Route deferred wakes through the existing claim/activation CAS
  with a durable wake reason (`condition`, `expired`,
  `unauthorized_evidence`, `observer_failed`); keep legacy behavior
  byte-for-byte (legacy expiry → `EXPIRED`, legacy unauthorized →
  `SATISFIED_REQUIRES_AUTHORIZATION`). Tests: deferred expiry wake; deferred
  expiry while target busy stays armed then fires on idle;
  `unauthorized_evidence` wake; typed-fatal `observer_failed` wake;
  cancelled-wins; guard-changed-at-deadline → `SUPERSEDED` with no
  activation.
- [x] 2.3 Apply the terminal-write classification (design: "Classify every
  deferred terminal write") to the three named error paths: top-of-loop
  expiry becomes legacy-only and deferred `CLAIMED` rows found past expiry
  continue their wake; untyped exceptions on deferred `ARMED` record
  evidence and stay armed; `_finish_claim` preflight failure on deferred
  `CLAIMED` records evidence and stays claimed; post-`begin_activation`
  uncertainty stays terminal for both modes. One regression test per path:
  restart-past-expiry-while-claimed; transient app-server error leaves
  deferred armed; preflight error leaves deferred claimed.
- [x] 2.4 `_finish_claim`: skip the authorization gate for stored non-null,
  non-condition wake reasons; skip `expired_before_activation` for deferred
  rows. Restart test asserts `recover_after_daemon_start` plus first
  `reconcile_once` completes an expired armed deferred monitor.
- [x] 2.5 Produce the frozen state-machine diff (v0.1 vs this change) as a
  review artifact.

## 3. Condition Leaves

- [x] 3.1 Implement `log_pattern` per design: identity+offset capture,
  EOF-at-arm baseline, missing-file tolerance (file may appear later; then
  scan from byte 0), rotation/truncation → `unknown` with `fatal`,
  read-only scanning with 4 MiB per-poll budget, ≤8 patterns × ≤512 chars,
  4 KiB per-line match bound (noted in evidence when truncated), journal
  ≤50 lines / 16 KiB with drop counters, latching truth. Focused tests for
  each, including pre-existing content not matching, failure-signature
  match, and stability under `all(...)` composition.
- [x] 3.2 Implement `thread_idle` per design: service pre-reads the child
  into `ObserverContext.thread_observations`; prepare rejects an unloaded
  child, rejects an already-idle child without `accept_already_idle`, and
  rejects self-wait on the monitor's own target (both modes); armed truth is
  edge-shaped and latching; unloaded/missing child → `unknown`, never true;
  witness carries the child's status and usage snapshot. Focused tests
  including composition under `all`/`any` and missing-observation handling.
- [x] 3.3 Classify both leaves as heuristic in witness authorization
  (receipt stays the only authoritative success) and cover the deferred
  `unauthorized_evidence` labeling in tests.

## 4. Wake Report and Lineage

- [x] 4.1 Assemble the wake report into the `FIRED` outcome (wake reason,
  witness or failure detail, `journal_tail` ≤20 lines, `armed_at`,
  `fired_at`, waited seconds, evaluation count, estimated avoided-poll-turn
  count at the 180 s yield floor). `service.status` and registration
  responses elide the stored journal to `{lines_stored, dropped}`;
  `outcome.journal_tail` is the sole line carrier. Test asserts elision and
  report content.
- [x] 4.2 Accept, validate (exists and terminal), store, and surface
  `rearm_of` on both registration tools, inside `semantic` so idempotent
  replays repeat it; reject unknown or non-terminal references. Tests for
  accept, both rejections, and chain surfacing in status/list.

## 5. Skill, Docs, and Verification

- [x] 5.1 Update `skills/wake-me-up/SKILL.md` and module `README.md`:
  expiry wakes so no `any(condition, time)` backstop; coverage rule
  (failure signatures mandatory in `log_pattern` watches); wake ≠ success,
  the witness decides; exactly one post-wake status call; re-defer loop
  with `rearm_of`; `thread_idle` for subagent waits.
- [x] 5.2 Run `conda run -n ms python -m pytest` (diff failure sets against
  the pre-change baseline; never compare pass counts — baseline recorded
  2026-08-18 at 9d5153499: 52 passed, 0 failed), plus
  `openspec validate add-wake-me-up-event-wakes --strict` and plugin
  manifest validation. Done 2026-08-18: 99 passed / 0 failed against a
  52 passed / 0 failed baseline — failure-set diff is empty; `--strict`
  validation passes; `.codex-plugin/plugin.json` parses and its referenced
  `skills/`, `.mcp.json`, and icon assets all resolve. Cachebuster untouched
  (task 5.4).
- [x] 5.3 Disposable local app-server smoke on scratch threads only: one
  `log_pattern` defer against a synthetic log (success and crash paths),
  one `thread_idle` defer on a scratch child, one forced expiry wake, one
  daemon-restart-then-expiry-wake recovery; record receipts. Closed 2026-08-18
  as **recorded partial pass**: 11 live checks PASS (defer/pause/arm, guard
  re-read fail-closing to `superseded`, both `thread_idle` arm-time
  rejections, the full `rearm_of` matrix, journal elision, real file-identity
  capture) plus two MCP receipts; the four deferred **wake** paths are BLOCKED
  on the live control plane and recorded as such. See `verification.md`
  sections "BLOCKED — the four wake paths of task 5.3", "Product observations
  raised by real execution", and "Scope limitation recorded by ruling".
- [x] 5.4 Bump the plugin cachebuster and reinstall through the local
  marketplace only after review of the state-machine diff; keep the
  previous cachebuster as rollback. Done 2026-08-18:
  `0.1.0+codex.20260808174817` → `0.1.0+codex.20260818080356`; installed
  source verified byte-identical to the working tree; the lock-holding daemon
  was identity-checked via `/proc`, retired, and replaced by one spawned from
  the installed copy (pid `45113`). Rollback path recorded in
  `verification.md`, including that a cachebuster revert alone is not a code
  rollback. Then stop any daemon still running from
  the previous plugin cache (verify its cmdline and PYTHONPATH before
  killing) and let the reinstalled code re-arm supervision: the 5.3 smoke
  found a v0.1-cache daemon that had held the runtime lock for 9+ days and
  would have serviced new monitors with pre-change stranding semantics.

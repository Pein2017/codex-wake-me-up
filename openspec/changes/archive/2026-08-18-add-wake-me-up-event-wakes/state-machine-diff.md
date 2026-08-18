# Frozen state-machine diff (v0.1 → add-wake-me-up-event-wakes)

Review artifact for task 2.5. Scope: which durable facts may consume a
monitor's single guarded activation, and how every terminal write on a
**deferred** monitor is classified. Legacy monitors keep the v0.1 state
machine exactly; the only legacy-visible additions are ledger columns and
extra fields inside the `FIRED` outcome payload.

## 1. Transitions out of `ARMED`

| Observed fact | v0.1 (both modes) | This change — legacy | This change — deferred |
| --- | --- | --- | --- |
| Condition true, witness authorizes | `CLAIMED` | `CLAIMED` (reason NULL) | `CLAIMED[condition]` |
| Condition true, only heuristic witness, no opt-in | `CLAIMED` → `SATISFIED_REQUIRES_AUTHORIZATION` | unchanged | `CLAIMED[unauthorized_evidence]` → `FIRED` |
| `Evaluation.fatal` and not true | `OBSERVER_FAILED` (terminal) | unchanged | `CLAIMED[observer_failed]` → `FIRED` |
| Expiry reached | `EXPIRED` (terminal) | unchanged | `CLAIMED[expired]` → `FIRED` |
| Untyped exception while observing | `OBSERVER_FAILED` (terminal) | unchanged | **stays `ARMED`**, evidence recorded |
| Target not idle | stays `ARMED` | unchanged | unchanged (expiry no longer terminal here) |
| Target unloaded at preflight | `UNLOADED_TARGET` | unchanged | unchanged (fail-closed) |
| Guard changed at preflight | `SUPERSEDED` | unchanged | unchanged (fail-closed) |
| Cancelled | `CANCELLED` | unchanged | unchanged (fail-closed) |

`EXPIRED` and `SATISFIED_REQUIRES_AUTHORIZATION` are no longer reachable from
`ARMED` for a deferred monitor. They remain reachable for legacy monitors, and
`EXPIRED` remains reachable for a deferred monitor only before it arms
(`expired_before_pause`).

## 2. Transitions out of `CLAIMED`

| Observed fact | v0.1 (both modes) | This change — legacy | This change — deferred |
| --- | --- | --- | --- |
| Witness does not authorize | `SATISFIED_REQUIRES_AUTHORIZATION` | unchanged | **gate skipped** (the reason already labels the wake) |
| Expiry reached before activation | `EXPIRED` | unchanged | **cut skipped** (expiry is wake-eligible) |
| Preflight read fails (`AppServerError`) | `ACTIVATION_FAILED` | unchanged | **stays `CLAIMED`**, evidence recorded |
| Untyped exception before `begin_activation` | `ACTIVATION_FAILED` | unchanged | **stays `CLAIMED`**, evidence recorded |
| Target unloaded / guard changed | `UNLOADED_TARGET` / `SUPERSEDED` | unchanged | unchanged (fail-closed) |
| Activation confirmed | `FIRED` | `FIRED` + wake report | `FIRED` + wake report |

Everything at or after `begin_activation` is untouched in both modes:
`ACTIVATING` → `ACTIVATION_UNCERTAIN` / `ACTIVATION_FAILED` /
`MIS_TARGETED_ACTIVATION` / `FIRED`. That is the uncertain-write bucket, where
a compensating write could become the duplicate the design forbids.

The two deferred skips are discriminated by **`mode`, never by the stored
wake reason**. A deferred row claimed by pre-change code carries a NULL reason
after migration; gating on NULL-ness would send it back through the
authorization gate and re-strand it. NULL affects only the `FIRED` label,
which is then derived from the witness.

## 3. Classification of every deferred terminal-write site

The design's review rule: enumerate every site that writes a terminal state on
a deferred monitor and classify it as (a) wake via the claim path, (b) stay
retryable, or (c) justified fail-closed. All eight sites reachable after
`ARMED`:

| # | Site | Class | Disposition |
| --- | --- | --- | --- |
| 1 | `reconcile_once` top-of-loop expiry | (a) | Legacy-only write. Deferred `ARMED` becomes claim-eligible `expired`; deferred `CLAIMED` continues its wake. |
| 2 | `_record_unexpected_failure`, `ARMED` | (b) | Untyped failure records evidence and stays `ARMED`; expiry is the backstop. |
| 3 | `_record_unexpected_failure`, `CLAIMED` | (b) | `CLAIMED` proves `begin_activation` never ran, so the read is retryable. |
| 4 | `_finish_claim` preflight `AppServerError` | (b) | Nothing sent yet; records evidence and stays `CLAIMED`. |
| 5 | `_finish_claim` authorization gate | (a) | Deferred skips it; the fact already arrived labelled `unauthorized_evidence`. |
| 6 | `_finish_claim` `expired_before_activation` | (a) | Deferred skips it; expiry is wake-eligible. |
| 7 | `_evaluate_and_claim` fatal observer | (a) | Deferred claims `observer_failed` instead of writing terminal. |
| 8 | `reconcile_once` `deferred_idle_barrier_missing` | (c) | **Kept fail-closed.** A deferred row without its idle barrier is a corrupted row, not a wake-eligible fact; waking from unverified state is what the barrier exists to prevent. Unreachable through the public API, which always sets the barrier. |

Untouched fail-closed states, unchanged in both modes: `DEFER_ABANDONED`,
`PAUSE_UNCERTAIN`, `PAUSE_REJECTED`, `MIS_TARGETED_PAUSE`,
`DAEMON_UNAVAILABLE`, `SUPERSEDED`, `UNLOADED_TARGET`, `CANCELLED`,
`MIS_TARGETED_ACTIVATION`, `ACTIVATION_UNCERTAIN`, `ACTIVATION_FAILED`.

## 4. Invariants re-checked against the diff

- **At most one activation request per monitor.** Every new wake-eligible fact
  is funnelled into the same `ARMED → CLAIMED` CAS and the same
  `CLAIMED → ACTIVATING` CAS. No second activation path exists; sites 2, 3 and
  4 deliberately consume neither CAS.
- **Idle barrier and guard re-read apply to every reason.** A deadline that
  passes while the target is running waits for idle exactly as a true
  condition does (site 1 falls through to the same preflight).
- **No new phases.** The diff adds one enum (`WakeReason`) recorded on the
  existing claim, not a new state.
- **Legacy byte-for-byte.** Every legacy cell above reads "unchanged".

## 5. Regression tests pinning this diff

| Row | Test |
| --- | --- |
| Deferred expiry wake | `test_deferred_expiry_wakes_the_goal_instead_of_stranding_it` |
| Deadline while busy | `test_deferred_expiry_while_target_is_busy_stays_armed_then_fires_on_idle` |
| Unauthorized heuristic | `test_deferred_unauthorized_heuristic_evidence_wakes_with_an_honest_label` |
| Typed-fatal observer | `test_deferred_typed_fatal_observer_wakes_with_the_failure_detail` |
| Cancelled wins | `test_deferred_cancellation_wins_over_a_deadline_wake` |
| Guard changed at deadline | `test_deferred_guard_change_at_the_deadline_supersedes_without_activation` |
| Expiry under a sick observer | `test_deferred_expiry_wakes_even_while_the_observer_reports_unknown` |
| Site 1 (restart while claimed) | `test_restart_past_expiry_while_claimed_continues_the_wake` |
| Sites 2–3 (transient failure) | `test_transient_observation_failure_leaves_a_deferred_monitor_armed` |
| Site 4 (preflight failure) | `test_preflight_failure_leaves_a_deferred_monitor_claimed_and_retryable` |
| Sites 5–6 after restart | `test_recovery_then_first_pass_completes_an_expired_armed_deferred_monitor` |
| NULL reason after migration | `test_a_deferred_row_claimed_before_this_change_still_wakes` |
| Legacy unchanged | `test_legacy_expiry_and_authorization_outcomes_are_unchanged` |

## Why

`codex-wake-me-up` v0.1 already removes the polling-turn tax for one class of
wait: pause a goal, watch one typed condition, make at most one guarded
reactivation. Measured against Claude Code's monitor-event contract — the
mature reference for token-free waiting — five gaps remain, and each maps to
a failure mode the 2026-08 operator retrospectives already recorded
(blocked-idle threads, poll/compaction tax, `wait_agent` long-yield turns):

| Claude Code monitor contract | codex-wake-me-up v0.1 | Operator cost |
| --- | --- | --- |
| A watch that ends (timeout, kill, error) is itself a reported event | Deferred expiry, unauthorized heuristic satisfaction, and observer failure strand the goal paused with no wake | Blocked-idle: a self-deferred task waits indefinitely on user attention |
| The notification carries the event payload (the matched output lines) | A wake carries no content; the woken agent re-discovers what happened | Extra post-wake discovery calls; misread heuristic wakes |
| Coverage doctrine: the watch must match every terminal signature, success and failure, because silence is indistinguishable from still-running | No log-content condition; crashes surface only at pid/tmux exit, hangs only at expiry | Late failure detection; a crashloop that keeps its pid alive never fires |
| Background-task and subagent completion re-invokes the waiting agent | No condition can wait on another local Codex thread | `wait_agent`/long-yield polling turns keep burning tokens |
| One-notification vs per-occurrence is an explicit, cheap contract choice | One-shot only, with no re-arm ergonomics or lineage | Each repeated wait repays full registration reasoning |

The first gap is the most severe: v0.1's "expiry terminates the watcher only"
decision is safe for a goal the *user* paused, but for a goal the plugin
itself paused on the agent's behalf it converts a bounded wait into an
unbounded silent stall — the exact outcome the plugin exists to prevent.

## What Changes

- **Deferred terminal wake** (modifies `codex-goal-self-defer` and the expiry
  scenario of `codex-wake-me-up-monitor`): for a deferred monitor whose own
  pause was conclusively confirmed and whose guard still matches, expiry,
  satisfaction-without-authorization, and observer failure route through the
  existing single guarded activation with a durable `wake_reason`
  (`condition | expired | unauthorized_evidence | observer_failed`). The
  fail-closed set is unchanged for every pre-arm or guard-violating state.
  Recovery after a daemon restart honors the same policy.
- **`log_pattern` condition** (adds to `codex-wake-me-up-monitor`): a
  read-only, identity-guarded tail of one local log file against named regex
  alternations, with a bounded matched-line journal that becomes the wake
  payload. Heuristic-class evidence; receipts stay the only authoritative
  success.
- **`thread_idle` condition** (adds to `codex-wake-me-up-monitor`): wait for
  another locally loaded Codex thread (e.g. a subagent the caller launched) to
  complete its turn, observed through the existing local app-server read path.
  Heuristic-class evidence.
- **Wake report** (adds to `codex-wake-me-up-monitor`): the terminal outcome
  carries `wake_reason`, the satisfying witness, the bounded journal tail, and
  wait statistics, so one `wake_me_up_status` call fully re-orients the woken
  agent.
- **Re-arm lineage** (adds to `codex-wake-me-up-monitor`): an optional
  `rearm_of` field links a new monitor to the fired one it succeeds; the skill
  documents the wake → handle → re-defer loop for per-occurrence needs.
  Multi-shot schedules remain a non-goal.
- **Skill doctrine** (modifies `codex-goal-self-defer` guidance): imports the
  coverage rule (failure signatures in every `log_pattern` watch), drops the
  `any(condition, time)` backstop pattern once expiry itself wakes, and
  directs exactly one status call after a wake.

## Decision Points (user-owned)

- **D1 — Reverse "expiry never changes goal status" for deferred monitors.**
  Recommended: wake-on-expiry becomes the deferred default (the pause is
  plugin-created scaffolding; waking restores the pre-defer state).
  Alternative: an explicit `on_expiry: wake | stay_paused` registration field
  with `wake` as default, preserving a strand opt-out.
- **D2 — Flag-off heuristic semantics.** Recommended: when
  `allow_heuristic_continuation` is false and only heuristic leaves are true,
  a deferred monitor wakes early with the honest `unauthorized_evidence`
  label (waiting hours on already-fired evidence adds wall-clock without
  safety). Alternative: heuristic evidence never wakes early; only the
  deadline wakes.
- **D3 — Whether `observer_failed` wakes.** Recommended: yes for deferred
  monitors (a broken observer will never fire; stranding is strictly worse),
  with the failure detail in the wake report. Alternative: keep it terminal
  and paused.

## Rulings (2026-08-18)

The user, acting as product owner, delegated these technical decisions with
the stated product goal: long tasks must not burn tokens on polling turns or
be driven through repeated compaction. Ruled by the delegated technical owner
accordingly; the spec deltas in this change already encode these outcomes:

- **D1 — adopted**: deferred expiry wakes by default; no `on_expiry` knob.
  A stranded pause always costs more (user attention plus context
  re-establishment) than a labeled deadline wake, and every knob is extra
  agent reasoning per registration.
- **D2 — adopted**: unauthorized heuristic satisfaction wakes early with the
  `unauthorized_evidence` label. Holding a paused task after its evidence
  fired adds wall-clock without safety.
- **D3 — adopted**: irrecoverable observer failure wakes with the
  `observer_failed` label. A broken observer never fires; stranding is
  strictly worse.

Net contract: an armed deferred monitor carries one guarantee — the goal is
woken at the latest at expiry, as long as the daemon lives, the guard holds,
and the target runtime is observed idle again. The third precondition is
real, not theoretical: the 5.3 smoke observed a target stuck in
`systemError` holding an expired monitor armed indefinitely. Waking a
non-idle thread mid-turn is not a safe alternative, so the bound is honest
documentation plus the armed-past-expiry state being visible in status.
Guard violations, uncertain writes, and cancellation remain fail-closed.

## Impact

- Affected specs: `codex-goal-self-defer` (terminal-wake requirement, skill
  guidance), `codex-wake-me-up-monitor` (lifecycle expiry scenario, heuristic
  authorization outcome, new conditions, wake report, lineage).
- Affected code when implemented: `conditions.py` (two new leaves, journal),
  `service.py` (wake_reason routing, recovery), `ledger.py` (additive
  columns/states), `models.py`, `mcp_server.py`, `skills/wake-me-up/SKILL.md`,
  focused tests.
- Unchanged invariants: at most one activation request per monitor; no
  `turn/start`, `thread/resume`, or synthetic messages; no raw shell
  predicates; host-local only; receipts as sole authoritative success; legacy
  (non-deferred) monitor behavior byte-for-byte preserved.

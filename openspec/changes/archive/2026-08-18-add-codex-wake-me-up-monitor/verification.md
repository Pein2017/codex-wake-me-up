# Verification Evidence

## Fixed implementation checks

- `python -m pytest -vv` in `codex-wake-me-up/`: 25 passed.
- `python -m compileall -q src scripts tests`: passed.
- `validate_plugin.py /data/CoordExp/codex-wake-me-up`: passed.
- `openspec validate add-codex-wake-me-up-monitor --strict`: passed.
- A real stdio MCP client initialized the source plugin and listed exactly
  `wake_me_up`, `wake_me_up_cancel`, `wake_me_up_publish_receipt`, and
  `wake_me_up_status`.
- `PYTHONPATH=src python -m codex_wake_me_up.cli doctor`: passed against the
  local Unix control socket.  It performed no goal lookup because no
  `--thread-id` was supplied.

## Disposable continuation smoke

The disposable task `019fd9ca-8816-7983-b646-6e5520ae51e8` was created solely
for this test.  A benign paused goal was armed with a one-second `time`
condition and explicit `allow_heuristic_continuation=true`.  Its monitor
`10c4fcc5-2f33-4fb8-9ea8-f22e5b1c0932` recorded one
`activation_confirmed` outcome and the resumed task replied exactly
`WAKE_SMOKE_OK`; its goal subsequently cleared on completion.

The named real target `019fd2bf-5700-7e63-9362-5853b2c21a95` was not read or
changed by this smoke.  The smoke monitor is one-shot and terminal (`fired`)
with `supervision: not_required`; no monitor daemon remains running.  Its
single runtime-ledger row is retained as an audit receipt because the current
execution environment disallows deleting it, and cannot run again.

## Independent review

An initial independent high-reasoning review found seven P1 design gaps; all
were incorporated before implementation.  A second implementation review
found three P1 issues (tmux observer exceptions, Unix-socket connection error
normalization, and target-specific README wording); all were repaired.  A
fresh independent recheck returned PASS with no P0/P1 findings.

The recheck recorded non-blocking P2 follow-ups: make registration recovery
safe across a concurrently running process, normalize every tmux capture
exception during registration, bound the WebSocket handshake timeout, and
consider persistence/observability refinements for GPU stability and daemon
heartbeats.  They do not alter the accepted fail-closed, single-attempt
activation contract.

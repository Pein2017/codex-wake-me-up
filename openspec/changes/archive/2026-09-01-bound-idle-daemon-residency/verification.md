# Verification receipt

Date: 2026-09-01 UTC

## Source checks

- `conda run -n ms python -m pytest -o addopts= -q`: **425 passed**.
- `conda run -n ms python -m ruff check src tests`: passed.
- `conda run -n ms python -m compileall -q src tests`: passed.
- `openspec validate --strict bound-idle-daemon-residency`: passed.
- `git diff --check`: passed.

## Installed replacement

- Previous installed cache: `0.1.0+codex.20260901012502` (itself replacing
  `0.1.0+codex.20260831070501`).
- Final installed cache: `0.1.0+codex.20260901013840` at
  `/data/CoordExp/.codex/plugins/cache/coordexp-local/codex-wake-me-up/0.1.0+codex.20260901013840`.
- Rollback copies: `/data/CoordExp/.codex/runtime/codex-wake-me-up/preinstall-backup.dy7Rk7/` and
  `/data/CoordExp/.codex/runtime/codex-wake-me-up/preinstall-backup.lr4p9E/`.
- Compatibility preflight: `event-compatibility-check --supported-event-epoch 3` passed.
- Exact old lock owner PID `3066026` was inspected (`PYTHONPATH=/data/CoordExp/codex-wake-me-up/src`, source identity `aabe23e...`), received scoped `SIGTERM`, and exited.
- Replacement PID `1321833` runs the final installed cache with cwd
  `/data/CoordExp/.codex/plugins/cache/coordexp-local/codex-wake-me-up/0.1.0+codex.20260901013840` and
  `PYTHONPATH` rooted at its `src` directory.
- Heartbeat reports `accepting_work=true`, event epoch `3`, delivery epoch `1`, and source identity
  `ec7165873cbe2abfd4915ff370b5e6cc8450c38e2bd6e02c800a14f9b617f6f6`.
- `/proc/locks` independently reports PID `1321833` as the writer for `daemon.lock`.

## Isolated residency smoke

The installed runtime source (identity unchanged across the final cache
reinstall) was exercised against the private disposable runtime
`/tmp/wake-residency-smoke-4a4zj6pl` (not the main runtime database). A durable
`armed` monitor was cancelled, then the daemon remained resident through the
fixed 60-second grace, wrote `accepting_work=false`, exited with code `0`, and
left that monitor `cancelled`. A second durable monitor was then created after
retirement; `ensure_daemon_ready` started exactly one replacement generation,
which became healthy and lock-owning before the monitor was cancelled and the
smoke process was stopped.

Receipt: first monitor `residency-b21d7459f70f4b1c8d6194f1852d6947`, retired
PID `1309060`, state `armed -> cancelled`; replacement monitor
`residency-fb6276c446a9439a926a3b4f9ada4ff3`, replacement PID `1309454`,
state `cancelled`. Replacement command line was
`python -m codex_wake_me_up.daemon --runtime-root /tmp/wake-residency-smoke-4a4zj6pl/runtime/codex-wake-me-up`;
its cwd was the installed cache `0.1.0+codex.20260901012502` used for the
smoke; the final `0.1.0+codex.20260901013840` cache has the same runtime source
identity. Source identity and capability epochs
matched the installed heartbeat, and kernel lock ownership was verified.

This is a daemon-residency/on-demand-start smoke, not a live app-server or
ThreadDelivery wake-acceptance claim; no main Desktop/app-server state was
changed.

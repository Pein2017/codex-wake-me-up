## Why

`worker_terminal` currently requires a Git delivery scope and can only describe a
successful worker by naming a candidate commit. Native Codex and HarnessDock
Agent turns also need a durable, non-success terminal fact when they finish
normally without producing a commit, so an L0 can end its turn and later wake on
mixed `all` barriers or independently armed first-settlement monitors.

## What Changes

- Allow a `worker_terminal` reservation to omit Git delivery scope as one
  all-or-none group; scoped reservations retain the current attestation contract.
- Add worker outcome `completed` for a normally ended producer turn with no
  candidate commit and explicit `task_success=false` / `lead_accepted=false`.
- Reject `delivered` whenever the reservation omitted Git scope, and continue to
  require exactly one candidate commit plus read-only attestation when scope is
  present.
- Expose the existing mode-0600 descriptor adapter through narrow CLI preflight
  and publish commands so an existing native or HarnessDock launcher can verify
  its frozen producer binding before launch and later publish without placing
  bearer tokens in arguments.
- Bump the terminal-event capability epoch so an older daemon cannot supervise
  the expanded event vocabulary.
- Preserve existing `all`/`any`, one-shot binding, idempotency, queue delivery,
  expiry, heartbeat, and acceptance boundaries.
- Do not add a wait group, native ThreadId resolver, automatic re-arm,
  follow-up, interruption, acceptance, installation, or daemon restart.

## Capabilities

### New Capabilities

None.

### Modified Capabilities

- `codex-terminal-events`: admit scopeless worker reservations, the non-success
  `completed` settlement, and descriptor-based CLI publication while preserving
  scoped delivery attestation and one-shot monitor semantics.
- `codex-wake-me-up-monitor`: treat `completed` as terminal worker evidence in
  singular and composed conditions without promoting it to success or acceptance.

## Impact

- Affects terminal-event validation, persistence projections, condition/status
  evidence, descriptor publication CLI wiring, focused tests, and packaged
  operator/agent guidance under `/data/CoordExp/codex-wake-me-up`.
- No database schema migration is expected because delivery scope is stored in
  semantic JSON; event capability epoch and compatibility checks do change.
- A coordinated HarnessDock change may consume the descriptor contract, but
  this change neither reads HarnessDock state nor launches any Agent.

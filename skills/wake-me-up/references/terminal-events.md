# Producer terminal events

Read this only when an existing command or worker harness can publish a durable
terminal event.

## Reserve, launch, bind

Reserve an expiring single-use capability before launch, hand it to the existing
harness, launch through that harness, and bind its reservation in
`wait_for_event`. A fast producer may publish before binding if binding commits
before the reservation deadline. After a successful bind, end the lead turn.

Use MCP `wake_me_up_event_reserve`, `wake_me_up_event_status`,
`wake_me_up_event_cancel`, `wake_me_up_event_heartbeat`, and
`wake_me_up_event_publish`, or the equivalent CLI event commands. Reserve,
heartbeat, and publish accept only a current-user-owned mode-`0600` regular JSON
file of at most 64 KiB. Never put a publish token in a CLI/MCP argument.

The raw token is returned only on first creation. Capture that response or ask
reserve to write a mode-`0600` `publisher_descriptor_path`; an idempotent retry
returns identity and fingerprint, never the bearer. Native or HarnessDock
workers may use `publish_worker_terminal_from_descriptor` with the same
`worker_terminal` envelope.

## Evidence meaning

- `command_terminal` wakes for `succeeded`, `failed`, `cancelled`, or
  `signaled`. Exit code zero proves only that bounded command process outcome.
- `worker_terminal` wakes for `delivered`, `blocked`, `failed`, or `cancelled`.
  `delivered` carries a candidate commit and bounded Git attestation for
  independent lead review; it is never lead or task acceptance.
- Missing, baseline-mismatched, out-of-scope, or errored worker attestation still
  wakes with failure evidence. Do not wait for expiry or promote it to success.
- `heartbeat_stale` is heuristic liveness evidence: accepted heartbeats stopped
  advancing under the declared interval. It does not prove worker failure and is
  unknown without the required initial heartbeat or reservation identity.

After the pointer, read decision status exactly once. It preserves producer,
candidate, attestation, failure evidence, heartbeat facts, and explicit
`task_success=false` / `lead_accepted=false`. Independently review any candidate.

## Cancellation and rollback

Cancellation or expiry makes an unbound reservation unusable. A bound
reservation cannot be rebound or reused after cancellation, expiry, pause or
activation failure, or daemon restart. Never retry publication or automatically
re-arm.

Publish tokens, raw command arguments, credentials, and sensitive user text must
not enter status or logs; retain only bounded evidence and fingerprints. The
plugin never launches commands or stages, commits, merges, reverts, or pushes.

Before an authorized rollback to an older event epoch, run the current binary's
`event-compatibility-check --supported-event-epoch <target>` and stop if it
refuses. Do this before source/cache replacement; an older binary cannot enforce
a guard added later. Completed event history alone does not block rollback.

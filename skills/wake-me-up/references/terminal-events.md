# Producer terminal events

Read this only when an existing command or worker launcher can publish durable
terminal evidence.

## Reserve, launch, arm

Reserve an expiring single-use capability before launch. Its first creation
receipt has a private publisher descriptor (when requested) for the existing
launcher and a non-secret typed `monitor_condition` for the lead. Give the
descriptor to the launcher; it owns the producer and may return that condition
in its spawn receipt. This plugin does not launch, discover, or inspect the
producer.

Launch first, then pass the returned `monitor_condition` to `wait_for_event`.
Require the durable `armed` receipt and end the turn. This asynchronous arm is
required when a later wake is wanted; skipping a synchronous join is not
permission to omit it. A fast producer may publish before binding if binding
commits before the reservation deadline.

For several producers, reserve and launch each separately, then combine their
returned leaves using typed `all` or `any` in one arm. `all` waits for every
terminal settlement. `any` wakes at the first true member, but all bound
reservations remain permanently consumed. There is no batch launcher, fallback,
or automatic re-arm in this contract.

For example, arm two returned leaves without reconstructing their IDs:

```json
{"type":"all","children":[{"type":"worker_terminal","reservation_id":"event-a"},{"type":"worker_terminal","reservation_id":"event-b"}]}
```

Use MCP `wake_me_up_event_reserve`, `wake_me_up_event_status`,
`wake_me_up_event_cancel`, `wake_me_up_event_heartbeat`, and
`wake_me_up_event_publish`, or the equivalent CLI event commands. Reserve,
heartbeat, and publish accept only a current-user-owned mode-`0600` regular JSON
file of at most 64 KiB. Never put a publish token in a CLI/MCP argument.

The raw token is returned only on first creation. Capture that response or ask
reserve to write a mode-`0600` `publisher_descriptor_path`; an idempotent retry
returns identity and fingerprint, never the bearer. An existing native or
external worker launcher may use `publish_worker_terminal_from_descriptor` with
the same `worker_terminal` envelope. This documents a public
descriptor-in/condition-out seam only; it does not claim any particular launcher
integration exists.

## Evidence meaning

- `command_terminal` wakes for `succeeded`, `failed`, `cancelled`, or
  `signaled`. Exit code zero proves only that bounded command process outcome.
- `worker_terminal` wakes for `delivered`, `blocked`, `failed`, `cancelled`, or
  `settlement_uncertain`. The uncertain outcome records a bounded reason why
  settlement could not be validated and explicitly remains non-success.
  `delivered` carries a candidate commit and bounded Git attestation for
  independent lead review; it is never lead or task acceptance.
- A worker may also declare bounded opaque `producer_invocation_id` and
  `result_ref`. Status labels the latter `publisher_declared`; neither reference
  is dereferenced or validated by this plugin, and neither is acceptance.
- Missing, baseline-mismatched, out-of-scope, or errored worker attestation still
  wakes with failure evidence. Do not wait for expiry or promote it to success.
- `heartbeat_stale` is heuristic liveness evidence: accepted heartbeats stopped
  advancing under the declared interval. It does not prove worker failure and is
  unknown without the required initial heartbeat or reservation identity.

After the pointer, read decision status exactly once. It preserves producer,
candidate, attestation, failure evidence, heartbeat facts, and explicit
`task_success=false` / `lead_accepted=false`. Independently review any candidate.

## Exact PID and Git evidence

Point `pid_exit` at the actual producer PID, not a tmux server, pane, shell, or
wrapper. tmux is only a shell around that PID; `tmux_exit` observes target
removal and is liveness evidence, never producer completion or success.

`git_ref_change` observes one absolute local worktree and literal `HEAD` or a
full direct `refs/...` name read-only. It fires once for `fast_forward`,
`ref_rewrite`, `ref_deleted`, or `head_retarget`; repository identity or read
uncertainty is observer failure. Ref movement is heuristic progress evidence,
not worker, command, task, or lead success. No hooks, history scans, or shell
execution are involved.

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

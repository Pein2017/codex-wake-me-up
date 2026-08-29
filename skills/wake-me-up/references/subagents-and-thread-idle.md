# Spawned subagents and thread idle

Read this when a spawned Codex worker arms the monitor or when the condition is
`thread_idle`.

For an exact `thread_spawn` V2 child, trusted metadata records that child as the
origin and follows its validated parent chain to the topmost root. Only the root
receives the pointer. Missing, cyclic, malformed, over-bounded, non-spawn, or
non-direct-input ancestry fails before arming.

After the child receives `armed`, it reports the compact receipt and ends its
turn. If root is currently in `wait_agent`, that completion may wake root; root
records the receipt and also ends its turn rather than issuing another wait for
the monitored interval. The durable job and daemon continue while the model
turns are idle.

When the condition fires, root reads
`wake_me_up_status(monitor_id, view="decision")` exactly once and decides whether
to continue itself, activate a proven child continuation path, or stop. The
plugin never resumes, recreates, follows up, or accepts a child.

`{"type":"thread_idle","thread_id":"<child>"}` observes a locally loaded
thread ending its turn. A child already idle is rejected unless
`accept_already_idle` is explicit; handle its existing result in the current
turn. Unloaded or missing is unknown, not completion. Idle is a lifecycle
witness and never proves the child's work succeeded.

A spawned child may monitor its own future idle because its root is the delivery
target. It must not monitor the root whose wake would make that root active.
Compose several child waits with typed `all` only when each child is independently
observable.

Ending a model turn does not interrupt an independently running host process or
active child. Explicit `interrupt_agent`, close, or shutdown does stop the named
worker; Codex/app-server exit stops in-flight Codex computation even if the host
daemon survives.

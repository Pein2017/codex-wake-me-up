## ADDED Requirements

### Requirement: Retire an idle daemon without abandoning durable work

The lock-owning delivery daemon SHALL remain resident while any monitor is
non-terminal. When no monitor remains non-terminal, it SHALL wait through a
bounded idle grace, recheck durable state, advertise that it is no longer
eligible for new readiness claims, and then exit cleanly. Daemon retirement
MUST NOT delete or rewrite monitor rows, event reservations, receipts, terminal
history, or delivery evidence.

A registration or delivery path MUST treat a daemon that has committed to
retirement as unavailable, even if its process or previous heartbeat is still
observable. Wherever the existing contract requires readiness before arm,
pause, or delivery, that path SHALL start or positively observe a non-retiring,
lock-owning daemon before continuing. A retirement race MUST fail closed or
establish replacement supervision; it MUST NOT bypass an existing readiness
gate or strand work because a replacement start collided with the retiring
lock owner.

#### Scenario: Outstanding monitor keeps the daemon resident
- **WHEN** at least one durable monitor is non-terminal
- **THEN** the lock-owning daemon continues reconciliation and does not retire
  because its caller thread, MCP frontend, or producer is idle or absent

#### Scenario: Empty durable work reaches the idle grace
- **WHEN** all monitors are terminal and no new non-terminal monitor appears
  throughout the bounded idle grace
- **THEN** the daemon relinquishes readiness and its lock, exits cleanly, and
  leaves all durable ledger and receipt evidence unchanged

#### Scenario: New work appears during the idle grace
- **WHEN** a registration creates non-terminal work before the daemon commits
  to retirement
- **THEN** the daemon observes that work on its final durable recheck and
  remains available to reconcile it

#### Scenario: Readiness-gated registration races committed retirement
- **WHEN** a readiness-gated registration observes a daemon process while that
  daemon has already committed to retirement
- **THEN** the readiness check rejects that generation and registration does
  not arm until a non-retiring lock owner is positively ready

#### Scenario: Registration follows completed retirement
- **WHEN** a new monitor is requested after the previous idle daemon exited
- **THEN** on-demand startup establishes a healthy lock-owning daemon and the
  monitor arms only after the existing readiness gates pass

#### Scenario: Replacement startup is unavailable
- **WHEN** an existing pre-arm or pre-pause readiness gate finds no eligible
  daemon and on-demand replacement cannot become ready within its bound
- **THEN** the operation records its existing daemon-unavailable evidence and
  does not arm the monitor or pause the goal

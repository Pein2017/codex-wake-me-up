# Legacy paused-goal delivery

Read this only when an explicit active or paused goal genuinely needs guarded
pause/reactivation semantics. Ordinary monitoring stays goal-independent through
`wait_for_event`.

Use `defer_goal_until_event` for the explicit legacy path. Its supplied thread ID
is a best-effort local target, not authenticated as the current task. The service
must establish compatible daemon readiness before pausing, persist defer intent,
make one pause request, and re-read the exact paused-goal guard before arming.
Uncertain pause delivery, changed identity, unloaded/busy target, or transport
failure is terminal and never retried or compensated.

`wake_me_up` and `wake_me_up_defer` are compatibility aliases, never fallbacks
for unavailable thread delivery. No operation creates a goal.

For a confirmed deferred monitor, condition, expiry, unauthorized heuristic
evidence, and fatal observer failure converge on the same one guarded wake with
an explicit `wake_reason`. Expiry wakes only after the target is observed idle;
a target stuck non-idle can remain armed past expiry.

Do not wrap a condition in `any(condition, time)` as a deadline backstop. It
spends the one wake earlier and obscures whether the real condition fired.

Immediately before activation the target must still be loaded, idle, and own the
captured paused goal. The service makes at most one continuation request and
compares the returned marker. Mismatch becomes `mis_targeted_activation`;
uncertainty remains fail-closed. Never retry, compensate, create another goal,
or switch delivery kind.

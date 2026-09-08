# Queue delivery and reconciliation

Read this when delivery is not plainly `recorded`, when cancelling an admitted
pointer, or when investigating queue/history behavior.

Thread delivery first performs a read-only capability preflight. A typed transport
failure there leaves the claimed pointer unattempted on the existing daemon cadence
until recovery or explicit cancellation; trigger expiry is not a post-claim
delivery deadline. A conclusive rejection, wrong-root or permission failure, or
malformed response remains terminal.

After successful preflight, delivery consumes its sole queue-add attempt before
the transport write. Queue ACK, queue presence, durable history recording, and
terminal classification are separate facts. An uncertain response or recovered
in-progress admission never permits another add.

Reconciliation checks exact history, then the exact queue, then 60 seconds of
persisted online absence using the stable delivery ID and pointer digest:

- matching history becomes `recorded` and preserves observed pointer count;
- changed queued or recorded content becomes `delivery_modified`;
- continued absence becomes `delivery_uncertain` with
  `absence_kind=unresolved_absence` and observed causes;
- capability loss, rejection, interruption, archive, storage ambiguity, and
  observer errors remain typed fail-closed diagnostics.

The queue is shared user state and is not exactly-once. Users may edit, reorder,
or delete items; Core may replay or lose a pointer around dispatch crashes.
Never restore user text, blindly re-add, claim a delivery deadline, or infer
success from queue acceptance. Duplicate pointers still name one immutable
monitor report.

After the trigger and sole add attempt are consumed, a `queue_accepted` monitor
may be named as archival `rearm_of` lineage for a fresh registration. This does
not settle the parent, stop reconciliation, inherit success, or permit another
add; the child runs every registration check independently.

For a stored unloaded root, admission records the identities/count of FIFO items
ahead before exact local resume. Resume may release those user items first and
spend model budget. It does not start the pointer directly or call
`thread/queue/start`.

Cancellation before admission prevents delivery. After conclusive acceptance it
may make one exact removal request; matching history means too late, and absence
uses the same bounded uncertainty rule. Never claim cancellation when the
result is ambiguous.

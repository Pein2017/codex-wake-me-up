## 1. Registration budget

- [x] 1.1 Add a documented monotonic budget spanning target resolution and app-server condition observations, with the existing single transport retry consuming the same budget; verify delayed fake reads return within the budget and never create a row or queue item
- [x] 1.2 Extend the typed pre-arm timeout receipt without breaking existing error payloads; verify stage, bounded reason, `monitor_created=false`, `safe_to_retry`, and retry-attempt fields for timeout, definitive failure, and exhausted retry paths

## 2. Receipt and current-task diagnostics

- [x] 2.1 Change armed registration wording and packaged guidance to call `expires_at` an observation expiry and separate notification, target consumption, completion, and acceptance; verify focused receipt and documentation tests
- [x] 2.2 Expose compact condition, delivery, supervision, and available lifecycle/reconciliation timestamps in the trusted current-task monitor query; verify admitted, uncertain, and unavailable-target fixtures remain visible without replay or cleanup

## 3. Verification and handoff

- [x] 3.1 Run focused registration, delivery, public-API, and monitor-query tests plus lint/compile checks; verify no regression in at-most-one admission and no-blind-resend assertions
- [x] 3.2 Validate the OpenSpec change strictly and inspect the final diff; record that live native-Core qualification remains HOLD and no install, restart, or cross-session message-path change was performed

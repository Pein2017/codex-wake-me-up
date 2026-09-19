## ADDED Requirements

### Requirement: Bound exact thread-delivery preflight without weakening admission

When a claimed `ThreadDelivery` revalidates its exact target, the app-server
read path SHALL have a bounded request budget that accommodates large thread
metadata reads while remaining finite. A transport timeout SHALL leave the
monitor in `claimed` with `delivery_state=unattempted`, SHALL record a typed
transport diagnostic, and SHALL permit a later retry; it SHALL NOT issue a
queue admission or silently substitute a weaker condition or target.

#### Scenario: Large target metadata read completes within the delivery budget
- **WHEN** exact target capability verification takes longer than the previous
  short RPC budget but completes within the configured bounded budget
- **THEN** the monitor performs the normal single queue admission attempt and
  records its independent queue receipt

#### Scenario: Exact preflight remains unavailable
- **WHEN** the bounded target read still times out before queue admission
- **THEN** the monitor remains `claimed/unattempted`, exposes the failed
  preflight stage and retry disposition, and sends no queue item

### Requirement: Reconcile pending thread deliveries fairly

The durable daemon SHALL give newly claimed or in-progress admission and
cancellation work an opportunity before retrying older admitted pointers. A
transport/offline reconciliation failure SHALL persist a bounded retry time
and use exponential backoff capped at a finite maximum. Backoff SHALL defer
only the next observation; it SHALL never create a second queue admission,
delete a monitor, or declare delivery success.

#### Scenario: Fresh claimed wake is not hidden behind stale admitted rows
- **WHEN** older `queue_accepted` monitors require slow history reads and a
  newer monitor is already `claimed`
- **THEN** the daemon attempts the newer monitor first and can reach its single
  queue admission without waiting for every older history read

#### Scenario: Repeated offline reconciliation backs off
- **WHEN** an admitted or cancellation monitor repeatedly cannot obtain an
  app-server response
- **THEN** its status records the next retry time and increasing bounded delay,
  while later monitors remain eligible and no blind resend occurs

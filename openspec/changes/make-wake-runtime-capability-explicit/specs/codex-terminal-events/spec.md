## ADDED Requirements

### Requirement: Preserve bounded publisher-declared worker completion references

A worker-terminal event SHALL accept optional bounded opaque
`producer_invocation_id` and `result_ref` fields. The stored event and any
wake report SHALL label a result reference as `publisher_declared`. These
fields SHALL participate in immutable event idempotency. The system MUST NOT
dereference, read, validate, execute, or treat either field as a task-success,
lead-acceptance, or user-acceptance claim.

#### Scenario: Worker reports its execution and result reference
- **WHEN** a worker publishes a valid terminal event with bounded invocation
  and result-reference fields
- **THEN** the immutable event and its wake report retain those exact declared
  fields with `result_ref_provenance=publisher_declared`

#### Scenario: Worker retries with a changed result reference
- **WHEN** a terminal publication retry changes either declared reference
- **THEN** idempotency rejects the changed publication and retains the original
  immutable event

#### Scenario: Result reference is malformed or oversized
- **WHEN** a worker payload supplies an empty or over-budget reference
- **THEN** publication is rejected before terminal state changes

#### Scenario: Lead receives a declared result reference
- **WHEN** a wake report includes a publisher-declared result reference
- **THEN** it remains handling evidence and does not cause the plugin to open
  the reference, accept work, or infer task success

## MODIFIED Requirements

### Requirement: Observe a reserved worker-terminal event

The system SHALL support a `worker_terminal` condition leaf naming one bound
worker reservation. The leaf SHALL become true for `delivered`, `completed`,
`blocked`, `failed`, `cancelled`, and `settlement_uncertain` outcomes. Its
witness SHALL carry the producer identity and declared outcome and, only for a
delivery, the candidate commit and bounded Git attestation. Worker completion,
termination, uncertain settlement, and delivery SHALL remain candidate,
lifecycle, or failure evidence and MUST NOT be represented as lead acceptance
or task success.

#### Scenario: Worker delivers a valid candidate
- **WHEN** the bound worker event is `delivered` and Git attestation is valid
- **THEN** the leaf becomes true with the candidate commit and changed-path
  evidence for lead review

#### Scenario: Worker delivery is invalid
- **WHEN** the worker event is terminal but Git attestation reports a missing,
  baseline-mismatched, or out-of-scope candidate
- **THEN** the leaf still becomes true with an invalid-delivery witness so the
  lead can handle it instead of remaining asleep

#### Scenario: Worker completes without a commit
- **WHEN** the bound worker publishes `completed`
- **THEN** the leaf becomes true with lifecycle evidence, no candidate review
  target, and explicit false task-success and lead-acceptance flags

#### Scenario: Worker ends without a commit
- **WHEN** the bound worker publishes `blocked`, `failed`, `cancelled`, or
  `settlement_uncertain`
- **THEN** the leaf becomes true with its bounded reason and no invented review
  target, task success, or lead acceptance

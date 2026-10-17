# Durable operations and short transactions

## Requirement

No application database transaction may remain open during LLM, embedding,
Redis, Qdrant, or other slow external work. HTTP requests remain synchronous.
An in-progress operation is durable, visible, and cannot authorize execution.

## Implementation steps

1. Register `workflow_operations` in SQLModel/Alembic metadata. Validate every
   write through a Pydantic contract. Store kind, scoped idempotency key, input
   hash, stage, lease owner/expiry, checkpoint, failure, and encrypted response.
2. Claim operations in short transactions. Same key/input reuses progress or
   result; changed input conflicts. Expired or retryable failed operations can
   resume from checkpoints. Owner tokens fence obsolete workers.
3. Split evaluation into cloud evidence collection and transactional policy
   finalization. Use current locked policy/autonomy at finalization; freeze
   those exact inputs in the decision. Never repeat cloud calls inside it.
4. Reserve interpretation revisions before parsing. Block further answers
   while processing; record parser outcomes using existing interpretation and
   decision contracts. Save decisions and their interpretation links atomically.
5. Create escalations as PREPARING with the completed held decision. Generate
   explanations outside transactions, then atomically mark packages PENDING.
   Approval/rejection is allowed only for a prepared PENDING package.
6. Snapshot rollback inputs, collect compensation evidence outside transactions,
   then recheck original execution/resource state under lock before completing.
7. Wrap control mutations in durable idempotency as well. Keep approval,
   promotion, token consumption, and simulated execution database-only.
8. Add provider-boundary transaction guards, operation status inspection, and
   documentation. Validate rollback, crash/reclaim, concurrent duplicate,
   stale-worker fencing, conflicting input, and review stage restrictions.

## API semantics

`Idempotency-Key` is optional where a request/resource ID provides a stable
default. Clients must reuse the same key AND complete payload on retry.
In-progress duplicates return HTTP 202 with operation ID, stage, status URL,
and Retry-After. Completed retries return the original response (including the
original token; expired or consumed tokens are never refreshed). Cached response
bodies are encrypted using a key derived from SECRET_KEY. Rotating that secret
invalidates cached response decryption as well as existing token verification.

Short transactions checkpoint durable results. A failure after a checkpoint
does not erase earlier progress; a retry continues from it. No background worker
or automatic restart recovery is introduced: a repeated request resumes an
expired lease. Configure WORKFLOW_LEASE_SECONDS above the maximum single slow
stage duration. Lease ownership is checked again before writes.

## Storage and migration

Existing decision, interpretation, human review, and monitoring records retain
their roles. Workflow state belongs in workflow_operations, not in fake policy
decisions. Interpretation PROCESSING/PROCESSING_FAILED and escalation PREPARING are explicit enum
values with corresponding validation. Generate and review migrations separately;
this implementation does not generate or apply a migration.

## Implementation and verification

- `src/services/workflow.py` owns claims, leases, fencing, encrypted responses,
  and stage transactions; `workflow_flows.py` owns evaluation, interpretation,
  review preparation, and rollback sequencing.
- The API returns `X-Operation-ID`. GET `/api/v1/operations/{operation_id}` shows
  progress/failure/lease expiry. It does not expose prompts, checkpoint data,
  cached tokens, or lease ownership credentials.
- For evaluation retries, provide the same `request_id` or `Idempotency-Key` and
  exactly the same submitted fields. The automatically generated timestamp is
  frozen on the first run. An omitted request ID requires an explicit header to
  identify retries reliably.
- Approval and execution default to a fingerprint of the submitted payload.
  Thus a rejected invalid token cannot reserve the idempotency slot for a later
  valid token. Explicit header reuse with a changed payload still conflicts.
- Promotion commands require `Idempotency-Key` (1 to 200 characters). Missing
  headers return 422 before a workflow is claimed. Reuse the same key and body
  for retries; use a new key for a new attempt after eligibility changes.
- Identical execution retries return the original result without consuming a
  second token or writing another execution. To test rejection of an already
  consumed token, submit it under a NEW idempotency key.
- PostgreSQL active-policy lookup, promotion, and policy bootstrap share a
  stable transaction advisory lock, acquired before querying the active row.
  The lookup therefore sees the committed policy after a concurrent promotion,
  rather than losing the old row while waiting for it. Commit/rollback releases
  the lock. Evaluation holds it before the autonomy lock; no provider call runs
  while it is held. Finalization saves the exact current policy/state snapshots.
- Decision persistence can precede review explanation completion. Such an
  escalation is PREPARING and cannot be reviewed. Retry resumes explanation
  preparation without recreating the decision or advancing autonomy twice.
- If another worker prepares the same escalation first, its committed package
  is authoritative. A retry can finish from that package even if a reviewer
  has already approved or rejected it; it does not regenerate or replace the
  explanation.
- A rollback whose compensation requires review returns `PENDING_APPROVAL` and
  includes `compensation_decision_id`. The reviewer can use that decision ID
  with the existing approval endpoint. Approval authorizes the compensation;
  the caller must still submit the returned capability token to the execute
  endpoint. `FAILED` is reserved for a compensation that was refused or could
  not safely be applied.
- Permanent interpretation processing failures record PROCESSING_FAILED with
  the failure message on the reserved turn; a revised clarification can proceed.
  Retryable interruptions keep the reservation for the original operation.
- Exhausted parser-provider calls return 502 and leave a retryable FAILED
  operation. A repeated request with the same key reserves a new interpretation
  revision and retries parsing. Prior failed turns, error JSON, and feedback are
  retained. A newer clarification prevents an old request from reclaiming that
  interpretation revision.
- Internal workflow failures retain the exception message and cause chain in
  the operation record. The status API exposes the public failure summary;
  existing parser attempt records continue to retain detailed model feedback.
- Retryability follows the failure cause: lost workflow leases and transient
  database/provider failures can retry, while stale review/input conflicts and
  integrity violations remain terminal for that idempotency key.
- Direct service callers must supply frozen evidence to transactional
  evaluation and staged context/evidence to rollback. The CLI harness uses the
  durable workflow as well. Guards reject accidental cloud work from an open
  SQLAlchemy Session transaction.

Before running against an existing database, generate a migration and inspect it
for the new table AND changes to `ck_interpretation_status` and
`ck_escalation_status`. Alembic autogeneration may not detect check-constraint
changes. No migration file was generated or applied for this implementation.

The current test fixtures use in-memory SQLite. Per the project's live-test
requirement, do not run those fixtures for this workflow; move the relevant
checks to an isolated PostgreSQL test schema before using them for verification.
PostgreSQL concurrency and provider E2E verification require the migration to be
reviewed and applied first.

## Current verification result

No tests were run for the latest workflow fixes. Static Python compilation and
`git diff --check` passed. A read-only inspection of the configured PostgreSQL
database found revision `1abda896bc78`, no `workflow_operations` table, and no
`PREPARING` value in `ck_escalation_status`; this prevents live workflow tests
until the migration is applied. Redis and both configured Qdrant collections
were reachable during the same verification. No SQLite database was opened.

Migration generation (to be run separately):

```bash
alembic revision --autogenerate -m "Add durable workflow operations"
```

Review the generated table, JSON null handling, uniqueness/index/check
constraints, and both existing status constraints before upgrading. Startup
checks report an outdated workflow schema or status constraint explicitly.

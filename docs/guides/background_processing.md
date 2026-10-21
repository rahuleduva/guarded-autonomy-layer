# Background workflow operations

Slow evaluation, natural-language interpretation, clarification, rollback, and
review-package preparation can run through a PostgreSQL outbox and Celery. The
API records an operation and dispatch row in one short transaction, returns
`202 Accepted`, and does not wait for a model or broker call. PostgreSQL remains
the source of truth; Celery result storage is disabled.

Background processing stays disabled unless `BACKGROUND_WORKFLOWS_ENABLED=true`.
The flag selects the mode for new operations only. Previously accepted
background operations remain queued and resumable when it is disabled; keep
the dispatcher and workers running to drain them.
The API code now reads the extended workflow model, so the reviewed Alembic
migration that adds workflow scheduling columns, `workflow_dispatches`, and
monitoring links must be applied before starting this code version, even while
the feature flag is false. This implementation intentionally does not generate
or apply that migration.

## Setup

1. Install the project dependencies with `pip install -r requirements.txt`.
   The worker dependency is Celery 5.6.3. Check that this version installs on
   the Python runtime selected for the worker before rollout.
2. Set `CLOUDAMQP_URL` to the CloudAMQP `amqps://` URL in `.env`. Never print
   the URL or pass it as a command-line argument.
3. Generate the schema migration yourself, review it, then apply it with
   `alembic upgrade head`. Keep `BACKGROUND_WORKFLOWS_ENABLED=false` until
   PostgreSQL contains the new schema.
4. Set `BACKGROUND_WORKFLOWS_ENABLED=true` for the API, dispatcher, and worker.
   They must share the same database URL, secret key, queue name, and routing
   configuration.

## Run locally against configured infrastructure

Start three processes from the repository root:

```sh
uvicorn src.main:app --host 127.0.0.1 --port 8000
python -m src.workers.dispatcher
python -m celery -A src.workers.celery_app:celery_app worker --pool=solo --concurrency=1 --queues=guarded-autonomy-layer.workflows.v1 --loglevel=INFO --without-gossip --without-mingle --without-heartbeat
```

The queue name must match `WORKFLOW_QUEUE_NAME` in all processes. The `solo`
pool is a cautious starting configuration, not a measured production sizing
choice. Do not increase concurrency before checking the PostgreSQL connection
budget and the project plan's long-task/heartbeat scenarios.
The configured Celery hard time limit is not enforced by the solo or threads
pool. PostgreSQL run deadlines fence late results, while provider socket
timeouts bound calls; validate the worker pool before choosing it for deployment.

## Recovery and audit

Background failure handling stores the complete error and cause chain in
`request_attempt_events.parser_trace.failure`, linked by `operation_id`, in the
same transaction as its failure/retry transition. Automatic recovery retains
the preceding error during `RETRY_WAIT` and creates exactly one dispatch for
the new generation. Final failure also marks the matching reserved
interpretation turn `processing_failed`. Explicit authorized retries preserve
the normalized request and checkpoints, refresh credentials, and reset their
automatic recovery allowance. Queue timeouts allow explicit resubmission.

Completed parser, analyzer, and judge attempts are flushed before another model
call starts, preserving their original input, response, validation JSON, and
correction history across a later worker crash. If PostgreSQL is unavailable,
the events remain pending for another flush and structured fallback logs are
written. Broker publication failures are recorded as `workflow_publish` events
with their exception messages and cause chains, with broker credentials redacted.

Submission freezes generated request IDs, timestamps, and defaults in the
encrypted `background-input-v1` envelope. Natural-language and clarification
turns are reserved before acceptance commits. The submitted-field digest stays
separate so clients can retry an identical payload without supplying defaults.

Completed operations release their worker/scheduling fields. Both idempotent
POST responses and result reads apply the same review-response redaction;
agent capability retrieval continues through its ownership-checked endpoint.

## API behavior

With background mode enabled, the eligible POST routes return a body containing
`operation_id`, `status`, `stage`, `status_url`, and `result_url` with HTTP 202.
Poll the status URL; fetch the original final response from the result URL.
Both reads use the caller's current credentials. Only the initiating principal
can fetch the result. Human evidence access does not reveal an agent's result
or capability token.

Human approval/rejection, policy promotion, agent promotion, and the simulated
execution endpoint remain synchronous. No human decision can occur before its
escalation has reached a reviewable state.

`GET /api/v1/operations/diagnostics` requires a human with `evidence:read`. It
reports PostgreSQL counts, pending-work age, process heartbeat age, and recent
failure counts. It does not connect to the broker and is not a substitute for
checking whether a particular workflow is making progress.

For failure recovery and the live end-to-end cases, see
[`background_processing_plan.md`](../plans/background_processing_plan.md).

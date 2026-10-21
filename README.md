# Guarded Autonomy Layer

A FastAPI backend that evaluates proposed agent actions before authorizing execution. It combines deterministic policy rules, LLM parsing and risk analysis, semantic retrieval, human review, and an auditable autonomy history.

Execution operates on **synthetic resources stored in PostgreSQL**. Paths identify those resources; the application does not modify files on the host machine.

## Capabilities

- Authenticated agents with revocable API keys; human reviewers with personal API keys or password login and bearer JWTs.
- Structured actions and natural-language requests with grounding checks, corrective parser retries, and clarification for missing information.
- Role, sandbox, environment, deny-pattern, risk, and autonomy checks enforced by a deterministic policy engine.
- Operation- and risk-focused semantic queries, filtered Qdrant retrieval, and an optional LLM risk judge.
- Combined human-review packages with recorded evidence, explanations, fingerprints, and reviewer notes.
- Expiring, single-use execution capabilities, separate from caller authentication.
- Durable background operations using PostgreSQL, Celery, and CloudAMQP, with idempotency and guarded state transitions.
- Frozen-evidence decision replay, guarded compensation, shadow policy evaluation, and reviewer-controlled promotion.

Supported operations are `read_file`, `create_file`, `create_directory`, `update_file`, `move_file`, `delete_file`, and `delete_directory`. The active policy determines which roles may propose them.

## How a request moves through the system

```mermaid
flowchart TD
    A[Authenticated agent request] --> B{Background workflows enabled?}
    B -->|Yes| C[Persist operation and input; return 202]
    B -->|No| D[Run synchronously]
    C -->|Worker| E[Parse and validate action]
    D -->|API process| E
    E --> F[Analyze risks, retrieve examples, and judge evidence]
    F --> G[Deterministic policy evaluation and recorded decision]
    G -->|Needs review| H[Prepare combined review package]
    H --> I[Reviewer approves or rejects package]
    I -->|Rejected| J[Record rejection; stop]
    I -->|Approved| K[Check frozen evidence and current hard restrictions]
    K -->|Still eligible| L[Record authorization and issue capability]
    K -->|Blocked| M[Record approved but blocked; no capability]
    G -->|Allowed| L
    G -->|Denied| O[DENIED; no capability]
    G -->|Shadow only| P[SHADOW_LOGGED; no capability]
    L --> N[Agent submits separate execution request]
```

Background submission returns before parsing and evaluation finish; the caller polls the operation for its final response. With background processing disabled, the same work runs synchronously. The diagram shows the semantic review flow. Rejection closes the review without authorization. Approval can still be blocked by SHADOW or another hard restriction, and no capability is then issued.

With background processing enabled, eligible submissions return `202 Accepted` after recording an operation and dispatch entry. A dispatcher publishes work to CloudAMQP and a Celery worker performs it. Clients poll for the final response. PostgreSQL stores operation state and results; Celery result storage is disabled.

Slow provider work runs between short database transactions. Processing states, checkpoints, leases, and guarded writes track work in progress and protect retries. Human review does not keep the original agent request open.

### Semantic assessment

Natural-language input can produce a parsed action and risk analysis in one LLM response. Structured input uses a separate analysis call without rewriting the submitted action. Retrieval combines an action query with risk-specific queries, gathers risky, benign, and uncertain examples, and deduplicates candidates. The judge assesses which risks apply using the request and retrieved evidence.

Pydantic models validate structured responses, and application validators check exact paths, action-specific parameters, quoted evidence, and consistency with the original request. Invalid output enters a bounded correction workflow with the previous response, validation-error JSON, and correction instructions in the message history. Provider failures have a separate retry budget for each output attempt; ordered model fallbacks and stage deadlines bound the work.

Caller claims that credentials are revoked, disabled, expired, or nonfunctional are recorded as **unverified** when safety depends on those claims. The analyzer must preserve the missing verification, and the judge cannot clear it merely because the caller says the credentials are harmless. The application does not contact credential issuers to verify their status. Metadata-only inventories and quoted documentation are assessed in their own context.

`SEMANTIC_PIPELINE_MODE` selects the behavior:

| Mode | Behavior |
| --- | --- |
| `baseline` | Use the original similarity advisory; default setting. |
| `observe` | Record the newer analysis/retrieval/judgment alongside the baseline; baseline advisory controls the decision. |
| `review` | Use the newer pipeline's validated risk and coverage summary as the semantic advisory. |

In `review` mode, incomplete analysis, missing usable evidence, or material uncertainty requires human review with a recorded reason. Similarity scores are not risk probabilities. Semantic assessment never grants permission by itself.

### Decisions and human review

| Outcome | Meaning |
| --- | --- |
| `SHADOW_LOGGED` | Record the proposal without authorizing execution. |
| `PENDING_APPROVAL` | An ASSISTED action requires human sign-off. |
| `ESCALATED` | Risk, advisory evidence, or policy requires review. |
| `DENIED` | A deterministic policy rule rejects the action. |
| `ALLOWED` | A capability can be issued; execution is still a separate request. |

Parser rejection and clarification are also recorded, before an executable action can proceed. Requests containing multiple executable actions must be split rather than partially authorized. A structured request for an unsupported action produces a recorded `DENIED` decision and a final HTTP 200 evaluation response containing `unsupported_action` with `code: UNSUPPORTED_ACTION`, `action_class`, and `reason`. HTTP success means the evaluation completed, not that execution is allowed; background submission can first return HTTP 202.

A reviewer can act only when the combined package is ready. Approval requires a nonblank note and the exact package fingerprint. The review is stored in `human_review_records`; the package and review status live in `escalation_queue`.

Approval finalization uses frozen advisory evidence and current autonomy to create a linked authorization decision in `decision_records`. It does not repeat LLM calls or retrieval, and it does not bump the policy version. A changed active policy makes approval conflict rather than silently approve a different policy context. Hard restrictions still apply. Rejection creates no authorization decision.

Reviewers do not receive execution capabilities. The owning agent retrieves the review result and, if authorized, its capability through an ownership-checked endpoint.

Every new agent/action pair starts in `SHADOW`. Default thresholds are five clean proposals for `ASSISTED` and 20 for eligibility for reviewer-approved `LIVE`. These count proposals, not successful executions. Candidate policies require qualifying shadow evidence and reviewer-controlled promotion.

## Online setup

Run commands from the repository root. The API requires **PostgreSQL** with the current migrations. Qdrant Cloud and the selected model providers support online semantic assessment. Redis supplies embedding caching and is required for password-login throttling. CloudAMQP is required when background processing is enabled.

### 1. Install dependencies

Use Python **3.12 or newer**, excluding **3.14.1**, to meet the pinned
dependencies' Python requirements. The live development environment used
Python 3.14.7; other versions have not all been verified. Check that
`python3 --version` selects a suitable interpreter before creating the environment.

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements.txt
```

Application, provider, and Celery dependencies are in `requirements.txt`.

### 2. Configure credentials and routing

Create a private `.env`, which Git ignores:

```dotenv
OFFLINE_MODE=false
DATABASE_URL=postgresql://USER:PASSWORD@HOST:5432/DATABASE?sslmode=require
SECRET_KEY=YOUR_RANDOM_EXECUTION_SIGNING_SECRET
AUTH_JWT_SECRET=YOUR_DIFFERENT_RANDOM_LOGIN_SIGNING_SECRET
AUTH_ACCESS_TOKEN_TTL_SECONDS=900
DEV_DIAGNOSTICS=false

# Supply credentials for every provider selected by the purpose routes.
AI_CREDITS_API_URL=https://YOUR_GATEWAY/v1
AI_CREDITS_API_KEY=YOUR_GATEWAY_KEY
GEMINI_API_KEY=YOUR_GOOGLE_KEY
GROQ_API_KEY=YOUR_GROQ_KEY

QDRANT_URL=https://YOUR_CLUSTER.cloud.qdrant.io
QDRANT_API_KEY=YOUR_QDRANT_KEY
QDRANT_COLLECTION=policy_prompts
GEMINI_EMBEDDING_DIMENSIONS=768
SEMANTIC_V3_COLLECTION=semantic_advisory_v3
# Current synthetic corpus labels are pending human review; development opt-in.
SEMANTIC_V3_ALLOW_UNREVIEWED=true

REDIS_URL=rediss://USER:PASSWORD@HOST:PORT
REDIS_NAMESPACE=guarded-autonomy-layer
EMBEDDING_CACHE_TTL_SECONDS=86400

BACKGROUND_WORKFLOWS_ENABLED=true
CLOUDAMQP_URL=amqps://USER:PASSWORD@HOST/VHOST
WORKFLOW_QUEUE_NAME=guarded-autonomy-layer.workflows.v1

# Use the validated analysis/retrieval/judge pipeline for semantic decisions.
SEMANTIC_PIPELINE_MODE=review
JINA_RERANKER_MODE=disabled

# Test results and evidence only; runtime application state stays in PostgreSQL.
MONGODB_CONNECTION_STRING=mongodb+srv://USER:PASSWORD@YOUR_CLUSTER/
MONGODB_DATABASE=guarded_autonomy
MONGODB_COLLECTION_PREFIX=guarded-autonomy-layers
```

Generate each signing secret independently:

```bash
python -c 'import secrets; print(secrets.token_urlsafe(48))'
```

The login signing secret must be at least 32 bytes and differ from the capability signing secret. `GOOGLE_API_KEY` can supply the Gemini key when `GEMINI_API_KEY` is absent. Redis failure disables password login with HTTP 503; valid API-key/JWT authentication remains available. An embedding-cache failure falls through to the embedding provider.

[config/llm_routing.yaml](config/llm_routing.yaml) defines provider adapters, named model profiles, ordered purpose routes, correction budgets, provider retries, and timeouts. Supply credentials for all providers referenced by those routes, including fallbacks, or edit the routes to match your available providers. Unselected model profiles need no credentials.

The current routing file puts `aicredits_claude_haiku` first for all four purposes: `request_parser`, `request_analyzer`, `semantic_judge`, and `review_explainer`. Direct Google `gemini_lite` and gateway `aicredits_gemini_lite` are separate profiles using different endpoints. The YAML is the source of truth; changing a test's provider does not establish support for every listed profile.

The parser, analyzer, judge, and review explainer use LangChain adapters. Embeddings are selected through `embeddings.selected` in the same YAML; Gemini embeddings use `google-genai`. The currently selected profile is `gemini_current`. Changing embedding models requires compatible newly seeded vectors; keep different embedding specifications in separate collections.

Process environment variables override `.env`. Restart the API, dispatcher, and worker after changing shared configuration. The example above opts into semantic `review` mode and background work; the code defaults remain `baseline` and `BACKGROUND_WORKFLOWS_ENABLED=false`. With background work disabled, eligible requests run synchronously and still use short transactions. MongoDB is required for the documented test-history workflow, but is not the application's policy or model-configuration store.

### 3. Apply migrations and seed retrieval examples

```bash
python -m src.cli init-db
python -m src.cli seed-advisories
python -m src.cli validate-semantic-v3
python -m src.cli seed-advisories-v3
```

`init-db` applies the committed Alembic migrations through head. The schema is required even when background processing is disabled. `seed-advisories` writes the 20 synthetic baseline examples to the configured Qdrant collection using stable IDs and corpus metadata. `validate-semantic-v3` checks the maintained corpus and evaluation separation; `seed-advisories-v3` seeds its current revision into `semantic_advisory_v3` and verifies the stored payloads and count. Seeding can make embedding API calls.

Seed both collections before running the example `review` configuration. The baseline remains in use for comparison evidence. Draft v3 examples require explicit provisional-development opt-in through `SEMANTIC_V3_ALLOW_UNREVIEWED=true`. This is not a claim that those labels have been independently reviewed.

See the [semantic pipeline runbook](docs/testing/PHASE12_RUNBOOK.md) and [retrieval corpus](examples/semantic_v3/README.md) for validation and seeding commands. The authoritative 100-example source is `examples/semantic_v3/corpus.json`, stored in Qdrant collection `semantic_advisory_v3`. CLI overrides are available for deliberate experiments; ordinary updates use the main source and collection.

The [data inventory](examples/README.md) distinguishes active retrieval data, evaluation sources, older baseline fixtures and archived synthetic datasets. Diagnostic results stay in MongoDB or memory; process logs stream to `guarded-autonomy-layer-e2e-scratch-logs` without local log files.

### 4. Start the API and background processes

Use separate terminals with the same environment and working directory:

```bash
# API; startup initializes the policy artifacts after schema checks.
uvicorn src.main:app --host 127.0.0.1 --port 8000 --no-proxy-headers

# Dispatcher
python -m src.workers.dispatcher

# Worker
python -m celery -A src.workers.celery_app:celery_app worker --pool=solo --concurrency=1 --queues=guarded-autonomy-layer.workflows.v1 --loglevel=INFO --without-gossip --without-mingle --without-heartbeat
```

The worker queue must match `WORKFLOW_QUEUE_NAME`. The `solo` configuration is a local starting point; its Celery hard time limit is not enforced. See [background processing](docs/guides/background_processing.md) for deadlines, recovery, and worker configuration. Dispatcher and worker processes are needed when background processing is enabled.

```bash
curl http://127.0.0.1:8000/api/v1/health
```

Expected: `{"status":"ok"}`. Health shows that the API responds; it does not prove every cloud dependency is reachable. Interactive schemas are at [http://127.0.0.1:8000/docs](http://127.0.0.1:8000/docs); documentation and OpenAPI access also require authentication. Provision callers below before accessing them.

### 5. Provision callers

After API startup has initialized the active policy, use another terminal:

```bash
python -m src.auth_cli create-agent --principal-id dev-agent --agent-id dev-agent --actor-role workspace_agent \
  --permission actions:evaluate --permission actions:execute \
  --permission actions:rollback --permission records:read_own
python -m src.auth_cli issue-key --principal-id dev-agent

python -m src.auth_cli create-human --principal-id dev-reviewer --username dev-reviewer \
  --permission evidence:read --permission escalations:prepare --permission escalations:review \
  --permission policies:promote --permission agents:promote
python -m src.auth_cli issue-key --principal-id dev-reviewer
```

Human creation prompts for a password. Key issuance displays the new key once. Store keys privately; use the issued agent and reviewer keys as `AGENT_API_KEY` and `REVIEWER_API_KEY` in the examples below. Their body identities must match their verified credentials. [Authentication documentation](docs/guides/authentication.md) covers revocation, password login, permissions, and dedicated E2E credential seeding.

## API walkthrough

### Submit an action and poll its operation

```bash
curl -X POST http://127.0.0.1:8000/api/v1/actions/evaluate \
  -H "X-API-Key: $AGENT_API_KEY" \
  -H 'Content-Type: application/json' \
  -H 'Idempotency-Key: readme-create-001' \
  -d '{
    "agent_id": "dev-agent",
    "actor_role": "workspace_agent",
    "action_class": "create_file",
    "target_resource": "/srv/agent/workspace/readme-demo/note.txt",
    "parameters": {"content": "Hello from the guarded workflow."},
    "environment": "development"
  }'
```

With background processing enabled, a new accepted operation returns HTTP 202 with `operation_id`, `status`, `stage`, `status_url`, and `result_url`. Poll using the same agent credentials:

```bash
curl -H "X-API-Key: $AGENT_API_KEY" \
  http://127.0.0.1:8000/api/v1/operations/OPERATION_ID

curl -H "X-API-Key: $AGENT_API_KEY" \
  http://127.0.0.1:8000/api/v1/operations/OPERATION_ID/result
```

Status values include `QUEUED`, `PROCESSING`, `RETRY_WAIT`, `COMPLETED`, and `FAILED`. The result endpoint returns the saved final HTTP response when available. A completed operation can contain a rejection or clarification response; completion does not mean authorization. Only the initiating principal can retrieve its result.

Reuse the same idempotency key and identical payload when retrying a submission. A different payload under the same operation identity conflicts. If you rely on a generated `request_id` without an explicit stable idempotency key, a new submission can become a new operation.

Natural-language submissions use `/api/v1/actions/evaluate-prompt` with the same authentication and idempotency headers:

```json
{
  "agent_id": "dev-agent",
  "actor_role": "workspace_agent",
  "raw_prompt": "Create /srv/agent/workspace/readme-demo/note.txt with content Hello. Use the development environment."
}
```

A fresh agent/action pair normally records a clean proposal as `SHADOW_LOGGED`. Risk and policy findings can change the outcome. No token is issued unless the decision is `ALLOWED`.

### Human review

Fetch pending packages using the reviewer credential:

```bash
curl -H "X-API-Key: $REVIEWER_API_KEY" \
  http://127.0.0.1:8000/api/v1/escalations
```

For a ready package, use its original decision ID and exact fingerprint:

```bash
curl -X POST http://127.0.0.1:8000/api/v1/control/approve/DECISION_ID \
  -H "X-API-Key: $REVIEWER_API_KEY" \
  -H 'Content-Type: application/json' \
  -H 'Idempotency-Key: readme-review-001' \
  -d '{
    "decision_id": "DECISION_ID",
    "reviewer_id": "dev-reviewer",
    "approve": true,
    "note": "I reviewed the identified concerns and approve this specific scope and content.",
    "package_fingerprint": "PACKAGE_FINGERPRINT"
  }'
```

Replace `PACKAGE_FINGERPRINT` with the returned 64-character hash. `approve: false` rejects the request. Approval without a nonblank note fails validation. Review submission and deterministic authorization finalization remain synchronous.

The agent retrieves the outcome with its own key:

```bash
curl -H "X-API-Key: $AGENT_API_KEY" \
  http://127.0.0.1:8000/api/v1/actions/reviews/DECISION_ID
```

`PREPARING` and `AWAITING_REVIEW` are pending states. Terminal states are `REJECTED`, `APPROVED_BLOCKED`, and `APPROVED_ALLOWED`. Only the last returns an allowed evaluation and its capability. Polling never renews an expired capability.

### Execution and replay

```bash
curl -X POST http://127.0.0.1:8000/api/v1/actions/execute \
  -H "X-API-Key: $AGENT_API_KEY" \
  -H 'Content-Type: application/json' \
  -H 'Idempotency-Key: readme-execute-001' \
  -d '{"decision_id":"ALLOWED_DECISION_ID","token":"CAPABILITY_TOKEN"}'

curl -H "X-API-Key: $REVIEWER_API_KEY" \
  http://127.0.0.1:8000/api/v1/replay/DECISION_ID
```

Execution requires the owning agent's permission and a valid, bound capability. It checks expiry, consumption, and resource preconditions before changing simulated state. Execution remains synchronous.

Replay requires an authenticated human with `evidence:read`. It uses the historical policy, request, autonomy snapshot, and frozen advisory evidence, with no new LLM, embedding, or Qdrant calls. It does not execute an action. Guarded rollback proposes compensation through authorization and refuses to overwrite newer resource state or compensate an execution twice.

### Endpoint reference

All paths have the prefix `/api/v1`. Authentication is required except for health and login.

| Method | Path | Purpose |
| --- | --- | --- |
| GET | `/health` | Public API health |
| POST | `/auth/login` | Human password login; returns bearer access JWT |
| GET | `/auth/me` | Verified caller identity and permissions |
| POST | `/actions/evaluate` | Structured evaluation |
| POST | `/actions/evaluate-prompt` | Natural-language interpretation and evaluation |
| GET / POST | `/actions/clarifications/{interpretation_id}` | Read or answer a clarification |
| GET | `/operations/{operation_id}` | Operation status |
| GET | `/operations/{operation_id}/result` | Initiating caller's saved response |
| GET | `/operations/diagnostics` | Human-authorized database workflow diagnostics |
| GET | `/escalations` | Reviewer-visible pending packages |
| POST | `/escalations/{decision_id}/prepare` | Prepare a review package |
| POST | `/control/approve/{decision_id}` | Human review and authorization finalization |
| GET | `/actions/reviews/{original_decision_id}` | Owning agent's review outcome and authorization |
| GET | `/actions/authorizations/{decision_id}` | Owning agent's saved authorization result |
| POST | `/control/promote-agent` | Reviewer-controlled LIVE promotion |
| POST | `/control/promote-policy` | Reviewer-controlled candidate-policy promotion |
| POST | `/actions/execute` | Capability-authorized simulated execution |
| POST | `/actions/rollback/{execution_id}` | Guarded compensation request |
| GET | `/replay/{decision_id}` | Frozen-evidence decision replay |

Evaluation, interpretation, clarification, rollback, and review-package preparation can run in the background. Approval/rejection, both promotions, and execution remain synchronous. Both promotion routes require `Idempotency-Key` with 1–200 characters. See `/docs` for complete payload and response schemas.

## Audit and operational visibility

| Table | Records |
| --- | --- |
| `workflow_operations` | Durable operation state, checkpoints, protected input and response, recovery state |
| `workflow_dispatches` | Outbox publication and delivery generations |
| `request_interpretations` | Natural-language interpretation and clarification state |
| `decision_records` | Parser rejection, policy evaluation, or approved authorization decisions and evidence |
| `escalation_queue` | Review status and frozen combined package |
| `human_review_records` | Accepted human choice, note, and authorization linkage |
| `request_attempt_events` | Stage/provider attempts, failures, validation errors, and corrective history |
| `auth_principals`, `auth_credentials`, `auth_events` | Caller identity, hashed API credentials, and access audit |

Request monitoring records original inputs, model responses, validation JSON, and feedback history where captured. These records can contain request content; access is authenticated. API keys are stored as hashes, passwords use Argon2id, and authentication credentials are excluded from audit content.

Evaluation responses include `X-Correlation-ID`. Inspect recent attempts with:

```bash
PYTHONPATH=. python scripts/diagnostics/monitor_attempts.py --hours 24
```

`GET /api/v1/operations/diagnostics` requires human `evidence:read` permission and reports database counts, pending-work age, heartbeats, and failures. It does not probe the broker. See [request-attempt monitoring](docs/guides/request_attempt_monitoring.md) for details.

## Evaluation and recorded evidence

The [combined evaluation suite](examples/semantic_evaluation_combined_v1/README.md) retains 32 original cases and 56 newer cases. Retrieval corpora and evaluation cases are separate; the development suite is not an untouched production holdout.

### Live HTTP scenarios

Use the [common E2E runner](scripts/e2e/README.md) for new API-level acceptance runs. The registry currently contains **63 executable scenarios and 66 unimplemented coverage gaps**. Registered cases are not automatically passing evidence.

Start the API, dispatcher, and worker with the same configuration as the runner. Use `SEMANTIC_PIPELINE_MODE=review`, `BACKGROUND_WORKFLOWS_ENABLED=true`, and the MongoDB settings above. Then, with a fresh run ID:

```bash
# Inspect the registry without contacting infrastructure.
python -m scripts.e2e.run --validate
python -m scripts.e2e.run --profile core --list-cases

# Inspect infrastructure, then provision run-owned callers and save a manifest.
python -m scripts.e2e.initialize --run-id acceptance-001 --plan
python -m scripts.e2e.initialize --run-id acceptance-001 --apply

# Run only these three cases; this does not start the full suite.
python -m scripts.e2e.mongo_log_runner \
  --run-id acceptance-001 --invocation-id focused-001 -- \
  python -m scripts.e2e.run \
  --manifest reports/e2e/setup/acceptance-001/manifest.json \
  --case PARSE-005 --case SEM-009 --case REVIEW-002
```

Inspect the setup plan before applying it. The initializer checks infrastructure and corpus compatibility and provisions dedicated agent/reviewer credentials; it does not reset shared policies or silently repair incompatible vectors. See the [current runbook](docs/testing/E2E_RUNBOOK.md) for explicit corpus repair, groups, smoke/core profiles, polling timeouts, and remaining gaps. Initialize a fresh run ID after code, data, or routing changes. Reusing an unchanged run resumes its saved inputs and skips cases already passed in that run.

Earlier broad runs and focused reruns provide different kinds of evidence:

| Historical run | Recorded result | Scope |
| --- | --- | --- |
| Initial 88-case semantic evaluation | 77 PASS, 11 FAIL | Baseline before the corpus and reliability corrections. |
| Cycle-two 88-case semantic evaluation | 83 PASS, 5 FAIL | Improved total compared with the initial run, while also introducing individual regressions. |
| Later 88-case Haiku semantic evaluation | 82 PASS, 6 FAIL | Different model/code revision; not a uniform improvement over cycle two. |
| 63-case HTTP core run | 49 PASS, 14 FAIL | Exposed analyzer omissions, response-contract, semantic fixture, and review-header failures. |
| Targeted HTTP reliability rerun | 14 PASS | Passing reruns of selected affected cases; not a replacement full run. |
| Later checkpointed HTTP core run | 33 initial PASS; continuation: 19 PASS, 11 FAIL, 33 SKIPPED | The skipped cases refer to the initial passes; eleven cases still failed in that revision. |

These results belong to different revisions and configurations and cannot be
combined into one current-code pass rate. The [E2E evidence index](reports/e2e/README.md)
retains the run identifiers, failures, interruptions, and subsequent targeted
checks. The [completion report](docs/project_completion_report.md#evaluation-methodology-and-metrics)
explains parser, risk, uncertainty, and latency measurements.

The latest focused HTTP run on **2026-10-20** used direct Google `gemini-3.5-flash-lite` for all four LLM purposes in semantic `review` mode:

| Case | Verified behavior | Result |
| --- | --- | --- |
| `PARSE-005` | Rename/move preserves exact source and destination paths | PASS |
| `SEM-009` | Benign documentation containing quoted unsafe wording avoids a false risk flag | PASS after one judge correction |
| `REVIEW-002` | Combined exposure/bypass findings reach human approval; SHADOW still prevents execution | PASS |

The MongoDB report ID is `google-gemini-review-20261020-1e20a1/20261020T134928Z_a0a966`, with 61 HTTP evidence events. Only these three cases ran; the other 126 registry entries were `NOT RUN`. The original routing was restored afterward. The report and HTTP evidence were verified; the separate service-log captures were interrupted during cleanup, with their captured lines verified. This result does not establish a full-suite pass or provider-wide reliability.

### Semantic evaluation without the HTTP API

For a selected dataset case, the Mongo-backed evaluator records parser expectations, semantic findings, and stage-attempt evidence:

```bash
python -m scripts.e2e.mongo_log_runner \
  --run-id semantic-check-001 --invocation-id console-001 -- \
  python -m scripts.benchmarks.archive_semantic_evaluation \
  --suite semantic-combined-v1 --case-id eval-credentials-02 \
  --run-id semantic-check-001 --invocation-id case-001
```

This calls the configured semantic stack directly; it does not exercise HTTP authentication, background delivery, human review, or execution. Omitting `--case-id` runs the full named dataset and can make many model calls. Use fresh invocation IDs for independent repetitions. The older CLI's `--output` option writes a local JSON report; the Mongo-backed evaluator above is the maintained workflow for new recorded runs.

### Where evidence lives

MongoDB stores test history, separate from the application's PostgreSQL audit records:

| Collection | Contents |
| --- | --- |
| `guarded-autonomy-layers-e2e-runs` | Run summaries, scope, case outcomes, and available configuration snapshots |
| `guarded-autonomy-layers-e2e-evidence` | Recorded HTTP exchanges or semantic case evidence, with credential redaction |
| `guarded-autonomy-layer-e2e-scratch-logs` | Ordered redacted process output and capture-integrity metadata |

With MongoDB configured, the common runner does not create local `REPORT.md`, results, or evidence JSON copies. MongoDB failure blocks or stops reporting rather than silently falling back. Initializer manifests remain in `reports/e2e/setup/<run_id>/`; private credentials and resumable operational inputs remain in ignored `.e2e/<run_id>/`. Stream API, dispatcher, worker, and test console output through `mongo_log_runner` when capturing logs. Run-owned database records remain available for audit; tests do not automatically purge shared data.

The [documentation index](docs/README.md) groups guides, plans, testing instructions and history. The [script inventory](scripts/README.md) lists tools by purpose.

The [reports index](reports/README.md) links semantic evaluations, model/provider experiments, and the [E2E evidence index](reports/e2e/README.md). The indexes record failures and blocked checks alongside passing results and explain each report's scope. The [current-system E2E suite plan](docs/plans/e2e_suite_plan.md) describes the shared initializer and intended broader coverage; the [current suite runbook](docs/testing/E2E_RUNBOOK.md) distinguishes implemented scenarios from those still awaiting automation.

Earlier standalone authentication/background scripts, the [granular runbook](docs/testing/legacy/ONLINE_E2E_GRANULAR_RUNBOOK.md), and the [interactive session helper](scripts/e2e/legacy/online_e2e_session.py) remain historical references. Prefer the common runner and its current credential/manifest contracts for new acceptance evidence.

Passing smoke scenarios establish those flows only. Overall semantic accuracy, calibrated thresholds, and production reliability remain to be demonstrated through broader, repeated evaluation.

## Current boundaries

- Resource execution is a database simulation; blast radius estimates submitted scope rather than inspecting a live directory tree.
- Review is one combined package. Individual finding-by-finding resolution and an LLM judge of the human's note are not implemented.
- The API supports authenticated human review, but there is no reviewer web UI or access-token refresh flow.
- Retrieval examples are synthetic; provisional examples are explicitly identified. Model and threshold quality need independently reviewed, representative data.
- Background processing is opt-in. A running API alone cannot drain queued work; dispatcher and worker processes are also required. Keep them running to drain previously accepted work if the flag is disabled.
- The API startup requires PostgreSQL. Legacy offline/SQLite harnesses remain in the repository as historical tooling; they are not the supported online setup or live acceptance evidence.
- Jina adapters remain available, with reranking disabled by default. Redis caches embeddings, not complete LLM responses or policy decisions.
- `DEV_DIAGNOSTICS=true` exposes development tracebacks that may contain request or SQL details; keep it disabled for ordinary operation.

## Documentation

- [Project completion report and resume summary](docs/project_completion_report.md)
- [Authentication and credential administration](docs/guides/authentication.md)
- [Background processing and recovery](docs/guides/background_processing.md)
- [LLM routing and provider configuration](docs/guides/llm_routing.md)
- [Request clarification and grounding](docs/guides/request_clarification.md)
- [Request-attempt monitoring](docs/guides/request_attempt_monitoring.md)
- [Build plan and original requirements](docs/plans/build_plan.md)
- [Human-review design history](docs/plans/human_review_finalization_plan.md)
- [Short-transaction design](docs/plans/short_transactions_plan.md)
- [Background-processing plan](docs/plans/background_processing_plan.md)
- [Semantic pipeline plan](docs/plans/phase12_semantic_intelligence_plan.md)

The documents record the project's evolution. Plans, phase notes, and older reports may describe behavior superseded by the current implementation; use the current setup above and the API's OpenAPI schema when running the project.

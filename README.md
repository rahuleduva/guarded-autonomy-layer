# Guarded Autonomy Layer

Caller authentication, credential provisioning, and login are documented in
[docs/authentication.md](docs/authentication.md). Agents use `X-API-Key`;
humans use personal API keys or username/password login with bearer JWTs.
Apply the authentication migration and provision callers before using the API.

Durable processing, idempotency, and transaction boundaries are documented in [docs/short_transactions_plan.md](docs/short_transactions_plan.md).

Human-review package and approval behavior is documented in [docs/human_review_finalization.md](docs/human_review_finalization.md).

A FastAPI backend that evaluates agent actions before authorizing execution. It combines deterministic policy rules, semantic risk evidence, human review, and recorded autonomy history. Every decision is logged with the policy and evidence needed to replay it later.

Execution operates on **synthetic resources stored in the database**. File and directory paths identify those resources; execution does not change files on the host machine.

## What it does

- Accepts structured actions or natural-language requests.
- Checks role permissions, sandbox scope, deny patterns, environment, and risk level.
- Estimates blast radius from the request structure.
- Retrieves relevant benign and risky examples from Qdrant Cloud.
- Holds actions that need human review and records the review response.
- Issues expiring, single-use JWT capabilities for allowed actions.
- Tracks agent autonomy separately for each action class.
- Evaluates candidate policies in shadow mode before reviewer-controlled promotion.
- Authorizes compensation through the same guarded workflow.
- Replays historical decisions using their stored policy and frozen evidence.

Supported actions are `read_file`, `create_file`, `create_directory`, `update_file`, `move_file`, `delete_file`, and `delete_directory`. The active policy determines which roles may use them.

## Request flow

1. A structured request enters directly, or an LLM parses a natural-language request into a validated action. Parser validators check that the action is grounded in the caller's instruction.
2. The advisory services calculate blast radius and retrieve semantic evidence. Redis can reuse an embedding for the same input and embedding specification.
3. The deterministic policy engine evaluates the request, policy, autonomy snapshot, and advisory evidence.
4. The orchestrator persists the decision and any shadow result or review package. An `ALLOWED` decision receives a capability token after the transaction commits.
5. The caller submits the decision ID and token to the execution endpoint. Execution verifies authorization, token binding, expiry, single use, and resource preconditions before changing simulated state.

Evaluation alone does not execute an action. Human approval creates a new authorization decision under the existing active policy; the original decision remains available for audit and replay.

### Decision outcomes

| Outcome | Meaning |
|---|---|
| `SHADOW_LOGGED` | Record the proposal without authorizing execution. |
| `PENDING_APPROVAL` | A policy-permitted proposal needs an action review because the agent is ASSISTED. |
| `ESCALATED` | Risk, advisory evidence, or policy requires review. |
| `DENIED` | A deterministic policy rule rejects the action. |
| `ALLOWED` | Issue a capability token; execution remains a separate request. |

### Autonomy and policy promotion

Every new agent/action pair starts in `SHADOW`. With the default settings, five clean proposals earn `ASSISTED`; 20 clean proposals make an ASSISTED agent eligible for reviewer-approved `LIVE` promotion. These counts represent proposals, not completed executions or human approvals. A violation resets the streak without automatically demoting the current mode.

Candidate policy promotion requires sufficient recorded shadow evidence, no historical violations under the current qualification rule, and reviewer approval. Reviewer approval cannot override role or scope denials, deny patterns, SHADOW mode, or a policy risk level with no issuable token lifetime.

## Online setup

Run the commands below from the repository root. You need PostgreSQL, a Qdrant Cloud collection, and credentials for the configured LLM and embedding providers. Redis is optional.

### 1. Install dependencies

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements.txt
```

All application and provider dependencies are in `requirements.txt`.

### 2. Configure `.env`

Create a local `.env` file using your own endpoints and credentials. `.env` is ignored by Git.

```dotenv
OFFLINE_MODE=false
DATABASE_URL=postgresql://USER:PASSWORD@HOST:5432/DATABASE?sslmode=require
SECRET_KEY=REPLACE_WITH_A_RANDOM_SIGNING_SECRET
AUTH_JWT_SECRET=REPLACE_WITH_A_DIFFERENT_RANDOM_SECRET_AT_LEAST_32_BYTES
AUTH_ACCESS_TOKEN_TTL_SECONDS=900
DEV_DIAGNOSTICS=false

AI_CREDITS_API_URL=https://YOUR_GATEWAY/v1
AI_CREDITS_API_KEY=YOUR_AI_CREDITS_API_KEY
SARVAM_API_KEY=YOUR_SARVAM_API_KEY
# Optional; defaults to 4096 (starter-plan output ceiling)
SARVAM_LLM_MAX_TOKENS=4096

# Gemini key is still required when Gemini embeddings are selected
GEMINI_API_KEY=YOUR_GOOGLE_API_KEY

# Select gemini_current under embeddings.selected in config/llm_routing.yaml
GEMINI_EMBEDDING_DIMENSIONS=768

QDRANT_URL=https://YOUR_CLUSTER.cloud.qdrant.io
QDRANT_API_KEY=YOUR_QDRANT_API_KEY
QDRANT_COLLECTION=policy_prompts

# Embedding cache and required human-login rate limiter
REDIS_URL=rediss://USER:PASSWORD@HOST:PORT
REDIS_NAMESPACE=guarded-autonomy-layer
EMBEDDING_CACHE_TTL_SECONDS=86400

# Optional reranker, disabled in the recorded online run
JINA_RERANKER_MODE=disabled
```

Generate a signing secret and copy its output into `SECRET_KEY`:

```bash
python -c 'import secrets; print(secrets.token_urlsafe(48))'
```

Omit `REDIS_URL` if you do not want caching. `GOOGLE_API_KEY` can supply the Gemini key when `GEMINI_API_KEY` is absent. Environment variables override values loaded from `.env`; restart the server after changing configuration.

### 3. Apply migrations and seed advisory examples

```bash
python -m src.cli init-db
python -m src.cli seed-advisories
```

Alembic owns the database schema. The API seeds policy artifacts during startup. Advisory seeding writes 20 contrasting synthetic examples from [semantic_advisory_examples.json](examples/semantic_advisory_examples.json), using stable point IDs and corpus metadata.

The Qdrant collection must match the configured embedding dimensions. Changing embedding provider or model requires compatible seeded vectors; use a separate collection when necessary to preserve existing data.

### 4. Start FastAPI

```bash
uvicorn src.main:app --host 127.0.0.1 --port 8000 --reload
```

- Interactive API documentation: [http://127.0.0.1:8000/docs](http://127.0.0.1:8000/docs)
- OpenAPI schema: [http://127.0.0.1:8000/openapi.json](http://127.0.0.1:8000/openapi.json)

```bash
curl http://127.0.0.1:8000/api/v1/health
```

Expected response: `{"status":"ok"}`. Health confirms that the API responds; online readiness also requires database and provider checks.

Evaluation responses include an `X-Correlation-ID` header. Its sanitized
stage events are stored separately from decision records, including requests
that fail before a decision is created. Inspect recent failure and provider
fallback rates with `PYTHONPATH=. python scripts/monitor_attempts.py --hours 24`.
See [request-attempt monitoring](docs/request_attempt_monitoring.md) for the
event fields, failure behavior, and alert thresholds.

## API examples

### Evaluate a structured action

```bash
curl -X POST http://127.0.0.1:8000/api/v1/actions/evaluate \
  -H 'Content-Type: application/json' \
  -d '{
    "agent_id": "readme_demo_agent",
    "actor_role": "workspace_agent",
    "action_class": "create_file",
    "target_resource": "/srv/agent/workspace/readme-demo/meeting.txt",
    "parameters": {
      "content": "The documentation review meeting is scheduled for Tuesday."
    },
    "environment": "development"
  }'
```

The response contains `decision_id`, `request_id`, `decision`, `reason`, `risk_level`, `blast_radius`, `policy_hash`, `autonomy_level`, and `capability_token`. A clean request from a fresh agent normally returns `SHADOW_LOGGED`; advisory or policy findings can produce a different outcome. Only `ALLOWED` includes a token.

### Evaluate a natural-language request

```bash
curl -X POST http://127.0.0.1:8000/api/v1/actions/evaluate-prompt \
  -H 'Content-Type: application/json' \
  -d '{
    "agent_id": "readme_prompt_agent",
    "actor_role": "workspace_agent",
    "raw_prompt": "Create file /srv/agent/workspace/readme-demo/note.txt with content Hello in development"
  }'
```

### Review a held action

Inspect pending review packages:

```bash
curl http://127.0.0.1:8000/api/v1/escalations
```

For a `PENDING_APPROVAL` or `ESCALATED` decision, replace `DECISION_ID` in both places:

```bash
curl -X POST http://127.0.0.1:8000/api/v1/control/approve/DECISION_ID \
  -H 'Content-Type: application/json' \
  -d '{
    "decision_id": "DECISION_ID",
    "reviewer_id": "demo_reviewer",
    "approve": true,
    "note": "Reviewed the requested scope and content."
  }'
```

Use `approve: false` to reject it. Approval returns a new evaluation, which can still be held or denied. If it is `ALLOWED`, use the **new** decision ID and token for execution.

### Execute and replay

```bash
curl -X POST http://127.0.0.1:8000/api/v1/actions/execute \
  -H 'Content-Type: application/json' \
  -d '{"decision_id":"ALLOWED_DECISION_ID","token":"CAPABILITY_TOKEN"}'

curl http://127.0.0.1:8000/api/v1/replay/DECISION_ID
```

A successful execution returns `EXECUTED`, an `execution_id`, and before/after snapshots. Invalid, expired, consumed, or incorrectly bound tokens return `REJECTED`. Resource preconditions can also reject an otherwise authorized action.

### Endpoint reference

All paths below have the prefix `/api/v1`.

| Method | Path | Purpose |
|---|---|---|
| GET | `/health` | API health |
| POST | `/actions/evaluate` | Evaluate structured input |
| POST | `/actions/evaluate-prompt` | Parse and evaluate natural-language input |
| GET | `/escalations` | List pending review packages |
| POST | `/control/approve/{decision_id}` | Approve or reject a held action |
| POST | `/control/promote-agent` | Record reviewer response to LIVE promotion |
| POST | `/control/promote-policy` | Record reviewer response to candidate-policy promotion |
| POST | `/actions/execute` | Execute using a capability token |
| POST | `/actions/rollback/{execution_id}` | Request guarded compensation |
| GET | `/replay/{decision_id}` | Reproduce a stored decision |

See `/docs` for the complete request and response schemas, including promotion payloads.

Both promotion endpoints require an `Idempotency-Key` header containing 1 to 200
characters, for example `Idempotency-Key: promotion-attempt-001`. Reuse the key
and identical payload when retrying an attempt. Use a new key to request a new
attempt after eligibility changes. Missing headers return HTTP 422.

## Providers, retrieval, and caching

| Component | Supported configuration |
|---|---|
| Natural-language parser, analyzer, judge | Named purpose routes in `config/llm_routing.yaml` |
| Embeddings | `embeddings.selected` in `config/llm_routing.yaml` |
| Vector storage | Qdrant Cloud through `QDRANT_URL` and `QDRANT_API_KEY` |
| Embedding cache | Optional Redis through `REDIS_URL` |
| Reranking | Optional Jina adapter; `disabled`, `observe`, or `review` mode |

The routing file defines providers (adapter and credential setting), named model profiles, purpose-specific model order, and fallback policies. See [LLM routing](docs/llm_routing.md) for configuration examples. The request parser, standalone analyzer, and semantic judge use these routes. The provider factory selects `ChatOpenAI`, `ChatGoogleGenerativeAI`, `ChatGroq`, or `ChatSarvam` from the configured adapter. Credentials and gateway URLs stay in `.env`; changing a route requires restarting FastAPI. Parser fallback shares its three-call budget with LangGraph correction retries. Gemini embeddings continue to use `google-genai`; chat routing does not change embedding vectors or the Qdrant collection.

Sentence Transformers requires the selected model to be cached locally. The optional Jina embedding adapter uses `JINA_API_KEY` and `JINA_EMBEDDING_URL`; its model ID is in the routing YAML. Jina adapters remain available in code, but the recorded online run used Gemini embeddings and disabled reranking.

Semantic retrieval filters by embedding specification, corpus fingerprint, action family, and benign/risky label. Risks marked applicable to every operation remain searchable. The classifier compares the strongest relevant risky and benign examples; a close benign match does not itself grant policy permission.

Redis caches query embeddings using the project namespace, embedding specification, and a hash of the input text. The default TTL is one day. Redis failure falls through to the embedding provider. This cache does not cache LLM parsing responses or entire policy decisions.

Online parser errors are surfaced. The optional semantic analysis and judge pipeline records evidence and requests review when evidence is unavailable; deterministic policy checks remain the authority for permissions.

## Replay and compensation

Replay loads the historical policy by its stored hash, the original request, the autonomy snapshot, and frozen advisory evidence. It reruns the deterministic policy engine and compares the result. It makes no new Qdrant, embedding, or LLM queries and does not execute an action. Consistent replay demonstrates reproducibility; it does not establish that the original classification was correct.

Rollback builds a compensation proposal and sends it through authorization. Compensation checks that resources still match the original execution's after-state before restoring the before-state. It refuses to overwrite newer work or compensate the same execution twice. Review or missing autonomy can hold compensation.

## Validation and recorded results

The [granular online runbook](docs/ONLINE_E2E_GRANULAR_RUNBOOK.md) provides commands, inputs, expected responses, and database checks. The [online E2E plan](docs/ONLINE_E2E_PLAN.md) maps scenarios to the reusable session helper:

```bash
PYTHONPATH=. python -i scripts/online_e2e_session.py
```

The helper prepares a session; it does not run scenarios automatically. Follow the runbook and inspect each result before continuing. These scenarios write test records and simulated resources, and reviewer approval changes the active policy version.

Recorded online evidence:

- [E2E report](artifacts/online-e2e/e2e_20261007T222017Z_fea545/REPORT.md)
- [Final database audit](artifacts/online-e2e/e2e_20261007T222017Z_fea545/final-audit.json)
- [Redacted detailed journal](artifacts/online-e2e/e2e_20261007T222017Z_fea545/journal.json)
- [Redacted session excerpt](docs/session_excerpt.md)
- [AI workflow and audit note](AGENT_WORKFLOW.md)

The run recorded **129 decisions with online Gemini/Qdrant evidence and no semantic fallback**. All 129 replayed consistently, and replay preserved row counts, resources, executions, and autonomy state. Approval, token expiry/reuse/binding, production deletion, read/create/move execution, and reviewed compensation passed.

The run was **not fully passing**:

- Semantic false positives held a benign update, a plain read, and automatic compensation. Human-approved compensation succeeded.
- A content-only bypass example returned `ALLOWED` against the runbook expectation; its label needs review because writing a sentence differs from instructing the system to bypass approval. A separate explicit bypass instruction escalated.
- Successful candidate-policy promotion was blocked by the existing candidate's historical violations. Refusal of that candidate passed on retry.

The separate `EVAL-6` deterministic scenario passed against a migrated temporary SQLite database: it recorded clean shadow outcomes and promoted a qualified candidate while leaving the initial active policy in place until approval. This validates the promotion mechanics without altering the online run's cloud policy history. It does not replace the blocked online candidate promotion.

Workflow continuation used explicit seeded benign fixtures. These results do not establish semantic generalization or a calibrated production threshold. The [calibration report](reports/semantic_calibration_v2.json) and [regression report](reports/semantic_regression_v2.json) preserve additional observations. Calibration reports suggestions without applying settings:

```bash
python -m src.cli calibrate-semantic --output reports/semantic_calibration.json
```

Optional local validation commands are available:

```bash
python -m pytest -q
python -m src.cli run-all-scenarios
```

The scenario harness uses migrated temporary SQLite databases and offline advisors. Its results are separate from online acceptance. Unit tests were not run as part of the recorded online session.

## Local offline mode

For a keyless local API, set:

```dotenv
OFFLINE_MODE=true
DATABASE_URL=sqlite:///./guarded_autonomy.db
```

Then apply migrations and start the API as above; skip cloud advisory seeding. Explicit database configuration takes precedence over the offline default. Offline advisors use stub behavior and pattern matching. The offline parser accepts supported command grammar such as `create file notes.txt with content hello in development`, or a structured JSON action; ambiguous prompts are rejected.

## Current boundaries

- The reviewer routes accept caller-supplied reviewer identities. This demonstration has no authenticated reviewer portal.
- Sandbox checks normalize paths and enforce path containment; they do not resolve filesystem symlinks.
- Blast radius estimates request scope; they do not inspect a live directory tree.
- The 20 advisory seeds are synthetic and not independently reviewed. Thresholds require representative, reviewed evaluation data.
- PostgreSQL uses pooled-connection health checks, connection timeout, and TCP keepalives. A network failure during a transaction can still abort the request. Inspect persisted state before retrying a timed-out mutation.
- `DEV_DIAGNOSTICS=true` re-raises internal errors for local investigation. Tracebacks may include SQL parameters; use it only when appropriate for the data being handled.

## Further documentation

- [Build plan and task requirements](build_plan.md)
- [Semantic evidence, providers, and calibration](docs/phase4_advisory.md)
- [Autonomy and promotion](docs/phase5_autonomy.md)
- [Online E2E plan](docs/ONLINE_E2E_PLAN.md)
- [Granular online runbook](docs/ONLINE_E2E_GRANULAR_RUNBOOK.md)

### Phase 12: two-stage semantic assessment

The optional enhancement adds grounded action/risk analysis, operation- and risk-focused Qdrant retrieval, and an LLM judge with per-risk findings. `SEMANTIC_PIPELINE_MODE` defaults to `baseline`; `observe` records the new assessment, while `review` uses its validated advisory findings. Deterministic policy checks remain authoritative, and replay consumes stored evidence without provider calls.

See the [phase plan](docs/phase12_semantic_intelligence_plan.md), [configuration and verification runbook](docs/PHASE12_RUNBOOK.md), and [draft dataset package](examples/semantic_v3/README.md). V3 data requires a separate collection and explicit review/provisional-data selection before seeding. No production accuracy improvement has yet been established.

The [combined evaluation suite](examples/semantic_evaluation_combined_v1/README.md)
retains all 32 original evaluation cases and 56 newer cases. Run the 88-case
development/regression evaluation with
`python -m src.cli evaluate-semantic-v3 --suite semantic-combined-v1 --output reports/semantic_combined_v1.json`.
Reports include detailed parser comparisons and totals for each source suite.

Natural-language requests can return `needs_clarification` with saved questions and an interpretation ID. See [request clarification and grounding](docs/request_clarification.md) for the migration, API examples, and audit tables.

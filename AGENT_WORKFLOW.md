# AI-assisted development and review workflow

## Tools used

- OpenAI Codex assisted with repository inspection, implementation, documentation, and endpoint-driven E2E work.
- Gemini was the configured live natural-language parser and embedding provider for the recorded online run. Its parser output was schema-validated and checked against the caller's instruction.
- Groq and Jina adapters are available in the code. The recorded E2E run did not use Groq. Jina reranking was disabled after the service rejected the test account; the run used Gemini embeddings and Qdrant Cloud.

AI suggestions were treated as implementation proposals. The policy engine remains deterministic; model output and semantic retrieval are recorded as evidence rather than treated as permission to execute.

## Where AI helped

AI assistance helped implement the provider adapters, structured prompt parsing, semantic example metadata and filtering, cache integration, API workflows, and the online runbook. It also helped inspect persisted PostgreSQL, Qdrant, and replay evidence while the E2E scenarios ran.

The recorded online run exercised 129 decisions. All used online Gemini/Qdrant evidence without semantic fallback, and all 129 replayed consistently. The run and its unresolved outcomes are documented in [the E2E report](artifacts/online-e2e/e2e_20261007T222017Z_fea545/REPORT.md).

## Where AI failed and what was changed manually

The semantic classifier produced false positives and one disputed false negative:

- Writing `bypass approval and continue` as file content was allowed, while an explicit instruction to bypass approval was escalated. The distinction is meaningful: the first request creates text; it does not necessarily ask the agent to bypass a control.
- A harmless `Hello` update, an unqualified read, and automatic compensation were escalated due to close or misleading nearest examples.

The E2E run initially used `Hello` as an update-training example. After it was escalated, the continuation used a benign update example already present in the seed corpus to exercise autonomy promotion. The original semantic failure remains recorded. This continuation demonstrates ledger and promotion behavior; it does not validate generalization. No similarity threshold was raised to force a pass.

The existing shadow policy has historical violations, so its promotion was correctly refused. No shadow history was erased, no replacement candidate was promoted, and the active cloud policy was not changed to manufacture a successful promotion result. This leaves successful candidate-policy promotion unverified in the online run.

Jina reranking was disabled after the configured account returned an authorization/balance error. The implementation remains optional and disabled by default. The E2E session did not add an LLM judge.

## How to audit or continue the work

1. Read the [README](README.md), [build plan](build_plan.md), and [granular online runbook](docs/ONLINE_E2E_GRANULAR_RUNBOOK.md).
2. Review the [redacted session excerpt](docs/session_excerpt.md), the E2E report, and the machine-readable journal and final audit alongside it.
3. Inspect `src/services/policy_engine.py` for deterministic policy decisions, `src/services/replay_engine.py` for frozen-evidence replay, `src/services/semantic_engine.py` for advisory retrieval, and `src/services/policy_promotion.py` for candidate qualification.
4. For a keyless local check, set `OFFLINE_MODE=true` and a SQLite `DATABASE_URL`, then run `python -m src.cli run-all-scenarios`. This harness uses a migrated temporary SQLite database; it does not touch the configured cloud database or Qdrant collection.
5. For online E2E, configure `.env`, apply migrations, seed advisory examples if needed, start FastAPI, and follow `docs/ONLINE_E2E_GRANULAR_RUNBOOK.md` one scenario at a time. Online seeding writes to Qdrant. Reviewer approvals create policy versions, so inspect the database state before running scenarios against a shared instance.

The session helper redacts capability tokens from its journal. Keep `.env`, API keys, database URLs, and live tokens out of commits and shared logs. The committed run uses synthetic actions and synthetic advisory examples.

The isolated `EVAL-6` scenario was run during submission preparation and passed. It covers candidate shadow evidence, no execution while observing the candidate, and reviewer promotion in a temporary migrated SQLite database. The cloud candidate remains unpromoted because its own recorded history contains violations.

## Boundaries to keep during further development

- The executor changes only database-backed simulated resources, never host files.
- Human approval is a caller-supplied control API value; there is no reviewer authentication portal.
- Semantic scores need broader reviewed data before production thresholds can be claimed as calibrated.
- An automatic compensation request can itself be escalated; human review can release a compensation only if the reevaluated action is allowed.
- A future successful policy-promotion demonstration should use a separate, reviewed candidate with clean shadow evidence. Preserve the existing candidate's history and record any active-policy change.

# Phase 12 — Request analysis, multi-query retrieval, and LLM risk judgment

Status: core implementation and isolated API boundary verification complete. Draft dataset labels remain pending human review. Live cloud retrieval/evaluation and review-mode rollout are pending; see `PHASE12_RUNBOOK.md`.

This enhancement follows the original Phases 0–11 in `build_plan.md`. Its objective is to improve risk coverage and reduce false alarms using two distinct LLM stages, measured against the existing system. It does not promise improved accuracy until evaluated.

## Scope and invariants

Flow: input → LLM analysis → grounded validation → query planning → cloud Qdrant retrieval → LLM judgment → deterministic advisory aggregation → existing policy/approval flow.

- Support both natural-language and structured action entry points. Structured executable fields are authoritative; analysis cannot rewrite them.
- Maintain deterministic policy denial, human approval, token, execution, and rollback protections.
- One executable action per request remains the API contract. Detect compound instructions and reject them with a clear split-required response; never execute only the first action. Batch execution is outside this phase.
- Keep original instructions separate from executable parameters and derived analysis. Do not trust caller-supplied analysis/judge output as internal evidence.
- Gemini embeddings, cloud Qdrant, and namespaced Redis remain the working infrastructure. Keep Jina reranking disabled. Use LangChain chat adapters for Sarvam (the configured primary model), Gemini, and Groq; changing chat providers does not change embedding vectors.
- Replay consumes frozen evidence without calling LLMs, embeddings, Redis, or Qdrant. Fresh reevaluation is a separate operation.
- No old dataset deletion, global cache flush, deployment, or broad infrastructure changes are required.

## Existing work and reference files

The draft package is [`examples/semantic_v3`](../examples/semantic_v3/README.md): 64 retrieval examples, 32 held-out cases, taxonomy, schema, and manifest. Every label is pending human review. Its detailed adapter/seeding instructions supplement this phase.

Relevant integration points:

| Area | Existing files |
| --- | --- |
| Parsing and providers | `src/services/llm_parser.py`, `src/services/llm_providers.py` |
| Request contracts | `src/models/request.py` |
| Retrieval and corpus | `src/services/semantic_engine.py`, `src/services/semantic_corpus.py` |
| Embeddings and seeding | `src/services/embeddings.py`, `src/services/vector_setup.py`, `src/cli.py` |
| Evidence and orchestration | `src/services/advisory_evidence.py`, `src/services/orchestrator.py` |
| Decisions and replay | `src/services/policy_engine.py`, `src/services/replay_engine.py`, `src/services/decision_ledger.py` |
| Review and configuration | `src/services/escalation_service.py`, `src/config.py` |

Use separate modules for the new analysis, retrieval planning, and judge contracts rather than growing the parser into the entire pipeline. Names below describe proposed interfaces, not existing commands.

## Step 12.1 — Record the baseline and acceptance contract

1. Inspect current git state, configuration names, server version, policy mode, Qdrant collection/version/count, and Redis namespace without printing secrets.
2. Record baseline embedding/model/prompt versions and existing semantic thresholds. Preserve existing collections and data.
3. Write a non-executing evaluation runner specification. Each run records request, stage status, expected and actual findings, latency, and errors.
4. Fix a development set separate from the 32 held-out cases; use reviewed corpus-side scenarios for debugging. Never seed held-out data.
5. Define pass criteria before tuning: no deterministic policy bypass; no partial execution of compound requests; every applicable labeled risk counted independently; no silent success after unavailable evidence.

Done when: a baseline configuration snapshot and evaluation protocol exist. Historical reports are references, not a substitute for measuring the current version.

## Step 12.2 — Review and freeze data

1. Review all proposed action lists, excerpts, risk findings, and uncertainty labels in both files.
2. Record actual reviewer identity, disposition, and corrections. Do not infer approval from schema validation.
3. Retain all existing baseline datasets. Treat the 1,024 variants as historical stress data, not independent calibration evidence. Keep old reports separately.
4. Approve reviewed cases explicitly; retain rejected/pending cases outside normal seeding. An expressly authorized provisional experiment must label itself as such.
5. Regenerate hashes and assign a frozen revision after edits.

Done when: selected corpus cases are eligible for the intended seeding mode, evaluation labels are reviewed, and split integrity is documented. Human/domain review is the input needed for approved labels; implementation can continue against draft fixtures meanwhile.

## Step 12.3 — Add shared schemas and configuration

Define strict Pydantic contracts with forbidden extra fields:

- `RequestAnalysis`: action list, grounded intent summary, potential risk aspects, supporting excerpts, unknowns, unmapped concerns, and analysis status.
- `RetrievalQuery`: stable ID, text, purpose (`operation`, `risk`, `general`), action references, optional risk categories, and coverage references.
- `RetrievalEvidence`: candidates, per-query scores/ranks, filters, corpus/text-format/embedding versions, and coverage gaps.
- `RiskFinding`: category or explicit unmapped concern, `applies` / `does_not_apply` / `uncertain`, request excerpts, candidate IDs, reason, missing facts.
- `JudgeAssessment`: findings, coverage status, contradictions, unavailable/error state, and version metadata.

Share action and risk enums; derive action families in code. Represent multi-label corpus findings and benign contrasts without collapsing them into one risk category.

Add documented settings for pipeline mode (`baseline`, `observe`, `review`), analyzer/judge provider and model selection, query/candidate budgets, and timeouts. Start in baseline/observe, never silently enable review mode. Validate enum/range settings on startup.

Done when: malformed output, unknown categories, invented candidate IDs, and contradictory mappings cannot enter the decision evidence. Do not require a database migration unless inspection shows existing JSON evidence storage cannot support this versioned payload.

## Step 12.4 — Implement LLM 1: grounded request analysis

1. Extend natural-language processing to obtain action interpretation and retrieval analysis in one structured response where practical.
2. Keep existing grounding checks for paths, explicit contents, environment, and parameters. Analyze quoted content as data, not an instruction to execute its text.
3. Extract all requested actions; return split-required with no token for compound input. Document its API status and response body and test both entry points.
4. For structured input, analyze its fields and any instruction text without changing executable values. Report material contradictions explicitly.
5. Validate excerpts against source text. Preserve unknowns; do not invent approval, sensitivity, public accessibility, or filesystem state.
6. Detect unsupported/unmapped concerns instead of discarding them.

Done when: development examples cover negation, quotes, role-spoofing text, multiple actions, unsupported actions, and missing facts. An analyzer timeout or schema failure is visible. Failed executable parsing rejects the request; failed optional risk analysis marks coverage incomplete and requires review in review mode.

## Step 12.5 — Implement controlled query planning

1. Always build a canonical query from the authoritative action plus original instruction.
2. Allow up to three complementary queries initially, each attached to a distinct grounded aspect. Deduplicate equivalent queries.
3. Derive actual action-family filters from validated actions. Risk-focused searches can cross operation families; generated searches do not change the requested action.
4. Code builds allowlisted Qdrant filters. Enforce embedding-spec and corpus-hash constraints on every query.
5. Reserve an unfiltered-by-predicted-risk/general evidence path when no supported category describes a concern; do not make LLM risk classification the only gate to all evidence.
6. Record aspects dropped by the query budget as uncovered. Incomplete coverage cannot become a confident clean result.

Done when: canonical evidence remains searchable regardless of generated queries; each additional query has traceable purpose and a separate embedding/cache entry.

## Step 12.6 — Implement v3 loading and cloud seeding

Follow the dataset README's adapter contract. Do not reuse the existing binary corpus loader without extending or versioning it.

1. Validate schema plus cross-field invariants and review eligibility.
2. Embed consistently formatted instruction text; keep answer annotations out of embedding input. Record text-format version.
3. Store multi-valued action families/risk aspects, full findings, review status, source ID, and version/hash metadata as payload.
4. Create a separate v3 cloud collection with actual configured dimensions and cosine distance. Add payload indexes for planned filters.
5. Seed only eligible retrieval cases with stable IDs; verify IDs, hashes, counts, dimensions, and payloads. Leave the v2 collection intact.
6. Reuse namespaced embedding cache. No project-wide or global Redis flush is necessary merely because the corpus changes.

Done when: a seed audit proves no held-out IDs entered Qdrant and every point belongs to the intended corpus/model/text version. Supply exact verified CLI commands in the implementation handoff; do not invent commands in this plan.

## Step 12.7 — Retrieve and merge evidence

1. Search each planned query independently; preserve per-query scores and ranks.
2. Reserve candidates across risk aspects and risky/benign/uncertain labels. Deduplicate by example ID while preserving all retrieval provenance.
3. Bound the judge context and disclose any evidence/aspects truncated by the budget.
4. Treat empty results as a coverage gap; distinguish partial query failure from successful completion.
5. Do not apply the old single-query threshold to the maximum across expanded queries. Retain old scoring as baseline evidence only.

Done when: the combined-risk development cases retrieve evidence for every supported aspect without one category crowding out another. Missing corpus coverage is reported rather than manufactured.

## Step 12.8 — Implement LLM 2: evidence-based judgment

1. Send the original instruction, authoritative action, grounded analysis, unknowns, and selected corpus evidence to the judge.
2. Require an independent applicability assessment for each aspect; the analyzer's hypotheses and corpus labels are evidence, not authoritative answers.
3. Allow new concerns grounded in the original request. If additional evidence would be needed, return uncertainty/coverage gap rather than inventing a source. Automatic retrieval loops are outside the initial scope.
4. Validate source excerpts, candidate references, categories, and required coverage in code. Reject invalid output as unavailable evidence.
5. Distinguish dangerous file contents quoted as documentation from executable requests. Do not treat returned similarity as probability or model confidence as calibrated probability.
6. Preserve multiple findings and missing facts. Benign findings cannot cancel unrelated risks.

Done when: judge outputs are structured, auditable, and incapable of directly issuing tokens or changing policy. Failure/timeout produces an explicit unavailable state.

## Step 12.9 — Integrate observation and decision modes

Observation mode preserves current decision behavior and records the complete new pipeline beside it. It must not accidentally cause approvals or escalations through new fields.

In review mode, use deterministic aggregation:

| Condition | Behavior |
| --- | --- |
| Invalid or compound executable request | Reject/split-required; no token |
| Deterministic policy denial | Preserve denial |
| Any applicable semantic risk | Set advisory flag; existing policy and human-review rules determine outcome |
| Material uncertainty, missing coverage, unavailable judge | Set advisory flag with precise reason |
| All relevant findings do not apply and coverage is complete | No new semantic flag; other policy checks still apply |

A judge may clear a similarity-only warning only in explicitly enabled review mode after evaluation supports it. Store baseline and judge assessments separately so this change can be audited. Do not OR every baseline warning into the final result forever; doing so prevents the intended false-positive improvement.

Done when: observe mode is decision-equivalent to baseline for accepted single-action requests, and review mode cannot override deterministic denial or bypass the existing approval requirements.

## Step 12.10 — Freeze evidence and expose it to reviewers

1. Persist analysis, queries, filters, candidates, scores, versions, judge output, aggregation rules, and coverage before releasing any decision/token.
2. Keep internal evidence separate from caller-controlled parameters. Preserve compatibility with old records and record evidence schema version.
3. Render all applicable/uncertain findings, supporting excerpts, and missing facts in escalation details. Do not expose provider credentials or unrelated sensitive log content.
4. Replay from frozen evidence with no advisory I/O. Dispatch by evidence/aggregation version where required; unsupported versions report inconsistency instead of silently using new semantics.
5. Preserve active/shadow policy evaluation against the same frozen evidence. Check human approval and reevaluation behavior explicitly; a previous approval must not silently authorize a changed action.

Done when: replay remains consistent after retrieval corpus/model settings change or providers become unavailable. Replay does not execute actions or make new policy-independent judgments.

## Step 12.11 — Execute staged online validation

Use the running FastAPI service with configured cloud providers. Honor the user's preference for online E2E validation; do not substitute a unit-test run for this phase's acceptance evidence.

First write a granular runbook containing exact commands, API JSON, expected HTTP status and evidence fields, reviewer actions, and cleanup ownership. Verify endpoint contracts from the implemented code/OpenAPI before publishing commands.

Sequence:

1. Read-only preflight: server version, health, DB/policy/autonomy state, Qdrant version/count, Redis namespace; stop on unexpected state.
2. Non-executing provider/schema smoke calls for analyzer and judge; verify embedding cache reuse.
3. Development-set parsing/retrieval/judgment checks. Fix problems here, not on held-out answers.
4. Freeze prompts, code, data hashes, budgets, and aggregation settings.
5. Evaluate the held-out set under four configurations: baseline, expanded retrieval only, judge with original retrieval, and both layers. Share identical cached parse/retrieval inputs where valid to isolate effects and reduce API usage. Expanded-retrieval-only is a diagnostic arm until an explicit aggregation rule is frozen; do not pretend legacy thresholds transfer automatically.
6. Count missed/invented actions, per-risk retrieval coverage, per-risk judge misses, benign false positives, uncertainty handling, compound handling, and full recovery of combined risks. Record provider errors, latency and call counts separately.
7. Exercise actual decision and escalation endpoints in controlled fixtures: semantic risk, uncertainty, deterministic denial, judge-dismissed false positive, human rejection, human approval, and replay. Explicitly set and report autonomy mode: SHADOW_LOGGED is not an execution authorization.
8. Verify parser/judge timeout, malformed output, unknown references, partial Qdrant failure, and Redis unavailability through bounded fault injection or dependency isolation in development. Restore settings afterward.
9. Exercise token/execution/rollback only on dedicated disposable fixtures through existing authorization paths. Never execute dataset instructions involving public data, credential material, broad deletion, or audit destruction.
10. Save a concise report, failure list, version snapshot, and decision/replay audit. Keep secrets and verbose raw provider logs out of git.

Done when: every scenario is reported as pass/fail/blocked with observed evidence. A server restart requirement, provider limit, or missing reviewer input must be surfaced promptly, not hidden by repeated retries.

## Step 12.12 — Evaluate improvement and roll out

Hard acceptance gates: no policy bypass, no partial compound execution, no invented evidence accepted, no silent clean result after missing evidence, and replay consistency for supported record versions.

Quality gate: report raw counts and category-level changes against baseline. Seek fewer benign false positives with no new missed known risky cases on the reviewed set; explain every regression. Small sample results are provisional, not statistically established production accuracy. If quality regresses, retain observe mode and revise using development data; do not repeatedly tune against the same holdout.

Only after passing these gates enable review mode deliberately. Document how to return to baseline mode and the preserved v2 collection. Do not destroy v3 audit evidence on rollback. Update README/config examples/runbooks and prepare a focused commit.

## Progress checklist

- [x] Draft 64 retrieval examples and 32 held-out examples with taxonomy/schema/manifest.
- [x] Inventory existing data and specify retention roles.
- [x] 12.1 Baseline and acceptance protocol. Implemented; live acceptance evidence remains separate.
- [ ] 12.2 Label review and frozen dataset revision.
- [x] 12.3 Strict contracts and configuration. Implemented; live acceptance evidence remains separate.
- [x] 12.4 First LLM analysis and grounding. Implemented; live acceptance evidence remains separate.
- [x] 12.5 Controlled multi-query planning. Implemented; live acceptance evidence remains separate.
- [ ] 12.6 V3 loader/seeder implemented; actual cloud seeding and verification pending data selection.
- [x] 12.7 Evidence retrieval and merge. Implemented; live acceptance evidence remains separate.
- [x] 12.8 Second LLM judgment. Implemented; live acceptance evidence remains separate.
- [x] 12.9 Observation and deterministic advisory integration. Implemented; live acceptance evidence remains separate.
- [x] 12.10 Frozen evidence, escalation details, replay. Implemented; live acceptance evidence remains separate.
- [ ] 12.11 Runbook and evaluation CLI implemented; complete online execution report pending.
- [ ] 12.12 Quality review, deliberate rollout, docs and commit preparation.

Implementation can proceed on contracts and draft fixtures while domain-label review is pending. Approved seeding depends on 12.2; live review-mode rollout depends on all verification gates. This plan does not authorize claiming human approval or successful live tests that have not occurred.

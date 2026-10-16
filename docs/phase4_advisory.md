# Phase 4: scope and semantic evidence

`advisory_evidence.build_evidence(request, autonomy_state)` collects inputs once.
`shadow_engine.evaluate_and_record` calls it when no frozen bundle is supplied.
The active and candidate policies consume that same bundle. Supplied evidence
is preserved, and the pure policy evaluator never reruns an estimator or query.

The ledger stores the full bundle: blast metrics, semantic flag/score/warning,
advisor source and threshold, matched-pattern reference, environment, and an
autonomy snapshot. Replay can use these stored inputs even after settings,
patterns, or estimator code change.

## Blast-radius estimate

The estimator performs no filesystem reads, symlink resolution, or LLM calls.
It infers file/directory type from the action suffix or a trailing slash. Unknown
action types remain unknown. It ignores caller-supplied file counts.

| Factor | Scope-score contribution |
|---|---:|
| File | 1 |
| Directory | 3 |
| Unknown target type | 5 |
| Recursive flag | +5 |
| Wildcard target | +4 |
| Path depth | +max(0, 3 - depth) |

The score is a relative scope index, not a count of files or a risk category.
The policy's `risk_map` still determines risk. Recursive, wildcard, or unknown
scope sets `requires_review`; counts for these scopes remain unknown. A single
named resource is estimated as one resource, not a claim about directory contents.
Path depth is computed from the normalized request string, without anchoring
relative paths to the process working directory. Scope containment remains the
policy engine's separate path check.

## Advisory precedence

RBAC, scope, and regex denials take precedence. Environment escalation and an
unissuable TTL retain their existing behavior. Otherwise a semantic flag or
blast `requires_review` produces `ESCALATED` with no issuable token, preserving
the policy risk category. An absence of warnings never grants access by itself:
policy checks and the autonomy gate still apply.

## Offline calibration

The offline advisor matches bounded patterns for mass deletion, bypassing review,
credential export, and credential-shaped targets. A match scores 1; no match
scores 0. It is a keyword/regex stub, not an embedding similarity measurement.

`examples/semantic_calibration_cases.json` contains eight synthetic labeled
cases: four benign and four suspicious. The Phase 4 test measures their scores
and verifies that 0.5 separates the two groups. This demonstrates the offline
cutoff; the small dataset does not establish broad detection accuracy or cover
arbitrary paraphrases and prompt injection.

## Optional vector advisor

Online mode with `QDRANT_URL` configured attempts a read-only query against
`QDRANT_COLLECTION` on an HTTPS cloud Qdrant endpoint. With
`embeddings.selected: local_minilm_l6` in `config/llm_routing.yaml`, the optional `sentence-transformers`
package and cached model are required. The adapter loads
with `local_files_only=True`, so it will not download a model during a request.
See the [SentenceTransformer constructor documentation](https://www.sbert.net/docs/package_reference/sentence_transformer/model.html).

With `embeddings.selected: gemini_current`, the adapter uses the model ID from
the YAML, `GEMINI_EMBEDDING_DIMENSIONS`, and the Gemini/Google key. Optional provider
packages are listed in `requirements.txt`.

The online corpus is stored in `examples/semantic_advisory_examples.json`: 20
synthetic examples with benign/risky labels, operation families, risk categories,
and review status. The corpus is explicitly marked as not domain-reviewed.
`semantic_corpus` validates metadata and hashes its complete contents. Qdrant
payload filters require the exact corpus hash and embedding specification, so
old points remain available without participating in new queries.
Bump the corpus version when changing the embedding-input template, then reseed;
the same model specification alone does not identify a representation change.

Requests use a consistent representation: operation, scope, target filename
(including sensitive `.ssh` context), and executable parameters. Parser
provenance is excluded. Original user instructions remain available because
intent such as bypassing review may be missing from executable arguments;
caller-supplied instructions can never replace or hide those arguments.

Canonical action classes determine the family: create, read, update, move, or
delete. A rename is represented by `move_file` with source and destination.
Each query includes its operation family plus `any` examples for risks that can
occur across operations. Mass-deletion examples must use the delete family;
credential and review-bypass examples remain searchable for all families.
Qdrant uses keyword indexes on `embedding_spec`, `corpus_hash`, `action_family`,
and `label`.

The same query embedding retrieves up to five risky and five benign examples.
The best risky score is compared with `SEMANTIC_THRESHOLD` and the best benign
score. A risky lead above `SEMANTIC_SCORE_MARGIN` requests review; a benign lead
suppresses only the vector advisory. A tie or lead within the configured margin
requests review as ambiguous. Margin defaults to zero; wider intervals require
calibration. These scores are similarity measurements, not probabilities.
Policy checks and the autonomy gate always run independently.

### Optional Jina reranking

Set `JINA_RERANKER_API_KEY` and `JINA_RERANKER_API_URL` (default:
`https://api.jina.ai/v1/rerank`). The adapter uses the existing `httpx` dependency.
`JINA_RERANKER_MODEL` defaults to `jina-reranker-v2-base-multilingual` and
`JINA_RERANKER_TIMEOUT_SECONDS` defaults to eight seconds. It sends the cleaned
request and the retrieved example texts to Jina, requesting all results so
both labels remain available. It validates scores and unique document indices.

`JINA_RERANKER_MODE=disabled` is the default and makes no reranker API calls.
Set `JINA_RERANKER_MODE=observe` to record ranking, scores, and the
preferred label alongside the cosine assessment. Jina relevance scores do not
use `SEMANTIC_THRESHOLD`. The current calibration report measures the vector
classifier, not Jina. Evaluate the reranker separately before changing authority.
`review` mode can add a human-review warning when Jina prefers a risky example
or ties; it never clears a pre-existing warning. `disabled` makes no Jina call.

Missing settings, HTTP 400/401/429/5xx, timeouts, and malformed or incomplete
results skip reranking and preserve the vector assessment. The stored evidence
includes a safe reason such as `http_400`, without response bodies or keys.
There are no automatic retries or redirects. Offline mode makes no Jina call.

Missing corpus metadata, invalid vectors, or cloud failures produce an
`unavailable` advisory requesting human review. Explicit offline mode retains
the pattern stub and never calls cloud services. The evidence ledger freezes
both scores, their difference, threshold, required margin, classification,
corpus hash, and retrieved references so replay never reruns retrieval.

Run from the project root with online settings:

```sh
python -m src.cli seed-advisories
python -m src.cli calibrate-semantic --output reports/semantic_calibration_v2.json
```

Seeding computes all vectors before mutation, upserts stable IDs, and verifies
the stored examples. It preserves old points and rejects dimension mismatches.
Redis caches vectors using model specification and a hash of input text, so
new representations naturally receive separate keys.

Calibration uses `examples/semantic_action_validation_cases.json`, with 12
calibration cases and eight separate holdout cases. Threshold selection uses
only calibration labels, prioritizing fewer missed risky cases and then fewer
false positives. Both splits report confusion counts at current and proposed
thresholds. Margin remains fixed. The CLI never edits configuration. These
small synthetic sets establish development regressions, not production
accuracy; representative traffic and domain-owner label review are still needed.

The checked-in `reports/semantic_calibration_v2.json` records a live Gemini/
Qdrant run: all 12 calibration and eight holdout labels matched at threshold
0.5 and margin zero. `reports/semantic_regression_v2.json` records the original
benign create-file API request plus its persisted cloud evidence. It now has
no semantic flag and returns `SHADOW_LOGGED`, with no execution token.

## Requirement coverage

| Requirement | Evidence |
|---|---|
| REQ-1 / B2 / C3: deterministic scope estimation | File versus recursive directory, wildcard/depth, ignored client counts, and no-filesystem tests |
| REQ-7 / B4: frozen evidence | Ledger integration test persists metrics, warning, source, threshold, environment, and autonomy snapshot |
| C2: advisor cannot override hard denial | Warning escalation preserves risk; RBAC, path, and regex denials remain denied |
| C4: explicit keyless mode | Offline no-network test and cloud-unavailable review test |
| A2: replay inputs preserved | Pure evaluator echoes evidence without recomputing advisors |

All checks are in `tests/test_phase4_blast_radius.py`. Full replay, human review
queueing, and persisted autonomy promotion are implemented in later phases.


### Jina embeddings

Set `embeddings.selected: jina_v3` in `config/llm_routing.yaml` to use Jina for both seed and query embeddings.
`JINA_EMBEDDING_URL` defaults to `https://api.jina.ai/v1/embeddings`.
Set `JINA_API_KEY` for authentication. Its model ID is in the YAML; the default
dimension setting is `JINA_EMBEDDING_DIMENSIONS=768`. The API uses `text-matching` for both sides.

Redis cache keys and Qdrant filters include the provider, model, dimensions and
embedding task. Switching providers requires seeding again, even with equal
vector dimensions. Prefer a separate `QDRANT_COLLECTION=policy_prompts_jina`
to retain the Gemini collection. Run `python -m src.cli seed-advisories`, then
`python -m src.cli calibrate-semantic --output reports/semantic_calibration_jina.json`.
Restart the server after changing its environment. Earlier Gemini calibration
results do not establish Jina thresholds. Embedding errors request human review
through the existing semantic-unavailable path; vectors from another provider
are never substituted. Jina reranking remains independently configurable.

API schema: https://api.jina.ai/docs

## Explicit semantic completion (pipeline v2)

The new `semantic-pipeline-v2` / `risk-union-v2` evidence includes a validated
`summary`. `completion` is `complete`, `partial`, or `unavailable`;
`assessment` is `clear`, `risk_detected`, `uncertain`, or `not_assessed`.
A confirmed risk can coexist with incomplete checking. A clear assessment
requires all necessary checking to complete without blocking gaps.

Qdrant outcomes distinguish successful empty retrieval (`no_evidence`) from
failed retrieval (`unavailable`) and partly completed work (`partial`). Each
query records each label search, its filters, matches, and failures. Valid
matches survive an independent failure; invalid candidates are excluded.
An empty label bucket alone is informational. Query and candidate budget limits
remain review reasons. Pending corpus review remains provenance governed by
`SEMANTIC_V3_ALLOW_UNREVIEWED`.

For example, a judge provider outage produces `partial / not_assessed`, a
`judge_unavailable` reason, preserved analysis/retrieval evidence, and a skipped
or unavailable stage where appropriate. Successful searches with zero eligible
examples produce `partial / not_assessed` with `no_evidence`: the review reason
says the searches completed without supporting examples and points to the saved
query/filter evidence. Neither situation is a finding that the action is safe.

In `review` mode these reasons drive the existing semantic escalation input;
hard policy denials retain precedence. In `observe` mode the summary is recorded
but the baseline controls the semantic decision. Baseline mode does not run the
new pipeline. Evaluation responses expose `semantic_summary` and `semantic_mode`.
Held decisions have no capability token.

Storage uses existing JSON columns: `decision_records.evidence_json.semantic_pipeline`
and `escalation_queue.package_json.semantic_findings`. Provider attempts and
validation errors continue in `request_attempt_events`. The judge explicitly
addresses each analyzer unknown by its original index, with grounded evidence
for resolution. Unresolved unknowns remain review reasons.

Replay dispatches by the stored pipeline/aggregation version. V1 retains its
original aggregation and warning text. V2 validates the frozen stages and
recomputes the summary and selected flag/warning without cloud calls.
Reviewer actions require a completed held decision and a committed pending
escalation. Execution, compensation, and promotion enforce their existing
lifecycle preconditions under locks. Post-approval policy behavior is deferred
for a separate discussion.

This implementation has not yet been tested; no live verification is claimed.

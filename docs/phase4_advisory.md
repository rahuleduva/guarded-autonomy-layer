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
`EMBEDDING_PROVIDER=sentence_transformers`, the optional `sentence-transformers`
package and cached `SENTENCE_TRANSFORMER_MODEL` are required. The adapter loads
with `local_files_only=True`, so it will not download a model during a request.
See the [SentenceTransformer constructor documentation](https://www.sbert.net/docs/package_reference/sentence_transformer/model.html).

With `EMBEDDING_PROVIDER=gemini`, the adapter uses `GEMINI_EMBEDDING_MODEL`,
`GEMINI_EMBEDDING_DIMENSIONS`, and the Gemini/Google key. Optional provider
packages are listed in `requirements.txt`.

The collection must contain advisory examples embedded by that same model,
one unnamed cosine vector, and matching `embedding_spec` payloads. The request
path does not create or populate collections. Explicit commands
`python -m src.cli seed-advisories` and `python -m src.cli calibrate-semantic`
seed synthetic patterns and measure the labeled cases in online mode. Seeding
upserts stable point IDs without deleting existing points; a dimension mismatch
requires a different collection name. Calibration reports a suggested cutoff
without editing configuration. Queries return
the highest-scoring example and record its point ID as provenance. See the
[Qdrant query-points reference](https://api.qdrant.tech/api-reference/search/query-points/).

Missing dependencies, model files, collection data, invalid vector settings,
or a failed query trigger the deterministic offline stub. Qdrant operations use
a two-second client timeout. The fallback stores only the exception class,
avoiding sensitive details from connection errors. Explicit offline mode never
loads the vector model or calls Qdrant.

The vector cutoff still needs calibration using real cosine scores from the
deployment's seeded collection. Offline binary scores cannot calibrate cosine
similarity. Before relying on online warnings, embed labeled synthetic cases,
measure their positive/negative score distributions, choose a cutoff, and set
`SEMANTIC_THRESHOLD`. The actual threshold and source are frozen per decision.
The adapter has been exercised against an in-memory cosine collection with a
controlled embedding; real MiniLM/cloud calibration is not claimed.

## Requirement coverage

| Requirement | Evidence |
|---|---|
| REQ-1 / B2 / C3: deterministic scope estimation | File versus recursive directory, wildcard/depth, ignored client counts, and no-filesystem tests |
| REQ-7 / B4: frozen evidence | Ledger integration test persists metrics, warning, source, threshold, environment, and autonomy snapshot |
| C2: advisor cannot override hard denial | Warning escalation preserves risk; RBAC, path, and regex denials remain denied |
| C4: keyless fallback | Offline no-network test and cloud-failure parity test |
| A2: replay inputs preserved | Pure evaluator echoes evidence without recomputing advisors |

All checks are in `tests/test_phase4_blast_radius.py`. Full replay, human review
queueing, and persisted autonomy promotion are implemented in later phases.

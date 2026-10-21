# Semantic retrieval corpus and evaluation data

`corpus.json` is the single authoritative retrieval dataset: **100 synthetic examples**.
The application loads this file and searches Qdrant collection `semantic_advisory_v3`.
Update this source first, validate it, then seed the same collection. Redis caches
embeddings by text and embedding configuration; no cache flush is needed for corpus updates.

## Files

| File | Purpose |
| --- | --- |
| `corpus.json` | 100 retrieval examples: risky, benign and uncertain contrasts |
| `evaluation.json` | 32 historical evaluation cases; never seed |
| `taxonomy.json` | Action families, risk categories and annotation rules |
| `dataset.schema.json` | Dataset structure |
| `changes.json` | Revision counts/fingerprints and archived-source references |
| `manifest.json` | Current hashes, counts and coverage from the local validator |

The maintained full evaluation suite is `../semantic_evaluation_combined_v1/`:
88 cases including the 32 historical cases and 56 development cases. Evaluation
instructions and answers stay separate from retrieval examples.

## Annotation and quality limits

Every example includes the instruction, action classes/families, scope, risk
assessments with literal supporting excerpts, overall label, missing information
and review status. Risks cover mass deletion, review bypass, secret export,
credential access, data exposure, destructive overwrite and audit tampering.
A risky label means at least one category applies; uncertain means a material fact
is missing; benign means the annotated risks do not apply to the stated scenario.
These are advisory examples and never grant execution permission.

All 100 labels remain **pending human review**. The latest revision retains the
96 cycle-one examples, expands three benign annotations, and adds four contrasts
for public-document reads, uncertain overwrites, documentation and ordinary renames.
The current annotation revision also marks `contrast-18` as `secret_export: does_not_apply`;
a public manual read provides benign category-matched evidence for read-only requests.
Those changes were informed by development-suite findings. Exact overlap is
forbidden, but statistical independence and production reliability are not claimed.
The complete live evaluation of the latest revision remains pending.

## Validate and update

```bash
python -m scripts.datasets.validate_semantic_corpus
python -m scripts.datasets.validate_combined_semantic_evaluation
python -m src.cli validate-semantic-v3
python -m src.cli seed-advisories-v3
```

After intentional edits, update `changes.json` with the new fingerprint/count and
regenerate the manifest using `--write-manifest`; inspect the changes before seeding.
Pending labels require the explicit configured provisional-development setting.
The canonical seeder validates evaluation separation, vector dimensions and exact
payloads, and verifies the number of points for the current corpus hash. Older
revisions are filtered by hash; remove obsolete vectors only after verification.

## Consolidation history

The former candidate and cycle-two folders were consolidated into this package.
Their complete source snapshots, including the original 64-case version, are
preserved as test evidence in MongoDB under run `risk-quality-20261017`, invocation
`corpus-consolidation`. Earlier measured results remain in their original run
records. MongoDB stores historical evidence; runtime retrieval reads this JSON
and Qdrant, not MongoDB configuration.

`policy_prompts` remains in use by the older baseline pipeline and is separate
from this corpus. Do not merge its different schema into this collection or
remove it while that pipeline is still used. Do not touch unrelated collections.

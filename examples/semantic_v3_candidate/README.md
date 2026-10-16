# Retrieval corpus candidate 1

96 synthetic retrieval examples: 64 inherited, five corrected in place within
this candidate, and 32 new examples. All remain pending human review. The active
`../semantic_v3/corpus.json` and its fingerprint are unchanged. This directory is
not selected by the production loader and has not been seeded into Qdrant.

`corpus.json` conforms to the existing Dataset schema. `changes.json` records the
base fingerprint, corrected IDs, and added IDs. `manifest.json` records coverage,
hashes and proximity to evaluation examples. Stable IDs identify inherited
examples; the different dataset fingerprint distinguishes their revisions.

## What improved

| Added IDs | Purpose |
| --- | --- |
| contrast-01–04 | Actual audience versus path names; reversible pseudonymization; unverified aggregate privacy |
| contrast-05–08 | Confirmed revocation versus pending revocation; true versus claimed redaction |
| contrast-09–12 | Overwrite caused by moving files; collision refusal; unknown destination; disposable representations |
| contrast-13–16 | Required retention despite innocent cleanup intent; expired obligations; destructive redaction; discovery/access gaps |
| contrast-17–20 | Approval bound to another action; valid approval with checks retained; unverified references; emergency wording |
| contrast-21–24 | Broad deletion of reproducible data; one large file; wildcards with unknown count; link traversal uncertainty |
| contrast-25–28 | Quoted instruction-like content; real bypass outside quotations; negation/history; fictitious multi-action text |
| contrast-29–32 | Interacting credential/export/bypass risks; benign new contents destroying old evidence; multiple uncertainties; benign compound actions |

The inherited corrections make operation type explicit in `deletion-08`,
`bypass-01`, and `bypass-07`, recipient authorization explicit in `export-03`, and
the preserved authorized audience explicit in `credentials-04`. Being external
alone does not establish an unauthorized audience, and a folder named vault is
not proof of authorization. Those facts must be in the scenario.

Similar contrast pairs are deliberate teaching evidence inside the corpus;
they are not independent evaluation observations. An overall `risky` label does
not mean every annotated category applies. Preserve all per-category findings
when retrieving and judging; e.g. confidential data exposure can be absent while
audit destruction applies to the same request.

## Scope and limitations

This improves authored coverage and label clarity, not demonstrated retrieval
recall or judge accuracy. It uses the same seven-category taxonomy and supported
file-operation scope. Network actions, permission changes and shell execution
need an explicit product/schema decision before introducing executable labels.
Path traversal, sandbox restrictions, token reuse and time-of-check changes also
need deterministic integration tests. The corpus cannot replace those controls.

All facts here are synthetic scenario stipulations, not verified facts about a
real filesystem or approvals. The executor and policy checks still enforce the
actual boundary. Examples with conditional collision behavior illustrate the
requested risk; they do not claim the current parser/executor supports that
condition. The immutable old corpus remains available for comparison.

The development evaluation suite was reviewed to identify gaps. It must not be
presented as an untouched final holdout for this candidate. No evaluation row is
copied into this corpus, but related mechanisms and contrast structures overlap.
Use a future independently authored/reviewed holdout for final performance claims.

## Validate locally

```sh
PYTHONPATH=. python scripts/validate_semantic_corpus_candidate.py
```

After intentional edits, review and regenerate with `--write-manifest`. Checks
cover Pydantic invariants, lineage, exact declared changes, review flags, coverage
of every risk/assessment and global-label bucket, duplicate instructions and
cross-evaluation exact overlap. No provider or database calls occur.

## Before activation

1. Review proposed labels and contrast cases. Record independent reviewer identity,
   date, disposition and corrections; do not mark approved based on validation.
2. Snapshot the current corpus and evaluation configuration for a baseline run.
3. Validate and seed this candidate explicitly with the existing CLI (see below).
   Without overrides the command uses the configured runtime corpus/collection,
   which default to the original corpus and collection.
4. Seed an isolated cloud collection with the same selected Gemini embedding
   specification and full metadata; verify count, fingerprint, dimensions and
   payload. Preserve the current collection for rollback and comparison.
5. Change the runtime dataset and collection together. Retrieval filters by the
   local corpus fingerprint, so merely replacing the active JSON before seeding
   would return empty matches. Model-specific Redis embedding keys can be reused;
   a cache flush is unnecessary for new text under the same embedding spec.
6. Compare baseline and candidate on the development suite using identical model
   routing, prompts, pipeline mode and retrieval budgets. Report relevant-evidence
   recall, benign false flags, missed risks, uncertainty and failures. Larger
   corpus size can increase irrelevant matches and candidate truncation, so a
   quality gain must be measured before rollout.

Neither cloud seeding nor runtime activation is part of this local data review.

## CLI and runtime selection

Validate locally, then seed when ready with your existing online credentials and
explicit provisional-data setting (`SEMANTIC_V3_ALLOW_UNREVIEWED=true` for these
pending examples):

```sh
python -m src.cli validate-semantic-v3 --corpus examples/semantic_v3_candidate/corpus.json
python -m src.cli seed-advisories-v3 --corpus examples/semantic_v3_candidate/corpus.json --collection semantic_advisory_v3_candidate
```

The seed command creates the collection if needed, preserves the embedding
specification and payload indexes, and verifies stored payloads and counts. It
does not select the candidate for the running application. Relative corpus paths
resolve against the project root, including when supplied through `.env`.

After successful seeding and comparison, select both settings together and
restart FastAPI:

```dotenv
SEMANTIC_V3_CORPUS_FILE=examples/semantic_v3_candidate/corpus.json
SEMANTIC_V3_COLLECTION=semantic_advisory_v3_candidate
```

For rollback, restore the original corpus path and collection together, then
restart. Keep the candidate files immutable after seeding; edits change the
fingerprint and require reseeding. Redis reuse remains separated by the selected
embedding specification and actual text.

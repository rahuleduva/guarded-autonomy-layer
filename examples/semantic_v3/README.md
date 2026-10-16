# Semantic dataset v3 — draft corpus and evaluation handoff

This package prepares data for the proposed two-LLM pipeline: request analysis before retrieval, then evidence-based risk judgment after retrieval. It does not change the running application, configured collection, Redis, or current corpus. No cloud calls or seeding were performed to create it.

## Files and status

| File | Role | Count | Seed into Qdrant? |
| --- | --- | ---: | --- |
| `corpus.json` | Individually authored retrieval candidates and benign contrasts | 64 | Only after label review and a v3-compatible loader |
| `evaluation.json` | Separate held-out interpretation and judgment expectations | 32 | Never |
| `taxonomy.json` | Allowed action mappings, risks, and label definitions | — | Metadata contract only |
| `dataset.schema.json` | Structural JSON Schema for both datasets | — | No |
| `manifest.json` | File hashes, counts, and coverage | — | No |

All cases are synthetic and **pending human review**. Authorship and consistency checking do not establish correct domain labels. No real credentials, personal records, or external dataset content are included. Secret-like strings are explicitly fictional placeholders. These are retrieval/evaluation examples, not model fine-tuning data.

## Meaning of the records

- `instruction`: the complete scenario text; fictional facts explicitly stated here may be used for semantic interpretation.
- `actions`: expected operation classes and deterministically mapped families. These annotations are not complete `ActionRequest` objects. Targets and execution parameters must be grounded by the future parser; never execute these files as fixtures.
- `scope`: the stated scope, including `unknown` and `multiple_actions`.
- `risk_aspects`: categories to investigate, each with an expected applicability and an exact supporting instruction excerpt. Includes benign contrasts and uncertainty, not just positive risks.
- `label`: `risky` if any aspect applies; otherwise `uncertain` if any is uncertain; otherwise `benign`.
- `expected_semantic_flag`: proposed advisory expectation, true for risky or uncertain. It is not an ALLOWED/DENIED expectation and does not bypass policy.
- `expected_request_handling`: `split_required` for multiple actions, otherwise `single_action`. Single action does not promise the current grounding validator or executor accepts the request.
- `reason` and `missing_information`: reviewer-facing rationale and unknowns. These are answer annotations and must not be supplied to the model when evaluating a held-out case.
- `scenario_group`: groups related mechanisms, not a security category or runtime filter.
- `review_status`: remains `pending_human_review` until an actual reviewer approves or rejects the case.

`does_not_apply` means the given scenario does not support that particular risk. It does not establish safety in the real filesystem. For example, a claimed empty directory, public certificate, authorized audience, or reviewed operation still needs independent execution/authorization checks. A filename alone does not prove sensitivity. Risk categories are not exhaustive; deleting the only backup can be harmful without meeting the definition of mass deletion.

## Coverage

The corpus has eight groups of eight cases: deletion, review bypass, secret export, credential access, data exposure, destructive overwrite, audit tampering, and combinations. Each group contains four risky, three benign, and one uncertain case: **32 risky, 24 benign, 8 uncertain** in total.

The held-out set has eight groups of four cases: **9 risky, 16 benign, 7 uncertain** in total. It includes a five-risk/two-action request and a case combining confirmed bypass with uncertain exposure.

The cases vary intent, content-vs-instruction, scope, resource significance, audience, credentials, negation, historical/hypothetical language, and compound instructions. This is not a filename substitution generator. It remains a small synthetic development set, not proof of production coverage. No measured recall, false-positive rate, calibrated threshold, or independently established ground truth is claimed. Related mechanisms intentionally occur across splits, so this is not a guarantee of semantic independence. Before a final benchmark, add independently authored and reviewed real-world-style cases.

## Review before seeding

1. Read each instruction independently of its proposed answer.
2. Check the action list for omissions, especially compound instructions.
3. Check each category and exact supporting excerpt; distinguish quoted content from requested behavior.
4. Verify that unknown facts are not being assumed. Preserve `uncertain` where the conclusion needs missing facts.
5. Check that benign contrasts do not accidentally authorize bypass or ignore another applicable risk.
6. Record reviewer identity, date, disposition, and corrections in a separate review log before changing `review_status` to `approved`.
7. Review both files, but do not tune prompts or thresholds on the evaluation set. Use corpus-side development cases or a new separate development set for tuning. Once evaluation cases are used for tuning, retire that version as held out and author a new evaluation set.
8. Bump the dataset revision and regenerate hashes after any edits.

Human sign-off was not requested or implied by creating these files. An implementation agent must not change review flags merely because schema validation succeeds.

## Implementation handoff: data adapter and seeding

The current `AdvisoryCorpus` schema expects one `request`, one family, one risk category, and a binary label. **These files are intentionally not drop-in replacements.** Do not point the existing `seed-advisories` command at them or strip metadata to force compatibility.

1. Implement a separate strict v3 loader, preserving the old loader and corpus for baseline runs. Validate against the schema plus cross-field invariants: IDs unique; every excerpt appears verbatim in the instruction; category names legal; action family matches action class; label and flag agree with risk findings; compound actions require splitting.
2. Exclude rejected/unreviewed cases from normal seeding. If the user explicitly authorizes a provisional development experiment, make that mode explicit and persist `review_status`; do not present it as approved data.
3. Construct embedding text from **instruction only** initially, prefixed consistently (for example, `Instruction: ...`). Do not embed expected labels, risk judgments, reasons, missing-information answers, or handling expectations. Store those as payload. Treat a future canonical action representation as a separately versioned experiment requiring re-embedding.
4. Store arrays `action_families` and `risk_aspects` in payload. `risk_aspects` includes every annotated category, including benign contrasts. Store full findings separately. Preserve ID, schema/corpus versions, review status, origin, embedding specification and text-format version.
5. Operation searches match an action family and cross-operation categories via explicit OR filters. Cross-operation risks include review bypass, secret export, credential access, data exposure, and audit tampering. Risk-focused searches match the relevant `risk_aspects` across families. Do not invent `any` as an action enum or relabel the actual operation. Retrieve benign/uncertain/risky evidence separately or reserve coverage so one class cannot crowd out another.
6. Use a new cloud Qdrant collection with an explicit v3 name, cosine distance, and dimensions discovered from the configured Gemini embedding result. Do not guess dimensions, overwrite the baseline collection, change `.env`, or delete old vectors as part of data preparation.
7. Index filter fields and preserve embedding-spec/corpus-hash filtering. Use stable point IDs derived from corpus hash, embedding specification, text-format version, and example ID. A changed corpus must not accidentally mix with the previous revision.
8. Seed only `corpus.json`; verify the exact ID set, counts, dimensions, required payload, and matching corpus hash. An empty filtered result means a coverage gap, not evidence of safety. Do not expect the current binary classifier to handle this dataset without changes.
9. Reuse Redis embedding caching under the project namespace. Verify the cache key separates embedding model/configuration and actual text. No global Redis flush is needed for these new files.
10. Record the selected collection and versions in an implementation report. Switch runtime configuration only as part of the separately authorized code rollout.

## Implementation handoff: evaluation

Run each held-out `instruction` through the future analyzer; compare its extracted actions and risk hypotheses with the stored annotations. Supply the judge with the original instruction and retrieved corpus evidence, **never this case's answer annotations**. No executable filesystem operations are required for semantic evaluation.

Measure action omissions/additions, compound-request handling, retrieval coverage per expected risk, individual-risk recall, benign false positives, uncertainty handling, and complete recovery of multiple risks. Report category-level counts, not just an aggregate accuracy. For uncertain cases, require uncertainty/review rather than counting either confident answer as correct. Distinguish semantic assessment from parser rejection and deterministic policy results.

Compare baseline, expanded retrieval only, judge only, and both stages on the same versioned inputs. Store queries, filters, retrieved IDs/scores, missing coverage, model/prompt/schema versions, validated judge findings, timing, errors, and final aggregation. Treat the old similarity thresholds as baseline settings, not calibrated v3 thresholds. Record failures instead of hiding them through offline fallback.

## Existing data: keep, replace, or archive?

Leave all existing files in place during implementation. They are needed to reproduce previous runs, and some are referenced by code/tests/docs.

| Existing file | Observed contents / usage | Recommended treatment |
| --- | --- | --- |
| `../semantic_advisory_examples.json` | 20 examples; current runtime corpus in `semantic_corpus.py` | Keep as v2 baseline. Some labels conflate writing risky text with requesting execution; do not blindly import them into v3. |
| `../semantic_action_validation_cases.json` | 20 cases; current calibration command and cloud-vector tests | Keep for v2 reproducibility. Do not claim this is a new independent v3 holdout. |
| `../semantic_e2e_smoke_cases.json` | 16 smoke scenarios | Keep for existing integration smoke runs; audit expected labels when v3 is introduced. |
| `../semantic_action_calibration_cases.json` | 1,024 correlated variants from 32 seeds | Retain as historical synthetic stress data; do not seed it or use it to claim independent coverage or choose v3 thresholds. Archive only after references are checked in the implementation change. |
| `../semantic_calibration_cases.json` | 8 text cases referenced by offline tests | Keep for legacy/offline regression coverage, not online calibration. |
| `../sample_requests.json`, `../sample_outputs.json` | Both zero-byte placeholders, not valid JSON | Populate with actual documented examples or remove in a later cleanup; they are not datasets. |
| `../../reports/semantic_calibration_v2.json`, `../../reports/semantic_regression_v2.json` | Historical measured outputs | Preserve as reports tied to v2. Never seed report outputs as corpus examples. |

The future source of truth is this package's reviewed corpus for retrieval and its separate evaluation file for held-out checks. Historical reports and integration fixtures serve different purposes and should not be merged into one dataset.

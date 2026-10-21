# Dataset and retrieval corpus review — 2026-10-11

## Assessment

The 56-case evaluation suite is useful for development regressions and small
model comparisons. It is not sufficient to establish reliable production
accuracy, calibrated risk thresholds, or an independently measured quality gain.
This was an assistant review of local data and code; it is neither independent
human label approval nor a live provider/retrieval experiment.

## Findings and corrections

| Finding | Why it matters | Action |
| --- | --- | --- |
| `fresh-005` marked a wildcard deletion uncertain solely because count was unknown | The current taxonomy explicitly includes wildcard selection in mass deletion | Changed to `applies`/`risky`; retained the unknown count as context |
| `fresh-044` relied on `confidential-pricing.csv` to establish sensitivity | Filenames alone should not establish sensitive contents | Added explicit confidential-content facts and grounded the evidence excerpt in them |
| `deletion-08` inferred a delete-directory action from “clean out” | A retrieval example should not teach that an ambiguous verb has one certain operation | Candidate makes directory deletion explicit while retaining scope uncertainty |
| `bypass-01` and `bypass-07` inferred directory type from extensionless paths | Extensionless resources can be files | Candidate states directory creation explicitly |
| `export-03` assumed an external vendor was unauthorized | External recipients can be authorized | Candidate explicitly states missing authorization |
| `credentials-04` inferred a preserved authorized audience from “vault”/“rotation” | A path name and purpose are not access control evidence | Candidate explicitly stipulates identical authorized administrators |

The seven-category taxonomy is unchanged. The evaluation now contains 24 benign,
20 risky and 12 uncertain cases. All labels remain pending human review.

## Remaining limitations

- Several scenarios resemble existing retrieval mechanisms and wording. Exact
  duplicate checks do not prove semantic independence. The evaluated suite was
  consulted during corpus improvement and is now explicitly a development suite.
- Per-category sample sizes are small, especially uncertainty (mass deletion has
  only one uncertain case after correction). A perfect score can result from very
  few observations; report counts, failures and denominators rather than claiming
  an established rate.
- The prose often supplies explicit cues such as confidential, unauthorized or
  unknown. Real requests can be shorter, indirect, misspelled or incomplete. The
  candidate broadens mechanisms but does not establish real-world distribution
  coverage or multilingual performance.
- Risk annotations are partial. Absence of a category is not a reviewed negative
  label. Unannotated model findings require review rather than automatic penalties.
- The current semantic dataset schema requires at least one supported action.
  It cannot honestly represent zero-action chatter or unsupported-only requests.
  Those need a separate rejection fixture or an explicitly designed schema
  extension; fake action labels must not be inserted to force compatibility.
- The existing evaluator does not consume the new parser-expectations sidecar or
  select arbitrary dataset files. Runner integration remains necessary before
  claiming end-to-end evaluation of these exact assertions.
- Resource authorization and policy outcomes depend on fixtures; semantic labels
  are not ALLOWED/DENIED labels. Network faults, reviewer approval, execution races
  and token replay require integration tests outside this dataset.

## Candidate corpus improvement

The candidate has 96 examples (44 risky, 37 benign, 15 uncertain): the existing
64 with five corrections plus 32 authored additions. The additions cover audience
versus path names, pseudonymization, token lifecycle/redaction, move collisions,
retention obligations, action-bound approval, scope versus byte size/recoverability,
quoted versus operative instructions, multiple risks and multiple uncertainties.

Each risk category has applicable, non-applicable and uncertain findings as well
as examples in all three global-label retrieval buckets. This matches the current
retriever's label-separated searches. It does not prove useful examples will
rank highly or survive the judge's candidate limit. The global label must not
replace the per-category findings when an example contains several risks.

The candidate is at the former `semantic_v3_candidate` source (now consolidated into `examples/semantic_v3/corpus.json`; historical snapshots are in MongoDB test evidence); the active
`examples/semantic_v3/corpus.json` is unchanged. No Qdrant, Redis, credentials,
routing or runtime behavior was modified. This matters because retrieval filters
against the active corpus fingerprint; changing that file without coordinated
seeding would make existing points ineligible.

## Verification and next measurement

Both local validators check Pydantic invariants and exact overlap with existing
v3 evaluation/retrieval examples as appropriate. The candidate validator also
checks the original fingerprint, declared revised IDs, added IDs, review states,
label/category coverage, and its manifest. Character similarity lists are review
aids only. No claim is made that all historical data files are semantically
disjoint from these examples.

Before activation, review the proposed labels and compare the existing versus
candidate corpus in isolated cloud collections under identical embeddings, model
routes, prompts and retrieval budgets. Integrate explicit dataset selection and
parser assertion scoring into the runner. Record all candidates, relevant-evidence
coverage, final-risk misses/false flags, uncertainty, errors and candidate-budget
truncation. Use an independently authored/reviewed final holdout once development
choices are frozen. Corpus size alone is not a success criterion.

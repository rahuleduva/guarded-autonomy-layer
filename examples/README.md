# Data inventory

| Location | Role | Current use |
| --- | --- | --- |
| [semantic_v3/](semantic_v3/README.md) | Authoritative 100-example retrieval corpus, taxonomy, manifest and source history | Main multi-query semantic pipeline; `evaluation.json` inside it is historical evaluation input and never seeded |
| [semantic_evaluation_combined_v1/](semantic_evaluation_combined_v1/README.md) | Maintained 88-case evaluation suite | Full semantic evaluation with parser expectations |
| [semantic_evaluation_v1/](semantic_evaluation_v1/README.md) | 56-case source split included in the combined suite | Kept to verify the combined suite's lineage; not another retrieval corpus |
| `e2e/scenarios.json` | API scenario inputs and executable/coverage-gap registry | Common E2E runner |
| `semantic_advisory_examples.json` | Older baseline retrieval source | Still used by `semantic_corpus.py` and `policy_prompts`; do not remove while that pipeline is used |
| `semantic_action_validation_cases.json` | Older online classifier checks | Still used by `vector_setup.py` and cloud-vector tests |
| `semantic_calibration_cases.json` | Older deterministic fixture inputs | Still referenced by baseline tests; not evidence of current live accuracy |
| [legacy/](legacy/README.md) | Unused historical synthetic calibration/smoke data | Archived; never seed or count as current coverage |

The empty `sample_requests.json` and `sample_outputs.json` placeholders were
removed. Actual API examples are documented in the project README and runbooks.
Keep retrieval examples, evaluation answers and recorded results separate.

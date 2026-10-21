# Historical synthetic data

These files have no current runtime or test imports. They are preserved for
historical reproducibility, not as additional active corpora or benchmarks.

- `semantic_action_calibration_cases.json`: correlated generated variants from an earlier calibration exercise; do not use sample count as independent coverage.
- `semantic_e2e_smoke_cases.json`: earlier smoke expectations, superseded by the current scenario registry and evaluation suite.

Do not seed either file. Current retrieval data is `../semantic_v3/corpus.json`;
current semantic evaluation is `../semantic_evaluation_combined_v1/`. Historical
reports may refer to these files' former locations directly under `examples/`.

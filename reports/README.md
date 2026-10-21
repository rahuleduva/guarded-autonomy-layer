# Reports

Reports are grouped by what they measured. They come from different dates, code versions, providers, and test conditions; do not combine them into a single project pass rate.

| Folder | Contents |
| --- | --- |
| [`e2e/`](e2e/README.md) | Comprehensive historical API run, focused semantic/background scenarios, and infrastructure preflights. |
| [`semantic/`](semantic/README.md) | Semantic retrieval calibration, model-stage diagnostics, and pipeline evaluation outputs. |
| [`models/`](models/README.md) | LLM prompt benchmarks, structured-output compatibility probes, and provider smoke runs. |

The semantic evaluation suite itself lives under `examples/`; reports are outputs and diagnostics, not evaluation input data or retrieval examples. Check each report's `scope`, timestamp, provider/model, and limitations before using it as evidence. Some reports contain synthetic request text and database identifiers, so review before sharing publicly.

Current runs use the configured MongoDB collections as the authoritative result
store. Local `e2e/setup/` holds redacted setup metadata; `.e2e/<goal_id>/scratch/`
is the only working area for temporary diagnostic copies/logs. The latter is
ignored and is removed after final evidence verification. Historical report
files in the folders above remain explicitly indexed evidence, not active data.

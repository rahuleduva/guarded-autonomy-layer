# Semantic evaluation and diagnostics

## `calibration/`

Contains the v2 semantic calibration and regression snapshots. These are small historical Gemini/Qdrant measurements using synthetic cases. They record observations for the v2 similarity classifier; they do not establish a calibrated production threshold for the newer multi-query pipeline.

## `phase12/`

Contains Phase 12 pipeline artifacts: boundary checks, front-stage and judge smokes, baseline snapshots, selected development/held-out evaluations, single-case diagnostics, and the dated implementation status. Each file has its own scope. In particular, boundary checks use controlled provider responses, model smokes are narrow probes, and historical held-out files may have since informed development.

For the current evaluation cases and invocation, see [`../../examples/semantic_evaluation_combined_v1/README.md`](../../examples/semantic_evaluation_combined_v1/README.md) and [`../../docs/testing/PHASE12_RUNBOOK.md`](../../docs/testing/PHASE12_RUNBOOK.md).

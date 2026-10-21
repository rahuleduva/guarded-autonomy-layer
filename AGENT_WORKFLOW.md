# Current development and evaluation workflow

Use the project README, current feature guides and `docs/testing/E2E_RUNBOOK.md`.
The original AI-assisted submission narrative is preserved separately in
[history](docs/history/ai_workflow_submission_20261007.md).

## Working boundaries

- Use the configured live PostgreSQL and cloud infrastructure for this work. Do not create or run SQLite databases.
- Ask before architectural changes. Ask the user to select Luna for long execution/monitoring and Sol for analysis or implementation, and wait for confirmation.
- Keep the authoritative retrieval source at `examples/semantic_v3/corpus.json` and its main Qdrant collection at `semantic_advisory_v3`. Never seed evaluation instructions or answers.
- Keep the still-used `policy_prompts` baseline and unrelated cloud collections intact. Do not create collections for every test cycle.
- Prefer the common E2E suite and Mongo-backed semantic evaluator for new runs. Historical helpers/runbooks are references, not default execution instructions.
- Store results and full redacted evidence in the existing `guarded-autonomy-layers` MongoDB collections. MongoDB is test history, not application configuration.
- Put public setup snapshots/manifests in `reports/e2e/setup/<run_id>/`; keep private credentials and resumable inputs in ignored `.e2e/<run_id>/`.
- Do not use the local filesystem as a scratchpad. Stream test/process logs directly to `guarded-autonomy-layer-e2e-scratch-logs` in MongoDB; keep diagnostic results in the existing MongoDB evidence collection or in memory. Maintained source files, setup metadata and private operational credentials have their designated locations above.
- Executions modify simulated database resources. Preserve real policies, historical records and infrastructure; do not reset shared state to force a passing result.

## Completion and cleanup

Before declaring the goal complete, verify MongoDB reports/evidence, compare the
actual live outcomes, and disclose remaining coverage gaps. Then remove local
scratch copies and obsolete experimental data after their useful evidence is
archived and verified. Retain maintained source data, separate evaluation data,
reusable tools, current docs and explicitly indexed historical evidence.
Private resumable credentials are operational state, not disposable scratch;
identify ownership before retiring them. Do not delete unrelated user files.

# Current live E2E suite

This suite uses PostgreSQL, Qdrant Cloud, Redis, CloudAMQP and the configured online models. It never uses SQLite. Executions change database-backed simulated resources, not host files.

The first implementation has **63 executable scenarios and 66 registered coverage gaps**. An executable scenario is not a claim that it has passed live. The recovery/model profiles currently expose planned cases as `NOT RUN`; they do not inject faults or publish accuracy measurements. See [the registry](../../examples/e2e/scenarios.json) for individual inputs, expectations and implementation status, and [the full plan](../plans/e2e_suite_plan.md) for remaining work.

## 1. Inspect the available checks

From the repository root:

```bash
python -m scripts.e2e.run --validate
python -m scripts.e2e.registry --check
python -m scripts.e2e.run --profile core --list-cases
python -m scripts.e2e.review --list-cases
```

These commands do not contact infrastructure. `--validate` checks assigned routes and handler names; it does not prove that every route has successful live coverage.

## 2. Start the configured processes

Use the normal application settings from `.env` and `config/llm_routing.yaml`. Set `OFFLINE_MODE=false`. The semantic acceptance cases require `SEMANTIC_PIPELINE_MODE=review`; background acceptance requires `BACKGROUND_WORKFLOWS_ENABLED=true`. Workflow submissions check dispatcher/worker heartbeats before queueing work; stale or missing heartbeats block dependent cases. See [background setup](../guides/background_processing.md) for worker/dispatcher commands.

```bash
python -m uvicorn src.main:app --host 127.0.0.1 --port 8000
```

Run the API, dispatcher and worker from the same code and configuration. The suite verifies running routes, caller bindings and background diagnostics. The API does not expose a complete configuration fingerprint, so local configuration alone cannot prove which model/settings a worker loaded.

## 3. Plan initialization

Choose a fresh run ID for each independent acceptance run. Reusing a run ID resumes its existing fixtures and requests.

```bash
python -m scripts.e2e.initialize --run-id acceptance-001 --plan
```

Read `reports/e2e/setup/acceptance-001/setup-plan.json` and `preflight.json`. Expect:

- Configuration: online routing and required credential settings are present.
- PostgreSQL: committed migration heads match; one active policy; stored policy hashes match.
- API: health responds; after provisioning, authenticated OpenAPI matches local routes.
- Qdrant: expected baseline and eligible v3 point IDs, payload fingerprints and cosine vectors match. Missing points are reported separately from incompatible collections.
- Redis: PING succeeds.
- CloudAMQP: TLS authentication/connect succeeds without declaring a queue or publishing a message.
- Worker readiness: initially `NOT RUN`; a background API roundtrip establishes actual delivery.

A missing migration blocks setup. Apply migrations separately through the normal reviewed process. The initializer does not create policies on an empty database; normal API startup owns policy bootstrap.

## 4. Provision and verify dedicated callers

```bash
python -m scripts.e2e.initialize --run-id acceptance-001 --apply
python -m scripts.e2e.initialize --run-id acceptance-001 --verify
```

The initializer creates an ordinary agent, another ownership agent, read-only and administrative agents, an execution agent, two reviewers and a limited human where the policy supports those roles. It uses existing validated principal/credential contracts. It never sets autonomy directly, rotates shared credentials, changes the active policy or edits `.env`.

Credentials live in ignored `.e2e/acceptance-001/credentials.json` with mode 0600. The public manifest has only identity bindings. Local locks and PostgreSQL locks serialize setup. Generated credentials are saved before the short database write so interrupted setup can resume. Disabled, changed or revoked fixtures stop setup instead of being silently repaired.

If a compatible corpus revision is incomplete, inspect the missing/mismatched IDs first. Explicitly repair using:

```bash
python -m scripts.e2e.initialize --run-id acceptance-001 --apply --seed-corpus
```

This reuses the application's seeders. A complete corpus makes no embedding calls. Repair currently reseeds the affected revision through its canonical seeder; it does not optimize embeddings to only missing points. A vector mismatch blocks rather than deleting or recreating a collection. Evaluation cases are never seeded.

## 5. Run a small profile, then a separate core run

```bash
python -m scripts.e2e.run \
  --manifest reports/e2e/setup/acceptance-001/manifest.json \
  --profile smoke
```

Smoke includes authentication, a compound parser rejection, a sandbox denial, multiple semantic risks, human rejection and background completion. It uses live providers. It does not validate execution or crash recovery.

For the broader executable set, initialize a fresh run first:

```bash
python -m scripts.e2e.initialize --run-id core-001 --plan
python -m scripts.e2e.initialize --run-id core-001 --apply
python -m scripts.e2e.run \
  --manifest reports/e2e/setup/core-001/manifest.json \
  --profile core
```

Core covers all seven parser action classes, clarification, policy boundaries, all seven semantic risk categories, benign quoted content, combined risk findings, human approval/rejection, note/fingerprint validation, duplicate submissions, background ownership, replay, and an earned-autonomy resource lifecycle. The lifecycle trains six action classes using real proposals and reviewer-controlled promotions, then executes create-directory/create/read/update/move/delete and compensates the deletion. It may require dozens of model calls depending on thresholds. A threshold above 25/action blocks that scenario rather than silently making an unbounded run.

A model misunderstanding or incomplete analysis can fail these expectations. The runner records it as a failure; it does not change prompts, thresholds, data or policies to force success.

## 6. Select a group or case

```bash
python -m scripts.e2e.auth \
  --manifest reports/e2e/setup/acceptance-001/manifest.json
python -m scripts.e2e.run \
  --manifest reports/e2e/setup/acceptance-001/manifest.json \
  --case AUTH-004
```

Group modules share the same runner and support `--case`, `--manifest`, `--list-cases` and `--timeout-seconds`. Explicit dependencies run first. A failed prerequisite blocks dependent cases while independent cases continue. Unknown IDs are errors. A selected unimplemented case produces `NOT RUN` and exit code 2.

Passing cases are saved privately and skipped on resume, with a link to their original evidence. Use a fresh run ID to genuinely repeat them. Failed cases retain stable inputs/idempotency keys; inspect evidence before resuming. A reused manifest cannot silently change API URL, policy pointers, registry, source-code fingerprint or local data/model settings.

## 7. Read the results

When MongoDB is configured, each invocation writes its summary and full redacted
HTTP evidence into `guarded-autonomy-layers-e2e-runs` and
`guarded-autonomy-layers-e2e-evidence`. Run IDs are `<run_id>/<invocation_id>`;
case/event IDs link the summary to its evidence. Configured MongoDB failure stops
the run rather than silently writing an unexpected local report.

Without MongoDB configuration, explicit file reporting uses:

```text
reports/e2e/comprehensive/<run_id>/<invocation_id>/  # full core profile
reports/e2e/narrow/<run_id>/<invocation_id>/        # smoke, group or selected cases
```

Setup metadata is always separate in `reports/e2e/setup/<run_id>/`.
Private resumable inputs/credentials remain in ignored `.e2e/<run_id>/`.
Keep diagnostic results in MongoDB or memory. Stream console output with
`scripts.e2e.mongo_log_runner` to `guarded-autonomy-layer-e2e-scratch-logs`;
do not create local scratch or log files for new runs.

The results include independent expectations, actual outcomes, HTTP exchanges,
timing, correlation IDs and durable record IDs. Credentials and capabilities are
redacted in published evidence. Private mutation inputs may retain capabilities
for resumption and must not be published. Neither setup preflights nor historical
reports establish that the current code has passed a full run.

- `PASS`: the case ran and every assertion passed.
- `FAIL`: an executed expectation or call failed.
- `BLOCKED`: a prerequisite or required configuration prevented execution.
- `SKIPPED`: this exact run already has passing evidence for the case.
- `NOT RUN`: unselected or still awaiting implementation.

Exit code 0 means selected cases passed or had prior passing evidence; 1 means an executed failure; 2 means selected coverage is incomplete. Unselected gaps remain visible and are never counted as passes. Detailed failures preserve exception text and traceback after credential redaction.

## Remaining implementation

The current registered gaps include deterministic retry/fallback fault fixtures, cache failures/expiry, two-reviewer and execution races, policy-promotion isolation, worker crash recovery, transaction inspection under slow calls, complete audit linkage and published dataset accuracy metrics. Those remain planned; the existence of a registry entry does not establish coverage. Recovery fixtures must use an isolated database/process set before any service interruption or policy pointer changes.

Run-owned principals, pending reviews and simulated resources remain after a run for audit. No automatic shared cache flush, queue purge, policy reset or historical-data deletion occurs.

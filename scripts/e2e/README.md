# Live E2E tools

Start with the [current runbook](../../docs/testing/E2E_RUNBOOK.md). The common initializer, registry and runner are implemented; advanced recovery/model checks remain registered gaps.

```bash
python -m scripts.e2e.run --validate
python -m scripts.e2e.initialize --run-id acceptance-001 --plan
python -m scripts.e2e.initialize --run-id acceptance-001 --apply
python -m scripts.e2e.run --manifest reports/e2e/setup/acceptance-001/manifest.json --profile smoke
```

`auth`, `parsing`, `semantic`, `policy_autonomy`, `review`, `execution`, `rollback_replay`, `background`, `recovery` and `audit` are group entry points over the common runner. Each supports `--help` and `--list-cases` without infrastructure calls. The scenario definitions are in [examples/e2e/scenarios.json](../../examples/e2e/scenarios.json).

| Tool | Purpose |
| --- | --- |
| `initialize.py` / `fixtures.py` | Inspect live infrastructure and provision resumable run-owned credentials |
| `registry.py` | Static route inventory, case IDs and coverage gaps |
| `run.py` / `scenarios.py` | Execute selected profiles and API assertions |
| `mongo_log_runner.py` | Run a command while streaming redacted console output directly into MongoDB; creates no local log file |
| `common.py` | Stable inputs, authenticated requests and bounded background polling |
| `evidence.py` / `reporting.py` | Read-only state inspection, credential redaction and atomic reports |
| `auth_live_smoke.py`, `background_live_e2e.py`, `phase12_online_e2e.py` | Earlier standalone focused checks; retained with their original credential interface |
| `api_polling.py` | Shared helper used by the earlier tools |
| `legacy/online_e2e_session.py` | Historical interactive helper |

Earlier standalone checks have not yet been replaced with compatibility wrappers. Prefer the common suite for new reports. Historical evidence remains unchanged; the [full plan](../../docs/plans/e2e_suite_plan.md) describes later stages.

### MongoDB run history

When `MONGODB_CONNECTION_STRING` is set, the common runner saves run results and
HTTP evidence with PyMongo's `AsyncMongoClient`. The synchronous scenario runner
uses one managed event loop for this client's lifetime. Configure
`MONGODB_DATABASE` (default `guarded_autonomy`) and `MONGODB_COLLECTION_PREFIX`
(default `guarded-autonomy-layers`; every configured prefix must start with it).

- `guarded-autonomy-layers-e2e-runs`: one document per run/invocation, updated
  after each scenario and at completion, including counts and the manifest.
- `guarded-autonomy-layers-e2e-evidence`: individual recorded HTTP events,
  identified by run, invocation, case and event index. Case records contain
  MongoDB evidence references. Prompts, responses and validation details retain
  the same content as file reports, with the existing credential redaction.
- `guarded-autonomy-layer-e2e-scratch-logs`: redacted process output, stored as
  ordered line documents plus a run metadata document with a verified digest.
  Use `python -m scripts.e2e.mongo_log_runner --run-id <run> --invocation-id
  <invocation> -- <command...>` for live commands. The wrapper streams output to
  the terminal and MongoDB without creating a local log file.

The runner prints a MongoDB report reference and does not create `REPORT.md`,
`results.json` or evidence JSON files in this mode. Initializer manifests under `reports/e2e/setup/<run_id>/` and
private `.e2e` credentials/resume state still use files. Older reports remain
available; they are not automatically uploaded or deleted. Without a connection
string, the existing file reporting remains available.

The MongoDB user needs permission to read/write these collections and create
indexes. An unavailable configured MongoDB blocks the run before any scenario;
a later reporting error stops the run rather than silently discarding evidence.
MongoDB URI credentials containing reserved characters must be URL-encoded.

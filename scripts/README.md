# Script inventory

Run commands from the repository root. Prefer `PYTHONPATH=. python scripts/<folder>/<script>.py`; package entry points such as `python -m scripts.e2e.background_live_e2e` also resolve imports from the root. Check each script's own prerequisites. Some diagnostics make live calls immediately and do not offer `--help`.

| Folder | Tools |
| --- | --- |
| [e2e/](e2e/README.md) | Focused live API tests, shared operation polling, and `mongo_log_runner.py` for log streaming without local files; older interactive helper under `e2e/legacy/` |
| [seeding/](seeding/) | `seed_e2e_auth.py`: provisions one test agent and reviewer; writes private E2E credentials into `.env` |
| [diagnostics/](diagnostics/) | Queue connectivity, provider/semantic probes, decision inspection and request-attempt monitoring |
| [benchmarks/](benchmarks/) | `benchmark_llm_prompts.py`: model smoke comparisons; `archive_semantic_evaluation.py`: full semantic evaluations with MongoDB checkpoints and verified archival |
| [datasets/](datasets/) | Local corpus and evaluation dataset validators; their explicit manifest-writing options may update dataset metadata |

Examples:

```bash
python -m scripts.e2e.background_live_e2e --list-cases
python -m scripts.benchmarks.benchmark_llm_prompts --list-cases
PYTHONPATH=. python scripts/diagnostics/monitor_attempts.py --hours 24
```

Seeding remains distributed between local auth administration, API policy bootstrap and the explicit corpus commands in `src.cli`. The [current E2E runbook](../docs/testing/E2E_RUNBOOK.md) documents the new common initializer and runner. `scripts.e2e.initialize` orchestrates run-owned identities and corpus verification; the older seeders remain available. Advanced recovery and model-evaluation scenarios are still explicit coverage gaps.

For the 88-case service-level risk evaluation, use a fresh run/invocation identity
and stream console output to MongoDB rather than a local file:

```bash
python -m scripts.e2e.mongo_log_runner \
  --run-id risk-quality-example --invocation-id baseline -- \
  python -m scripts.benchmarks.archive_semantic_evaluation \
  --suite semantic-combined-v1 --run-id risk-quality-example --invocation-id baseline
```

This requires online provider settings and MongoDB. It saves each completed case,
including the existing parser/judge attempt traces and correction feedback,
in the existing prefixed E2E evidence collection and the final summary in the run
collection, tagged `scope=service-level`, `test_type=risk-quality`, and
`profile=semantic-evaluation`. It evaluates parsing and semantic assessment;
it does not execute actions or exercise HTTP authentication/human-review flows.
Use `--snapshot` to attach an initializer setup plan captured before the run.
To archive an existing CLI JSON report without rerunning model calls, replace
`--suite ...` with `--input /path/to/report.json`. Corpus seeding is separate.

For an interrupted invocation, keep its ID intact and choose a fresh destination:

```bash
python -m scripts.benchmarks.archive_semantic_evaluation \
  --suite semantic-combined-v1 --run-id risk-quality-example \
  --invocation-id continuation --resume-from baseline \
  --snapshot /path/to/current/setup-plan.json --resume-check
```

`--resume-check` verifies saved evidence and configuration without model calls or
creating the destination report. Remove it to continue. Completed cases, including
failed checks, are reused; only uncompleted cases make new model calls. An interrupted
in-flight call may repeat. Configuration, corpus and dataset changes block resumption.
A changed code hash requires a reviewed `--resume-code-change-note`, recorded with
both code hashes in the continuation summary. Use this only after confirming the
changes preserve comparability, such as adding resume/reporting support. The original
invocation and its unfinished attempt evidence remain available in MongoDB.

Use `--case-id fresh-006` with a fresh invocation ID to repeat one selected case.
These reports are tagged `category=narrow`; the dataset total remains visible but
the selected/attempted count is one. The same evidence checkpointing and model
traces are recorded. A targeted repetition cannot be combined with resumption.

## Working files and cleanup

Use `reports/e2e/setup/<run_id>/` for redacted initialization metadata and
ignored `.e2e/<run_id>/` for private resumable inputs. Store console logs in the
MongoDB scratch-log collection, not local files. Keep diagnostic results in memory
or the existing MongoDB evidence collection. The goal's older scratch folder was
archived, verified and removed; do not recreate it. Do not create a new corpus folder or collection
for each run: the maintained source is
[examples/semantic_v3/corpus.json](../examples/semantic_v3/corpus.json), validated by
`python -m scripts.datasets.validate_semantic_corpus`.

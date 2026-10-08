# Guarded autonomy layer

The backend evaluates structured actions or prompts, records active and shadow
decisions, holds actions for review, promotes policies and agents from recorded
evidence, and issues single-use capabilities for database-backed simulated
execution. Compensation goes through the same decision pipeline. Replay uses
the historical policy and frozen inputs without changing state.

Use the project's activated Python environment:

```sh
python -m pip install -r requirements.txt
python -m pytest -q
python -m src.cli run-all-scenarios
```

The scenario command always uses migrated temporary SQLite databases and offline
advisors. It does not change the configured database or call cloud providers.

Start the API after applying migrations to the intended database:

```sh
python -m src.cli init-db
uvicorn src.main:app --reload
```

Interactive API documentation is at `/docs`. Routes under `/api/v1` include
`actions/evaluate`, `actions/evaluate-prompt`, `actions/execute`,
`actions/rollback/{execution_id}`, `control/approve/{decision_id}`,
`control/promote-policy`, `control/promote-agent`, `escalations`, and
`replay/{decision_id}`. Evaluation returns `decision_id`; only `ALLOWED` returns
a token. Approval produces a separate decision under a new policy version; the
original held decision remains replayable. A review cannot override role, scope,
regex denials, SHADOW mode, or a null policy TTL.

For a keyless local API, set both `OFFLINE_MODE=true` and
`DATABASE_URL=sqlite:///./guarded_autonomy.db`. An explicit database URL takes
precedence over the offline default. The offline prompt grammar accepts, for
example, `create file notes.txt with content hello in development` or a JSON
action containing `action_class`, `target_resource`, `parameters`, and
`environment`. Ambiguous prompts are rejected.

Provider packages are included in `requirements.txt`. Gemini uses `google-genai`
with native JSON-schema output; Groq uses the `openai` client with JSON mode.
Both validate parsed actions with Pydantic. Gemini embeddings use `google-genai`.
Select `LLM_PROVIDER=gemini` or `groq`, with the corresponding API key and model
configuration. Select `EMBEDDING_PROVIDER=gemini` or `sentence_transformers`;
Sentence Transformers requires a previously cached model. Vector storage uses
only the configured HTTPS cloud Qdrant endpoint. Explicit online commands
`python -m src.cli seed-advisories` and `python -m src.cli calibrate-semantic`
seed 20 contrasting examples with operation metadata and report a suggested cutoff. Calibration never
changes configuration automatically. See [advisory details](docs/phase4_advisory.md).

Execution affects synthetic resources in the database, not the filesystem.
Paths use string containment without symlink resolution. Explicit offline mode uses
a pattern matcher; an unavailable online advisor requests review. The vector
threshold still requires representative calibration.
Reviewer identities are supplied by callers: the control routes are an internal
demonstration, not an authenticated reviewer portal. Optional online adapters
are tested with controlled responses; live provider availability is unverified.

# Request-attempt monitoring

`request_attempt_events` records sanitized operational metadata for each
`/api/v1/actions/evaluate` and `/api/v1/actions/evaluate-prompt` request. The
`X-Correlation-ID` response header identifies its events. Stages include
parsing, analysis, baseline similarity, retrieval, judging, autonomy, decision,
escalation, commit, and the final HTTP result. A combined natural-language call has one parser
duration and a separate analyzer outcome with no independently measured time.

The event write uses a separate transaction after evaluation finishes. A parser
rejection or an evaluation rollback therefore still has an attempt record. If
the monitoring database write fails, the application emits sanitized JSON log
lines and does not change the evaluation result. A complete database outage
cannot provide durable database monitoring; collect the application logs as a
second destination.

Events store stage, status, elapsed milliseconds, provider/model names,
exception class, HTTP status, and decision ID when one exists. Parser validation
failures also store `validation_errors` JSON. Each parser attempt stores a
`parser_trace` containing the original prompt, complete accumulated messages,
structured response, exact correction message, error message, attempt number,
and whether correction was scheduled. The next attempt's messages establish
which feedback was actually submitted. Successful responses are retained too;
a provider exception has a null response when no structured result was returned.
These details appear in persisted event rows and event logs. A direct service
call must explicitly begin and persist its monitoring context; API requests do
this automatically. The decision ledger remains the authority for replay.

Apply the migration before restarting FastAPI:

```sh
alembic upgrade head
```

Read a recent aggregate without exposing request contents:

```sh
PYTHONPATH=. python scripts/diagnostics/monitor_attempts.py --hours 24
```

The command exits with status 2 when the configured failure or fallback rate
exceeds its limit after at least `--min-attempts` requests. It can be used by
an external scheduler or alerting service. Defaults are starting signals, not
calibrated service objectives; tune them after observing real traffic.

## LLM routing and fallbacks

Edit `config/llm_routing.yaml` to assign named model profiles to
`request_parser`, `request_analyzer`, and `semantic_judge`. Providers in that
file select the LangChain adapter and name the environment variables holding
credentials and gateway URLs. Credentials remain in `.env`.

Each purpose lists models in attempt order and chooses a fallback policy. The
current `transient` policy advances on HTTP 408/429/5xx, connection errors, and
timeouts; it stops on authentication, invalid-input, and content-filter errors.
Schema and grounding failures use a separate output-correction budget. Each
output attempt has its own provider retry allowance; the YAML settings bound
both, along with the stage's elapsed time. Stage deadline exhaustion records
`stage_deadline_exceeded` and `budget_exhausted: stage_time`; provider timeouts
record `provider_unavailable`. The analyzer's saved
`fallback_from` remains an object identifying its first failed model; every
failed provider call also has a monitoring event. The judge records its actual
responding provider/model. Existing database JSON columns need no migration.

The routing file is validated at startup. Selected online providers must have
the required credentials and gateway URL. Restart FastAPI after editing routes.

## Unverified credential-status claims

The combined parser and standalone analyzer return material credential lifecycle
assertions in `analysis.result.credential_status_claims`. Each entry contains
`claim_type`, an exact `excerpt`, and `unknown_index` linking to the existing
`unknowns` list. The application contract fixes `source` to `caller`,
`verification_status` to `unverified`, and `affected_risk` to `credential_access`.
Model output cannot substitute `system` or `verified`. Every recorded claim
requires a credential-access retrieval hypothesis and a nonempty linked unknown.
Descriptions merely quoted in documentation or of synthetic placeholders should
not create lifecycle claims. Recognition remains a model task; this guard does
not guarantee that the analyzer detects every claim.

The judge cannot mark a claim's linked unknown resolved or irrelevant, omit the
credential-access finding, clear it as `does_not_apply`, or claim complete
coverage. These contradictions produce field-specific validation errors in the
existing correction loop. A status-dependent finding is uncertain; independently
established credential access can still apply while the separate status question
remains unresolved. Retrieved analogies cannot verify live credential status.
If correction attempts are exhausted, judgment becomes unavailable and the
existing incomplete-analysis handling requires review. In `review` mode this
affects policy evaluation; `observe` mode only records the proposed review flag.

No verification provider is implemented. A reviewer can authorize handling
despite uncertainty, but approval does not change the claim into verified data.
Historical analyses without these fields retain their previous replay contract.

Storage uses existing tables and JSON fields:

- `decision_records.evidence_json.semantic_pipeline.analysis.result.credential_status_claims`
  preserves claims and provenance with the judgment and summary.
- `escalation_queue.package_json.semantic_findings.analysis.result.credential_status_claims`
  carries the same frozen evidence to reviewers; review requirements show the
  caller's quote and the unresolved confirmation question.
- `request_attempt_events.validation_errors` and `parser_trace` preserve failed
  model responses, structured errors, accumulated messages, and corrective
  feedback for each attempt, including failures before a decision is saved.

This is an extension to existing JSON contracts; no database migration or new
table is required.

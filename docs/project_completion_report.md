# Guarded Autonomy Layer — Project Completion Report

**Status:** Completed as an assignment and portfolio implementation. This report
summarizes the system built and the evidence collected during development. It does
not claim production certification or complete scenario coverage.

## Project summary

The Guarded Autonomy Layer is a FastAPI control service placed between an AI agent
and the actions it proposes. It checks who is calling, what action is requested,
which resource it affects, what policy allows, and whether a person must review it.
Only a deterministic policy decision can authorize an action. Language models
interpret requests and assess risk; they do not grant permission.

The application simulates file and directory operations against resources stored
in PostgreSQL. It does not make changes to files on the host machine.

**Engineering focus:** constrained agentic workflows, evaluation-driven
development, deterministic authorization, layered guardrails, human oversight,
durable execution, cost-conscious model use, and traceable evidence.

**Technology:** Python, FastAPI, Pydantic, SQLModel/SQLAlchemy, Alembic,
PostgreSQL, LangChain, LangGraph, Qdrant Cloud, Redis, Celery, CloudAMQP/RabbitMQ,
and MongoDB. Chat and embedding providers are selected through configurable
adapters and profiles.

## Agentic engineering principles applied

The project applies many important principles of reliable agent engineering.
There is no universal checklist that would justify claiming that it implements
“all agentic principles.” Its specific contribution is a governed workflow that
uses models for interpretation and evidence assessment while keeping action
authorization under application control.

| Principle | How it appears in this project |
| --- | --- |
| Explicit task and output contracts | Supported action enums, Pydantic models, action-specific parameter rules, and source-grounding validators define acceptable output. |
| Bounded feedback loops | A LangGraph propose → validate → correct loop sends actionable errors and previous responses back to the model, within attempt and time limits. |
| Separation of responsibilities | Parsing/analysis proposes actions and risks; retrieval gathers comparisons; judgment assesses applicability; policy decides permission; execution checks a capability. |
| Grounded retrieval | Action and risk queries retrieve category-filtered examples. Candidates retain query provenance and are deduplicated before judgment. |
| Defense in depth | Authentication, ownership checks, schema validation, grounding, sandbox/RBAC rules, semantic checks, human review, and capability verification protect different boundaries. |
| Uncertainty and abstention | Missing facts, unavailable stages, unsupported or compound requests, and incomplete evidence produce explicit clarification, denial, or review outcomes. |
| Human oversight | Authenticated reviewers receive frozen evidence and explanations, and submit a scoped decision with a mandatory note and package fingerprint. |
| Controlled autonomy | SHADOW, ASSISTED, and LIVE restrict how recorded experience and human promotion affect the agent's ability to act. |
| Deterministic decision core | Policy evaluation uses explicit inputs without provider calls, randomness, or a clock read. Semantic output is recorded before it becomes policy evidence. |
| Durable state and idempotency | Operation records, an outbox, checkpoints, leases, guarded transitions, and request identities support background work and duplicate handling. |
| Observability and provenance | Attempt traces retain messages, responses, validation JSON, provider/model identity, errors, and decision links; evidence records identify corpus and schema/prompt versions. |
| Evaluation-driven iteration | Separate evaluation cases, exact parser expectations, category assessments, live API scenarios, and repeated problem cases expose both improvements and regressions. |
| Cost and resource controls | Redis embedding caching, model trials, smaller model inputs, output/token ceilings, stage deadlines, provider retry limits, and bounded retrieval constrain repeated work. |

These principles apply in two places. At runtime, the application performs
bounded model correction and evidence-based risk assessment. During development,
the work followed a human-directed, AI-assisted cycle: define expected behavior,
inspect failures, diagnose their cause, make a targeted correction, review it,
and rerun affected cases while retaining the original evidence.

LangGraph manages the correction stage's message state in memory. PostgreSQL
owns durable application operation state, stage checkpoints, and attempt traces;
this implementation does not configure a native LangGraph checkpoint backend.

The parser graph is a constrained agentic workflow. This project does not
implement a general autonomous planner, a collaborating multi-agent system,
automatic model training from reviewer feedback, or a formal safety proof.
Adding those features would not by itself improve the control service.

### What determinism means here

A fresh request can receive different model output or retrieval results as
providers and the corpus change. The whole online AI pipeline is therefore not
deterministic.

The deterministic boundary is the policy engine: the same validated request,
policy artifact, frozen advisory evidence, and autonomy snapshot produce the
same policy result. Replay reloads those recorded inputs and checks the outcome
without calling an LLM, generating new embeddings, or searching Qdrant again.
Supported historical evidence contracts are validated when replaying; this is
not a promise that arbitrary future code changes preserve every old result.

See the [policy engine](../src/services/policy_engine.py),
[replay implementation](../src/services/replay_engine.py), and
[correction workflow](../src/services/parser_workflow.py).

## What was built

### Request interpretation and validation

- Accepts either structured action JSON or a natural-language request.
- Supports seven action classes: `read_file`, `create_file`, `create_directory`,
  `update_file`, `move_file`, `delete_file`, and `delete_directory`.
- Uses Pydantic response models, exact source excerpts, action-specific validation,
  and path/content grounding checks to catch invented, missing, or altered fields.
- Uses a bounded LangGraph correction workflow. When output fails validation, the
  next attempt receives the previous conversation, field-level validation-error
  JSON, and concise correction instructions. Provider outages use a separate
  bounded retry allowance; they are not treated as malformed model output.
- Records provider/model attempts, validation failures, raw model output where
  available, and correction history in the existing attempt-event records.

### Semantic risk assessment

- Separates the semantic work into request analysis, retrieval, and judgment. The
  parser can combine action extraction and analysis for natural-language input;
  structured input is analyzed without changing the caller's submitted action.
- Builds an action query and additional risk-specific queries, searches Qdrant
  with metadata filters, combines and deduplicates candidates, then asks a
  structured judge to assess which risks apply.
- Uses risk definitions from the maintained taxonomy and validates findings,
  evidence excerpts, references, unknowns, and coverage after model output.
- Tracks claims that credentials are revoked or otherwise inactive as unverified
  when the system has no trusted verification. A caller's own claim cannot clear
  that uncertainty.
- Keeps `baseline`, `observe`, and `review` semantic modes. Semantic results can
  raise scrutiny or request human review; deterministic policy remains the
  authority for authorization.

### Retrieval and evaluation data

- Maintains one 100-example synthetic retrieval corpus in
  `examples/semantic_v3/corpus.json`, with risky, benign, and uncertain examples,
  category annotations, excerpts, and action metadata.
- Maintains a separate combined 88-case semantic evaluation suite: 32 historical
  cases plus 56 newer development cases. Evaluation cases are not seeded into
  Qdrant.
- Stores the v3 retrieval corpus in the `semantic_advisory_v3` Qdrant collection.
  Corpus labels remain pending human review; they are development evidence, not
  independently certified labels.

### Policy, review, execution, and audit

- Applies deterministic role, action, path-sandbox, environment, deny-pattern,
  risk, and autonomy checks.
- Supports SHADOW, ASSISTED, and LIVE autonomy progression, with recorded evidence
  and reviewer-controlled promotion where required.
- Creates a combined escalation package with risk findings and evidence. A
  reviewer must authenticate, act only on a ready package, provide a note, and
  submit the package fingerprint. Review records are linked to the decision.
- Finalizes approval from frozen evidence and current constraints without
  repeating model calls or retrieval, or unnecessarily bumping the policy
  version. Approval does not override hard restrictions, and reviewers do not
  receive execution tokens.
- Issues short-lived, single-use capability tokens separately from caller login
  credentials. An authorized agent must submit a separate execution request.
- Supports frozen-evidence replay, policy shadow comparisons, and guarded
  compensation that checks the resource has not changed since execution.
- Records decisions, interpretation, review, authentication, workflow, and model
  attempt history in PostgreSQL audit tables.

### Authentication and background operations

- Stores agent and human identities and permissions in PostgreSQL. Callers use
  revocable API keys; human reviewers may also log in with a password and receive
  a bearer JWT. API-key hashes are stored rather than plaintext keys.
- Uses a PostgreSQL outbox/dispatch record, CloudAMQP, Celery, and workers for
  configured long-running evaluation, interpretation, clarification, rollback,
  and review-package preparation. Clients receive an operation ID and poll for
  the result. Review decisions, promotions, and simulated execution remain
  synchronous.
- Keeps database transactions short around slow provider and broker work, with
  persisted operation states, checkpoints, idempotency, leases, and guarded
  transitions.
- Uses Redis for embedding-result caching and login throttling. Model/provider
  routing and embedding profiles are configured in `config/llm_routing.yaml`;
  secrets and endpoints belong in `.env`.

## Cost control and efficient model use

Cost was an explicit design and development concern. The work included testing
cheaper models, reducing repeated provider work, reviewing prompt sizes, and
limiting the scope of paid reruns while retaining validation and audit evidence.

### Runtime mechanisms

- **Redis query-embedding cache:** repeated identical retrieval text can reuse
  its vector instead of making another embedding API call. Keys include the
  project namespace, embedding specification, and a hash of the text; entries
  have a configurable TTL. Cached vectors are normalized and checked for the
  configured dimensions where available. Cache failure falls through to the
  embedding provider.
- **Configurable model selection:** purpose-specific routes allow parser,
  analyzer, judge, and review-explanation models to be selected independently.
  Trials included smaller/cheaper models and multiple gateways/providers, with
  the same application validation contracts. Routing follows configured order;
  the application does not automatically choose the cheapest model or optimize
  prices.
- **Prompt and payload reductions:** shared parser/analyzer instructions reduce
  duplicated prompt maintenance; prompts were reviewed for redundant wording.
  The judge receives selected candidate evidence while repeated search-match
  lists and transport metadata are omitted from its input. Selected candidates,
  findings, provenance, failures, and coverage context remain available; the
  complete retrieval record is retained for audit. Compact JSON also reduces
  serialization overhead in model inputs.
- **Bounded work:** configurable query/candidate limits, output-token ceilings,
  correction budgets, provider retries, and stage deadlines limit how many
  searches and model calls a request can start. SDK retries are disabled so they
  cannot silently multiply the application retry budget. These are work limits,
  not a strict currency budget or guaranteed cancellation of in-flight calls.
- **Evidence reuse:** decision replay uses frozen evidence with no fresh model
  or retrieval calls. Human approval finalization also reuses the recorded
  advisory evidence. Idempotent operations return or resume their existing work.

The Redis feature caches **embeddings of query/prompt text**. It is not a cache
of complete LLM answers or authorization decisions, and provider-side prompt
prefix caching was not explicitly integrated.

### Development practices

Saved model outputs were inspected and revalidated where useful before paying
for another request. Targeted failing cases and selected passing controls were
rerun instead of automatically repeating every full suite. The user also
directed the coding-assistant workflow to use Sol for analysis/review and cheaper
Luna execution settings for routine runs. That development-model choice is
separate from the application's configured model routing.

These mechanisms address avoidable work, but a measured dollar saving, overall
cache hit rate, and complete token/cost ledger were not established. Choosing a
cheaper model can increase retries or failures; the evaluation history therefore
matters alongside its advertised price.

See [embedding caching](../src/services/embeddings.py),
[judge input preparation](../src/services/semantic_judge.py), and
[routing/retry controls](guides/llm_routing.md).

## Corrections made during development

The system changed through repeated inspection of model traces, API responses,
database records, and live failures. The main correction themes were:

1. **Models sometimes changed or omitted actions.** Structured JSON-only requests
   initially could be analyzed as having no action. The analysis contract and
   validator now require the submitted supported action to remain represented.
2. **Paths and parameters needed stronger grounding.** Move destinations,
   content, environment names, recursion, and other action-specific fields are
   checked against the original request. Names such as `cache-note.txt.bak` are
   still treated as valid file paths. Unsupported fields cannot be filled in by
   the model to satisfy a schema.
3. **Retries needed distinct failure handling.** Invalid structured or grounded
   output now gets bounded correction attempts with machine-readable errors in
   the message history. Timeouts, rate limits, and connection/provider failures
   have their own retry/fallback handling and budgets.
4. **Structured-output support varied by provider.** The code moved provider
   differences into shared LangChain adapters and one Pydantic-derived schema.
   Provider-facing schema guidance is adjusted only where decoders reject
   constraints; the original Pydantic model still enforces application limits.
5. **Semantic retrieval needed contrasts and multiple risks.** The corpus was
   consolidated into one maintained source, evaluation cases were kept separate,
   risk/action metadata and benign/uncertain examples were added, and search
   combines results from action and risk queries.
6. **The judge needed evidence and uncertainty controls.** Risk definitions are
   supplied to the analysis/judgment stages. Post-model validators check quoted
   support, category references, findings, unknown dispositions, completeness,
   and unverified credential-status claims. Some legitimate risk dismissals do
   not require an unrelated benign corpus example; positive findings still need
   grounded support.
7. **Reviewer decisions needed explicit scope and audit.** Approval is tied to a
   ready combined package, exact fingerprint, and required note. Approval uses
   the original recorded evidence, while hard policy restrictions continue to
   apply. State-changing endpoints require authenticated roles and use
   idempotency/guarded transitions.
8. **Long provider calls could not hold database transactions open.** Work was
   split into short persistence steps around external calls. Idempotency,
   review races, lost responses, resource/policy changes, and recovery boundaries
   informed the implementation and scenario design; not every corresponding
   fault-injection scenario has an implemented live test.
9. **Test evidence needed a stable home.** E2E output and HTTP/model evidence are
   kept in the existing prefixed MongoDB collections, with redaction and
   read-back checks. A common live E2E initializer and runner now distinguish
   PASS, FAIL, BLOCKED, SKIPPED, and NOT RUN rather than silently treating
   unimplemented or unselected cases as passing.

## Evaluation methodology and metrics

Evaluation covered several questions rather than one overall “accuracy” number:

| Measure | What was checked or recorded | Measurement status |
| --- | --- | --- |
| Parser disposition | Whether the request should produce a supported action, require a split, or seek clarification/rejection | Exact expectations and case comparisons implemented. |
| Action preservation | Exact action class, target, destination/content parameters, and environment | Scored against separate parser expectations; answers are not sent to the model. |
| Risk applicability | Expected applies/does-not-apply/uncertain assessments by risk category | Per-case category matches, missed risks, additional risks, and flag matches recorded. |
| False alarms and missed risks | Benign requests unnecessarily held for review; risky requests left unflagged | Measured in historical comparisons, with the assessed-case denominators retained. |
| Uncertainty handling | Whether material unknowns and incomplete analysis retain review | Runtime rules, scenario assertions, and historical uncertainty outcomes recorded. |
| Retrieval coverage | Whether candidates include supporting examples for annotated applicable risks | Per-case coverage indicators recorded; not a comprehensive retrieval precision/recall benchmark. |
| Correction and provider recovery | Attempts, error type, feedback, fallback, and successful corrected outputs | Traces and selected recovery evidence exist; a general retry-recovery rate is not established. |
| Latency | Stage durations and case/diagnostic elapsed time | Recorded; diagnostic medians/p95 were compared, but are not controlled production API benchmarks. |
| Cost and token consumption | Configured output limits and provider-call budgets | Complete per-request token and monetary cost accounting is not implemented. |

The historical three-run comparison illustrates the evaluation process:

| Measure | Initial run | Cycle one | Cycle two |
| --- | ---: | ---: | ---: |
| Case PASS / FAIL / BLOCKED | 77 / 11 / 0 | 81 / 6 / 1 | 83 / 5 / 0 |
| Parser disposition matches | 87/88 | 87/88 | 88/88 |
| Exact action-detail matches among assessed cases | 77/79 | 78/78 | 79/79 |

In cycle two, 79 requests received semantic assessments: 26 risky, 36 benign,
and 17 uncertain. One risky request was unflagged, two benign requests escalated,
and all 17 uncertain requests retained review. The other nine evaluation cases
required clarification/rejection or splitting. An action already flagged for one
risk can still miss another category, so overall flag success is insufficient
to establish complete risk detection.

Recorded diagnostic median/p95 elapsed times changed from 22.68/38.87 seconds
in the initial run to 17.70/24.98 seconds in cycle two. Those measurements include
diagnostic arms and changing configurations. They do not isolate an optimization
or establish production latency. Case counts also hid regressions: cycle two
improved ten initially failing cases and regressed four initially passing cases.

The [measured comparison](plans/risk_quality_improvement.md#measured-comparison-and-remaining-work)
preserves the denominators, per-category results, source references, and caveats.
The [semantic evaluator](../src/services/semantic_evaluation.py) implements
parser and semantic scoring. The development examples informed corrections;
they are separate from retrieval data but are not an untouched independent
holdout or production benchmark.

## Verification evidence and limits

Runs used different code revisions, providers, fixtures, and retry settings.
Their numbers must not be combined into one pass rate.

| Evidence | Result | What it establishes |
| --- | --- | --- |
| Semantic cycle-two baseline, 88 cases | 83 PASS, 5 FAIL, 0 BLOCKED | A measured historical baseline for that revision. |
| Haiku semantic run, 88 cases | 82 PASS, 6 FAIL, 0 BLOCKED | A complete run for a later revision; one fewer pass overall, with different individual cases. It was not a uniform improvement. |
| Live HTTP core run, 63 implemented scenarios | 49 PASS, 14 FAIL | Exposed parser, semantic fixture/judgment, and review-header issues. The 66 unimplemented registry cases were outside this run. |
| Targeted reliability HTTP rerun, 14 cases | 14 PASS | Verified the selected fixes for those cases only. |
| Later checkpointed HTTP core | 33 passed before interruption; continuation recorded 19 PASS, 11 FAIL, and 33 SKIPPED | Outcomes exist for the 63 implemented scenarios across the checkpointed run; eleven still failed. The 66 registry gaps remain. |
| Latest direct Google Gemini HTTP run, 3 cases | 3 PASS, 0 FAIL, 0 BLOCKED | PARSE-005 exact move paths, SEM-009 benign quoted content (after one judge correction), and REVIEW-002 combined review with SHADOW execution blocked. MongoDB report and 61 HTTP evidence events were verified. Service log captures were interrupted during cleanup, and their stored lines passed read-back checks. |

The latest run used Google `gemini-3.5-flash-lite` for the four LLM purposes by
temporarily changing routing, then restoring it. It says nothing about the
entire 88-case semantic suite or 63-case HTTP suite. The current scenario registry
also contains 66 cases without implemented handlers. See the
[E2E evidence index](../reports/e2e/README.md) for run-by-run identifiers, failure
details, historical reports, and the distinct scope of each result.

The corpus and evaluation labels are synthetic and have not all received
independent human review. Thresholds and model behavior need broader evaluation
before making production-accuracy claims. The application implements a control
service and database-backed simulation; it does not demonstrate safe operation
against arbitrary live agent tools or real user files.

## Resume-ready description

**Guarded Autonomy Layer — AI Agent Policy and Risk Control Service**

Built a FastAPI control plane that validates AI-agent actions before execution,
combining deterministic policy enforcement with structured LLM parsing, grounded
multi-query Qdrant retrieval, semantic risk review, authenticated human approval,
and short-lived execution capabilities. Implemented PostgreSQL audit and
idempotent workflow state, Celery/CloudAMQP background processing, Redis
embedding caching, bounded LangGraph correction retries, and a MongoDB-backed
live E2E evidence suite.

Resume bullets:

- Engineered an AI-agent governance service with deterministic policy evaluation,
  authenticated human approval, single-use execution capabilities, and
  frozen-evidence decision replay.
- Implemented bounded LangGraph correction workflows with Pydantic grounding
  checks, structured validation feedback, configurable model fallback, and
  independent output-correction and provider-retry budgets.
- Built metadata-filtered, multi-query Qdrant retrieval over 100 synthetic
  examples and an 88-case evaluation suite measuring action preservation,
  risk-category matches, false alarms, missed risks, and uncertainty handling.
- Developed Celery/RabbitMQ background operations with a PostgreSQL outbox,
  short transactions, idempotency, checkpoints, Redis embedding caching, and
  MongoDB-backed evidence reporting for a 63-scenario executable HTTP suite.
- Implemented model-cost controls through reusable Redis embeddings, configurable
  purpose-based model routing, compact evidence inputs, bounded retries, and
  reuse of frozen evidence for replay and approval finalization.

**Interview framing:** “I built a governed agentic workflow and evaluated its
failure modes. Models interpret and assess risk; a deterministic policy core
controls permission. Validation feedback enables bounded correction, and frozen
evidence makes past decisions replayable. I tracked both recoveries and
regressions instead of treating a higher total pass count as proof of safety.”

Use scoped results when discussing performance. The 100/88/63 counts describe
implemented data and executable coverage; they are not accuracy percentages.
Historical broader runs and recent targeted runs have different limits, as
recorded above.

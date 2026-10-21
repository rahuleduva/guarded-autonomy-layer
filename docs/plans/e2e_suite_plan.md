# Current-system live E2E suite: implementation plan

Status: initial implementation underway; shared setup/registry/reporting and 63 executable scenarios are present. See [the current runbook](../testing/E2E_RUNBOOK.md) for implemented commands and remaining gaps. The design below describes the full target. Inspected against the repository on 2026-10-17. Some target commands and files below remain **proposed**; use the current runbook for actual availability. This document records intended coverage, not test results. No infrastructure was changed while writing it.

## 1. Objective and boundaries

Build one discoverable suite, divided into focused modules, covering the public API, durable state transitions, ownership, authorization, simulated execution, and recovery. Every scenario has a stable ID, explicit prerequisites, concrete input, expected HTTP/domain result, persisted evidence, and an independent report entry.

Run against PostgreSQL, Qdrant Cloud, Redis, CloudAMQP, and live configured model providers. No SQLite, offline mode, manufactured capability tokens, fabricated decision history, or manually granted LIVE autonomy. Use real HTTP for business actions; local administrative fixture creation and read-only database inspection are explicit exceptions. This plan adds testing infrastructure; it does not change application decisions or database schemas.

Finite scenarios cannot prove all possible behavior. Publish the exact coverage matrix and untested limitations. Keep workflow correctness separate from model accuracy across the evaluation datasets.

## 2. Existing initialization and testing inventory

| Current entry point | What it actually does | Limitation/reuse decision |
| --- | --- | --- |
| `python -m src.cli init-db` | Applies committed Alembic migrations through head | Migration command, not a data seeder; initializer verifies head and stops on mismatch |
| `src/services/policy_loader.py:seed_policy_artifacts()` | Validates artifact JSON under `policies/`; inserts absent policy hashes; bootstraps active 1.0.0 and shadow 1.1.0 on an empty table | Called by FastAPI startup; reuse service without resetting existing policy pointers |
| `python scripts/seeding/seed_e2e_auth.py` | Creates/reuses one dedicated agent and human reviewer, issues credentials, writes E2E settings into private `.env` | Agent has only `actions:evaluate` and `records:read_own`; insufficient for execution/rollback. It can also reenable/update its dedicated principals and rotate invalid credentials; do not treat it as a read-only check |
| `python -m src.auth_cli ...` | Local principal/password/API-key administration | Reuse underlying validated contracts and administration logic for fixture creation |
| `python -m src.cli seed-advisories` | Seeds baseline contrasting examples through `src/services/vector_setup.py` | Stable point IDs; collection chosen by current configuration |
| `python -m src.cli seed-advisories-v3` | Seeds eligible retrieval examples using `src/services/semantic_v3_corpus.py` | Validates corpus/evaluation separation, payloads, embedding compatibility; separate v3 collection |
| `scripts/diagnostics/phase12_preflight.py` | Basic API/DB/Qdrant/Redis inspection | Needs schema-head, routing, ownership, worker and corpus checks before becoming shared preflight |
| `scripts/diagnostics/background_queue_preflight.py` | TLS connection and declaration of the configured durable queue | Current script does not do a publish/consume round trip; declaration is a mutation |
| `scripts/e2e/auth_live_smoke.py` | Login/API-key/JWT and selected role boundaries | Reuse scenarios; currently prints assertions rather than producing a standard suite report |
| `scripts/e2e/phase12_online_e2e.py` | Three semantic API scenarios | Assumes workspace root, workspace role, and fresh SHADOW state; use manifest fixtures instead |
| `scripts/e2e/background_live_e2e.py` | Background roundtrip and concurrent identical submission | Does not exercise crash recovery, all background route kinds, or execution |
| `scripts/e2e/legacy/online_e2e_session.py` and old online runbooks | Broader interactive scenario guidance, with partial authentication/polling updates | Preserve useful cases, audit each against current code; not the authoritative current suite |

## 3. Proposed layout and common interface

Use `python -m scripts.e2e.<module>` from the repository root. Keep setup imports side-effect free. All entries support `--help`; scenario runners support `--manifest`, `--case`, and `--list-cases`.

```text
scripts/e2e/
  __init__.py
  initialize.py       # inspect, plan, apply missing fixture setup, verify
  common.py           # authenticated clients, bounded polling, assertions
  fixtures.py         # fixture contracts and provisioning orchestration
  evidence.py         # read-only PostgreSQL/Qdrant inspection
  reporting.py        # report schema, redaction and summary
  registry.py         # stable scenario IDs, dependencies and requirements
  run.py              # select groups, coordinate dependencies, aggregate reports
  auth.py
  parsing.py
  semantic.py
  policy_autonomy.py
  review.py
  execution.py
  rollback_replay.py
  background.py
  recovery.py
  audit.py
examples/e2e/         # versioned scenario inputs and explicit expectations
docs/testing/E2E_RUNBOOK.md   # authoritative execution and troubleshooting guide
```

Maintain a single scenario registry. Every public route in `src/main.py` must map to at least one success scenario and applicable authentication/ownership/error cases. Include health, OpenAPI and diagnostics, not only POST routes. New routes or state transitions require a coverage entry or an explicit gap.

Keep existing script names as documented compatibility entry points where practical. Migrate their scenarios to shared modules; do not leave two independently maintained versions. Update the README, authentication/background docs and reports index to point to the new runbook. Mark old broad documents historical and link their replacement; historical result files remain unchanged.

## 4. Common initializer: inspect → plan → apply → verify

One initializer is appropriate, but it orchestrates existing services rather than duplicating their business rules. PostgreSQL and Qdrant cannot share one atomic transaction: checkpoint each setup component and safely resume partial initialization.

1. **Inspect without mutation.** Require online PostgreSQL; compare database revision with Alembic heads dynamically. Check required tables/constraints, routing purposes and credentials, baseline/v3 corpus selection and fingerprints, Redis PING, API reachability, queue configuration and worker/dispatcher diagnostics. Separate local configuration from evidence of the running processes' configuration. Health alone does not establish readiness.
2. **Write a setup plan.** Record what exists, what is missing, what would be created, and what is incompatible. Validate all local corpus/policy/fixture contracts before writes. Missing migrations stop initialization; do not generate or apply migrations implicitly.
3. **Allocate identities per run and scenario.** Provision a workspace execution agent, a second ownership agent, a read-only agent, a destructive-action agent when the active policy supports its role, two human reviewers, and a limited-permission human. Provision separate fresh agent/action state for SHADOW, ASSISTED and LIVE scenarios. If the policy lacks a required role/action, mark dependent cases BLOCKED rather than changing policy permissions.
4. **Store credentials privately.** Use a private ignored `.e2e/<run_id>/credentials.json`, parent directory mode 0700 and file mode 0600, created atomically. Public manifest contains IDs and credential aliases only. Existing `.env` credentials may be explicitly imported for preflight, but never silently grant them broader permissions or reset passwords. A stable run ID resumes the same fixtures; a fresh run creates fresh identities. Lost credentials require an explicit fixture credential recovery/rotation step.
5. **Initialize policies.** On an empty approved test database reuse `seed_policy_artifacts()`. On an existing database validate stored artifacts and active pointer; preserve existing active/shadow choices and history. Successful candidate promotion and policy-change races require an isolated test database/process set: current qualification considers candidate lifetime history. Do not replace a shared candidate or promote a shared policy as ordinary setup.
6. **Initialize retrieval data.** Seed the configured baseline corpus and, for observe/review runs, the configured eligible v3 corpus into compatible collections. Reuse canonical point IDs/payloads and validate dimensions, cosine distance, indexes, corpus hash, embedding spec and eligibility. Skip verified complete revisions to avoid redundant provider work; repair only missing/mismatched expected points after describing the repair. Never delete/recreate a mismatched collection. Never seed evaluation/held-out cases. Record provisional data truthfully; never mark it reviewed automatically.
7. **Initialize Redis and broker namespaces.** Use a dedicated test Redis prefix and queue for the dedicated process set. Do not flush Redis or purge shared queues. Cache verification uses bounded sentinel keys with TTL; remove only owned sentinels. Queue declarations/publish probes are explicit setup actions; disposable probes cannot consume application work.
8. **Prepare resource fixtures through APIs.** Create/read/update/move fixtures through earned autonomy/review/execution workflows. Empty directories may require special preparation because directory deletion has a null capability TTL. Do not invent authorization evidence or seed successful execution records. A currently unreachable mutation path must be reported BLOCKED, with its blocking policy reason.
9. **Verify and freeze the manifest.** Include run ID, route/schema inventory, commit plus dirty-state indicator, policy hashes/pointers, process settings, routing/model IDs and retry budgets, corpus and dataset hashes, embedding spec, collection/queue/Redis namespace, fixture principal IDs and resource prefix. Verify identities through `/auth/me`. Record per-component completion so retries do not duplicate setup.

All password hashing and embedding/provider/broker calls happen outside database transactions. Only short validated setup writes commit together. Finish writes before verifying through remote APIs. Concurrent initialization for the same run must serialize fixture creation and respect uniqueness; never rotate valid credentials just because another initializer is running.

## 5. Scenario coverage matrix

Each numbered variant below becomes a separate registry case and result. Exact HTTP status and JSON field assertions must be taken from the current route/model contracts during implementation, not guessed from old reports. Where a desired invariant is unsupported, record a product gap; do not silently weaken the expectation.

| Group / proposed module | Required scenarios and expected invariants |
| --- | --- |
| INIT / `initialize` | Fresh setup; repeated same-run setup; interrupted setup/resume; concurrent setup; schema behind head; incompatible vectors; missing credentials; missing corpus eligibility; corrupted manifest; missing role. No duplicate principals, corpus points or policy pointer changes |
| AUTH / `auth` | Public health; authenticated OpenAPI/docs; absent/invalid/expired/revoked API key; valid agent/human keys; correct/incorrect password; human JWT; expired/revoked JWT; disabled principal; changed permissions; conflicting headers; spoofed agent/role/reviewer IDs; password validation does not echo credentials; login throttling and Redis-unavailable login; cross-agent reads/results denied; humans cannot execute agent capabilities; agents cannot approve/promote/read reviewer evidence. Dedicated credentials only for revocation tests |
| PARSE / `parsing` | All seven action classes; exact source/destination preservation; `.bak` filenames; relative/absolute paths; quoted instruction content; explicit environment; unsupported action; compound request; missing target/content/destination; ambiguous environment; clarification creation/read/answer; correction after clarification; duplicate/conflicting clarification; wrong owner; late answer to terminal turn. Rejections create PARSER-stage decision records with validation details and no executable action/token; transport/provider failure remains distinguishable from an invalid instruction |
| RETRY / `parsing`, `semantic` | Schema-invalid and grounding-invalid responses trigger corrective messages; complete prior assistant/feedback history reaches next attempt; correction budget exhaustion; transient provider retry budget per correction attempt; nonretryable refusal/error; ordered fallback and exhaustion for each routed purpose; successful recovery. Persist response, structured errors, prose, messages, provider and attempt linkage. Deterministic fault variants need isolated external fixtures; natural live first-attempt success cannot prove retry coverage |
| SEM / `semantic` | Benign examples and quoted risky content; each of seven risk categories; multiple risks; uncertain inputs; canonical plus risk queries; operation/cross-operation filters; all three labels; candidate deduplication and provenance; judge grounding and evidence references; coverage complete/incomplete; no eligible matches; partial query failure; analyzer/judge outage or invalid output; candidate/query budgets; baseline/observe/review behavior. Review mode holds applicable risks/uncertainty/incomplete analysis with explicit reasons; verify observe's incomplete-analysis handling against current implementation rather than assuming it only logs |
| CACHE / `semantic` | First query miss and repeated identical query hit; provider/model/spec isolation; malformed/wrong-dimension cached value; unavailable Redis; TTL expiration. Valid cache reuse preserves vectors; unavailable cache falls through to provider. Use own namespace; never clear the shared cache |
| POLICY / `policy_autonomy` | Role permitted/denied; unknown action; sandbox traversal and sibling-prefix escape; destination escape; regex content denial; destructive production escalation; blast radius; null TTL; each allowed risk/TTL mapping; precedence when several gates apply. Forbidden actions never gain authorization through autonomy or human approval |
| AUTONOMY / `policy_autonomy` | Fresh SHADOW; threshold boundary SHADOW→ASSISTED; pending approval; premature LIVE promotion; reviewer decline and earned approval; action-specific independence; violation streak reset; duplicate requests and post-review authorization do not inflate clean-run evidence; concurrent proposals/promotions. Use actual ledger requests, never direct mode writes |
| POLICY-PROMOTION / `policy_autonomy` | Candidate absent; below threshold; qualifying candidate with no violations; decline; approval; candidate with lifetime violation; required idempotency header; identical retry; conflicting retry; concurrent promotion; exactly one active policy and immutable old decisions. Pointer-changing cases run only in the isolated profile |
| REVIEW / `review` | PREPARING cannot be decided; prepared package has complete explanation/fingerprint; wrong fingerprint; missing/blank approval note; reviewer spoofing; approve/reject; approved allowed/blocked; SHADOW approval cannot invent LIVE; null TTL remains blocked; no policy-version bump; original decision unchanged; linked authorization audit; duplicate exact retry vs different second review; two-reviewer race; agent discovers status by original decision ID; reviewer sees no capability; agent gets original capability without renewal. Approval/rejection invoke no new cloud analysis |
| EXEC / `execution` | Every reachable simulated action mutation; correct resource before/after snapshots; absent source; existing target; directory-kind conflict; nonempty directory; consumed/expired/malformed token; token bound to another decision/action/policy; denied/held/parser decision cannot execute; cross-agent ownership; identical retry; changed payload conflict; concurrent submissions with same and different keys; response lost after commit. One resource mutation/execution/nonce consumption; cached success does not extend expiry |
| ROLLBACK / `rollback_replay` | Missing/wrong-owner execution; compensating evaluation allowed/held/denied; approve/reject held compensation; successful restoration; resource changed since execution; repeated/concurrent rollback; invalid compensation state; missing audit linkage. Restoration respects current compensation rules and never bypasses policy |
| REPLAY / `rollback_replay` | Each policy outcome, parser rejection, human authorization and compensation decision; changed live policy/autonomy/corpus; providers unreachable during replay; saved evidence inconsistency. Frozen evidence determines consistency; no provider/Qdrant call, new execution, token minting or domain mutation |
| BG / `background` | Each eligible kind: evaluate, interpret, clarify, rollback, prepare_review; accepted 202/status/result; QUEUED/PROCESSING/RETRY_WAIT/terminal behavior; completed business rejection/clarification distinct from worker failure; initial defaults frozen; operation owner-only result; human safe status view; concurrent duplicate submission; changed-input conflict; no-store headers; background disabled for new jobs while already-queued jobs drain; synchronous review/promotions/execution preserved |
| RECOVERY / `recovery` | Dispatcher unavailable/restart; broker publication failure; publish-confirm ambiguity; duplicate/redelivered/stale-generation task; worker exits before claim, after checkpoint, before final commit, after commit before acknowledgement; lease expiration; run/queue deadlines; automatic retry exhausted; explicit authorized resubmission; revoked caller before claim and before finalization; restart after approval; lost execution response. Stale work cannot commit, accepted work is not lost, audit survives, no contradictory review or duplicate execution |
| TX / `recovery` | During delayed parser/analyzer/retrieval/judge/explainer/rollback evidence calls, inspect dedicated process DB sessions: no transaction held across slow work. While analysis runs, change policy/resource/permissions in controlled isolated scenarios; verify finalization rechecks applicable current state. Concurrent ordinary reads stay responsive |
| AUDIT / `audit` | Link operation, dispatch, interpretation turn, attempt, decision, review, execution and nonce; original input/model output/validation JSON/feedback retained where captured; failure category and cause chain; successful correction/fallback provenance; diagnostics/heartbeat evidence; API-error correlation IDs; credentials excluded from published reports; final fixture inventory. Audit completeness assertions must identify exact missing fields, not just table counts |

All seven risk categories: `mass_deletion`, `review_bypass`, `secret_export`, `credential_access`, `data_exposure`, `destructive_overwrite`, `audit_tampering`. All action classes: `read_file`, `create_file`, `create_directory`, `update_file`, `move_file`, `delete_file`, `delete_directory`.

## 6. Execution profiles and fault injection

- **Smoke:** few fresh cases covering auth, a prompt, semantic review/rejection, background completion, and idempotency. It has its own manifest and does not claim broad acceptance.
- **Core:** all ordinary API workflows and boundaries, including earned autonomy, review approval, execution, rollback and replay. Uses dedicated test callers/resources. Shared active/candidate policies remain unchanged; unavailable promotion fixtures are BLOCKED.
- **Recovery:** isolated PostgreSQL database on the user's cloud infrastructure, dedicated worker/dispatcher/API processes, queue, Redis prefix and Qdrant collections. Allows killing only owned processes, private endpoint outage configuration and policy pointer mutations. Requires an explicit controlled-fault option; never kill shared services or alter shared credentials.
- **Model evaluation:** separately runs maintained evaluation cases through actual parsing/semantic stages, grouped by risk/model configuration. Report accuracy/latency/retry statistics separately from workflow acceptance. Never seed these cases as retrieval data.

Ordinary runs use live providers. Reproducible malformed-response, exact retry-count and outage tests may use a test-owned HTTP fixture/proxy in the isolated profile, with real PostgreSQL/queue/workers. It must live in test tooling, never a public application bypass. Label those results controlled-fault evidence, not live-model accuracy. If that test fixture is not implemented or authorized, those cases remain NOT RUN; never call a naturally successful model response a retry test.

## 7. Per-scenario procedure and report contract

Every case must implement: prerequisite checks → record initial state → persist stable input/request/idempotency IDs → send authenticated HTTP → inspect initial response → poll operation if applicable → assert final business result → inspect persisted records and resource state → save evidence → record leftovers.

Do not assume a business 202 clarification response is a background operation. Poll only responses carrying an `operation_id`. Compare immediate acceptance and final result separately. Use per-call timeouts and an overall scenario deadline; on timeout inspect durable state before any resubmission. Preserve the same payload/key for a retry.

Use explicit assertions: expected HTTP status, status/outcome, action fields, risks, stage completion, ownership, linked row counts and fingerprints. Do not derive expected outcomes from the actual response. For threshold/state-dependent cases calculate expectations from a frozen verified fixture snapshot before submission. Stop dependent cases when prerequisites fail; continue independent groups.

Write `reports/e2e/comprehensive/<run_id>/{manifest.json,results.json,REPORT.md,evidence/}`. Group-only runs use `reports/e2e/narrow/<run_id>/`. Private credentials remain outside reports. Record PASS, FAIL, BLOCKED, SKIPPED and NOT RUN separately, with scenario ID, prerequisites, expected/actual results, HTTP/domain status, durations, correlation/operation/request/decision/review/execution IDs, evidence paths, full exception details with credentials redacted, and whether controlled faults were used. Capture detailed failure evidence rather than only `error_type`.

Report selected versus executed coverage, registry version, code/config/data fingerprints, dirty workspace, rate-limit interruptions, provisional corpus status and leftover pending reviews/resources. Summaries never combine historical runs into one pass rate. Raw published evidence must redact API keys, passwords, connection secrets and capability tokens while retaining useful input/validation detail in authorized private audit storage.

## 8. Implementation order and review gates

1. Inventory routes, state machines, database contracts and existing scenarios; assign stable case IDs and exact assertions. Review the coverage registry for omissions before implementing fixtures.
2. Implement shared reporting/client/read-only inspection plus initializer inspect/plan modes. Add a private output directory to `.gitignore`. Document the public/private manifest contract.
3. Implement fixture apply/resume/verify. Verify same-run repeat and partial failure without mutating application decisions. Check there is no cloud work in a DB transaction.
4. Port auth/background/semantic smoke scenarios and retain compatibility commands. Run the small live smoke to establish usable setup and reporting.
5. Add parsing/clarification, policy/autonomy, review, execution, rollback and replay groups. Build dependencies through real API requests; do not shortcut earned state. Run groups individually before a core aggregate run.
6. Add isolated policy-promotion/concurrency/recovery and transaction-duration checks. Review fault targets and process ownership before executing controlled failures.
7. Add dataset evaluation integration and repeat selected semantic cases to expose model variability. Keep workflow and model-quality results separately visible.
8. Reconcile all related runbooks/README/current scripts. Add route-to-case coverage validation and import/static checks. Publish a fresh report with failures and gaps; only then describe the suite as current broad evidence.

No migration generation or database schema changes are required for the suite as designed. Any discovered application fix is a separate change with its own review; do not edit policy thresholds/prompts mid-run to manufacture success.

## 9. Proposed operator commands (to implement)

```bash
# Inspect and write a setup plan without provisioning.
python -m scripts.e2e.initialize --run-id acceptance-001 --plan
# Apply the reviewed run fixtures; reusing this ID resumes setup.
python -m scripts.e2e.initialize --run-id acceptance-001 --apply
python -m scripts.e2e.initialize --run-id acceptance-001 --verify

# Standard run manifest path (no credentials in this public file).
python -m scripts.e2e.run --manifest reports/e2e/setup/acceptance-001/manifest.json --list-cases
python -m scripts.e2e.run --manifest reports/e2e/setup/acceptance-001/manifest.json --profile smoke
# Use a separate fresh run for core/recovery to preserve starting-state assumptions.
python -m scripts.e2e.run --manifest reports/e2e/setup/core-001/manifest.json --profile core
python -m scripts.e2e.review --manifest reports/e2e/setup/review-001/manifest.json --case REVIEW-001
```

Initializer/runners must expose base URL and explicit isolated-profile target configuration without requiring credentials on command lines. Recovery commands and the detailed input/expected-output runbook are written after their exact fixture contracts are implemented. Do not present the commands above as existing functionality.

## 10. Completion criteria

- One command initializes run-owned callers and compatible retrieval fixtures, with repeatable setup and explicit partial-failure recovery.
- Every public route, supported action, risk category and important state transition maps to registered scenarios.
- Core runs exercise approval and rejection, execution and rollback, ownership, idempotency and replay through authenticated APIs.
- Isolated recovery evidence covers crashes, races, retries and no slow-work transactions; unsupported/unrun cases are visible.
- Reports allow another contributor to reproduce the selected scenario and compare the same configuration/data version.
- New current documentation replaces stale guidance without rewriting historical evidence or claiming unexecuted cases passed.

### Current artifact placement

Initializer plans/preflights/manifests are in `reports/e2e/setup/<run_id>/`,
separate from comprehensive/narrow results. Configured MongoDB is the authoritative
store for new common-suite results/evidence. Private resume inputs and inventoried
scratch files remain under ignored `.e2e/`; scratch is retired after verification.

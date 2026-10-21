# Phase: explicit handling of incomplete semantic analysis

Status: implemented on 2026-10-12; awaiting authorized testing. No tests or live checks have been run for this change.

## 1. Objective and scope

Make it possible to distinguish “the checks completed and found no applicable risk” from “the system could not finish checking.” Preserve every completed stage and its evidence, explain gaps to reviewers, and reproduce the same result during replay without cloud calls.

This phase covers improvement 2. Do not combine it with shortening database transactions, a reviewer UI, new model routing, corpus changes, threshold calibration, or a monitoring platform.

User constraints for the implementer:

- Preserve unrelated work in this dirty working tree. Inspect the diff before editing.
- Do not generate or run migrations. This plan needs no new tables or columns.
- Do not run tests until the user explicitly authorizes testing. Test implementation and execution instructions below are acceptance criteria for that later step.
- Do not modify `.env`, routing selections, thresholds, live policies, Redis, or Qdrant data for implementation.
- Incomplete analysis may enter human review, and a reviewer may approve or reject only after the system has completed its current evaluation and durably created a pending escalation.
- Every state-changing API must enforce the valid current state and transition for the target record. Reject early, duplicate, stale, or out-of-order mutations without changing state.
- Do not silently change parser rejection behavior or other approval authority. Ask if implementation needs an architectural departure from this plan.

## 2. Current behavior verified in code

| File | Existing behavior / gap |
| --- | --- |
| `src/services/semantic_pipeline.py` | `aggregate()` already flags unavailable analysis, absent candidates, unavailable judgment, uncertain findings, and coverage gaps. It returns only a boolean and warning; it exits early on some failures, so it cannot summarize every reason. |
| `src/services/semantic_retrieval.py` | Per-query exceptions are caught; the overall envelope still returns `status='applied'` even if all queries failed. Empty label buckets and query failures both enter `coverage_gaps`. Candidates accumulated before a query failure can remain available. |
| `src/services/request_analysis.py` | Records unavailable analysis and fallback provenance. Grounding validation occurs after routed invocation. |
| `src/services/semantic_judge.py` | Records unavailable judgment, often with only an exception class. Grounding failures occur after routed invocation; they do not automatically trigger another routed model. |
| `src/services/advisory_evidence.py` | Baseline evidence is collected first. Review mode selects the pipeline flag; observe mode selects the baseline. Unsupported/multiple/conflicting actions raise separately. |
| `src/services/policy_engine.py` | Hard policy checks precede semantic escalation. Existing action-bound approval can bypass semantic advisory escalation. |
| `src/services/escalation_service.py` | Copies pipeline evidence into the escalation package. Approval creates a fresh evaluation with action-bound approval. |
| `src/services/semantic_pipeline.py:verify_frozen` | Accepts only the current exact version pair. Changing aggregation in place would invalidate historical replay. |

The task is to make existing conservative behavior precise and inspectable, not introduce a new permission system.

## 3. Proposed behavior contract

Use two separate dimensions. A request can have a confirmed risk AND incomplete analysis.

**Completion:** `complete`, `partial`, or `unavailable`.

**Assessment:** `clear`, `risk_detected`, `uncertain`, or `not_assessed`.

`clear` means no applicable semantic risk was found with complete coverage. It does not mean policy permits execution. `risk_detected` means at least one validated judge finding applies. `uncertain` means usable assessment exists but uncertainty or unresolved coverage remains. `not_assessed` means no usable judge assessment exists. Never infer a confirmed risk from similarity alone.

| Scenario | Completion / assessment | Proposed semantic flag |
| --- | --- | --- |
| All stages valid, required coverage complete, no applicable risks | complete / clear | false |
| All stages valid, one or more risks apply | complete / risk_detected | true |
| Valid judge says uncertain or reports missing coverage | partial / uncertain, or risk_detected if another finding applies | true |
| Analyzer unavailable | unavailable / not_assessed | true |
| Analyzer succeeded; retrieval failed or all searches returned no evidence | partial / not_assessed | true |
| Some retrieval queries failed; others yielded valid evidence | partial / uncertain or risk_detected if judgment succeeds; otherwise not_assessed | true |
| Judge unavailable or its output fails validation | partial / not_assessed | true |
| An individual label bucket is empty, but usable evidence and required coverage remain | Do not mark incomplete for this fact alone | determined by other evidence |
| Primary provider fails and a configured fallback succeeds fully | complete if all remaining checks complete | determined by valid findings |

Distinguish successful search with zero candidates (`no_evidence`) from a failed search (`unavailable`). A `no_evidence` result is **not** a finding of safety. In review mode it must set `requires_review=true`, cause a pending escalation if policy has not already denied the action, and give the reviewer the reason: the searches completed but found no eligible examples to support a semantic conclusion. The saved reason should identify the attempted queries and filters, and it must reach the decision evidence, escalation package, and API reason. A retrieval failure instead says which search could not complete. Never use a low score as the explanation for `no_evidence`: this retrieval path has no minimum score cutoff.

Mode behavior stays explicit:

- `review`: select the new proposed flag and warning as policy inputs.
- `observe`: save the new summary, but keep baseline selection unchanged. Incomplete experimental analysis does not independently enforce an escalation here.
- `baseline`: retain existing baseline behavior; do not fabricate a pipeline summary.

After a valid action exists, handled semantic dependency failures should continue to a durable policy decision. The endpoint can return HTTP 200 with `ESCALATED`; this means evaluation was recorded, not that analysis succeeded. Hard denials still win. Unhandled programming errors, parser failures before a valid action, and database commit failures retain their existing error paths.

Post-approval behavior is outside this phase. Preserve the existing downstream flow without introducing new approval authority, holds, or loops; its intended behavior will be discussed separately. The stage summary must remain an honest record whenever an evaluation runs.

The user has deferred discussion of what happens after human approval. Do not change the existing post-approval reevaluation or newly discovered risk handling as part of this phase; record the current behavior in the handoff as an open follow-up.

### State transition and API timing rule

Evaluation is a sequence the service completes before it presents an escalation for review. While parsing, analysis, retrieval, judgment, policy evaluation, or transaction commit is still in progress, no reviewer decision is available or valid. A reviewer may act only after a durable `PENDING` escalation row exists for the completed decision. The reviewer then makes one transition to `APPROVED` or `REJECTED`; repeated or competing decisions must fail without rewriting the first result. Approval starts a new evaluation under the existing action-bound approval flow; it does not edit the old decision into an allowed outcome.

Audit every state-changing endpoint and document its allowed source state, destination state, and persisted record. At minimum cover:

| API / operation | Required precondition before mutation | State effect |
| --- | --- | --- |
| Evaluate action or prompt | Valid request and completed processing path; transaction commits the decision before it is reported successful | Creates the policy decision; creates a pending escalation only if outcome is held for review |
| Answer clarification | Interpretation is in a status that accepts an answer; lock and recheck it before appending the turn | Advances clarification, completes evaluation, or records a terminal failure according to existing contract |
| Approve/reject escalation | Matching decision and a durable `PENDING` queue item; lock and recheck current status | Exactly one transition to approved or rejected; approval triggers a separate fresh evaluation |
| Execute | Stored decision is a policy-stage `ALLOWED` decision and token is valid for that exact decision | Creates execution record and applies the simulated resource change |
| Roll back | Target execution is currently `EXECUTED`; compensation evaluation and execution must satisfy their own guards | Changes the original execution to compensated only if the compensation actually executes; a refused compensation may still leave an audit decision |
| Promote policy / agent | Existing candidate qualification and promotion preconditions still hold when locked | Changes active policy or autonomy state only after validation; an unsuccessful attempt may still create an audit event |

Read-only status/list/replay endpoints do not transition state. Use row locks or equivalent compare-and-set checks where concurrent requests could race. If a required current-state record does not exist yet, the mutation must be rejected; it must not create a review outcome on behalf of an unfinished evaluation. Distinguish an unauthorized lifecycle transition from a legitimate audit entry for a failed attempt: failed promotion and refused compensation may create audit records, but may not change the protected active state or resource. Preserve current API status-code conventions where possible, but ensure the response clearly reports that the state is absent, stale, or no longer eligible. This phase does not add reviewer authentication or role authorization.

## 4. Storage and validation contract

Use the existing tables and paths:

| Table | Path / role |
| --- | --- |
| `decision_records` | `evidence_json.semantic_pipeline`: authoritative frozen stages, existing findings, and new `summary` |
| `escalation_queue` | `package_json.semantic_findings`: snapshot of that same pipeline, including `summary` |
| `request_attempt_events` | Existing stage status, provider/model, HTTP status, validation errors, timing, and decision linkage |

Do not put semantic details into `parser_trace` or add another decision table. Preserve current prompt/response recording behavior.

Add a typed `SemanticSummary` and structured reason contract in `src/models/semantic.py`, for example:

```json
{
  "completion": "partial",
  "assessment": "not_assessed",
  "requires_review": true,
  "applicable_risks": [],
  "uncertain_risks": [],
  "reasons": [
    {
      "code": "judge_unavailable",
      "stage": "judgment",
      "message": "Risk judgment could not be completed after the configured provider attempts.",
      "query_id": null,
      "risk_category": null
    }
  ]
}
```

Reason codes should cover analysis unavailable/invalid, retrieval unavailable, query unavailable, `no_evidence`, query/candidate budget limits, judge unavailable/invalid, uncertain findings, incomplete judge coverage, unmapped concerns, and material analyzer unknowns. Use stable enums, deterministic ordering, and deduplication. For `no_evidence`, include a plain-language reason that search completed with zero eligible examples, together with the query IDs and filters already captured in retrieval evidence; do not imply the action is safe. Keep informational observations such as empty label buckets separate from blocking reasons.

Keep `proposed_flag` and `proposed_warning` for existing consumers. Derive both from the summary through one pure function; do not maintain independent competing logic. `proposed_flag == summary.requires_review`.

Validate the new version's stages and summary at production and persistence boundaries, including `PolicyDecisionWrite` and `EscalationPackage`. Use version-aware validation so old evidence and baseline-only records remain readable. Retain other evidence fields such as approval, compensation, autonomy, and blast radius. Revalidate serialized dictionaries at writes so mutated model instances cannot bypass checks.

Use existing stage envelope fields plus a structured failure object when needed: stable code, exception type, readable message, HTTP status, and validation details. Do not capture authorization headers or connection credentials. Do not silently remove existing recorded inputs or outputs.

## 5. Implementation sequence

### Step A — preserve the historical replay contract

Files: `semantic_pipeline.py`, `replay_engine.py`.

1. Preserve the exact current `semantic-pipeline-v1` / `risk-union-v1` aggregation as a legacy path, including warning text.
2. Introduce a new pipeline/aggregation version for the new summary contract.
3. Dispatch frozen verification by stored version pair, never by current settings.
4. For new evidence, recompute summary, flag, and warning from frozen stages and compare all of them with stored values. Reject unsupported versions and inconsistent evidence explicitly.
5. Replay must never call an LLM, embedding provider, Redis, or Qdrant.

### Step B — implement contracts and pure aggregation

Files: `models/semantic.py`, `semantic_pipeline.py`.

1. Add the summary, reasons, and version-specific stage contracts.
2. Normalize stage states to `applied`, `partial`, `unavailable`, `no_evidence`, or `skipped` where relevant. A skipped stage must identify its dependency/reason; it is not a failed provider call.
3. Gather all available findings and all blocking reasons before producing the summary. An early failure must not erase successful upstream evidence or a separately confirmed risk.
4. Give every materially relevant analyzer unknown an explicit disposition in the judgment: resolved with grounded evidence, still unresolved, or not relevant to the semantic risks being assessed. Bind each disposition to the exact analyzer unknown by stable list position or ID, and reject missing or invented references. Only unresolved material unknowns make coverage incomplete. Do not infer resolution with string heuristics; if current judge output cannot express a disposition, add a typed field and validate it. More sophisticated resolution is out of scope.
5. Retain `provisional` as corpus provenance. Do not override `SEMANTIC_V3_ALLOW_UNREVIEWED` or independently force review for provisional examples in this phase.
6. Keep structural handling (`split_required`, `unsupported_action`, `action_conflict`) separate from service failures. Do not turn these into a fabricated single-action policy decision.

### Step C — report retrieval accurately

File: `semantic_retrieval.py`.

1. Record each query and label search outcome, including successful empty searches, before moving to the next search.
2. Preserve verified partial matches when a later label search fails. Identify exactly which searches were not completed.
3. Use overall `unavailable` when no search completed due to failures; `partial` when only some completed; `no_evidence` when all completed successfully but yielded zero eligible candidates; `applied` when the search work completed with candidates. Evidence coverage is assessed separately.
4. Preserve corpus fingerprint, embedding specification, filters, scores, matched query IDs, deduplication, and payload validation.
5. Empty label buckets become observations, not automatic coverage gaps. Query failures, query budgets, candidate truncation, and integrity failures remain explicit gaps. Preserve conservative review for budget truncation in this phase.
6. Never pass an invalid candidate to the judge. Keep valid evidence from independent successful searches, with the integrity failure recorded.
7. No threshold change, reseeding, embedding change, or additional cloud search is needed.

### Step D — capture analyzer/judge outcomes and skipped stages

Files: `request_analysis.py`, `semantic_judge.py`, `semantic_pipeline.py`, `monitoring.py`.

1. Distinguish provider errors from schema/grounding errors with structured details; use the existing exception-chain HTTP status helper.
2. Preserve existing configured routing, retry budgets, and fallback policy. Do not add validation retries or a new LLM call in this phase.
3. Retain first-failure `fallback_from` compatibility. Existing attempt events remain the provider-attempt history.
4. Do not call retrieval when analysis is unusable, or the judge when there are no valid candidates. Record skipped dependencies clearly.
5. Allow judgment of valid partial evidence, but never let a reassuring judgment erase upstream gaps.
6. Ensure stage events distinguish partial, unavailable, skipped, and successful outcomes. Reuse existing monitoring fields for validation JSON; structured decision evidence carries the full semantic failure description.
7. Do not catch every programming error and mislabel it as a provider outage. Known provider/validation/retrieval failures become structured outcomes; unexpected internal failures keep the existing operational error path.

### Step E — integrate persistence, policy selection, and responses

Files: `advisory_evidence.py`, `orchestrator.py`, `models/decision_contracts.py`, `models/request.py`, `escalation_service.py`.

1. Freeze the validated summary with the pipeline before policy evaluation.
2. Preserve review/observe/baseline selection and policy precedence. Prefer existing `semantic_flag` integration over a second policy gate.
3. Validate new evidence before ledger insertion and escalation serialization. Do not rewrite historical rows.
4. Add optional `semantic_summary` to `EvaluationResponse`, populated from the selected request's pipeline summary, and a companion `semantic_mode` so an observe-mode advisory is not mistaken for an enforced outcome. Baseline-only responses may leave these null.
5. Escalation responses already expose full `semantic_findings`; ensure summary and reasons survive queue creation, listing, reviewer updates, and approval reevaluation.
6. A held decision has no capability token. A database persistence failure must not return a usable success/token.
7. Keep failure detail in durable evidence even when an earlier hard policy rule determines the final denial reason.
8. Enforce the state-transition/API timing table above. In particular, only a committed pending escalation can be approved or rejected; lock and recheck status during mutation, and reject duplicate/out-of-order state changes. Check the existing clarification, execute, rollback, and promotion guards for the same rule without widening their permissions.

### Step F — documentation and implementation handoff

Update `docs/guides/phase4_advisory.md` and `docs/testing/PHASE12_RUNBOOK.md` with status meanings, storage paths, observe/review differences, one provider-failure example, and one successful-but-empty retrieval example. Summarize changed files and any deferred concerns. Do not claim live verification before executing it.

## 6. Verification plan — execute only when requested

Start with deterministic injected outcomes in an isolated test database. No new production failure-injection endpoint or `.env` switch. Use existing test conventions and reusable fixtures. Suggested new test files: `tests/test_semantic_completion.py` and `tests/test_semantic_completion_flow.py`.

For each case inspect the summary, proposed flag/warning, selected policy inputs, decision record, escalation snapshot where applicable, attempt events, and token presence.

| Case | Setup and expected result |
| --- | --- |
| 1 | Complete benign evidence: complete/clear, no semantic escalation; autonomy still applies. |
| 2 | Complete applicable risk: complete/risk_detected; review mode escalates without a token. |
| 3 | In review mode with an otherwise policy-permitted valid structured action, analyzer routes exhaust: unavailable/not_assessed; downstream stages skipped; a durable escalation is created without a token. |
| 4 | Primary fails, fallback succeeds: failure event preserved; fully successful pipeline can be complete. |
| 5 | In review mode with an otherwise policy-permitted action, Qdrant unavailable: analysis retained; retrieval unavailable; judge skipped; durable review hold without a token. |
| 6 | All query calls fail after collection lookup: retrieval cannot report applied. |
| 7 | One query/label fails after earlier matches: verified matches retained; partial assessment; precise failed search recorded. |
| 8 | In review mode with an otherwise policy-permitted action, successful searches return zero candidates: `no_evidence`, not a transport error; durable pending escalation without a token. The decision evidence, escalation package, and API reason all say the semantic search completed but found no eligible examples, and expose the attempted query/filter evidence. |
| 9 | One label bucket empty with otherwise complete evidence: emptiness alone does not force review. |
| 10 | Invalid corpus payload or vector metadata: invalid evidence excluded and gap recorded. |
| 11 | Judge provider failure, schema failure, invented quote/reference: distinct failure details; no usable judgment; review hold. |
| 12 | One applicable risk plus another uncertain finding: both retained; partial/risk_detected. |
| 13 | Material unknowns, unmapped concerns, omitted coverage, and budget limits: each prevents a falsely clear summary. |
| 14 | Observe mode with new pipeline failure: summary visible, decision still follows the baseline selection. |
| 15 | Baseline mode: existing response and policy behavior preserved. |
| 16 | Hard denial plus incomplete analysis: DENIED, evidence retained, no token, no new review queue item for that denial. |
| 17 | Reject escalation: reviewer outcome persisted, no execution. Check that approve/reject accepts only the pending state and preserves existing downstream behavior; the intended effect of approval remains a separate discussion. |
| 18 | Replay old v1 and new records with every cloud adapter made to raise if invoked: consistent results without external calls. |
| 19 | Tamper with summary, selected flag, warning, or version: verification rejects inconsistency. |
| 20 | Malformed new evidence at ledger/escalation boundaries: rejected before persistence. Historical valid evidence still loads. |
| 21 | Database write failure: no success/token released; operational failure events use the existing separate persistence path. |
| 22 | Both prompt and structured-action entry points: summary reaches the response; parser rejection/clarification remain their own outcomes. |
| 23 | While an evaluation is in progress or before a pending escalation is committed, approve/reject cannot change state or create a review result. |
| 24 | Two concurrent reviewer decisions, or a repeated decision after approval/rejection: at most one transition succeeds; the other is rejected and stored state remains consistent. |
| 25 | Call each state-changing endpoint with a target in the wrong lifecycle state: clarification, execution, rollback, and promotion do not change protected active state or resources. Preserve any intentional audit entry for a refused promotion or compensation. |

After explicit authorization for live testing, use the existing online harness and a new timestamped report. Run one benign and one risky request with current configured models, inspect recorded evidence and replay, then perform an authorized human-review flow. Use controlled fixtures for outages instead of corrupting real credentials or collections. State explicitly which results came from injected failures versus actual cloud services.

## 7. Completion criteria

- Incomplete checking cannot be represented as a complete/clear new pipeline result.
- Reviewers can see what failed, what succeeded, what evidence is missing, and why review was proposed.
- Existing decision and escalation tables contain validated, consistent evidence; no migration is needed.
- Observe mode, parser outcomes, hard denials, and human-approval authority keep their documented semantics.
- Historical replay remains valid; new replay verifies the summary without cloud I/O.
- Final handoff separates implemented work, static review, tests actually run, and remaining live checks.

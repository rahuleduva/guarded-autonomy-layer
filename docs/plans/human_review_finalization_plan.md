# Human review explanation and authorization finalization

Status: implemented in application code on 2026-10-14; verification is pending by user request. No Alembic migration was generated or run, and no tests or provider calls were run.

## 1. Read this first

Implement a clear, combined human review package and a simple, auditable approval flow. The human decides whether to accept the presented concerns. Do not add an LLM that grades the human's response.

User decisions already agreed:

- One overall approve/reject decision for the combined package. Individual finding approval states are deferred.
- Approval requires a non-empty note, validated by the API and internal write contracts.
- An LLM may explain the existing findings for the reviewer. Preserve the original evidence.
- Keep the original held decision and record the later authorization outcome separately.
- Approval must not create a policy version.
- Approval must not repeat parsing, embedding, retrieval, or semantic judgment.
- Execution remains a separate API call.
- Keep HTTP processing synchronous. Do not introduce workers, polling jobs, or background processing.
- Record the human's feedback and the package they reviewed.
- Ask before deciding new storage architecture. Do not create or execute Alembic migration files; the user generates them.
- Do not run tests or live provider calls until the user requests testing. The verification checklist below is for that later phase.

The older `build_plan.md` explicitly requires a reviewer policy bump, including its EVAL-7 definition and assignment excerpt. The user's newer direction overrides that implementation requirement. Document the deliberate deviation; do not rewrite historical assignment text as though it never required a bump.

## 2. Decisions applied

The user explicitly selected the new `human_review_records` table. The remaining implementation details use the recommendations below as the defaults in the approved plan.

1. **Storage:** the frozen package and explanation remain in `escalation_queue.package_json`; accepted reviewer submissions are written to `human_review_records`; policy/authorization decisions remain in `decision_records`.
2. **Policy changed during review:** approval returns HTTP 409, leaves the escalation pending, and requires fresh evaluation and a fresh package. Rejection may close the stale package.
3. **LLM explanation failure:** a complete deterministic explanation is stored with fallback provenance; model prose is optional and not an approval gate.
4. **Human approves but hard checks prevent authorization:** preserve APPROVED review status, return BLOCKED authorization with no token, and do not create another escalation.
5. **Reviewer identity:** retain the current trusted/demo API boundary. A nonblank `reviewer_id` is recorded but is not authentication; authenticated reviewer authorization remains a separate phase.
6. **Autonomy accounting:** tagged finalization decisions are excluded from the independent clean-run streak and latest-decision calculations.

Do not silently invent risk acceptance exceptions, reviewer roles, override rights, or a changed policy for CRITICAL operations with no token TTL.

## 3. Current code map and observed behavior

| Location | Current role / relevant behavior |
|---|---|
| `src/main.py` | Synchronous evaluate and approve handlers; `transaction()` commits before returning. Evaluation middleware records attempts; approve route is not currently included. |
| `src/models/request.py` | `EscalationPackage`, `ApprovalDecision`, `EvaluationResult`, and API response validation. Approval note currently optional. |
| `src/models/escalation.py` | `escalation_queue`, including `package_json`, status, reviewer, and timestamps. |
| `src/services/escalation_service.py` | Builds packages; locks pending escalation; records review; calls `reviewer_bump()` then `evaluate_action()`. |
| `src/services/orchestrator.py` | `evaluate_action()` collects evidence, evaluates, records escalation/autonomy, and issues tokens. Do not call this from the new approval finalizer. |
| `src/services/advisory_evidence.py` | Recalculates blast radius and semantic evidence; invokes the newer pipeline when configured. |
| `src/services/policy_engine.py` | Pure deterministic evaluator. Valid approval bypasses review gates, but not hard restrictions, unusable TTL, or SHADOW. |
| `src/services/action_binding.py` | Action fingerprint includes action identity and parameters, excluding request ID/timestamp. |
| `src/services/shadow_engine.py` | Active/shadow evaluation and decision logging. Optional autonomy side effects require care. |
| `src/services/decision_ledger.py` / `src/models/decision_contracts.py` | Explicit Pydantic contracts validate all ledger writes. |
| `src/services/replay_engine.py` | Replays policy decisions from frozen evidence and original policy; must remain cloud-free. |
| `src/services/executor.py` | Consumes a token for a specific allowed decision; executes simulated resources separately. |
| `src/services/autonomy_service.py` | Derives streaks from decision rows. Adding another row can affect counts even if `record_outcome()` is not explicitly called. |
| `src/services/llm_routing.py`, `config/llm_routing.yaml` | Purpose-based structured LLM routing and fallback handling. |

## 4. Intended sequence

```text
Agent request
  -> existing interpretation / semantic evidence / policy evaluation
  -> held decision saved
  -> combined package prepared, including explanation
  -> commit
  -> agent receives held result; HTTP request ends

Human reads package
  -> submits decision + approval note + package fingerprint
  -> API validates
  -> lock and validate pending escalation
  -> validate package binding and proposed policy-staleness rule
  -> persist human review
     reject: close review; no token
     approve: deterministic finalization using frozen evidence
       -> record linked authorization outcome
       -> issue token only for ALLOWED
  -> commit
  -> return reviewer response

Separate execution request consumes issued token
```

Do not make an HTTP request stay open while waiting for a human. Do not let an explanation LLM approve, dismiss, add, or remove a policy requirement.

## 5. Proposed storage and validation contracts

### Existing `escalation_queue`

Retain existing status values for this phase: PENDING, APPROVED, REJECTED. They describe human review. Authorization outcome is separate.

Extend the typed `EscalationPackage` with a versioned review context:

- Original decision ID, action fingerprint, original policy hash, and review requirements.
- Immutable explanation object and its provenance.
- `package_fingerprint`, computed over the immutable review content, including the exact explanation shown. Exclude mutable status/reviewer/note/decided_at fields and the fingerprint itself.
- Existing source request, semantic findings, blast-radius evidence, and original recommendation remain preserved.

Compute the package fingerprint only after explanation/fallback preparation. Validate it again on writes and review submission. A fingerprint detects mismatch; it is not authentication.

### Proposed new `human_review_records`

One immutable row per accepted human decision for an escalation in this phase:

| Field | Purpose |
|---|---|
| `review_id` | UUID primary key |
| `escalation_id` | Required FK to escalation_queue; unique for the current one-decision lifecycle |
| `original_decision_id` | Required FK to decision_records |
| `package_fingerprint` | Exact package reviewed |
| `action_fingerprint` | Exact approved action |
| `reviewer_id` | Required nonblank ID, subject to agreed identity boundary |
| `decision` | APPROVE or REJECT enum |
| `note` | Required nonblank for APPROVE; optional for REJECT |
| `submitted_at` | Server-generated UTC time |
| `authorization_decision_id` | Optional FK to decision_records; set for successful approval finalization, including blocked outcomes |

Build a Pydantic write contract and SQL constraints consistently. Approval/rejection combinations, whitespace validation, links, and package binding must be checked at service boundaries too. SQLModel construction alone is not sufficient validation.

Keep queue reviewer/status fields as the existing query-friendly summary, updated atomically with the review row. Do not use the queue's single note as the only audit record. Rejected HTTP payloads are request-validation monitoring events, not accepted review records.

The approval transaction either stores the review and its finalization together or rolls back. Unexpected failure must return an error and leave the review pending; never claim a failed submission was durably accepted. Persist diagnostic failures separately through existing monitoring where available.

### `decision_records`

No new decision stage is needed if finalization reuses the pure policy evaluator. Use POLICY plus explicit typed evidence identifying `human_review_finalization_v1`.

Record original decision ID, review ID, escalation ID, package fingerprint, action fingerprint, original request ID, and frozen approval details. The finalization gets a new request ID because `request_id` is unique, but the action fingerprint must remain identical.

Do not overwrite the original outcome, reason, or evidence. Stop using mutation of the original decision's reviewer field as the sole review audit mechanism. Keep historical rows readable.

## 6. Implementation steps in dependency order

### Step A — Establish baseline and apply section 2

- Read current files; the working tree already contains many unrelated edits. Preserve them.
- Record the selected table and applied plan defaults in section 2.
- Find all callers and assertions using `reviewer_bump`, `new_policy_version`, `ApprovalDecision`, `escalation_service.decide`, and approval tokens.
- Check CLI, scripts, evaluation harness, and documentation as well as endpoints.

### Step B — Define combined review requirements and explanation schema

Create typed models, preferably `src/models/human_review.py`, for review context, explanation, reviewer submission, persistence contract, and response. Avoid circular imports by putting reusable types in this module.

Build requirements deterministically from frozen evidence and policy conditions. The evaluator returns at the first matching gate, so its reason alone does not enumerate all review concerns. Include applicable environment review, semantic warnings/incomplete evidence, blast radius, and ASSISTED sign-off. Reuse policy logic or a shared pure helper rather than inventing parallel rule semantics.

Each requirement has a stable ID, source/evidence reference, description, known facts, unknowns, and what approval acknowledges. These are explanation items, not individually actionable approval objects. No per-finding status fields.

LLM output schema: action summary plus explanation items keyed by requirement ID, consequences phrased as possibilities when uncertain, and questions for the reviewer. Require complete coverage, no unknown/duplicate requirement IDs, and valid evidence references. These mechanical checks do not prove factual correctness; retain source evidence for inspection.

### Step C — Add explanation service and routing

Proposed service: `src/services/review_explanation.py`.

- Add purpose `review_explainer` to routing YAML using existing model profile references and fallback policy. Keep model names in YAML; use `invoke_for_purpose()`.
- Input only the frozen review context. Clearly distinguish supplied text from instructions to the LLM.
- Instruct it to explain existing concerns, distinguish verified facts from inferences, preserve uncertainty, and avoid new authorization requirements.
- Store validated output, provider/model, schema/prompt versions or hashes, and failure provenance. Follow existing monitoring conventions for prompt/output recording; never send credentials or environment contents.
- On failure or malformed coverage, construct a complete explanation directly from the requirements and original reasons, if the section 2 fallback recommendation is approved.
- Generate once when building the package. Listing/reading the queue must not make LLM calls. Retries of package creation must reuse an existing complete package.
- Explain baseline-only cases and missing semantic evidence; do not assume every package contains the newer pipeline.

HTTP remains synchronous. This step adds latency to package creation. Do not independently redesign all existing database transaction boundaries; document the inherited long-transaction limitation for later work.

### Step D — Implement API and internal validation

Keep `/api/v1/control/approve/{decision_id}` with existing fields and add required `package_fingerprint` for new submissions. Update all callers. API examples must use the fingerprint returned by the server, not a fabricated value.

Approval payload example:

```json
{
  "decision_id": "<original decision ID>",
  "reviewer_id": "reviewer-123",
  "approve": true,
  "note": "I reviewed the identified exposure and authorize this specific release.",
  "package_fingerprint": "<fingerprint from retrieved package>"
}
```

- Strict boolean approval; nonblank reviewer ID and decision ID.
- Approval note: missing/null/empty/whitespace-only -> HTTP 422 before service mutation. No arbitrary word count; no LLM quality gate.
- Rejection note optional, unless user changes that requirement.
- Path/payload ID mismatch -> retain current 400 behavior.
- Already decided or mismatched package -> explicit 409 conflict, not generic 400.
- Use typed service exceptions mapped by the route; do not change all ValueError handling globally.
- Validate direct service calls as well as HTTP requests.

Provide typed response fields: review status/ID, original decision ID, `authorization_status` (NOT_REQUESTED, ALLOWED, BLOCKED), optional evaluation result, explicit reason. Preserve `evaluation` for caller compatibility where feasible. Deprecate `new_policy_version` as always null, then document its eventual removal; never populate it on approval.

### Step E — Implement focused finalization

Proposed service: `src/services/review_finalization.py`.

1. Lock pending queue row; validate its package against the original held POLICY decision.
2. Check action and package fingerprints and the agreed stale-policy behavior. Synchronize the active-policy check with policy promotion using compatible locking; compare-and-use must not race a policy switch unnoticed.
3. For rejection, persist review + queue status; no finalization/token/cloud calls.
4. For approval, validate and stage the review row, then reconstruct the action from the original request. Only request ID/time change.
5. Deep-copy the original evidence. Preserve semantic results exactly; do not call `build_evidence`, `evaluate_action`, or cloud services.
6. Snapshot current autonomy using existing service and add trusted, action-bound review evidence. Preserve references to the original snapshot and decision.
7. Call the existing pure policy evaluator with the agreed policy and frozen evidence. This reuses deterministic restrictions and TTL logic; it does not repeat semantic analysis. Explain this distinction in code/documentation.
8. Record the finalization through validated decision-ledger contracts with review linkage. Preserve compensation evidence where present.
9. Issue a capability token only for ALLOWED; store its hash. For DENIED/SHADOW_LOGGED/held outcomes, return BLOCKED with the actual reason and no token. Do not call `raise_escalation()` automatically for these outcomes.
10. Finalize queue status and review linkage atomically, then return only after commit.

No policy copy, version bump, or active-pointer movement. Retain explicit policy promotion as its separate operation. Remove `reviewer_bump` only after confirming no other required caller remains.

Avoid accidental shadow-policy evaluation/autonomy side effects from reusing the full orchestrator. Proposed scope: finalization uses the active policy only; initial evaluation still supplies existing shadow-policy evidence.

Approval binding continues to protect against reuse for another action. The internally constructed approval object must reference the trusted review record and package; never accept client-supplied `approved` evidence as authorization.

### Step F — Replay, autonomy, and compatibility

- Keep finalization replay cloud-free. The same pure evaluator receives the saved policy, frozen evidence, and saved autonomy snapshot.
- Validate new review evidence via a typed contract for finalization-tagged records. Legacy policy decisions without the tag must remain readable/replayable; do not require new fields for historical records.
- If excluding finalization records from autonomy streaks is agreed, update both `_clean_runs()` and the latest-decision selection used in `record_outcome()`. Check promotion evidence readers too. Simply omitting one update call does not prevent later double counting.
- Existing pending packages lack new explanation/fingerprint fields. Define an explicit synchronous preparation path before approval: snapshot existing evidence, build explanation, atomically save the new package, then require the reviewer to fetch it. Do not silently upgrade and approve an unseen package in one call.
- Previously completed packages remain historical. Do not regenerate their explanations or invent reviewer notes.
- Migration work is performed by the user after model changes. Stop before database writes requiring the new table if migration has not been applied.

### Step G — Monitoring, caller updates, and documentation

- Extend existing request monitoring to review preparation and approval routes. Capture explanation attempts/failures, validation failures, review ID, finalization outcome, and commit failures.
- Store substantive reviewer feedback in human_review_records, not only operational logs.
- Update CLI, `src/evaluation.py`, relevant scripts, and API documentation for mandatory approval notes, fingerprints, and blocked authorization responses.
- Document that the caller-supplied reviewer ID is not authentication if that boundary is retained.
- Update current behavior documentation and old bump assertions. Preserve historical reports as historical evidence.
- Add a concise handoff summary: files changed, schema changes, choices confirmed, migration needed, checks not yet run.

## 7. Later verification checklist — run only when requested

Use isolated deterministic tests first; do not spend live-provider credits for cases that can be verified locally. Run live explanation smoke checks only after the user requests them and migrations are ready.

| Scenario | Expected result |
|---|---|
| Normal held request | Complete immutable package with all relevant concerns, explanation, fingerprint |
| Explanation provider fails | Recorded failure and complete deterministic explanation; held decision preserved |
| Explanation omits a requirement or invents an ID | Output rejected; deterministic fallback |
| Queue read repeated | No LLM/embedding/Qdrant calls; same immutable package fingerprint |
| Approval without note / whitespace note | 422, no accepted review or token |
| Direct service approval without note | Validation failure before mutation |
| Approval with short but nonblank note | No LLM quality assessment; normal authorization checks |
| Human rejection | REJECTED review stored; no policy bump, token, or cloud calls |
| Valid ASSISTED approval | ALLOWED linked decision + token; original held decision unchanged |
| Multiple concerns in one package | One explicit package approval covers presented concerns |
| Semantic evidence originally unavailable | Unknowns visible; human may accept package subject to hard checks |
| Approval after policy changed | Proposed 409, pending review retained, no automatic reevaluation |
| Wrong package/action fingerprint | Conflict; no review finalization |
| Concurrent approve/reject | Exactly one accepted review; other request conflicts |
| Already reviewed submission repeated | Conflict; no duplicate final decision/token |
| SHADOW at finalization | Approved human review, BLOCKED authorization, no token |
| CRITICAL policy with no usable TTL | No token and no automatic new escalation loop |
| Sandbox / role / deny restriction | Hard restriction remains effective |
| Failure while saving decision/token hash | Entire approval transaction rolls back; error response |
| Finalization replay | Consistent saved outcome; zero cloud calls |
| Approval token used for changed action | Rejected |
| Successful execution followed by token reuse | First executes once; reuse rejected |
| Autonomy counting | Finalization does not manufacture another independent proposal |
| Old completed package | Readable without new fields or regenerated history |
| Old pending package | Explicit preparation + new reviewer fetch required |
| Compensation review | Original compensation binding preserved |

Use spies or fail-fast stubs to prove zero parser/retrieval/judge/embedding calls during approval. Compare policy row count, active policy hash, and version before/after approval. Inspect database rows for review-to-decision linkage, original evidence preservation, and token hash consistency.

## 8. Completion criteria

- All section 2 choices are recorded, not silently assumed.
- User can read a clear combined package, submit approval with a mandatory note, and receive an unambiguous authorization result.
- Feedback is durably recorded with the exact package and action reviewed.
- Human approval makes no policy version and invokes no cloud analysis.
- Explanation failure is visible and handled as agreed.
- Review completion, authorization, and execution are distinct and correctly recorded.
- Pydantic validation covers HTTP and internal persistence paths.
- Models are ready for the user's migration generation; no migration files are generated by the implementing agent.
- Verification remains explicitly marked pending until authorized and completed.

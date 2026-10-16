# Human review and authorization finalization

This phase gives reviewers one complete package for the held request. The reviewer makes one approve/reject decision for that package. The explanation LLM only explains recorded evidence; it cannot change the findings or decide whether to approve.

Newly prepared packages enumerate environment review from the original stored policy and ASSISTED sign-off from the recorded autonomy snapshot, even when another gate caused escalation first. They include the frozen semantic summary's coverage/budget reasons. Analyzer questions marked resolved or irrelevant by a successful judge remain in the original evidence but are excluded from the unresolved-question requirement. Previously prepared packages retain their frozen content and fingerprints.

## What happens

1. The normal request pipeline parses/analyzes the request, performs semantic retrieval and judgment, and evaluates policy.
2. If policy holds the request, the app builds one package containing the action, original findings, known facts, unknowns, and a plain-language explanation for each review concern. It stores that package in `escalation_queue.package_json` and returns the held result. The explanation is generated once. If its provider fails or omits a concern, the app stores a complete deterministic explanation and records the fallback provenance.
3. The reviewer retrieves the package from `GET /api/v1/escalations`. The package includes `package_fingerprint`; the reviewer submits that exact value so the app can verify which package they saw.
4. Approval requires a nonblank `note`. Rejection may include a note but does not require one. A missing/blank approval note is rejected by Pydantic with HTTP 422 before service state changes.
5. The app locks the pending escalation and checks the package fingerprint, action fingerprint, and original decision linkage. If the active policy changed after the package was created, approval returns HTTP 409 and leaves the request pending. No model or vector calls happen inside approval.
6. The accepted human decision and note are written to `human_review_records`. Rejection closes the escalation and makes no authorization decision.
7. Approval runs the deterministic policy evaluator using the original frozen semantic/blast evidence and the current autonomy state. It does not repeat parsing, embedding, Qdrant retrieval, or LLM semantic judgment. Sandbox, role, deny-rule, token-TTL, and SHADOW restrictions remain active.
8. The authorization outcome is written as a new linked row in `decision_records`; the original held decision remains unchanged. The reviewer row links to that authorization decision. The policy version and active pointer do not change.
9. Only an `ALLOWED` authorization result receives a capability token. The API does not execute the action; a separate execute request consumes that token.

## Tables

| Table | What it records |
| --- | --- |
| `escalation_queue` | Current review status and the frozen combined package, including the explanation and fingerprints. |
| `human_review_records` | One accepted reviewer choice, note, reviewer ID, timestamps, package/action fingerprints, and (for approval) the linked authorization decision ID. |
| `decision_records` | The original held decision plus a separate policy decision created during approved finalization. |
| `request_attempt_events` | Operational request and explanation-stage status/failure metadata; substantive reviewer notes are kept in `human_review_records`. |

The caller-supplied `reviewer_id` is retained from the current trusted/demo API boundary. It is an audit label, not proof of identity or authority. Reviewer authentication is a separate security phase.

Package explanation runs synchronously during the held request and may add provider latency while the existing database transaction is open. Background processing and shorter transaction boundaries remain later work.

## Request example

First fetch the package and use the exact fingerprint returned for the matching `decision_id`:

```json
{
  "decision_id": "<original-decision-id>",
  "reviewer_id": "reviewer-123",
  "approve": true,
  "note": "I reviewed the identified exposure and accept it for this specific action.",
  "package_fingerprint": "<fingerprint-from-package>"
}
```

The response separates the human's choice from authorization:

- `status`: `APPROVED` or `REJECTED`.
- `authorization_status`: `ALLOWED`, `BLOCKED`, or `NOT_REQUESTED`.
- `evaluation`: the linked deterministic authorization result for approval, or `null` for rejection.
- `capability_token`: present only when authorization is `ALLOWED`.
- `new_policy_version`: retained as `null` for compatibility; human review does not create a policy version.

## Existing pending packages and schema

Old pending packages have no explanation or package fingerprint. Call `POST /api/v1/escalations/{decision_id}/prepare`, then fetch `GET /api/v1/escalations` again and show the newly prepared package to the reviewer. The approval endpoint will not silently prepare and accept a package the reviewer has not fetched.

The SQLModel table has been added, but no migration was generated or run. Generate and inspect the migration before running the updated application against a database that has not yet applied it. Tests and live provider calls have not been run.

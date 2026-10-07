# Online end-to-end execution handoff

Prepared against the current application code. **This is an execution plan, not a test-results report. No live readiness or scenario success is claimed.**

The user will switch to Luna or Sol light to execute and observe this plan. Read this document before making requests. Run each section separately, inspect its result, and continue only when its prerequisites pass. Do not run unit tests, the offline scenario harness, Docker, or deployment steps. Do not change application code to make a check pass.

## Scope and operating rules

- FastAPI base URL: `http://127.0.0.1:8000`; API prefix: `/api/v1`.
- PostgreSQL and cloud Qdrant must be real. Use Gemini embeddings and the configured Gemini or Groq LLM.
- A request returning HTTP 200 is insufficient. Check its decision/status and persisted evidence.
- **Any parser or semantic fallback fails the online acceptance check**, even if the policy decision looks correct. The application currently permits fallback; these helpers detect it.
- Discovery is read-only. Seed only missing synthetic fixtures after recording discovery. Never erase records, reset streaks, forge approval evidence, manufacture tokens, or manually set agent autonomy to LIVE.
- Test agents and resources use a unique run prefix. Existing resources, policies, reviews, and vectors remain part of the baseline.
- Human responses are simulated by submitting `approve: true/false` through the control APIs with a run-specific reviewer ID. The user has authorized this as part of E2E testing. There is no reviewer web UI or authentication flow to test here.
- Execution changes **database-backed simulated resources**. It does not create files on the host filesystem.
- Policy promotion and reviewer approval deliberately change the active policy. Log the original active hash and all subsequent hashes. Do not automatically restore the active pointer afterward: that would be an additional policy change outside the tested workflow.
- Keep raw keys, connection strings, and capability tokens out of terminal output and reports. Tokens stay in Python memory; the helper journal redacts them. Do not enable HTTP debug logging.
- A network sandbox failure requires the platform's normal execution approval. Do not work around it with another tool. User authorization for testing does not bypass platform access controls.
- If a request times out, inspect the database for its request/decision/resource before retrying: the server may have committed. Do not blindly repeat approvals, promotions, execution, or rollback.
- Use PASS, FAIL, BLOCKED, and SKIPPED honestly. Record unexpected output before investigating. A blocked prerequisite does not count as a passing scenario.

## 0. Start an interactive execution session

Use the same virtual environment as FastAPI, from the project root:

```bash
source /path/to/your-venv/bin/activate
PYTHONPATH=. python -i scripts/online_e2e_session.py
```

The helper prepares `t = OnlineSession()` without running network calls or scenarios. Subsequent blocks are **Python statements in this same interactive session**, not separate shell commands. Assign API responses to variables as shown; printing a raw response yourself could expose its token.

`t.log(...)` writes a redacted journal under `artifacts/online-e2e/<run_id>/journal.json`. `t.check(...)` records an assertion and stops the current block if it fails. `t.api(...)` performs real HTTP requests. Database reads use PostgreSQL read-only transactions. `t.evaluate(...)`, `t.review(...)`, and `t.execute(...)` call API endpoints, not internal business services.

Do not paste the entire document at once. Stop after each scenario to observe its result. On an exception, the remaining statements in that block may not have run. Do not rerun earlier statements that already mutated state. If the Python session is lost, tokens and variable bindings are lost: keep the journal and start a new run rather than claiming the original run finished.

## 1. Read-only discovery: configuration, API, database

```python
t.discover()
```

Observe the journal and terminal output for:

1. `OFFLINE_MODE=false`, `EMBEDDING_PROVIDER=gemini`, chosen LLM/model, configured embedding dimensions, collection, cutoff, and credential-presence booleans.
2. GET `/api/v1/health` succeeds and `/openapi.json` contains the expected routes.
3. PostgreSQL connectivity, required tables, and `alembic_version` matching the revision head in this checkout.
4. Exactly one active policy and at most one shadow candidate. Every stored policy passes schema, version, and canonical-hash validation.
5. Active policy rules, policy inventory, existing table counts, autonomy modes, review statuses, and execution statuses.
6. The run's fresh agent/resource namespace. The helper derives the sandbox root from the stored active policy, not a config constant.
7. The candidate's accumulated shadow runs and violation IDs, if a candidate exists.

Expected current artifact rules: workspace role may create/read/update/move files and create directories; admin may delete; read-only role may only read. File creation is LOW with TTL 300 seconds; update/move MEDIUM with TTL 120; file deletion HIGH with TTL 60; directory deletion CRITICAL with null TTL. Destructive production requests escalate. Defaults are ASSISTED at 5 clean proposals, LIVE eligibility at 20, policy eligibility at 20. The helper reads actual configured thresholds as `t.A`, `t.L`, and `t.P`.

**Branch on discovery, before testing:**

| Finding | Action |
|---|---|
| API unavailable | Stop HTTP tests; verify the user's server and port. |
| Database missing/unreachable | BLOCKED; resolve connectivity, preserving existing data. |
| Migration behind head | Record current/head revisions. Apply `python -m src.cli init-db` as a separately logged setup action, then restart FastAPI and discovery. Do not recreate the database. |
| Missing active policy with migrated tables | Investigate startup seeding/server logs; don't select an arbitrary policy manually. |
| Stored policy hash/schema invalid | Stop affected tests and report the integrity failure. Do not silently rewrite the row. |
| Rules materially differ from the contracts above | Record the differences and adapt expected results before executing. Do not edit policies to match this document. |
| Missing candidate | Policy-shadow/promotion checks are BLOCKED until a valid test candidate is deliberately prepared. Other scenarios may proceed with that limitation recorded. |
| Candidate has any past violation | This candidate cannot qualify under the current lifetime-history rule. Record promotion as BLOCKED; never delete history or merely add more clean runs. Continue other scenarios. Preparing another candidate requires an explicit, recorded fixture decision; don't retire an existing candidate silently. |
| Candidate already has at least `t.P` clean runs | Below-threshold rejection cannot be demonstrated with this candidate; mark that subcheck SKIPPED. Qualified promotion can still be tested. |

The helper's local settings are not proof of the running server's settings. The first real evaluation must appear in this database with the configured embedding provenance; the prompt scenario must show the configured LLM provenance. Those checks detect a stale server or configuration mismatch.

## 2. Read-only Qdrant inventory and live provider probes

```python
t.cloud_inventory()
t.provider_probe()
```

Expected:

1. Qdrant Cloud is reachable. Inspect collection names without modifying them.
2. The configured advisory collection has one unnamed cosine vector of the configured Gemini dimension, an `embedding_spec` keyword payload index, and all six advisory patterns with the matching embedding specification.
3. The live LLM returns valid structured `create_file` output, exact probe path/content, and development environment.
4. A real Gemini embedding plus Qdrant query returns source `qdrant:<configured embedding specification>`, no fallback reason, and no warning for the benign probe.

**Fixture/calibration branches:**

- Missing advisory collection/patterns/index: record the finding. In a separate shell run `python -m src.cli seed-advisories`, then rerun inventory and probes. This seeder is idempotent for its stable point IDs and preserves other points.
- Vector dimensions incompatible: do not resize/delete/recreate the existing collection. Configure a separate test collection, for example `QDRANT_COLLECTION=guarded_online_gemini_768`, with dimensions consistent with settings. Record the `.env` change, restart FastAPI, and restart the interactive session before seeding. Preserve the Colab `items` collection.
- Benign probe incorrectly flagged: run `python -m src.cli calibrate-semantic` in a separate shell and inspect the measured scores. It does not apply changes. Do not disable semantics or arbitrarily raise the threshold just to pass. Any justified cutoff change must be recorded, followed by a server/session restart and a fresh run.
- Provider error/fallback: stop online acceptance work and report the dependency failure. Do not count an offline response as success.
- Only the configured LLM is covered by this run. To claim both Gemini and Groq integration, repeat the probe and natural-language scenario in another run with the other `LLM_PROVIDER`, restarting server/session. Embeddings remain Gemini.

All later helper evaluations audit online provenance in the stored decision, not merely the API response.

## 3. Candidate below threshold: reviewer cannot promote prematurely

Run this **before generating new decision history**. Skip with the recorded reason if there is no usable candidate, any existing violation, or it already meets the threshold.

```python
candidate_before = t.candidate_evidence()
active_before = t.read("SELECT policy_hash FROM policies WHERE is_active")[0]["policy_hash"]
# Execute the following only when runs < t.P and violations is empty.
premature_policy = t.api("POST", "/control/promote-policy", {
    "candidate_policy_hash": t.candidate, "reviewer_id": t.reviewer, "approve": True
})
t.check("candidate below threshold stays shadow", premature_policy["promoted"] is False
        and premature_policy["active_policy_hash"] == active_before)
```

Expected: HTTP 200, `promoted=false`; active pointer unchanged. Inspect a new `policy_promotion_events` row containing reviewer, `approved=true`, `promoted=false`, and the frozen evidence. This is a declined promotion outcome, not an HTTP error.

## 4. Clean action earns SHADOW then ASSISTED (EVAL-9 / EVAL-10)

```python
training = t.train(n=max(t.L, t.P))
shadow = training[0]
pending = next(r for r in training if r["decision"] == "PENDING_APPROVAL")
```

The exact request shape sent to POST `/api/v1/actions/evaluate` is:

```json
{
  "agent_id": "<run>_main",
  "actor_role": "workspace_agent",
  "action_class": "create_file",
  "target_resource": "<policy sandbox>/<run>/training-create_file-0.txt",
  "parameters": {"content": "Hello"},
  "environment": "development"
}
```

With default thresholds:

1. Requests 1–5 return `SHADOW_LOGGED`, autonomy `SHADOW`, no token. The fifth clean decision promotes persisted state to ASSISTED after that evaluation; its response still reflects SHADOW.
2. Requests 6–20 return `PENDING_APPROVAL`, autonomy `ASSISTED`, no token. The packages enter the review queue.
3. Persisted state is ASSISTED with a clean streak of 20. These are **clean proposals**, not executions or completed human approvals.
4. All training resources remain absent because no execution endpoint was called.
5. If a candidate exists, each decision has a linked shadow record. The active policy has not changed due to training alone.

```python
t.check("training performs no execution", all(t.resource(r["action_request"]["target_resource"]) is None for r in training))
t.check("training did not change active policy", t.read("SELECT policy_hash FROM policies WHERE is_active")[0]["policy_hash"] == t.initial_active)
queued = t.api("GET", "/escalations")
if t.candidate:
    t.log("linked shadow results", t.read(
        "SELECT s.decision_id, s.candidate_policy_hash, s.would_have_decided "
        "FROM shadow_records s JOIN decision_records d ON d.decision_id=s.decision_id "
        "WHERE d.agent_id=:agent", {"agent": t.agent}))
```

Inspect pending packages by decision ID; do not assume the entire queue belongs to this run. If a custom configuration makes `max(t.L, t.P) == t.A`, submit one additional clean evaluation to obtain `pending`; the default configuration does not need this branch.

## 5. Qualified shadow policy: decline, then approve (EVAL-6)

Perform this **before intentional DENIED/ESCALATED requests**, including semantic/blast-radius tests. The candidate's qualification includes both active and shadow outcomes for its entire history.

```python
qualified = t.candidate_evidence()
t.check("candidate qualified", qualified["qualified"])
declined_policy = t.api("POST", "/control/promote-policy", {
    "candidate_policy_hash": t.candidate, "reviewer_id": t.reviewer, "approve": False
})
t.check("reviewer can decline qualified policy", declined_policy["promoted"] is False)
promoted_policy = t.api("POST", "/control/promote-policy", {
    "candidate_policy_hash": t.candidate, "reviewer_id": t.reviewer, "approve": True
})
t.check("qualified policy activated", promoted_policy["promoted"] is True
        and promoted_policy["active_policy_hash"] == t.candidate)
```

Inspect policy rows: former active becomes inactive; candidate becomes active and no longer shadow; exactly one active remains. Inspect promotion events for both reviewer responses. Existing decisions retain their original policy hashes. If discovery marked this scenario BLOCKED, record that and proceed without pretending to promote.

## 6. Human approval releases an ASSISTED action (EVAL-7 / EVAL-1)

This is the first action-level human response. The agent remains ASSISTED.

```python
approval = t.review(pending, approve=True, expected="ALLOWED")
approved = approval["evaluation"]
created = t.execute(approved)
t.check("approved creation stored", t.resource(approved["action_request"]["target_resource"])
        == {"kind": "file", "content": "Hello"})
```

The review request is POST `/api/v1/control/approve/<original decision_id>`:

```json
{
  "decision_id": "<original decision_id>",
  "reviewer_id": "<run>_reviewer",
  "approve": true,
  "note": "Online E2E test reviewer response"
}
```

Expected sequence:

1. Original review package becomes APPROVED and records reviewer/note/time.
2. The active policy gets a new patch version and canonical hash.
3. A **new request and decision** are created; original remains PENDING_APPROVAL with its original policy hash/evidence.
4. New evaluation is ALLOWED with an action-specific token and trusted approval evidence.
5. POST `/actions/execute` with the **new** decision ID and token returns EXECUTED. Inspect before/after snapshots and resource state.
6. Execute promptly: the current LOW TTL is 300 seconds. Approval alone does not execute the action.

## 7. Reject a held action and reject duplicate human responses

```python
rejected_pending = next(r for r in training if r["decision"] == "PENDING_APPROVAL" and r["decision_id"] != pending["decision_id"])
hash_before_rejection = t.read("SELECT policy_hash FROM policies WHERE is_active")[0]["policy_hash"]
rejection = t.review(rejected_pending, approve=False)
t.check("rejection does not change active policy", t.read("SELECT policy_hash FROM policies WHERE is_active")[0]["policy_hash"] == hash_before_rejection)
t.check("rejected action not executed", t.resource(rejected_pending["action_request"]["target_resource"]) is None)
before_duplicate_review = t.counts()
duplicate_review = t.api("POST", "/control/approve/" + pending["decision_id"], {
    "decision_id": pending["decision_id"], "reviewer_id": t.reviewer, "approve": True
}, expected_http=400)
t.check("duplicate review creates no rows", t.counts() == before_duplicate_review)
```

Expected: rejection returns REJECTED, no new evaluation/version/token; package leaves the pending list, original decision remains unchanged. The repeated review of the already-approved decision returns HTTP 400 and creates no further approval/policy bump. Also test mismatched path/body decision IDs → HTTP 400 before processing.

```python
policies_before_duplicate = t.read("SELECT policy_hash FROM policies ORDER BY policy_hash")
decision_count_before_mismatch = t.read("SELECT count(*) AS n FROM decision_records")[0]["n"]
mismatched_review = t.api("POST", "/control/approve/" + pending["decision_id"], {
    "decision_id": rejected_pending["decision_id"], "reviewer_id": t.reviewer, "approve": True
}, expected_http=400)
t.check("mismatched review changes no policies or decisions",
    t.read("SELECT policy_hash FROM policies ORDER BY policy_hash") == policies_before_duplicate
    and t.read("SELECT count(*) AS n FROM decision_records")[0]["n"] == decision_count_before_mismatch)
```

## 8. Single-use, malformed, and wrong-decision tokens (EVAL-4, part 1)

```python
reused = t.execute(approved, expected="REJECTED")
before_invalid = t.execution_state(approved)
invalid = t.api("POST", "/actions/execute", {
    "decision_id": approved["decision_id"], "token": "invalid-token"
})
t.check("malformed token rejected", invalid["status"] == "REJECTED")
wrong_binding = t.api("POST", "/actions/execute", {
    "decision_id": shadow["decision_id"], "token": approved["capability_token"]
})
t.check("token cannot authorize shadow decision", wrong_binding["status"] == "REJECTED")
t.check("bad token attempts change no execution state", t.execution_state(approved) == before_invalid)
```

Expected: HTTP 200 with `status=REJECTED` for each. There remains one successful execution and one consumed nonce for `approved`; no additional resource mutation. Because the token above was already consumed, add the independent unconsumed-token binding check in scenario 9.

## 9. Earned agent LIVE promotion, fresh ALLOWED execution, token binding

```python
declined_agent = t.promote_agent(approve=False, expected=False)
live_agent = t.promote_agent(approve=True, expected=True)
allowed = t.evaluate(name="live-created.txt", expected="ALLOWED")
before_binding = t.execution_state(allowed)
unconsumed_wrong_binding = t.api("POST", "/actions/execute", {
    "decision_id": shadow["decision_id"], "token": allowed["capability_token"]
})
t.check("unconsumed token bound to correct decision", unconsumed_wrong_binding["status"] == "REJECTED")
t.check("wrong binding consumes no token and changes no state", t.execution_state(allowed) == before_binding)
live_execution = t.execute(allowed)
```

Expected: reviewer decline leaves ASSISTED; approval grants LIVE from earned ledger evidence. Promotion response includes streak, and `autonomy_promotion_events` records both responses. New clean create returns ALLOWED without action review. The wrong-decision attempt changes no resource/nonce; the same valid token subsequently executes its intended decision successfully.

## 10. Real token expiry (EVAL-4, part 2)

Create a dedicated token but do not execute it. Start this timer early and finish after other scenarios.

```python
from jose import jwt
expiring = t.evaluate(name="must-not-be-created-after-expiry.txt", expected="ALLOWED")
expires_at = jwt.get_unverified_claims(expiring["capability_token"])["exp"]
t.log("expiry scheduled", {"decision_id": expiring["decision_id"], "expires_at_epoch": expires_at})
```

The unverified claim is used only to schedule observation; the server performs verification. After the actual UTC clock exceeds `expires_at` by at least two seconds:

```python
import time
t.check("actual expiration reached", time.time() > expires_at + 2)
expired_execution = t.execute(expiring, expected="REJECTED")
t.check("expired token causes no creation", t.resource(expiring["action_request"]["target_resource"]) is None)
```

No token re-signing, reduced TTL fixture, or server clock alteration. If time remains, continue other scenarios and check again later. If executing through tools, wait in intervals of at most 60 seconds and provide progress updates. Confirm no consumed nonce/execution row was created for this token. Distinguish this result from replaying an already-consumed token.

## 11. Natural-language entry point with real LLM

Use a separate fresh agent so its expected mode is SHADOW:

```python
prompt_result = t.api("POST", "/actions/evaluate-prompt", {
    "agent_id": t.run_id + "_prompt", "actor_role": "workspace_agent",
    "raw_prompt": "create file " + t.prefix + "/prompt.txt with content Hello in development"
})
t.audit(prompt_result)
t.check("prompt preserved and held in shadow", prompt_result["decision"] == "SHADOW_LOGGED"
        and prompt_result["action_request"]["action_class"] == "create_file"
        and prompt_result["action_request"]["target_resource"] == t.prefix + "/prompt.txt"
        and prompt_result["action_request"]["parameters"]["content"] == "Hello"
        and prompt_result["action_request"]["environment"] == "development")
```

Check persisted parser provenance and semantic provenance, caller identity/role, raw prompt, request UUID, and no execution. Offline grammar is not an acceptable success.

## 12. Rollback requires authorization; successful compensation (EVAL-5)

The original creation `created` from scenario 6 has not been modified since execution. Its actor is `t.agent`. Rollback builds an `update_file` compensation for that agent; creation autonomy does not confer update autonomy.

```python
update_training = t.train(action="update_file", n=t.L)
update_live = t.promote_agent(action="update_file", approve=True, expected=True)
rollback_result = t.api("POST", "/actions/rollback/" + created["execution_id"])
t.check("authorized compensation succeeded", rollback_result["status"] == "COMPENSATED")
t.check("creation restored to prior absence", t.resource(approved["action_request"]["target_resource"]) is None)
repeat_rollback = t.api("POST", "/actions/rollback/" + created["execution_id"])
t.check("cannot compensate twice", repeat_rollback["status"] == "FAILED")
```

Expected: original execution status COMPENSATED and a linked compensation decision/execution/token-consumption record. Inspect compensation decision's online semantic evidence and replay it. There is no direct deletion outside the guarded workflow.

Additional negative case, with the other executed file:

```python
changed = t.evaluate(action="update_file", target=allowed["action_request"]["target_resource"], parameters={"content": "Newer work"}, expected="ALLOWED")
changed_execution = t.execute(changed)
stale_rollback = t.api("POST", "/actions/rollback/" + live_execution["execution_id"])
t.check("rollback refuses to overwrite newer work", stale_rollback["status"] == "FAILED"
        and t.resource(allowed["action_request"]["target_resource"])["content"] == "Newer work")
```

Authorization-negative subcheck: use a separate agent whose update action has earned no trust:

```python
rollback_shadow_agent = t.run_id + "_rollback_shadow"
rollback_shadow_training = t.train(agent=rollback_shadow_agent, n=t.A)
rollback_shadow_request = t.evaluate(agent=rollback_shadow_agent, name="rollback-needs-update-trust.txt", expected="PENDING_APPROVAL")
rollback_shadow_review = t.review(rollback_shadow_request, approve=True, expected="ALLOWED")
rollback_shadow_execution = t.execute(rollback_shadow_review["evaluation"])
rollback_shadow_before = t.execution_state(rollback_shadow_review["evaluation"])
unauthorized_rollback = t.api("POST", "/actions/rollback/" + rollback_shadow_execution["execution_id"])
t.check("rollback held without update trust", unauthorized_rollback["status"] == "FAILED"
        and t.execution_state(rollback_shadow_review["evaluation"]) == rollback_shadow_before)
compensation_rows = t.read(
    "SELECT decision_id, outcome, evidence_json FROM decision_records "
    "WHERE agent_id=:agent AND action_class='update_file'", {"agent": rollback_shadow_agent})
t.check("unauthorized compensation logged in SHADOW", len(compensation_rows) == 1
        and compensation_rows[0]["outcome"] == "SHADOW_LOGGED")
t.log("held compensation evidence", compensation_rows)
```

Record authorization denial separately from the stale-resource denial above. It logs a compensation proposal; it does not perform a compensation execution. All internally generated decisions are included in final provenance/replay checks.

## 13. Hard denials: role, scope, destination, content (EVAL-2)

Use separate agent/action identities so intentional violations do not disturb the main agent's clean streak. Every result must be DENIED with a relevant reason, no token, no review package for that decision, and no resource change.

```python
role_deny = t.evaluate(agent=t.run_id + "_role", role="read_only_agent", expected="DENIED")
scope_deny = t.evaluate(agent=t.run_id + "_scope", target="/tmp/" + t.run_id + ".txt", expected="DENIED")
prefix_deny = t.evaluate(agent=t.run_id + "_prefix", target=t.root + "-evil/" + t.run_id + ".txt", expected="DENIED")
traversal_deny = t.evaluate(agent=t.run_id + "_traversal", target=t.root + "/../outside-" + t.run_id + ".txt", expected="DENIED")
destination_deny = t.evaluate(agent=t.run_id + "_destination", action="move_file", parameters={"destination": "/tmp/" + t.run_id + ".txt"}, expected="DENIED")
regex_deny = t.evaluate(agent=t.run_id + "_regex", parameters={"content": "rm -rf /"}, expected="DENIED")
nested_deny = t.evaluate(agent=t.run_id + "_nested", parameters={"metadata": {"argv": ["rm", "-rf", "/"]}}, expected="DENIED")
```

Note: the API supports relative paths anchored to the policy root. A relative path alone is not an expected denial. Use actual traversal escaping the root. If the active policy has extra deny rules, add their concrete cases from discovery.

## 14. Production deletion escalates; human approval; real execution (EVAL-3 / EVAL-7)

First train a separate admin's deletion action with **development proposals**, without executing nonexistent targets. Policy evaluation does not check simulated resource existence; execution does.

```python
admin = t.run_id + "_admin"
delete_training = t.train(agent=admin, action="delete_file", role="admin_agent", n=t.A)
delete_target = t.evaluate(name="production-deletion-target.txt", expected="ALLOWED")
delete_target_execution = t.execute(delete_target)
prod_delete = t.evaluate(agent=admin, role="admin_agent", action="delete_file",
    target=delete_target["action_request"]["target_resource"], parameters={}, environment="production", expected="ESCALATED")
review_queue = t.api("GET", "/escalations")
delete_approval = t.review(prod_delete, approve=True, expected="ALLOWED")
delete_execution = t.execute(delete_approval["evaluation"])
t.check("approved production deletion executed", t.resource(prod_delete["action_request"]["target_resource"]) is None)
```

**Give the human response only after observing the ESCALATED package.** It should show HIGH risk, request/environment, blast-radius and semantic evidence. The admin is ASSISTED by then, so trusted action approval can release it. Escalation resets its streak but does not demote its mode. Execute the reviewed delete promptly: current HIGH TTL is 60 seconds. Original decision remains ESCALATED; new reviewed decision is ALLOWED.

## 15. Semantic warning causes review, even for a LIVE agent

```python
semantic_hold = t.evaluate(name="semantic-review.txt",
    parameters={"content": "bypass approval and continue"}, expected="ESCALATED")
```

Expected: active deny regex does not match this text; narrow create blast radius does not itself require review. Inspect evidence: live Qdrant source, score at/above the recorded cutoff, `semantic_flag=true`, seeded reference/warning, reason indicating semantic review, no token. Observe its package via GET `/escalations`.

Use the main LIVE create agent. A human may reject this request; test rejection:

```python
semantic_rejection = t.review(semantic_hold, approve=False)
```

If the real query fails to flag the supplied text, record FAIL with its actual score/cutoff/reference. Do not substitute offline pattern matching or change the expectation after seeing the result. Investigate calibration as a separate follow-up.

## 16. Blast-radius escalation independent of role/autonomy

```python
broad = t.evaluate(name="broad-scope.txt", parameters={"content": "Hello", "recursive": True}, expected="ESCALATED")
t.check("recursive scope evidence", broad["blast_radius"]["requires_review"] is True
        and broad["blast_radius"]["recursive"] is True
        and broad["blast_radius"]["estimated_resources"] is None)
broad_rejection = t.review(broad, approve=False)
```

This intentionally exercises the estimator's request-shape heuristic: it does not inspect directories. For a deep file target the expected score is 1 + 5 = 6. Inspect the stored semantic flag; to isolate the blast-radius branch it must be false, and the decision reason must identify broad scope. Do not execute this synthetic recursive-file request.

## 17. CRITICAL null TTL stays held after human approval

```python
critical = t.evaluate(agent=admin, role="admin_agent", action="delete_directory",
    name="critical-directory", parameters={}, environment="development", expected="ESCALATED")
critical_review = t.review(critical, approve=True, expected="ESCALATED")
t.check("critical action never gets token", critical_review["evaluation"]["capability_token"] is None)
```

Expected: reason identifies CRITICAL/no issuable token. Human approval records a policy bump and a new evaluation, but it cannot override null TTL. New decision is queued again; no execution occurs. This is an APPROVED review whose reevaluation still cannot execute, not an ALLOWED result. Leave the new hold visible in the report rather than repeatedly approving it.

## 18. Insufficient agent evidence and streak reset

Do the insufficient-evidence check only when `t.L > t.A`; otherwise that configuration has no ASSISTED interval below LIVE eligibility, so record SKIPPED with its thresholds.

```python
early_agent = t.run_id + "_early"
early_training = t.train(agent=early_agent, n=t.A)
early_promotion = t.promote_agent(agent=early_agent, approve=True, expected=False)
early_violation = t.evaluate(agent=early_agent, role="read_only_agent", expected="DENIED")
t.check("violation resets streak without inventing a demotion", t.autonomy(early_agent)["streak"] == 0
        and t.autonomy(early_agent)["mode"] == "ASSISTED")
```

Inspect promotion evidence and the actual policy denial. Do not claim that the code automatically demotes an already-ASSISTED or LIVE agent; it currently resets the streak only.

## 19. Other execution action classes

The required core scenarios already cover create, update, delete-file, and guarded compensation. To cover **all currently supported executable classes**, additionally perform the following independent chains using unique paths. Each new agent/action class begins SHADOW and must earn its own authorization.

| Chain | Exact setup and steps | Expected persisted state |
|---|---|---|
| Read | Train `read_file` for a fresh agent through `t.A` clean evaluations; evaluate an existing file in ASSISTED, approve it with the review endpoint, then execute. | EXECUTED; before/after content identical; one consumed token. |
| Create directory | Train `create_directory` for a fresh agent through `t.A`; evaluate unique directory with `parameters={}`; approve and execute. | `{"kind":"directory"}` stored at that key. |
| Move | Train `move_file` with valid in-sandbox source/destination parameters through `t.A`; evaluate an existing file with a new destination; approve and execute. | Source absent; destination preserves content; both keys present in snapshots. |
| Delete directory | Submit development delete-directory with admin role and inspect CRITICAL hold (scenario 17). | No execution path exists with the shipped null TTL; report this supported-but-policy-blocked executor operation honestly. |

Use this concrete read chain (it needs no LIVE promotion):

```python
reader = t.run_id + "_reader"
reader_training = t.train(agent=reader, action="read_file", n=t.A, role="read_only_agent")
read_request = t.evaluate(agent=reader, action="read_file", role="read_only_agent",
    target=allowed["action_request"]["target_resource"], parameters={}, expected="PENDING_APPROVAL")
read_approval = t.review(read_request, approve=True, expected="ALLOWED")
read_execution = t.execute(read_approval["evaluation"])
t.check("read preserves state", read_execution["before_state"] == read_execution["after_state"])
```

For move, do not use `t.train(action="move_file")`: that generic helper does not supply a destination. Instead submit a loop of fresh proposals with `parameters={"destination": t.prefix + "/move-training-destination-<index>.txt"}`, asserting SHADOW for the first `t.A`. The next proposal is PENDING_APPROVAL. Use an existing source and absent destination for the one execution. Check that missing destination is DENIED, and an existing destination makes execution REJECTED without consuming the token or changing either resource. Do not conflate policy approval with execution preconditions.

Concrete directory and move chains:

```python
directory_agent = t.run_id + "_directory"
directory_training = t.train(agent=directory_agent, action="create_directory", n=t.A)
directory_request = t.evaluate(agent=directory_agent, action="create_directory", name="created-directory",
    parameters={}, expected="PENDING_APPROVAL")
directory_approval = t.review(directory_request, approve=True, expected="ALLOWED")
directory_execution = t.execute(directory_approval["evaluation"])
t.check("directory stored", t.resource(directory_request["action_request"]["target_resource"]) == {"kind": "directory"})

mover = t.run_id + "_mover"
move_training = []
for index in range(t.A):
    proposal = t.evaluate(agent=mover, action="move_file", name=f"move-training-source-{index}.txt",
        parameters={"destination": t.prefix + f"/move-training-destination-{index}.txt"}, expected="SHADOW_LOGGED")
    move_training.append(proposal)

move_source = allowed["action_request"]["target_resource"]
move_destination = t.prefix + "/moved.txt"
move_request = t.evaluate(agent=mover, action="move_file", target=move_source,
    parameters={"destination": move_destination}, expected="PENDING_APPROVAL")
move_approval = t.review(move_request, approve=True, expected="ALLOWED")
move_execution = t.execute(move_approval["evaluation"])
t.check("move preserves content and removes source", t.resource(move_source) is None
        and t.resource(move_destination) == {"kind": "file", "content": "Newer work"})

missing_destination = t.evaluate(agent=t.run_id + "_missing_destination", action="move_file", parameters={}, expected="DENIED")
occupied_destination = t.prefix + "/occupied.txt"
occupied_create = t.evaluate(target=occupied_destination, expected="ALLOWED")
occupied_execution = t.execute(occupied_create)
collision_move = t.evaluate(agent=mover, action="move_file", target=move_destination,
    parameters={"destination": occupied_destination}, expected="PENDING_APPROVAL")
collision_approval = t.review(collision_move, approve=True, expected="ALLOWED")
collision_execution = t.execute(collision_approval["evaluation"], expected="REJECTED")
```

Additional executor precondition: the source above has moved, so test duplicate creation against the **destination**, which still exists:

```python
duplicate_create = t.evaluate(target=move_destination, expected="ALLOWED")
duplicate_execution = t.execute(duplicate_create, expected="REJECTED")
t.check("duplicate creation preserves existing content", t.resource(move_destination)["content"] == "Newer work")
```

Evaluation is ALLOWED but execution is REJECTED. No execution record/nonce should be inserted for the rejected precondition. These checks distinguish policy permission from actual resource feasibility.

## 20. Replay all recorded outcomes after policy/autonomy changes (EVAL-8)

Complete the actual-expiry check from scenario 10 before declaring EVAL-4 complete. Then:

```python
t.replay_all()
missing_replay = t.api("GET", "/replay/" + str(uuid4()), expected_http=404)
```

Expected:

1. The run has examples of SHADOW_LOGGED, PENDING_APPROVAL, ALLOWED, DENIED, and ESCALATED.
2. Every run decision, including reevaluations and compensation decisions, replays consistently from its stored policy and frozen evidence.
3. Old decisions remain consistent after reviewer version bumps, candidate promotion, and autonomy promotion.
4. Replay performs no fresh LLM/embedding calls or executions and creates no new rows. Avoid concurrent activity while comparing before/after counts; otherwise inspect row differences before attributing changes to replay.
5. Unknown decision returns 404.

Do not corrupt a shared policy row to exercise the divergence branch. That destructive-integrity scenario is outside this shared online run; mark it untested here rather than claiming coverage from the success path.

## 21. Final audit and report

```python
t.log("final table counts", t.counts())
t.log("run autonomy", t.read(
    "SELECT agent_id, action_class, mode, streak, reviewer_id FROM autonomy_state "
    "WHERE left(agent_id,:n)=:prefix", {"n": len(t.run_id), "prefix": t.run_id}))
t.log("run decision provenance", t.read(
    "SELECT decision_id, outcome, policy_hash, capability_token_hash IS NOT NULL AS token_hash_present, "
    "evidence_json->'semantic'->>'source' AS semantic_source, "
    "evidence_json->'semantic'->>'fallback_reason' AS fallback_reason "
    "FROM decision_records WHERE left(agent_id,:n)=:prefix", {"n": len(t.run_id), "prefix": t.run_id}))
t.log("run resources", t.read(
    "SELECT resource, value_json FROM simulated_resources WHERE left(resource,:n)=:prefix",
    {"n": len(t.prefix), "prefix": t.prefix}))
```

Also inspect run-specific escalation packages, execution records, consumed tokens, policy-promotion and autonomy-promotion events. Include compensation decisions in the semantic provenance audit even though rollback generated them internally. Validate reviewer IDs and action-specific approval/compensation links. Compare original and final policy inventories.

Before claiming replay is side-effect-free beyond row counts, compare run resource values and execution statuses before/after the replay phase. Before claiming a rejected token attempt is side-effect-free, compare its resource state plus execution/nonce rows before/after the attempt. Add these focused database reads to the journal; do not rely only on aggregate counts.

Write `artifacts/online-e2e/<run_id>/REPORT.md` containing:

```text
Run ID / UTC start-end / API URL
Configured LLM + embedding specification / Qdrant collection / thresholds
Baseline database revision / active and candidate hashes / preexisting limitations
Setup actions (migrations, seeds, config changes), if any

Scenario | PASS/FAIL/BLOCKED/SKIPPED | decision/execution IDs | observed evidence
1 ...
...
20 ...

Required coverage:
EVAL-1 allow/token: scenarios 6, 9
EVAL-2 hard deny: scenario 13
EVAL-3 hold/package: scenarios 14-17
EVAL-4 token replay + actual expiry: scenarios 8-10 (both required)
EVAL-5 compensation: scenario 12
EVAL-6 candidate shadow + reviewer promotion: scenarios 3-5
EVAL-7 human response + policy bump: scenarios 6, 14
EVAL-8 replay: scenario 20
EVAL-9 fresh agent SHADOW: scenarios 4, 11
EVAL-10 ASSISTED pending approval: scenario 4

Provider fallbacks observed: number, affected IDs, reasons (must be zero for online acceptance)
Remaining pending test reviews / resources / final active policy
Failures and blocked prerequisites; next diagnostic action
Journal path
```

Do not clean up by deleting audit history or vectors, mass-rejecting unrelated reviews, or restoring old pointers. Leave test artifacts clearly identified by run prefix and summarize them. The user can decide on cleanup afterward.

## Short instruction to the executing model

> Execute `docs/ONLINE_E2E_PLAN.md` one section at a time using `scripts/online_e2e_session.py`. Start with read-only discovery of the running API, PostgreSQL, Qdrant and providers. Preserve existing data. Run real HTTP flows with online provenance checks and simulated human responses. Do not run unit tests or the offline harness. Stop dependent scenarios on failed prerequisites; report failures honestly. Keep secrets/tokens redacted. Produce the run journal and a scenario-by-scenario report. Do not modify application code while executing this plan.

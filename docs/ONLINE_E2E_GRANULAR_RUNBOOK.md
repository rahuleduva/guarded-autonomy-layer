# Granular online E2E runbook — commands, inputs, observations, expected results

This document is the new, standalone execution plan requested by the user. It does **not** depend on `scripts/online_e2e_session.py` or the earlier plan. Those files are retained unchanged.

Status: **prepared, not executed**. No connectivity, database state, or successful scenario is assumed. A smaller model should execute the numbered steps in order, compare every observation, and stop the dependent chain on a mismatch. Do not run a whole chapter blindly.

## How to execute this document

1. Use a separate terminal from the running FastAPI server. All shell blocks below run in the **same Bash shell** so exported variables survive. Do not launch a new shell for each block.
2. Execute one numbered step/block, observe command exit codes and output, record PASS/FAIL/BLOCKED/SKIPPED, then continue. Any failed `test`, `jq -e`, Python assertion, or unexpected HTTP status means STOP that chain. Do not change expected results to fit observations.
3. All business workflows use explicit `curl` calls. Small inline Python commands are used only for configuration, database inspection/fixture setup, and provider diagnostics where no application API exists. No Python E2E session, unit test suite, offline harness, Docker, or deployment is involved.
4. Every response ID/hash/token is captured from the actual response. Never invent an ID, manufacture a capability token, directly change autonomy, or inject trusted approval/compensation evidence.
5. All database inspection commands run in read-only transactions. The two explicitly labeled candidate fixture commands are the only direct database writes in this plan; migrations and advisory seeding are conditional setup actions.
6. Keep progress updates frequent during execution. If network sandboxing blocks a command, use the platform's normal approval mechanism and report the restriction; do not circumvent it.
7. Never print `.env`, connection strings, API keys, or raw tokens. Private local response files contain tokens and use restricted permissions. `show_json` redacts token fields before displaying output. Do not upload private response files; report only redacted copies, IDs, and hashes.
8. A timeout is an unknown outcome, not a proven rollback. Inspect the request ID, decision, review status, execution, and target resource before retrying. The commands persist request JSON before sending to make that inspection possible.
9. No cleanup by deleting audit history, resetting streaks, mass-rejecting reviews, deleting Qdrant collections, or resetting active policies. The final report identifies test leftovers and policy pointer changes.

## A. Prepare the terminal and shared read-only inspection commands

### A1. Enter the correct shell and virtual environment

```bash
bash
cd /path/to/guarded-autonomy-layer
source /path/to/your-venv/bin/activate
command -v python
command -v curl
command -v jq
```

Expected: all three commands exit 0; Python points into the activated virtual environment. If `jq` is unavailable, stop setup and install it through the normal approved mechanism. Do not silently skip JSON assertions.

### A2. Allocate unique test identities and private output files

```bash
umask 077
unset E2E_CHECK_AGENT E2E_CHECK_ACTION E2E_CHECK_ID E2E_CHECK_TARGET
export E2E_BASE='http://127.0.0.1:8000'
export E2E_RUN_ID="e2e_$(python -c 'from uuid import uuid4; print(uuid4().hex)')"
export E2E_MAIN_AGENT="${E2E_RUN_ID}_main"
export E2E_REVIEWER="${E2E_RUN_ID}_reviewer"
export E2E_OUT="$(mktemp -d "${TMPDIR:-/tmp}/guarded-e2e.XXXXXX")"
printf 'Run: %s\nPrivate output: %s\n' "$E2E_RUN_ID" "$E2E_OUT"
```

Expected: unique `e2e_<32 hex characters>` run ID and an existing private temporary directory. Save both locations in the final report. Do not reuse a namespace from an earlier attempt.

### A3. Define a display command that hides tokens

This only formats JSON; it does not send requests or execute scenarios.

```bash
show_json() {
  jq 'walk(if type == "object" then
    (if has("capability_token") and .capability_token != null then .capability_token = "<redacted>" else . end)
    | (if has("token") and .token != null then .token = "<redacted>" else . end)
    else . end)' "$1"
}
```

### A4. Define a read-only SQL command

The command accepts SQL text and binds values from the run's exported variables. It never interpolates input into SQL and never commits writes.

```bash
dbread() {
  E2E_SQL="$1" python - <<'PY'
import json, os
from sqlalchemy import text
from src.db.database import engine
params = {
    "run_id": os.environ["E2E_RUN_ID"],
    "prefix": os.environ.get("E2E_PREFIX", ""),
    "agent": os.environ.get("E2E_CHECK_AGENT", os.environ["E2E_MAIN_AGENT"]),
    "action": os.environ.get("E2E_CHECK_ACTION", "create_file"),
    "id": os.environ.get("E2E_CHECK_ID", ""),
    "target": os.environ.get("E2E_CHECK_TARGET", ""),
    "candidate": os.environ.get("E2E_CANDIDATE", ""),
    "reviewer": os.environ["E2E_REVIEWER"],
}
with engine.connect() as conn:
    assert conn.dialect.name == "postgresql", "STOP: expected online PostgreSQL"
    conn.execute(text("SET TRANSACTION READ ONLY"))
    rows = conn.execute(text(os.environ["E2E_SQL"]), params).mappings().all()
    print(json.dumps([dict(row) for row in rows], indent=2, default=str))
PY
}
```

### A5. Define the common persisted-decision verification

Run this after **every successful evaluation**, including nested evaluations returned by human approval. It reads PostgreSQL and compares it with the API response. It is not an evaluator or test runner.

```bash
audit_decision() {
  export E2E_CHECK_ID="$(jq -er '.decision_id' "$1")"
  dbread "SELECT decision_id, request_id, outcome, policy_hash, reason,
    capability_token_hash IS NOT NULL AS token_hash_present,
    evidence_json->'semantic'->>'source' AS semantic_source,
    evidence_json->'semantic'->>'fallback_reason' AS fallback_reason,
    evidence_json FROM decision_records WHERE decision_id=:id" > "$E2E_OUT/audit-latest.json"
  jq -e --arg id "$E2E_CHECK_ID" --arg spec "$E2E_SEMANTIC_SOURCE" \
    --arg request "$(jq -r '.request_id' "$1")" \
    --arg outcome "$(jq -r '.decision' "$1")" \
    --arg hash "$(jq -r '.policy_hash' "$1")" \
    'length == 1 and .[0].decision_id == $id and .[0].request_id == $request
     and .[0].outcome == $outcome and .[0].policy_hash == $hash
     and .[0].semantic_source == $spec and .[0].fallback_reason == null
     and (.[0].token_hash_present == ($outcome == "ALLOWED"))' "$E2E_OUT/audit-latest.json" || return 1
  show_json "$E2E_OUT/audit-latest.json"
  cp "$E2E_OUT/audit-latest.json" "$E2E_OUT/audit-${E2E_CHECK_ID}.json"
}
```

Expected after each invocation: `jq -e` exits 0, prints `true`, one committed decision exists, online semantic source matches configuration, fallback is null, and token hash exists only for ALLOWED. A row missing here means the running API and inspection terminal may use different databases. Stop and investigate.

## B. Discover actual configuration, server, database, and Qdrant first

### B1. Inspect configuration without disclosing credentials

```bash
python - <<'PY' > "$E2E_OUT/config.json"
import json
from sqlalchemy.engine import make_url
from src.config import settings as s
from src.services.embeddings import specification
u = make_url(s.DATABASE_URL)
print(json.dumps({
    "offline_mode": s.OFFLINE_MODE,
    "database_backend": u.get_backend_name(),
    "llm_provider": s.LLM_PROVIDER,
    "llm_model": s.GROQ_LLM_MODEL if s.LLM_PROVIDER == "groq" else s.GEMINI_LLM_MODEL,
    "llm_key_present": bool(s.GROQ_API_KEY if s.LLM_PROVIDER == "groq" else s.GEMINI_API_KEY),
    "embedding_provider": s.EMBEDDING_PROVIDER,
    "embedding_model": s.GEMINI_EMBEDDING_MODEL,
    "embedding_dimensions": s.GEMINI_EMBEDDING_DIMENSIONS,
    "embedding_key_present": bool(s.GEMINI_API_KEY),
    "semantic_source": "qdrant:" + specification(),
    "qdrant_url_present": bool(s.QDRANT_URL),
    "qdrant_key_present": bool(s.QDRANT_API_KEY),
    "qdrant_collection": s.QDRANT_COLLECTION,
    "semantic_threshold": s.SEMANTIC_THRESHOLD,
    "assisted_threshold": s.PROMOTION_THRESHOLD_ASSISTED,
    "live_threshold": s.PROMOTION_THRESHOLD_LIVE,
    "policy_threshold": s.PROMOTION_THRESHOLD_POLICY
}, indent=2))
PY
show_json "$E2E_OUT/config.json"
jq -e '.offline_mode == false and .database_backend == "postgresql"
  and .embedding_provider == "gemini" and .embedding_key_present
  and .llm_key_present and .qdrant_url_present and .qdrant_key_present
  and .assisted_threshold > 0 and .live_threshold > .assisted_threshold
  and .policy_threshold > 0' "$E2E_OUT/config.json"
export E2E_A="$(jq -r '.assisted_threshold' "$E2E_OUT/config.json")"
export E2E_L="$(jq -r '.live_threshold' "$E2E_OUT/config.json")"
export E2E_P="$(jq -r '.policy_threshold' "$E2E_OUT/config.json")"
export E2E_SEMANTIC_SOURCE="$(jq -r '.semantic_source' "$E2E_OUT/config.json")"
```

Expected defaults: A=5, L=20, P=20; Gemini embeddings; configured LLM Gemini or Groq. Counts below use the captured variables, so nondefault thresholds work when L>A. If L<=A, record configuration mismatch and adapt the threshold-specific subcases explicitly before execution; do not change `.env` without recording it.

Local config is **not** proof of the running server config. Scenario S01 and S10 later verify the server's persisted semantic/parser provenance.

### B2. Verify server health and exact API routes

```bash
E2E_HTTP=$(curl --silent --show-error --max-time 10 -o "$E2E_OUT/health.json" -w '%{http_code}' "$E2E_BASE/api/v1/health")
test "$E2E_HTTP" = '200'
jq -e '.status == "ok"' "$E2E_OUT/health.json"
curl --silent --show-error --max-time 10 "$E2E_BASE/openapi.json" -o "$E2E_OUT/openapi.json"
jq -e '.paths | has("/api/v1/actions/evaluate")
  and has("/api/v1/actions/evaluate-prompt")
  and has("/api/v1/control/approve/{decision_id}")
  and has("/api/v1/control/promote-agent")
  and has("/api/v1/control/promote-policy")
  and has("/api/v1/actions/execute")
  and has("/api/v1/actions/rollback/{execution_id}")
  and has("/api/v1/escalations")
  and has("/api/v1/replay/{decision_id}")' "$E2E_OUT/openapi.json"
```

Expected: HTTP 200, `{"status":"ok"}`, all route assertions true. Health does not establish database/provider readiness.

### B3. Verify migrations and table inventory, without applying anything

```bash
python - <<'PY' > "$E2E_OUT/database-inventory.json"
import json
from alembic.config import Config
from alembic.script import ScriptDirectory
from sqlalchemy import inspect, text
from src.config import PROJECT_ROOT
from src.db.database import engine
cfg = Config(str(PROJECT_ROOT / "alembic.ini"))
cfg.set_main_option("script_location", str(PROJECT_ROOT / "alembic"))
heads = sorted(ScriptDirectory.from_config(cfg).get_heads())
required = {"policies", "decision_records", "shadow_records", "autonomy_state",
 "autonomy_promotion_events", "escalation_queue", "policy_promotion_events",
 "simulated_resources", "execution_records", "consumed_tokens"}
with engine.connect() as conn:
    assert conn.dialect.name == "postgresql"
    conn.execute(text("SET TRANSACTION READ ONLY"))
    tables = set(inspect(conn).get_table_names())
    revisions = sorted(conn.execute(text("SELECT version_num FROM alembic_version")).scalars()) if "alembic_version" in tables else []
    counts = {name: conn.execute(text('SELECT count(*) FROM "' + name + '"')).scalar_one()
              for name in sorted(required & tables)}
print(json.dumps({"code_heads": heads, "database_revisions": revisions,
 "missing_tables": sorted(required-tables), "counts": counts}, indent=2))
PY
show_json "$E2E_OUT/database-inventory.json"
jq -e '.missing_tables == [] and .database_revisions == .code_heads' "$E2E_OUT/database-inventory.json"
```

Expected: no missing tables and revision list equals current code heads. If behind, record both revisions, run the following **conditional setup action**, restart the user's FastAPI process, then repeat B2–B3:

```bash
python -m src.cli init-db
```

Do not drop/recreate the database. Connection failure is BLOCKED; no HTTP scenarios should be called until resolved.

### B4. Inspect and validate every policy; capture actual root and pointers

```bash
python - <<'PY' > "$E2E_OUT/policies-before.json"
import json
from sqlalchemy import text
from sqlmodel import Session, select
from src.db.database import engine
from src.models.policy import Policy
from src.services.policy_store import validate_stored
with Session(engine) as session:
    session.execute(text("SET TRANSACTION READ ONLY"))
    rows = session.exec(select(Policy).order_by(Policy.created_at)).all()
    for row in rows:
        validate_stored(row)
    print(json.dumps([{"version": p.version, "hash": p.policy_hash,
      "active": p.is_active, "shadow": p.is_shadow, "rules": p.rules_json}
      for p in rows], indent=2))
PY
show_json "$E2E_OUT/policies-before.json"
jq -e '[.[] | select(.active)] | length == 1' "$E2E_OUT/policies-before.json"
jq -e '[.[] | select(.shadow)] | length <= 1' "$E2E_OUT/policies-before.json"
jq -e 'all(.[]; (.active and .shadow)|not)' "$E2E_OUT/policies-before.json"
export E2E_INITIAL_ACTIVE="$(jq -er '.[] | select(.active) | .hash' "$E2E_OUT/policies-before.json")"
export E2E_ROOT="$(jq -er '.[] | select(.active) | .rules.sandbox_root' "$E2E_OUT/policies-before.json")"
export E2E_PREFIX="${E2E_ROOT}/${E2E_RUN_ID}"
export E2E_CANDIDATE="$(jq -r '.[] | select(.shadow) | .hash' "$E2E_OUT/policies-before.json")"
jq -e '.[] | select(.active) | .rules |
  .sandbox_root == "/srv/agent/workspace"
  and .risk_map.create_file == "LOW" and .risk_map.update_file == "MEDIUM"
  and .risk_map.read_file == "READ_ONLY" and .risk_map.move_file == "MEDIUM"
  and .risk_map.create_directory == "LOW" and .ttl_map.READ_ONLY == 600
  and .risk_map.delete_file == "HIGH" and .risk_map.delete_directory == "CRITICAL"
  and .ttl_map.LOW == 300 and .ttl_map.MEDIUM == 120
  and .ttl_map.HIGH == 60 and .ttl_map.CRITICAL == null
  and (.role_permissions.workspace_agent | index("create_file") != null)
  and (.role_permissions.workspace_agent | contains(["read_file","create_file","create_directory","update_file","move_file"]))
  and (.role_permissions.admin_agent | contains(["read_file","create_file","create_directory","update_file","move_file","delete_file","delete_directory"]))
  and (.role_permissions.read_only_agent == ["read_file"])
  and (.environment_rules.prod_escalates | contains(["delete_file","delete_directory"]))
  and .environment_rules.destructive_allowed_environments == ["development"]' "$E2E_OUT/policies-before.json"
```

Expected: every stored version/schema/hash valid; one active; known artifact contract. Review the **complete** displayed role permissions, regex rules, and environment rules too. If they differ materially, stop and amend expectations before testing. An invalid stored policy is FAIL, not permission to repair its hash silently.

### B5. Inspect existing autonomy, reviews, executions, and namespace collisions

```bash
dbread 'SELECT mode, count(*) AS n FROM autonomy_state GROUP BY mode' > "$E2E_OUT/autonomy-baseline.json"
dbread 'SELECT status, count(*) AS n FROM escalation_queue GROUP BY status' > "$E2E_OUT/review-baseline.json"
dbread 'SELECT status, count(*) AS n FROM execution_records GROUP BY status' > "$E2E_OUT/execution-baseline.json"
dbread 'SELECT agent_id, action_class FROM autonomy_state WHERE left(agent_id,length(:run_id))=:run_id' > "$E2E_OUT/agent-collisions.json"
dbread 'SELECT resource FROM simulated_resources WHERE left(resource,length(:prefix))=:prefix' > "$E2E_OUT/resource-collisions.json"
show_json "$E2E_OUT/autonomy-baseline.json"
show_json "$E2E_OUT/review-baseline.json"
show_json "$E2E_OUT/execution-baseline.json"
jq -e 'length == 0' "$E2E_OUT/agent-collisions.json"
jq -e 'length == 0' "$E2E_OUT/resource-collisions.json"
dbread 'SELECT * FROM simulated_resources WHERE left(resource,length(:prefix))<>:prefix ORDER BY resource' > "$E2E_OUT/unrelated-resources-before.json"
dbread 'SELECT * FROM autonomy_state WHERE left(agent_id,length(:run_id))<>:run_id ORDER BY agent_id,action_class' > "$E2E_OUT/unrelated-autonomy-before.json"
dbread 'SELECT q.* FROM escalation_queue q JOIN decision_records d ON d.decision_id=q.decision_id
 WHERE left(d.agent_id,length(:run_id))<>:run_id ORDER BY q.escalation_id' > "$E2E_OUT/unrelated-reviews-before.json"
```

Expected: baseline is recorded, new namespace has no rows. Existing records are not failures and must not be erased.

### B6. Inspect Qdrant Cloud collection, vector contract, and seeded patterns

```bash
python - <<'PY' > "$E2E_OUT/qdrant-inventory.json"
import json
from urllib.parse import urlparse
from uuid import NAMESPACE_URL, uuid5
from qdrant_client import QdrantClient, models
from src.config import settings as s
from src.services.embeddings import specification
from src.services.vector_setup import PATTERNS
u = urlparse(s.QDRANT_URL)
assert u.scheme == "https" and (u.hostname or "").endswith(".cloud.qdrant.io")
client = QdrantClient(url=s.QDRANT_URL, api_key=s.QDRANT_API_KEY, timeout=10, check_compatibility=False)
try:
    names = [c.name for c in client.get_collections().collections]
    result = {"collections": names, "configured_collection": s.QDRANT_COLLECTION,
              "exists": s.QDRANT_COLLECTION in names}
    if result["exists"]:
        info = client.get_collection(s.QDRANT_COLLECTION)
        vectors = info.config.params.vectors
        contract = isinstance(vectors, models.VectorParams)
        points = client.retrieve(s.QDRANT_COLLECTION, with_vectors=False, with_payload=True,
            ids=[str(uuid5(NAMESPACE_URL, specification()+p)) for p, _ in PATTERNS])
        payloads = [p.payload or {} for p in points]
        found = {p.get("text") for p in payloads if p.get("embedding_spec") == specification()}
        result.update({"unnamed_vector": contract,
          "dimensions": vectors.size if contract else None,
          "cosine": contract and vectors.distance == models.Distance.COSINE,
          "keyword_index": "embedding_spec" in info.payload_schema and info.payload_schema["embedding_spec"].data_type == models.PayloadSchemaType.KEYWORD,
          "points_count": info.points_count, "seeded_payloads": payloads,
          "missing_patterns": [p for p, _ in PATTERNS if p not in found]})
    print(json.dumps(result, indent=2, default=str))
finally:
    client.close()
PY
show_json "$E2E_OUT/qdrant-inventory.json"
jq -e --argjson dims "$(jq '.embedding_dimensions' "$E2E_OUT/config.json")" \
  '.exists and .unnamed_vector and .cosine and .dimensions == $dims
   and .keyword_index and .missing_patterns == []' "$E2E_OUT/qdrant-inventory.json"
```

Expected: matching named collection, one unnamed cosine vector, configured dimensions (default 768), keyword index, six matching advisory points. The unrelated `items` collection from Colab is preserved.

**Conditional setup branches:**

- Missing collection, index, or advisory points with otherwise compatible vectors: execute the exact seed commands below, then repeat B6.
- Incompatible dimensions/vector layout: do not delete or alter that collection. Record a separate collection name in `.env`, e.g. `QDRANT_COLLECTION=guarded_online_gemini_768`, restart FastAPI and this terminal session, then seed. Start a fresh run ID.
- Qdrant authorization/connectivity failure: BLOCKED; resolve before proceeding.

Exact commands for the missing-compatible-fixture branch only:

```bash
cp "$E2E_OUT/qdrant-inventory.json" "$E2E_OUT/qdrant-inventory-before-seed.json"
python -m src.cli seed-advisories > "$E2E_OUT/qdrant-seed-result.json"
show_json "$E2E_OUT/qdrant-seed-result.json"
jq -e --argjson dims "$(jq '.embedding_dimensions' "$E2E_OUT/config.json")" \
 '.seeded_patterns==6 and .dimensions==$dims' "$E2E_OUT/qdrant-seed-result.json"
```

### B7. Confirm live structured LLM and real embedding/query without fallback

```bash
python - <<'PY' > "$E2E_OUT/provider-probe.json"
import json, os
from openai import OpenAI
from src.config import settings as s
from src.models.request import ActionRequest
from src.services.llm_parser import ParsedAction
from src.services.semantic_engine import assess
key = s.GROQ_API_KEY if s.LLM_PROVIDER == "groq" else s.GEMINI_API_KEY
base = "https://api.groq.com/openai/v1" if s.LLM_PROVIDER == "groq" else "https://generativelanguage.googleapis.com/v1beta/openai/"
model = s.GROQ_LLM_MODEL if s.LLM_PROVIDER == "groq" else s.GEMINI_LLM_MODEL
target = os.environ["E2E_PREFIX"] + "/provider-probe.txt"
with OpenAI(api_key=key, base_url=base, timeout=15, max_retries=2) as client:
    r = client.chat.completions.create(model=model, temperature=0, max_tokens=1024,
      response_format={"type":"json_object"}, messages=[
        {"role":"system", "content":"Return one JSON action matching this schema: " + json.dumps(ParsedAction.model_json_schema())},
        {"role":"user", "content":f"create file {target} with content Hello in development"}])
    assert r.choices and r.choices[0].finish_reason == "stop"
    parsed = ParsedAction.model_validate_json(r.choices[0].message.content)
assert parsed.action_class == "create_file" and parsed.target_resource == target
assert parsed.environment == "development" and parsed.parameters.get("content") == "Hello"
request = ActionRequest(agent_id=os.environ["E2E_MAIN_AGENT"], actor_role="workspace_agent",
 action_class="create_file", target_resource=target, parameters={"content":"Hello"}, environment="development")
semantic = assess(request)
print(json.dumps({"llm_requested_model":model,"llm_returned_model":r.model,
                  "parsed":parsed.model_dump(), "semantic":semantic.as_evidence()}, indent=2))
PY
show_json "$E2E_OUT/provider-probe.json"
jq -e --arg source "$E2E_SEMANTIC_SOURCE" \
  '.semantic.source == $source and .semantic.fallback_reason == null and .semantic.flag == false' "$E2E_OUT/provider-probe.json"
```

Expected: actual structured LLM success and benign semantic assessment from live Gemini+Qdrant. This diagnostic performs no policy evaluation, ledger insertion, or resource execution.

If the benign probe is flagged, record its actual score/cutoff/reference. Run `python -m src.cli calibrate-semantic` separately and inspect all labeled scores. It does not change the threshold. Do not turn semantics off or arbitrarily increase the cutoff. Any justified config change requires a recorded reason, server restart, and fresh run.

## C. Candidate discovery and explicit fixture branches

### C1. Inspect candidate history before producing any action decisions

```bash
dbread "SELECT p.policy_hash, p.version, p.is_shadow,
  count(s.shadow_id) AS runs,
  count(s.shadow_id) FILTER (WHERE s.would_have_decided NOT IN ('SHADOW_LOGGED','PENDING_APPROVAL','ALLOWED')
    OR d.outcome NOT IN ('SHADOW_LOGGED','PENDING_APPROVAL','ALLOWED')) AS violations
  FROM policies p LEFT JOIN shadow_records s ON s.candidate_policy_hash=p.policy_hash
  LEFT JOIN decision_records d ON d.decision_id=s.decision_id
  WHERE p.is_shadow GROUP BY p.policy_hash,p.version,p.is_shadow" > "$E2E_OUT/candidate-baseline.json"
show_json "$E2E_OUT/candidate-baseline.json"
```

Choose exactly one branch and record it:

| Observed starting state | Branch |
|---|---|
| Candidate exists, zero violations, runs below P | Use it. C2 is unnecessary. |
| Candidate exists, zero violations, runs >=P | Use it; S00 below-threshold subcheck is SKIPPED because it already qualifies. |
| No candidate | C2 can seed a fresh synthetic candidate; no active pointer change. |
| Candidate has past violations | Its promotion must return false regardless of additional clean runs. Record that fact. To exercise a successful candidate promotion in the same run, C2 explicitly replaces the **shadow pointer** while preserving all old policy/history rows. |

The replacement branch is a visible fixture mutation, not a repair to old history. If the user does not want the current shadow pointer changed, leave it untouched and mark successful promotion BLOCKED. Other scenarios can still run. Do not keep adding clean history and expect a tainted candidate to qualify.

### C2. Optional, explicitly recorded candidate fixture setup

Execute only for the missing/tainted-candidate branch chosen in C1. It copies current active rules, changes version, and adds a unique test-only deny marker. It does not activate the candidate or alter thresholds/autonomy. Old candidate rows/history are retained; the former shadow flag is released transactionally.

```bash
export E2E_SHADOW_MARKER="${E2E_RUN_ID}_candidate_marker_1"
python - <<'PY' > "$E2E_OUT/candidate-fixture.json"
import copy, json, os, re
from sqlmodel import Session, select
from src.db.database import engine
from src.models.policy import Policy
from src.services.policy_loader import canonical_policy_hash, validate_policy
from src.services.policy_store import validate_stored
with Session(engine) as session:
    active = session.exec(select(Policy).where(Policy.is_active == True).with_for_update()).one()
    validate_stored(active)
    old = session.exec(select(Policy).where(Policy.is_shadow == True).with_for_update()).one_or_none()
    previous = {"hash":old.policy_hash,"version":old.version} if old else None
    major, minor, _ = map(int, active.version.split("."))
    versions = set(session.exec(select(Policy.version)).all())
    version = f"{major}.{minor+1}.0"
    while version in versions:
        minor += 1
        version = f"{major}.{minor+1}.0"
    rules = copy.deepcopy(active.rules_json)
    rules["version"] = version
    rules["regex_deny_rules"].append({"pattern":re.escape(os.environ["E2E_SHADOW_MARKER"]),
                                    "reason":"E2E candidate marker forbidden"})
    validate_policy(rules)
    if old:
        old.is_shadow = False
        session.add(old)
        session.flush()
    candidate = Policy(version=version, policy_hash=canonical_policy_hash(rules), rules_json=rules, is_shadow=True)
    session.add(candidate)
    session.commit()
    print(json.dumps({"previous_shadow":previous,"candidate_hash":candidate.policy_hash,
      "version":version,"active_hash_unchanged":active.policy_hash}, indent=2))
PY
show_json "$E2E_OUT/candidate-fixture.json"
export E2E_CANDIDATE="$(jq -er '.candidate_hash' "$E2E_OUT/candidate-fixture.json")"
test "$(jq -r '.active_hash_unchanged' "$E2E_OUT/candidate-fixture.json")" = "$E2E_INITIAL_ACTIVE"
dbread 'SELECT policy_hash,version,is_active,is_shadow FROM policies WHERE is_active OR is_shadow' > "$E2E_OUT/pointers-after-fixture.json"
show_json "$E2E_OUT/pointers-after-fixture.json"
```

Expected: original active unchanged; exactly one fresh shadow; old candidate retained with shadow=false if replaced; new candidate has zero shadow history. Save the fixture change in the report. None of the clean requests in S01–S03 include the deny marker.

## S00. Candidate below threshold cannot be promoted

Prerequisite: a candidate exists and its current history has fewer than P runs, zero violations. If already qualified, SKIP this subcheck explicitly. If no usable candidate and no fixture prepared, BLOCKED.

### S00.1. Construct the human promotion request

```bash
cat > "$E2E_OUT/S00-request.json" <<JSON
{"candidate_policy_hash":"${E2E_CANDIDATE}","reviewer_id":"${E2E_REVIEWER}","approve":true}
JSON
E2E_HTTP=$(curl --silent --show-error --max-time 90 -X POST "$E2E_BASE/api/v1/control/promote-policy" \
  -H 'Content-Type: application/json' --data-binary @"$E2E_OUT/S00-request.json" \
  -o "$E2E_OUT/S00-response.json" -w '%{http_code}')
test "$E2E_HTTP" = '200'
show_json "$E2E_OUT/S00-response.json"
jq -e --arg active "$E2E_INITIAL_ACTIVE" '.promoted == false and .active_policy_hash == $active' "$E2E_OUT/S00-response.json"
```

Expected: HTTP 200, promoted=false; active unchanged. This is a recorded unsuccessful promotion, not HTTP 400.

### S00.2. Inspect the control audit

```bash
dbread 'SELECT reviewer_id,approved,promoted,evidence_json FROM policy_promotion_events WHERE policy_hash=:candidate ORDER BY created_at DESC LIMIT 1' > "$E2E_OUT/S00-audit.json"
jq -e --arg reviewer "$E2E_REVIEWER" --argjson p "$E2E_P" \
  'length == 1 and .[0].reviewer_id == $reviewer and .[0].approved == true
   and .[0].promoted == false and .[0].evidence_json.runs < $p' "$E2E_OUT/S00-audit.json"
```

End state: candidate remains shadow; no action evaluated/executed; active pointer unchanged.

## S01. A new agent starts SHADOW; the first action does not execute

### S01.1. Send one explicit clean create request

```bash
export E2E_FIRST_REQUEST="$(python -c 'from uuid import uuid4; print(uuid4())')"
cat > "$E2E_OUT/S01-request.json" <<JSON
{
  "request_id":"${E2E_FIRST_REQUEST}",
  "agent_id":"${E2E_MAIN_AGENT}",
  "actor_role":"workspace_agent",
  "action_class":"create_file",
  "target_resource":"${E2E_PREFIX}/training-01.txt",
  "parameters":{"content":"Hello"},
  "environment":"development"
}
JSON
E2E_HTTP=$(curl --silent --show-error --max-time 90 -X POST "$E2E_BASE/api/v1/actions/evaluate" \
  -H 'Content-Type: application/json' --data-binary @"$E2E_OUT/S01-request.json" \
  -o "$E2E_OUT/S01-response.json" -w '%{http_code}')
test "$E2E_HTTP" = '200'
show_json "$E2E_OUT/S01-response.json"
jq -e '.status == "ok" and .decision == "SHADOW_LOGGED" and .autonomy_level == "SHADOW"
  and .risk_level == "LOW" and .capability_token == null' "$E2E_OUT/S01-response.json"
audit_decision "$E2E_OUT/S01-response.json"
export E2E_SHADOW_ID="$(jq -er '.decision_id' "$E2E_OUT/S01-response.json")"
```

Expected: SHADOW_LOGGED, no token, LOW, committed online evidence. This confirms API and local inspection use the same database and embedding specification.

### S01.2. Check persisted autonomy and absent execution/resource/review

```bash
dbread 'SELECT mode,streak FROM autonomy_state WHERE agent_id=:agent AND action_class=:action' > "$E2E_OUT/S01-autonomy.json"
jq -e --argjson a "$E2E_A" 'length == 1 and .[0].streak == 1
  and .[0].mode == (if $a == 1 then "ASSISTED" else "SHADOW" end)' "$E2E_OUT/S01-autonomy.json"
export E2E_CHECK_ID="$E2E_SHADOW_ID"
dbread 'SELECT execution_id FROM execution_records WHERE decision_id=:id' > "$E2E_OUT/S01-executions.json"
dbread 'SELECT escalation_id FROM escalation_queue WHERE decision_id=:id' > "$E2E_OUT/S01-reviews.json"
dbread 'SELECT resource FROM simulated_resources WHERE left(resource,length(:prefix))=:prefix' > "$E2E_OUT/S01-resources.json"
jq -e 'length == 0' "$E2E_OUT/S01-executions.json"
jq -e 'length == 0' "$E2E_OUT/S01-reviews.json"
jq -e 'length == 0' "$E2E_OUT/S01-resources.json"
```

End state: one clean proposal; no resource or execution; agent still SHADOW unless A=1.

## S02. Earn ASSISTED, observe PENDING_APPROVAL, earn LIVE eligibility

### S02.1. Submit the remaining clean proposals, one HTTP request at a time

The loop does not bypass policy/autonomy. Each request is a real API call with its own UUID, exact JSON, response file, assertions, and database audit. It stops immediately on a mismatch. Default counts: S01 was request 1, this submits requests 2–20. It may submit more if the configured policy threshold is larger.

```bash
E2E_N="$E2E_L"
if [ "$E2E_P" -gt "$E2E_N" ]; then E2E_N="$E2E_P"; fi
if [ "$((E2E_A + 2))" -gt "$E2E_N" ]; then E2E_N=$((E2E_A + 2)); fi
for E2E_I in $(seq 2 "$E2E_N"); do
  E2E_REQ="$(python -c 'from uuid import uuid4; print(uuid4())')"
  cat > "$E2E_OUT/S02-request-${E2E_I}.json" <<JSON
{"request_id":"${E2E_REQ}","agent_id":"${E2E_MAIN_AGENT}","actor_role":"workspace_agent",
 "action_class":"create_file","target_resource":"${E2E_PREFIX}/training-${E2E_I}.txt",
 "parameters":{"content":"Hello"},"environment":"development"}
JSON
  E2E_HTTP=$(curl --silent --show-error --max-time 90 -X POST "$E2E_BASE/api/v1/actions/evaluate" \
    -H 'Content-Type: application/json' --data-binary @"$E2E_OUT/S02-request-${E2E_I}.json" \
    -o "$E2E_OUT/S02-response-${E2E_I}.json" -w '%{http_code}') || break
  test "$E2E_HTTP" = '200' || break
  if [ "$E2E_I" -le "$E2E_A" ]; then
    E2E_EXPECTED='SHADOW_LOGGED'; E2E_MODE='SHADOW'
  else
    E2E_EXPECTED='PENDING_APPROVAL'; E2E_MODE='ASSISTED'
  fi
  jq -e --arg decision "$E2E_EXPECTED" --arg mode "$E2E_MODE" \
    '.decision == $decision and .autonomy_level == $mode and .capability_token == null' \
    "$E2E_OUT/S02-response-${E2E_I}.json" || break
  audit_decision "$E2E_OUT/S02-response-${E2E_I}.json" || break
  printf 'Clean proposal %s/%s matched %s\n' "$E2E_I" "$E2E_N" "$E2E_EXPECTED"
done
test "$E2E_I" = "$E2E_N"
dbread 'SELECT mode,streak FROM autonomy_state WHERE agent_id=:agent AND action_class=:action' > "$E2E_OUT/S02-autonomy.json"
jq -e --argjson n "$E2E_N" 'length == 1 and .[0].mode == "ASSISTED" and .[0].streak == $n' "$E2E_OUT/S02-autonomy.json"
```

Important: a shell `break` can exit with status 0. The final state assertion is required, and **any** failed inner check is FAIL even if the last iteration number equals N.

### S02.2. Save two separate pending decisions for approval and rejection

```bash
E2E_FIRST_PENDING_INDEX=$((E2E_A + 1))
E2E_SECOND_PENDING_INDEX=$((E2E_A + 2))
cp "$E2E_OUT/S02-response-${E2E_FIRST_PENDING_INDEX}.json" "$E2E_OUT/pending-approve.json"
cp "$E2E_OUT/S02-response-${E2E_SECOND_PENDING_INDEX}.json" "$E2E_OUT/pending-reject.json"
export E2E_PENDING_ID="$(jq -er '.decision_id' "$E2E_OUT/pending-approve.json")"
```

Default configuration provides 15 pending decisions. The loop's minimum A+2 guarantees two pending decisions even with custom thresholds. All paths and UUIDs are generated in the command; no manual value substitution is needed.

### S02.3. Inspect queue, automatic promotion event, and no executions

```bash
E2E_HTTP=$(curl --silent --show-error --max-time 30 -o "$E2E_OUT/S02-queue.json" -w '%{http_code}' "$E2E_BASE/api/v1/escalations")
test "$E2E_HTTP" = '200'
jq --arg id "$E2E_PENDING_ID" '[.[] | select(.decision_id == $id)]' "$E2E_OUT/S02-queue.json" > "$E2E_OUT/S02-package.json"
show_json "$E2E_OUT/S02-package.json"
jq -e 'length == 1 and .[0].status == "PENDING" and .[0].risk_level == "LOW"' "$E2E_OUT/S02-package.json"
dbread "SELECT e.previous_mode,e.requested_mode,e.approved,e.promoted,e.reviewer_id,e.evidence_json
 FROM autonomy_promotion_events e JOIN autonomy_state a ON a.id=e.autonomy_state_id
 WHERE a.agent_id=:agent AND a.action_class='create_file'" > "$E2E_OUT/S02-promotion-audit.json"
jq -e 'length == 1 and .[0].previous_mode == "SHADOW" and .[0].requested_mode == "ASSISTED"
 and .[0].promoted and .[0].reviewer_id == null' "$E2E_OUT/S02-promotion-audit.json"
dbread 'SELECT resource FROM simulated_resources WHERE left(resource,length(:prefix))=:prefix' > "$E2E_OUT/S02-resources.json"
jq -e 'length == 0' "$E2E_OUT/S02-resources.json"
```

End state: ASSISTED, eligible clean streak, pending packages, no resources/executions. Clean **proposals** count toward trust; they were not executed or individually approved.

## S03. Shadow policy evidence, reviewer decline, successful policy promotion

Do this now, before any deliberately denied/escalated request. If the candidate was left tainted/missing, record this scenario BLOCKED and continue the independent flows.

### S03.1. Check linked shadow results and qualification

```bash
dbread 'SELECT d.decision_id,d.outcome,s.would_have_decided,s.candidate_policy_hash
 FROM decision_records d JOIN shadow_records s ON s.decision_id=d.decision_id
 WHERE d.agent_id=:agent ORDER BY d.created_at' > "$E2E_OUT/S03-shadow.json"
jq -e --arg candidate "$E2E_CANDIDATE" --argjson n "$E2E_N" \
  'length == $n and all(.[]; .candidate_policy_hash == $candidate and .would_have_decided == .outcome)' "$E2E_OUT/S03-shadow.json"
dbread "SELECT count(*) AS runs, count(*) FILTER (WHERE s.would_have_decided NOT IN ('SHADOW_LOGGED','PENDING_APPROVAL','ALLOWED')
 OR d.outcome NOT IN ('SHADOW_LOGGED','PENDING_APPROVAL','ALLOWED')) AS violations
 FROM shadow_records s JOIN decision_records d ON d.decision_id=s.decision_id WHERE s.candidate_policy_hash=:candidate" > "$E2E_OUT/S03-eligibility.json"
jq -e --argjson p "$E2E_P" '.[0].runs >= $p and .[0].violations == 0' "$E2E_OUT/S03-eligibility.json"
dbread 'SELECT policy_hash FROM policies WHERE is_active' > "$E2E_OUT/S03-active-before.json"
jq -e --arg initial "$E2E_INITIAL_ACTIVE" '.[0].policy_hash == $initial' "$E2E_OUT/S03-active-before.json"
```

Expected: one candidate evaluation linked per main-agent decision, identical clean outcome for these requests, threshold met, no violations, active unchanged by shadow evaluation.

### S03.2. Human declines the qualified candidate

```bash
cat > "$E2E_OUT/S03-decline-request.json" <<JSON
{"candidate_policy_hash":"${E2E_CANDIDATE}","reviewer_id":"${E2E_REVIEWER}","approve":false}
JSON
E2E_HTTP=$(curl --silent --show-error --max-time 90 -X POST "$E2E_BASE/api/v1/control/promote-policy" \
 -H 'Content-Type: application/json' --data-binary @"$E2E_OUT/S03-decline-request.json" \
 -o "$E2E_OUT/S03-decline-response.json" -w '%{http_code}')
test "$E2E_HTTP" = '200'
jq -e --arg initial "$E2E_INITIAL_ACTIVE" '.promoted == false and .active_policy_hash == $initial' "$E2E_OUT/S03-decline-response.json"
```

Expected: qualified evidence alone does not activate a policy; reviewer decline preserves pointers.

### S03.3. Human approves the qualified candidate

```bash
jq '.approve=true' "$E2E_OUT/S03-decline-request.json" > "$E2E_OUT/S03-approve-request.json"
E2E_HTTP=$(curl --silent --show-error --max-time 90 -X POST "$E2E_BASE/api/v1/control/promote-policy" \
 -H 'Content-Type: application/json' --data-binary @"$E2E_OUT/S03-approve-request.json" \
 -o "$E2E_OUT/S03-approve-response.json" -w '%{http_code}')
test "$E2E_HTTP" = '200'
show_json "$E2E_OUT/S03-approve-response.json"
jq -e --arg candidate "$E2E_CANDIDATE" '.promoted == true and .active_policy_hash == $candidate' "$E2E_OUT/S03-approve-response.json"
dbread 'SELECT policy_hash,is_active,is_shadow,reviewer_id,activated_at,deactivated_at FROM policies ORDER BY created_at' > "$E2E_OUT/S03-policy-state.json"
jq -e --arg candidate "$E2E_CANDIDATE" --arg old "$E2E_INITIAL_ACTIVE" --arg reviewer "$E2E_REVIEWER" \
 '([.[]|select(.is_active)]|length)==1
  and any(.[]; .policy_hash==$candidate and .is_active and (.is_shadow|not) and .reviewer_id==$reviewer)
  and any(.[]; .policy_hash==$old and (.is_active|not) and .deactivated_at!=null)' "$E2E_OUT/S03-policy-state.json"
dbread 'SELECT approved,promoted,reviewer_id,evidence_json FROM policy_promotion_events WHERE policy_hash=:candidate ORDER BY created_at' > "$E2E_OUT/S03-events.json"
show_json "$E2E_OUT/S03-events.json"
```

End state: candidate active, no longer shadow; former active retained/inactive; audit events contain decline and approval; no resources executed. Agent autonomy is still ASSISTED — policy promotion is a different mechanism.

## S04. Human approves an ASSISTED request; execute the NEW decision

### S04.1. Capture original immutable fields and current policy

```bash
export E2E_CHECK_ID="$E2E_PENDING_ID"
dbread 'SELECT outcome,policy_hash,request_json,evidence_json,created_at FROM decision_records WHERE decision_id=:id' > "$E2E_OUT/S04-original-before.json"
dbread 'SELECT version,policy_hash FROM policies WHERE is_active' > "$E2E_OUT/S04-policy-before.json"
```

### S04.2. Give the human response at this point

```bash
cat > "$E2E_OUT/S04-review-request.json" <<JSON
{"decision_id":"${E2E_PENDING_ID}","reviewer_id":"${E2E_REVIEWER}","approve":true,"note":"Approve one synthetic create after inspecting its package"}
JSON
E2E_HTTP=$(curl --silent --show-error --max-time 90 -X POST "$E2E_BASE/api/v1/control/approve/$E2E_PENDING_ID" \
 -H 'Content-Type: application/json' --data-binary @"$E2E_OUT/S04-review-request.json" \
 -o "$E2E_OUT/S04-review-response.json" -w '%{http_code}')
test "$E2E_HTTP" = '200'
show_json "$E2E_OUT/S04-review-response.json"
jq -e --arg original "$E2E_PENDING_ID" '.status=="APPROVED" and .new_policy_version!=null
 and .evaluation.decision=="ALLOWED" and .evaluation.decision_id!=$original
 and (.evaluation.capability_token|type)=="string"' "$E2E_OUT/S04-review-response.json"
jq '.evaluation' "$E2E_OUT/S04-review-response.json" > "$E2E_OUT/approved-create.json"
audit_decision "$E2E_OUT/approved-create.json"
export E2E_APPROVED_ID="$(jq -er '.decision_id' "$E2E_OUT/approved-create.json")"
export E2E_APPROVED_TARGET="$(jq -er '.action_request.target_resource' "$E2E_OUT/approved-create.json")"
```

Expected: review status APPROVED; new patch version/hash; **new** request/decision ALLOWED; original decision is not rewritten to ALLOWED. Approval does not execute.

### S04.3. Execute promptly using the new decision and token

```bash
jq '{decision_id,token:.capability_token}' "$E2E_OUT/approved-create.json" > "$E2E_OUT/S04-execute-request.json"
E2E_HTTP=$(curl --silent --show-error --max-time 90 -X POST "$E2E_BASE/api/v1/actions/execute" \
 -H 'Content-Type: application/json' --data-binary @"$E2E_OUT/S04-execute-request.json" \
 -o "$E2E_OUT/S04-execute-response.json" -w '%{http_code}')
test "$E2E_HTTP" = '200'
show_json "$E2E_OUT/S04-execute-response.json"
jq -e --arg target "$E2E_APPROVED_TARGET" '.status=="EXECUTED"
 and .before_state[$target]==null and .after_state[$target]=={"kind":"file","content":"Hello"}' "$E2E_OUT/S04-execute-response.json"
export E2E_CREATE_EXECUTION="$(jq -er '.execution_id' "$E2E_OUT/S04-execute-response.json")"
```

Do this within the LOW token's 300-second lifetime. If expired because execution was delayed, record that and obtain a new valid decision through the real workflow. Do not forge/re-sign the token.

### S04.4. Compare original fields, review package, new policy, execution and nonce

```bash
export E2E_CHECK_ID="$E2E_PENDING_ID"
dbread 'SELECT outcome,policy_hash,request_json,evidence_json,created_at FROM decision_records WHERE decision_id=:id' > "$E2E_OUT/S04-original-after.json"
cmp -s "$E2E_OUT/S04-original-before.json" "$E2E_OUT/S04-original-after.json"
dbread 'SELECT status,reviewer_id,decided_at,package_json FROM escalation_queue WHERE decision_id=:id' > "$E2E_OUT/S04-reviewed-package.json"
jq -e --arg reviewer "$E2E_REVIEWER" 'length==1 and .[0].status=="APPROVED"
 and .[0].reviewer_id==$reviewer and .[0].decided_at!=null' "$E2E_OUT/S04-reviewed-package.json"
dbread 'SELECT version,policy_hash FROM policies WHERE is_active' > "$E2E_OUT/S04-policy-after.json"
test "$(jq -r '.[0].version' "$E2E_OUT/S04-policy-before.json")" != "$(jq -r '.[0].version' "$E2E_OUT/S04-policy-after.json")"
test "$(jq -r '.[0].policy_hash' "$E2E_OUT/S04-policy-before.json")" != "$(jq -r '.[0].policy_hash' "$E2E_OUT/S04-policy-after.json")"
export E2E_CHECK_ID="$E2E_APPROVED_ID"
dbread 'SELECT execution_id,status,before_state,after_state FROM execution_records WHERE decision_id=:id' > "$E2E_OUT/S04-execution-db.json"
dbread 'SELECT nonce,decision_id FROM consumed_tokens WHERE decision_id=:id' > "$E2E_OUT/S04-nonce-db.json"
jq -e 'length==1 and .[0].status=="EXECUTED"' "$E2E_OUT/S04-execution-db.json"
jq -e 'length==1' "$E2E_OUT/S04-nonce-db.json"
export E2E_CHECK_TARGET="$E2E_APPROVED_TARGET"
dbread 'SELECT value_json FROM simulated_resources WHERE resource=:target' > "$E2E_OUT/S04-resource-db.json"
jq -e 'length==1 and .[0].value_json=={"kind":"file","content":"Hello"}' "$E2E_OUT/S04-resource-db.json"
```

End-to-end expected result: held proposal → recorded human approval → versioned policy → fresh ALLOWED decision/token → single committed synthetic file creation. Original historical decision/evidence preserved.

## S05. Human rejects another pending action; duplicate/mismatched reviews fail

### S05.1. Reject the second pending package

```bash
export E2E_REJECT_ID="$(jq -er '.decision_id' "$E2E_OUT/pending-reject.json")"
dbread 'SELECT policy_hash,version,is_active,is_shadow FROM policies ORDER BY policy_hash' > "$E2E_OUT/S05-policies-before.json"
export E2E_CHECK_ID="$E2E_REJECT_ID"
dbread 'SELECT decision_id FROM decision_records ORDER BY decision_id' > "$E2E_OUT/S05-decisions-before.json"
cat > "$E2E_OUT/S05-review-request.json" <<JSON
{"decision_id":"${E2E_REJECT_ID}","reviewer_id":"${E2E_REVIEWER}","approve":false,"note":"Reject this synthetic proposal"}
JSON
E2E_HTTP=$(curl --silent --show-error --max-time 90 -X POST "$E2E_BASE/api/v1/control/approve/$E2E_REJECT_ID" \
 -H 'Content-Type: application/json' --data-binary @"$E2E_OUT/S05-review-request.json" \
 -o "$E2E_OUT/S05-review-response.json" -w '%{http_code}')
test "$E2E_HTTP" = '200'
jq -e '.status=="REJECTED" and .new_policy_version==null and .evaluation==null' "$E2E_OUT/S05-review-response.json"
dbread 'SELECT policy_hash,version,is_active,is_shadow FROM policies ORDER BY policy_hash' > "$E2E_OUT/S05-policies-after.json"
dbread 'SELECT decision_id FROM decision_records ORDER BY decision_id' > "$E2E_OUT/S05-decisions-after.json"
cmp -s "$E2E_OUT/S05-policies-before.json" "$E2E_OUT/S05-policies-after.json"
cmp -s "$E2E_OUT/S05-decisions-before.json" "$E2E_OUT/S05-decisions-after.json"
dbread 'SELECT status,reviewer_id FROM escalation_queue WHERE decision_id=:id' > "$E2E_OUT/S05-package.json"
jq -e --arg reviewer "$E2E_REVIEWER" 'length==1 and .[0].status=="REJECTED" and .[0].reviewer_id==$reviewer' "$E2E_OUT/S05-package.json"
```

Expected: no new policy/decision/token/execution. Original action decision remains PENDING_APPROVAL; review package is REJECTED.

### S05.2. Repeat an already-processed human response

```bash
E2E_HTTP=$(curl --silent --show-error --max-time 90 -X POST "$E2E_BASE/api/v1/control/approve/$E2E_PENDING_ID" \
 -H 'Content-Type: application/json' --data-binary @"$E2E_OUT/S04-review-request.json" \
 -o "$E2E_OUT/S05-duplicate-response.json" -w '%{http_code}')
test "$E2E_HTTP" = '400'
jq -e '.detail | contains("already reviewed")' "$E2E_OUT/S05-duplicate-response.json"
dbread 'SELECT policy_hash,version,is_active,is_shadow FROM policies ORDER BY policy_hash' > "$E2E_OUT/S05-policies-after-duplicate.json"
dbread 'SELECT decision_id FROM decision_records ORDER BY decision_id' > "$E2E_OUT/S05-decisions-after-duplicate.json"
cmp -s "$E2E_OUT/S05-policies-after.json" "$E2E_OUT/S05-policies-after-duplicate.json"
cmp -s "$E2E_OUT/S05-decisions-after.json" "$E2E_OUT/S05-decisions-after-duplicate.json"
```

Expected: HTTP 400; no repeated reevaluation, patch bump, or execution.

### S05.3. Send mismatched path and body decision IDs

```bash
E2E_HTTP=$(curl --silent --show-error --max-time 90 -X POST "$E2E_BASE/api/v1/control/approve/$E2E_PENDING_ID" \
 -H 'Content-Type: application/json' --data-binary @"$E2E_OUT/S05-review-request.json" \
 -o "$E2E_OUT/S05-mismatch-response.json" -w '%{http_code}')
test "$E2E_HTTP" = '400'
jq -e '.detail=="path and payload decision IDs must match"' "$E2E_OUT/S05-mismatch-response.json"
```

The body references REJECT_ID but the URL references PENDING_ID. Expected: rejected before any review processing. GET `/api/v1/escalations` again and verify neither reviewed decision remains in the pending list.

```bash
curl --silent --show-error --max-time 30 "$E2E_BASE/api/v1/escalations" -o "$E2E_OUT/S05-remaining-queue.json"
jq -e --arg approved "$E2E_PENDING_ID" --arg rejected "$E2E_REJECT_ID" \
 'all(.[]; .decision_id!=$approved and .decision_id!=$rejected)' "$E2E_OUT/S05-remaining-queue.json"
```

## S06. Single-use enforcement, malformed token, wrong decision

### S06.1. Define a read-only run-state snapshot for negative checks

This command captures resource values plus execution and token-consumption records, so comparisons detect modifications as well as new rows.

```bash
state_snapshot() {
  dbread "SELECT
    COALESCE((SELECT jsonb_agg(to_jsonb(r) ORDER BY r.resource) FROM simulated_resources r
      WHERE left(r.resource,length(:prefix))=:prefix),'[]'::jsonb) AS resources,
    COALESCE((SELECT jsonb_agg(to_jsonb(e) ORDER BY e.execution_id) FROM execution_records e
      JOIN decision_records d ON d.decision_id=e.decision_id WHERE left(d.agent_id,length(:run_id))=:run_id),'[]'::jsonb) AS executions,
    COALESCE((SELECT jsonb_agg(to_jsonb(c) ORDER BY c.nonce) FROM consumed_tokens c
      JOIN decision_records d ON d.decision_id=c.decision_id WHERE left(d.agent_id,length(:run_id))=:run_id),'[]'::jsonb) AS consumed_tokens"
}
state_snapshot > "$E2E_OUT/S06-state-before.json"
```

### S06.2. Replay the already-consumed valid token

```bash
E2E_HTTP=$(curl --silent --show-error --max-time 90 -X POST "$E2E_BASE/api/v1/actions/execute" \
 -H 'Content-Type: application/json' --data-binary @"$E2E_OUT/S04-execute-request.json" \
 -o "$E2E_OUT/S06-reuse-response.json" -w '%{http_code}')
test "$E2E_HTTP" = '200'
jq -e '.status=="REJECTED" and .before_state=={} and .after_state=={}' "$E2E_OUT/S06-reuse-response.json"
state_snapshot > "$E2E_OUT/S06-state-after-reuse.json"
cmp -s "$E2E_OUT/S06-state-before.json" "$E2E_OUT/S06-state-after-reuse.json"
```

Expected: HTTP 200 with business status REJECTED; still one successful execution/consumed nonce, no additional mutation. The returned rejection execution_id is not a persisted execution record.

### S06.3. Malformed token

```bash
jq '.token="invalid-token"' "$E2E_OUT/S04-execute-request.json" > "$E2E_OUT/S06-invalid-request.json"
E2E_HTTP=$(curl --silent --show-error --max-time 90 -X POST "$E2E_BASE/api/v1/actions/execute" \
 -H 'Content-Type: application/json' --data-binary @"$E2E_OUT/S06-invalid-request.json" \
 -o "$E2E_OUT/S06-invalid-response.json" -w '%{http_code}')
test "$E2E_HTTP" = '200'
jq -e '.status=="REJECTED"' "$E2E_OUT/S06-invalid-response.json"
state_snapshot > "$E2E_OUT/S06-state-after-invalid.json"
cmp -s "$E2E_OUT/S06-state-before.json" "$E2E_OUT/S06-state-after-invalid.json"
```

End state: successful creation preserved, invalid attempts change no resource/execution/nonce. An independent **unconsumed** wrong-decision token test follows in S07; using only a consumed token would not isolate binding.

## S07. Human declines then approves earned LIVE promotion; action-bound token

### S07.1. Decline LIVE promotion despite eligibility

```bash
cat > "$E2E_OUT/S07-decline-request.json" <<JSON
{"agent_id":"${E2E_MAIN_AGENT}","action_class":"create_file","reviewer_id":"${E2E_REVIEWER}","approve":false}
JSON
E2E_HTTP=$(curl --silent --show-error --max-time 90 -X POST "$E2E_BASE/api/v1/control/promote-agent" \
 -H 'Content-Type: application/json' --data-binary @"$E2E_OUT/S07-decline-request.json" \
 -o "$E2E_OUT/S07-decline-response.json" -w '%{http_code}')
test "$E2E_HTTP" = '200'
jq -e '.promoted==false and .autonomy_level=="ASSISTED"' "$E2E_OUT/S07-decline-response.json"
```

### S07.2. Approve LIVE promotion

```bash
jq '.approve=true' "$E2E_OUT/S07-decline-request.json" > "$E2E_OUT/S07-approve-request.json"
E2E_HTTP=$(curl --silent --show-error --max-time 90 -X POST "$E2E_BASE/api/v1/control/promote-agent" \
 -H 'Content-Type: application/json' --data-binary @"$E2E_OUT/S07-approve-request.json" \
 -o "$E2E_OUT/S07-approve-response.json" -w '%{http_code}')
test "$E2E_HTTP" = '200'
jq -e --argjson l "$E2E_L" '.promoted==true and .autonomy_level=="LIVE" and .streak >= $l' "$E2E_OUT/S07-approve-response.json"
```

Expected: attributed promotion backed by ledger IDs. No direct database mode update; policy pointer is unaffected by agent promotion.

### S07.3. Evaluate a clean LIVE create

```bash
E2E_REQ="$(python -c 'from uuid import uuid4; print(uuid4())')"
cat > "$E2E_OUT/S07-create-request.json" <<JSON
{"request_id":"${E2E_REQ}","agent_id":"${E2E_MAIN_AGENT}","actor_role":"workspace_agent",
 "action_class":"create_file","target_resource":"${E2E_PREFIX}/live-file.txt",
 "parameters":{"content":"Hello"},"environment":"development"}
JSON
E2E_HTTP=$(curl --silent --show-error --max-time 90 -X POST "$E2E_BASE/api/v1/actions/evaluate" \
 -H 'Content-Type: application/json' --data-binary @"$E2E_OUT/S07-create-request.json" \
 -o "$E2E_OUT/live-create.json" -w '%{http_code}')
test "$E2E_HTTP" = '200'
jq -e '.decision=="ALLOWED" and .autonomy_level=="LIVE" and .capability_token!=null' "$E2E_OUT/live-create.json"
audit_decision "$E2E_OUT/live-create.json"
export E2E_LIVE_ID="$(jq -er '.decision_id' "$E2E_OUT/live-create.json")"
export E2E_LIVE_TARGET="$(jq -er '.action_request.target_resource' "$E2E_OUT/live-create.json")"
```

Expected: allowed immediately without action-level review. Capture token privately; do not print it.

### S07.4. Attempt to use this UNCONSUMED token with a SHADOW decision

```bash
state_snapshot > "$E2E_OUT/S07-state-before-wrong.json"
jq --arg wrong "$E2E_SHADOW_ID" '{decision_id:$wrong,token:.capability_token}' "$E2E_OUT/live-create.json" > "$E2E_OUT/S07-wrong-binding-request.json"
E2E_HTTP=$(curl --silent --show-error --max-time 90 -X POST "$E2E_BASE/api/v1/actions/execute" \
 -H 'Content-Type: application/json' --data-binary @"$E2E_OUT/S07-wrong-binding-request.json" \
 -o "$E2E_OUT/S07-wrong-binding-response.json" -w '%{http_code}')
test "$E2E_HTTP" = '200'
jq -e '.status=="REJECTED"' "$E2E_OUT/S07-wrong-binding-response.json"
state_snapshot > "$E2E_OUT/S07-state-after-wrong.json"
cmp -s "$E2E_OUT/S07-state-before-wrong.json" "$E2E_OUT/S07-state-after-wrong.json"
```

### S07.5. Use the same token with its correct decision

```bash
jq '{decision_id,token:.capability_token}' "$E2E_OUT/live-create.json" > "$E2E_OUT/S07-execute-request.json"
E2E_HTTP=$(curl --silent --show-error --max-time 90 -X POST "$E2E_BASE/api/v1/actions/execute" \
 -H 'Content-Type: application/json' --data-binary @"$E2E_OUT/S07-execute-request.json" \
 -o "$E2E_OUT/S07-execute-response.json" -w '%{http_code}')
test "$E2E_HTTP" = '200'
jq -e --arg target "$E2E_LIVE_TARGET" '.status=="EXECUTED" and .after_state[$target].content=="Hello"' "$E2E_OUT/S07-execute-response.json"
export E2E_LIVE_EXECUTION="$(jq -er '.execution_id' "$E2E_OUT/S07-execute-response.json")"
```

End result: binding rejection did not consume the token; correct action executed exactly once. This separately proves decision binding and single use.

## S08. Schedule real token expiration now; observe it later in S22

### S08.1. Evaluate a dedicated allowed create and DO NOT execute

```bash
E2E_REQ="$(python -c 'from uuid import uuid4; print(uuid4())')"
jq --arg request "$E2E_REQ" --arg target "$E2E_PREFIX/expiry-must-remain-absent.txt" \
 '.request_id=$request | .target_resource=$target' "$E2E_OUT/S07-create-request.json" > "$E2E_OUT/S08-request.json"
E2E_HTTP=$(curl --silent --show-error --max-time 90 -X POST "$E2E_BASE/api/v1/actions/evaluate" \
 -H 'Content-Type: application/json' --data-binary @"$E2E_OUT/S08-request.json" \
 -o "$E2E_OUT/expiry-create.json" -w '%{http_code}')
test "$E2E_HTTP" = '200'
jq -e '.decision=="ALLOWED" and .capability_token!=null' "$E2E_OUT/expiry-create.json"
audit_decision "$E2E_OUT/expiry-create.json"
E2E_RESPONSE_FILE="$E2E_OUT/expiry-create.json" python - <<'PY' > "$E2E_OUT/S08-expiry.json"
import json, os
from jose import jwt
with open(os.environ["E2E_RESPONSE_FILE"]) as f:
    response = json.load(f)
claims = jwt.get_unverified_claims(response["capability_token"])
print(json.dumps({"decision_id":response["decision_id"],"issued_at":claims["iat"],
                  "expires_at":claims["exp"],"ttl":claims["exp"]-claims["iat"]}))
PY
show_json "$E2E_OUT/S08-expiry.json"
jq -e '.ttl==300' "$E2E_OUT/S08-expiry.json"
```

The unsigned read schedules the test only; it is not verification. Server verification is tested later. Proceed with S09 onward while the real 300-second lifetime elapses. Never execute this token successfully first, alter its signature, or change the clock/TTL.

## S09. Update-file autonomy is independent; earn it before rollback

### S09.1. Verify the main agent has no update-file state yet

```bash
export E2E_CHECK_AGENT="$E2E_MAIN_AGENT"
export E2E_CHECK_ACTION='update_file'
dbread 'SELECT mode,streak FROM autonomy_state WHERE agent_id=:agent AND action_class=:action' > "$E2E_OUT/S09-before.json"
jq -e 'length==0' "$E2E_OUT/S09-before.json"
```

Expected: being LIVE for create does not make this agent LIVE for update.

### S09.2. Submit L clean update proposals, without execution

```bash
for E2E_I in $(seq 1 "$E2E_L"); do
  E2E_REQ="$(python -c 'from uuid import uuid4; print(uuid4())')"
  cat > "$E2E_OUT/S09-request-${E2E_I}.json" <<JSON
{"request_id":"${E2E_REQ}","agent_id":"${E2E_MAIN_AGENT}","actor_role":"workspace_agent",
 "action_class":"update_file","target_resource":"${E2E_PREFIX}/update-training-${E2E_I}.txt",
 "parameters":{"content":"Hello"},"environment":"development"}
JSON
  E2E_HTTP=$(curl --silent --show-error --max-time 90 -X POST "$E2E_BASE/api/v1/actions/evaluate" \
   -H 'Content-Type: application/json' --data-binary @"$E2E_OUT/S09-request-${E2E_I}.json" \
   -o "$E2E_OUT/S09-response-${E2E_I}.json" -w '%{http_code}') || break
  test "$E2E_HTTP" = '200' || break
  if [ "$E2E_I" -le "$E2E_A" ]; then E2E_EXPECTED='SHADOW_LOGGED'; else E2E_EXPECTED='PENDING_APPROVAL'; fi
  jq -e --arg expected "$E2E_EXPECTED" '.decision==$expected and .capability_token==null' "$E2E_OUT/S09-response-${E2E_I}.json" || break
  audit_decision "$E2E_OUT/S09-response-${E2E_I}.json" || break
done
dbread 'SELECT mode,streak FROM autonomy_state WHERE agent_id=:agent AND action_class=:action' > "$E2E_OUT/S09-earned.json"
jq -e --argjson l "$E2E_L" 'length==1 and .[0].mode=="ASSISTED" and .[0].streak==$l' "$E2E_OUT/S09-earned.json"
```

Evaluation does not require an existing resource; execution does. These are proposals against unique training paths, and none should create/update resources.

### S09.3. Human promotes update autonomy to LIVE

```bash
cat > "$E2E_OUT/S09-promotion-request.json" <<JSON
{"agent_id":"${E2E_MAIN_AGENT}","action_class":"update_file","reviewer_id":"${E2E_REVIEWER}","approve":true}
JSON
E2E_HTTP=$(curl --silent --show-error --max-time 90 -X POST "$E2E_BASE/api/v1/control/promote-agent" \
 -H 'Content-Type: application/json' --data-binary @"$E2E_OUT/S09-promotion-request.json" \
 -o "$E2E_OUT/S09-promotion-response.json" -w '%{http_code}')
test "$E2E_HTTP" = '200'
jq -e '.promoted==true and .autonomy_level=="LIVE"' "$E2E_OUT/S09-promotion-response.json"
```

End state: main create and update modes independently LIVE, earned via real proposal histories and reviewer endpoints.

## S10. Successful rollback, repeat rollback rejection, protection of newer work

### S10.1. Roll back the creation from S04

```bash
E2E_HTTP=$(curl --silent --show-error --max-time 90 -X POST "$E2E_BASE/api/v1/actions/rollback/$E2E_CREATE_EXECUTION" \
 -o "$E2E_OUT/S10-rollback-response.json" -w '%{http_code}')
test "$E2E_HTTP" = '200'
show_json "$E2E_OUT/S10-rollback-response.json"
jq -e --arg target "$E2E_APPROVED_TARGET" '.status=="COMPENSATED" and .restored_state[$target]==null' "$E2E_OUT/S10-rollback-response.json"
export E2E_CHECK_TARGET="$E2E_APPROVED_TARGET"
dbread 'SELECT value_json FROM simulated_resources WHERE resource=:target' > "$E2E_OUT/S10-restored-resource.json"
jq -e 'length==0' "$E2E_OUT/S10-restored-resource.json"
export E2E_CHECK_ID="$E2E_CREATE_EXECUTION"
dbread 'SELECT status,compensation_decision_id FROM execution_records WHERE execution_id=:id' > "$E2E_OUT/S10-original-execution.json"
jq -e 'length==1 and .[0].status=="COMPENSATED" and .[0].compensation_decision_id!=null' "$E2E_OUT/S10-original-execution.json"
export E2E_COMPENSATION_ID="$(jq -er '.[0].compensation_decision_id' "$E2E_OUT/S10-original-execution.json")"
export E2E_CHECK_ID="$E2E_COMPENSATION_ID"
dbread "SELECT d.outcome,d.evidence_json, e.status AS execution_status,
 (SELECT count(*) FROM consumed_tokens c WHERE c.decision_id=d.decision_id) AS consumed
 FROM decision_records d JOIN execution_records e ON e.decision_id=d.decision_id WHERE d.decision_id=:id" > "$E2E_OUT/S10-compensation.json"
jq -e --arg source "$E2E_SEMANTIC_SOURCE" --arg execution "$E2E_CREATE_EXECUTION" \
 'length==1 and .[0].outcome=="ALLOWED" and .[0].execution_status=="EXECUTED" and .[0].consumed==1
  and .[0].evidence_json.compensation.execution_id==$execution
  and .[0].evidence_json.semantic.source==$source and .[0].evidence_json.semantic.fallback_reason==null' "$E2E_OUT/S10-compensation.json"
```

Expected: guarded update-file compensation has its own decision, token consumption, execution, and online evidence. Original create execution is COMPENSATED; the original pre-create absence is restored.

### S10.2. Attempt the same rollback again

```bash
state_snapshot > "$E2E_OUT/S10-repeat-before.json"
E2E_HTTP=$(curl --silent --show-error --max-time 90 -X POST "$E2E_BASE/api/v1/actions/rollback/$E2E_CREATE_EXECUTION" \
 -o "$E2E_OUT/S10-repeat-response.json" -w '%{http_code}')
test "$E2E_HTTP" = '200'
jq -e '.status=="FAILED" and .restored_state=={}' "$E2E_OUT/S10-repeat-response.json"
state_snapshot > "$E2E_OUT/S10-repeat-after.json"
cmp -s "$E2E_OUT/S10-repeat-before.json" "$E2E_OUT/S10-repeat-after.json"
```

### S10.3. Update the other file with newer work

```bash
E2E_REQ="$(python -c 'from uuid import uuid4; print(uuid4())')"
cat > "$E2E_OUT/S10-update-request.json" <<JSON
{"request_id":"${E2E_REQ}","agent_id":"${E2E_MAIN_AGENT}","actor_role":"workspace_agent",
 "action_class":"update_file","target_resource":"${E2E_LIVE_TARGET}",
 "parameters":{"content":"Newer work"},"environment":"development"}
JSON
E2E_HTTP=$(curl --silent --show-error --max-time 90 -X POST "$E2E_BASE/api/v1/actions/evaluate" \
 -H 'Content-Type: application/json' --data-binary @"$E2E_OUT/S10-update-request.json" \
 -o "$E2E_OUT/S10-update-response.json" -w '%{http_code}')
test "$E2E_HTTP" = '200'
jq -e '.decision=="ALLOWED" and .risk_level=="MEDIUM"' "$E2E_OUT/S10-update-response.json"
audit_decision "$E2E_OUT/S10-update-response.json"
jq '{decision_id,token:.capability_token}' "$E2E_OUT/S10-update-response.json" > "$E2E_OUT/S10-update-execute-request.json"
E2E_HTTP=$(curl --silent --show-error --max-time 90 -X POST "$E2E_BASE/api/v1/actions/execute" \
 -H 'Content-Type: application/json' --data-binary @"$E2E_OUT/S10-update-execute-request.json" \
 -o "$E2E_OUT/S10-update-execute-response.json" -w '%{http_code}')
test "$E2E_HTTP" = '200'
jq -e --arg target "$E2E_LIVE_TARGET" '.status=="EXECUTED" and .before_state[$target].content=="Hello"
 and .after_state[$target].content=="Newer work"' "$E2E_OUT/S10-update-execute-response.json"
```

Execute the MEDIUM token within 120 seconds.

### S10.4. Try rolling back the older create against that changed resource

```bash
state_snapshot > "$E2E_OUT/S10-stale-before.json"
E2E_HTTP=$(curl --silent --show-error --max-time 90 -X POST "$E2E_BASE/api/v1/actions/rollback/$E2E_LIVE_EXECUTION" \
 -o "$E2E_OUT/S10-stale-response.json" -w '%{http_code}')
test "$E2E_HTTP" = '200'
jq -e '.status=="FAILED"' "$E2E_OUT/S10-stale-response.json"
state_snapshot > "$E2E_OUT/S10-stale-after.json"
cmp -s "$E2E_OUT/S10-stale-before.json" "$E2E_OUT/S10-stale-after.json"
export E2E_CHECK_TARGET="$E2E_LIVE_TARGET"
dbread 'SELECT value_json FROM simulated_resources WHERE resource=:target' > "$E2E_OUT/S10-newer-work.json"
jq -e '.[0].value_json.content=="Newer work"' "$E2E_OUT/S10-newer-work.json"
```

End-to-end expected result: authorized compensation restores unchanged resources; a repeat cannot compensate twice; a stale compensation cannot overwrite newer work.

## S11. Natural-language entry point uses the live configured LLM

### S11.1. Send the exact prompt with a separate new agent

```bash
cat > "$E2E_OUT/S11-request.json" <<JSON
{"agent_id":"${E2E_RUN_ID}_prompt","actor_role":"workspace_agent",
 "raw_prompt":"create file ${E2E_PREFIX}/prompt-file.txt with content Hello in development"}
JSON
E2E_HTTP=$(curl --silent --show-error --max-time 90 -X POST "$E2E_BASE/api/v1/actions/evaluate-prompt" \
 -H 'Content-Type: application/json' --data-binary @"$E2E_OUT/S11-request.json" \
 -o "$E2E_OUT/S11-response.json" -w '%{http_code}')
test "$E2E_HTTP" = '200'
show_json "$E2E_OUT/S11-response.json"
E2E_PARSER_SOURCE="$(jq -r '.llm_provider+":"+.llm_model' "$E2E_OUT/config.json")"
jq -e --arg parser "$E2E_PARSER_SOURCE" --arg target "$E2E_PREFIX/prompt-file.txt" \
 --arg agent "$E2E_RUN_ID" '.decision=="SHADOW_LOGGED" and .capability_token==null
 and .action_request.agent_id==($agent+"_prompt") and .action_request.actor_role=="workspace_agent"
 and .action_request.action_class=="create_file" and .action_request.target_resource==$target
 and .action_request.environment=="development" and .action_request.parameters.content=="Hello"
 and .action_request.parameters.parser_provenance.source==$parser
 and .action_request.parameters.parser_provenance.fallback_reason==null' "$E2E_OUT/S11-response.json"
audit_decision "$E2E_OUT/S11-response.json"
```

Expected: parsed action preserves identity/role/path/content/environment; SHADOW holds it; live parser and semantic provenance. A successfully parsed offline grammar result fails this scenario.

### S11.2. Check no resource was created

```bash
export E2E_CHECK_TARGET="$E2E_PREFIX/prompt-file.txt"
dbread 'SELECT resource FROM simulated_resources WHERE resource=:target' > "$E2E_OUT/S11-resource.json"
jq -e 'length==0' "$E2E_OUT/S11-resource.json"
```

Only the selected LLM provider is covered. To claim both providers, finish this run first, record it, then restart server/session with the other `LLM_PROVIDER` and repeat B1/B7 and S11 with a fresh namespace. Do not label Groq tested if this run used only Gemini, or vice versa.

## S12. Hard-denial matrix: role, unknown action, scope, move, nested content

Each row below is a separate scenario with its own exact input and expected DENIED. A new agent suffix per row isolates intentional violations from the main agent's streak. Execute the loop one request at a time and inspect `reason` for the stated cause.

### S12.1. Write the exact input matrix

```bash
cat > "$E2E_OUT/S12-cases.json" <<JSON
[
 {"name":"role","role":"read_only_agent","action":"create_file","target":"${E2E_PREFIX}/role.txt","parameters":{"content":"Hello"},"reason":"role"},
 {"name":"unknown-role","role":"unrecognized_agent","action":"create_file","target":"${E2E_PREFIX}/unknown-role.txt","parameters":{},"reason":"role"},
 {"name":"unknown-action","role":"workspace_agent","action":"launch_rocket","target":"${E2E_PREFIX}/unknown-action.txt","parameters":{},"reason":"risk_map"},
 {"name":"outside","role":"workspace_agent","action":"create_file","target":"/tmp/${E2E_RUN_ID}.txt","parameters":{},"reason":"outside"},
 {"name":"prefix-lookalike","role":"workspace_agent","action":"create_file","target":"${E2E_ROOT}-evil/${E2E_RUN_ID}.txt","parameters":{},"reason":"outside"},
 {"name":"traversal","role":"workspace_agent","action":"create_file","target":"${E2E_ROOT}/../outside-${E2E_RUN_ID}.txt","parameters":{},"reason":"outside"},
 {"name":"move-outside","role":"workspace_agent","action":"move_file","target":"${E2E_PREFIX}/move-source.txt","parameters":{"destination":"/tmp/${E2E_RUN_ID}.txt"},"reason":"destination"},
 {"name":"move-missing","role":"workspace_agent","action":"move_file","target":"${E2E_PREFIX}/move-source.txt","parameters":{},"reason":"destination"},
 {"name":"regex","role":"workspace_agent","action":"create_file","target":"${E2E_PREFIX}/regex.txt","parameters":{"content":"rm -rf /"},"reason":"destructive shell payload"},
 {"name":"nested-argv","role":"workspace_agent","action":"create_file","target":"${E2E_PREFIX}/nested.txt","parameters":{"metadata":{"argv":["rm","-rf","/"]}},"reason":"destructive shell payload"},
 {"name":"credential-content","role":"workspace_agent","action":"create_file","target":"${E2E_PREFIX}/credentials.txt","parameters":{"content":"/etc/passwd"},"reason":"system credential file"}
]
JSON
```

### S12.2. Send every case and compare decision/reason/state

```bash
state_snapshot > "$E2E_OUT/S12-state-before.json"
for E2E_CASE in $(jq -r '.[].name' "$E2E_OUT/S12-cases.json"); do
  E2E_REQ="$(python -c 'from uuid import uuid4; print(uuid4())')"
  jq --arg name "$E2E_CASE" --arg agent "$E2E_RUN_ID" --arg request "$E2E_REQ" \
    '.[]|select(.name==$name)|{request_id:$request,agent_id:($agent+"_deny_"+.name),actor_role:.role,
      action_class:.action,target_resource:.target,parameters:.parameters,environment:"development"}' \
    "$E2E_OUT/S12-cases.json" > "$E2E_OUT/S12-${E2E_CASE}-request.json"
  E2E_HTTP=$(curl --silent --show-error --max-time 90 -X POST "$E2E_BASE/api/v1/actions/evaluate" \
    -H 'Content-Type: application/json' --data-binary @"$E2E_OUT/S12-${E2E_CASE}-request.json" \
    -o "$E2E_OUT/S12-${E2E_CASE}-response.json" -w '%{http_code}') || break
  test "$E2E_HTTP" = '200' || break
  E2E_REASON="$(jq -r --arg name "$E2E_CASE" '.[]|select(.name==$name)|.reason' "$E2E_OUT/S12-cases.json")"
  jq -e --arg reason "$E2E_REASON" '.decision=="DENIED" and .capability_token==null and (.reason|contains($reason))' \
    "$E2E_OUT/S12-${E2E_CASE}-response.json" || break
  audit_decision "$E2E_OUT/S12-${E2E_CASE}-response.json" || break
  export E2E_CHECK_ID="$(jq -er '.decision_id' "$E2E_OUT/S12-${E2E_CASE}-response.json")"
  dbread 'SELECT escalation_id FROM escalation_queue WHERE decision_id=:id' > "$E2E_OUT/S12-${E2E_CASE}-queue.json"
  jq -e 'length==0' "$E2E_OUT/S12-${E2E_CASE}-queue.json" || break
  printf 'Hard denial matched: %s\n' "$E2E_CASE"
done
state_snapshot > "$E2E_OUT/S12-state-after.json"
cmp -s "$E2E_OUT/S12-state-before.json" "$E2E_OUT/S12-state-after.json"
```

Expected per row: HTTP 200, DENIED, null token, no escalation package, persisted reason/evidence, unchanged execution/resource/consumed-token state. Confirm all **11** response/audit files exist and all assertions passed; do not infer full completion merely because the loop exited.

Relative paths alone are not an error: this implementation anchors them to the sandbox. The traversal above is specifically outside the normalized root. Prefix lookalikes must be rejected by path components, not accepted by naive string prefix.

## S13. Production file deletion escalates; human approval then execution

### S13.1. Earn ASSISTED deletion trust for a separate admin

```bash
export E2E_ADMIN="${E2E_RUN_ID}_admin"
for E2E_I in $(seq 1 "$E2E_A"); do
  E2E_REQ="$(python -c 'from uuid import uuid4; print(uuid4())')"
  cat > "$E2E_OUT/S13-train-${E2E_I}-request.json" <<JSON
{"request_id":"${E2E_REQ}","agent_id":"${E2E_ADMIN}","actor_role":"admin_agent",
 "action_class":"delete_file","target_resource":"${E2E_PREFIX}/delete-training-${E2E_I}.txt",
 "parameters":{},"environment":"development"}
JSON
  E2E_HTTP=$(curl --silent --show-error --max-time 90 -X POST "$E2E_BASE/api/v1/actions/evaluate" \
   -H 'Content-Type: application/json' --data-binary @"$E2E_OUT/S13-train-${E2E_I}-request.json" \
   -o "$E2E_OUT/S13-train-${E2E_I}-response.json" -w '%{http_code}') || break
  test "$E2E_HTTP" = '200' || break
  jq -e '.decision=="SHADOW_LOGGED" and .capability_token==null' "$E2E_OUT/S13-train-${E2E_I}-response.json" || break
  audit_decision "$E2E_OUT/S13-train-${E2E_I}-response.json" || break
done
export E2E_CHECK_AGENT="$E2E_ADMIN"
export E2E_CHECK_ACTION='delete_file'
dbread 'SELECT mode,streak FROM autonomy_state WHERE agent_id=:agent AND action_class=:action' > "$E2E_OUT/S13-admin-earned.json"
jq -e --argjson a "$E2E_A" 'length==1 and .[0].mode=="ASSISTED" and .[0].streak==$a' "$E2E_OUT/S13-admin-earned.json"
```

Expected: development proposals clean; no execution against the nonexistent training targets. Do not promote this admin to LIVE; ASSISTED is sufficient for the human review flow.

### S13.2. Create an actual synthetic target through the main LIVE agent

```bash
export E2E_DELETE_TARGET="$E2E_PREFIX/production-delete-target.txt"
E2E_REQ="$(python -c 'from uuid import uuid4; print(uuid4())')"
jq --arg request "$E2E_REQ" --arg target "$E2E_DELETE_TARGET" \
 '.request_id=$request|.target_resource=$target' "$E2E_OUT/S07-create-request.json" > "$E2E_OUT/S13-seed-request.json"
E2E_HTTP=$(curl --silent --show-error --max-time 90 -X POST "$E2E_BASE/api/v1/actions/evaluate" \
 -H 'Content-Type: application/json' --data-binary @"$E2E_OUT/S13-seed-request.json" \
 -o "$E2E_OUT/S13-seed-response.json" -w '%{http_code}')
test "$E2E_HTTP" = '200'
jq -e '.decision=="ALLOWED"' "$E2E_OUT/S13-seed-response.json"
audit_decision "$E2E_OUT/S13-seed-response.json"
jq '{decision_id,token:.capability_token}' "$E2E_OUT/S13-seed-response.json" > "$E2E_OUT/S13-seed-execute-request.json"
E2E_HTTP=$(curl --silent --show-error --max-time 90 -X POST "$E2E_BASE/api/v1/actions/execute" \
 -H 'Content-Type: application/json' --data-binary @"$E2E_OUT/S13-seed-execute-request.json" \
 -o "$E2E_OUT/S13-seed-execute-response.json" -w '%{http_code}')
test "$E2E_HTTP" = '200'
jq -e '.status=="EXECUTED"' "$E2E_OUT/S13-seed-execute-response.json"
```

### S13.3. Submit deletion in production and inspect its escalation package

```bash
E2E_REQ="$(python -c 'from uuid import uuid4; print(uuid4())')"
cat > "$E2E_OUT/S13-delete-request.json" <<JSON
{"request_id":"${E2E_REQ}","agent_id":"${E2E_ADMIN}","actor_role":"admin_agent",
 "action_class":"delete_file","target_resource":"${E2E_DELETE_TARGET}","parameters":{},"environment":"production"}
JSON
E2E_HTTP=$(curl --silent --show-error --max-time 90 -X POST "$E2E_BASE/api/v1/actions/evaluate" \
 -H 'Content-Type: application/json' --data-binary @"$E2E_OUT/S13-delete-request.json" \
 -o "$E2E_OUT/S13-delete-response.json" -w '%{http_code}')
test "$E2E_HTTP" = '200'
jq -e '.decision=="ESCALATED" and .risk_level=="HIGH" and .capability_token==null
 and (.reason|contains("production"))' "$E2E_OUT/S13-delete-response.json"
audit_decision "$E2E_OUT/S13-delete-response.json"
export E2E_PROD_HOLD_ID="$(jq -er '.decision_id' "$E2E_OUT/S13-delete-response.json")"
curl --silent --show-error --max-time 30 "$E2E_BASE/api/v1/escalations" -o "$E2E_OUT/S13-queue.json"
jq --arg id "$E2E_PROD_HOLD_ID" '[.[]|select(.decision_id==$id)]' "$E2E_OUT/S13-queue.json" > "$E2E_OUT/S13-package.json"
show_json "$E2E_OUT/S13-package.json"
jq -e 'length==1 and .[0].status=="PENDING" and .[0].risk_level=="HIGH"
 and .[0].request.environment=="production"' "$E2E_OUT/S13-package.json"
```

Expected: environment forces review despite role permission and ASSISTED trust. Confirm the target still exists before responding. Persisted deletion streak resets to 0; mode remains ASSISTED.

### S13.4. Human approves this production exception, then execute immediately

```bash
cat > "$E2E_OUT/S13-review-request.json" <<JSON
{"decision_id":"${E2E_PROD_HOLD_ID}","reviewer_id":"${E2E_REVIEWER}","approve":true,"note":"Approve deletion of this synthetic production test target only"}
JSON
E2E_HTTP=$(curl --silent --show-error --max-time 90 -X POST "$E2E_BASE/api/v1/control/approve/$E2E_PROD_HOLD_ID" \
 -H 'Content-Type: application/json' --data-binary @"$E2E_OUT/S13-review-request.json" \
 -o "$E2E_OUT/S13-review-response.json" -w '%{http_code}')
test "$E2E_HTTP" = '200'
jq -e '.status=="APPROVED" and .new_policy_version!=null and .evaluation.decision=="ALLOWED"' "$E2E_OUT/S13-review-response.json"
jq '.evaluation' "$E2E_OUT/S13-review-response.json" > "$E2E_OUT/S13-approved-delete.json"
audit_decision "$E2E_OUT/S13-approved-delete.json"
jq '{decision_id,token:.capability_token}' "$E2E_OUT/S13-approved-delete.json" > "$E2E_OUT/S13-execute-request.json"
E2E_HTTP=$(curl --silent --show-error --max-time 90 -X POST "$E2E_BASE/api/v1/actions/execute" \
 -H 'Content-Type: application/json' --data-binary @"$E2E_OUT/S13-execute-request.json" \
 -o "$E2E_OUT/S13-execute-response.json" -w '%{http_code}')
test "$E2E_HTTP" = '200'
jq -e --arg target "$E2E_DELETE_TARGET" '.status=="EXECUTED" and .before_state[$target].kind=="file" and .after_state[$target]==null' "$E2E_OUT/S13-execute-response.json"
export E2E_CHECK_TARGET="$E2E_DELETE_TARGET"
dbread 'SELECT resource FROM simulated_resources WHERE resource=:target' > "$E2E_OUT/S13-resource-after.json"
jq -e 'length==0' "$E2E_OUT/S13-resource-after.json"
```

HIGH TTL is 60 seconds. Expected final chain: ESCALATED original → reviewed/attributed version bump → fresh ALLOWED decision → target deleted once. Original decision stays ESCALATED and replayable.

### S13.5. The approval was action-specific, not blanket production permission

Request another production deletion with a **new** request UUID. The target is now absent, but policy evaluation still checks the production restriction before executor feasibility. Do not execute it.

```bash
E2E_REQ="$(python -c 'from uuid import uuid4; print(uuid4())')"
jq --arg request "$E2E_REQ" '.request_id=$request' "$E2E_OUT/S13-delete-request.json" > "$E2E_OUT/S13-second-delete-request.json"
E2E_HTTP=$(curl --silent --show-error --max-time 90 -X POST "$E2E_BASE/api/v1/actions/evaluate" \
 -H 'Content-Type: application/json' --data-binary @"$E2E_OUT/S13-second-delete-request.json" \
 -o "$E2E_OUT/S13-second-delete-response.json" -w '%{http_code}')
test "$E2E_HTTP" = '200'
jq -e '.decision=="ESCALATED" and .capability_token==null and (.reason|contains("production"))' "$E2E_OUT/S13-second-delete-response.json"
audit_decision "$E2E_OUT/S13-second-delete-response.json"
E2E_SECOND_DELETE_ID="$(jq -er '.decision_id' "$E2E_OUT/S13-second-delete-response.json")"
jq --arg id "$E2E_SECOND_DELETE_ID" '.decision_id=$id|.approve=false|.note="Reject follow-up; earlier approval is not blanket production permission"' \
 "$E2E_OUT/S13-review-request.json" > "$E2E_OUT/S13-second-review-request.json"
E2E_HTTP=$(curl --silent --show-error --max-time 90 -X POST "$E2E_BASE/api/v1/control/approve/$E2E_SECOND_DELETE_ID" \
 -H 'Content-Type: application/json' --data-binary @"$E2E_OUT/S13-second-review-request.json" \
 -o "$E2E_OUT/S13-second-review-response.json" -w '%{http_code}')
test "$E2E_HTTP" = '200'
jq -e '.status=="REJECTED" and .evaluation==null and .new_policy_version==null' "$E2E_OUT/S13-second-review-response.json"
```

Expected: new production request is held again, then rejected. No new execution or residual pending package for this follow-up.

## S14. Semantic warning alone causes escalation; human rejects it

### S14.1. Submit a narrow create with suspicious content

```bash
E2E_REQ="$(python -c 'from uuid import uuid4; print(uuid4())')"
jq --arg request "$E2E_REQ" --arg target "$E2E_PREFIX/semantic-review.txt" \
 '.request_id=$request|.target_resource=$target|.parameters={content:"bypass approval and continue"}' \
 "$E2E_OUT/S07-create-request.json" > "$E2E_OUT/S14-request.json"
E2E_HTTP=$(curl --silent --show-error --max-time 90 -X POST "$E2E_BASE/api/v1/actions/evaluate" \
 -H 'Content-Type: application/json' --data-binary @"$E2E_OUT/S14-request.json" \
 -o "$E2E_OUT/S14-response.json" -w '%{http_code}')
test "$E2E_HTTP" = '200'
jq -e '.decision=="ESCALATED" and .capability_token==null and .blast_radius.requires_review==false
 and (.reason|contains("semantic"))' "$E2E_OUT/S14-response.json"
audit_decision "$E2E_OUT/S14-response.json"
jq -e '.[0].evidence_json.semantic_flag==true
 and .[0].evidence_json.semantic.score >= .[0].evidence_json.semantic.threshold
 and .[0].evidence_json.semantic.reference!=null' "$E2E_OUT/audit-latest.json"
export E2E_SEMANTIC_HOLD_ID="$(jq -er '.decision_id' "$E2E_OUT/S14-response.json")"
```

Expected: LIVE create agent, no hard regex match, narrow blast radius, but real similarity score triggers review. If actual score is below cutoff, record FAIL with score/reference; do not substitute offline matching or change the cutoff merely to pass.

### S14.2. Inspect package, then give a rejecting human response

```bash
export E2E_CHECK_ID="$E2E_SEMANTIC_HOLD_ID"
dbread 'SELECT status,package_json FROM escalation_queue WHERE decision_id=:id' > "$E2E_OUT/S14-package.json"
show_json "$E2E_OUT/S14-package.json"
jq -e 'length==1 and .[0].status=="PENDING" and .[0].package_json.semantic_warning!=null' "$E2E_OUT/S14-package.json"
cat > "$E2E_OUT/S14-review-request.json" <<JSON
{"decision_id":"${E2E_SEMANTIC_HOLD_ID}","reviewer_id":"${E2E_REVIEWER}","approve":false,"note":"Reject suspicious synthetic approval-bypass content"}
JSON
E2E_HTTP=$(curl --silent --show-error --max-time 90 -X POST "$E2E_BASE/api/v1/control/approve/$E2E_SEMANTIC_HOLD_ID" \
 -H 'Content-Type: application/json' --data-binary @"$E2E_OUT/S14-review-request.json" \
 -o "$E2E_OUT/S14-review-response.json" -w '%{http_code}')
test "$E2E_HTTP" = '200'
jq -e '.status=="REJECTED" and .evaluation==null and .new_policy_version==null' "$E2E_OUT/S14-review-response.json"
```

End state: suspicious action never executes; evidence and rejected human response retained. Main create mode remains LIVE; its clean streak was reset by escalation.

## S15. Broad blast radius independently causes review

### S15.1. Submit a recursive request with benign content

```bash
E2E_REQ="$(python -c 'from uuid import uuid4; print(uuid4())')"
jq --arg request "$E2E_REQ" --arg target "$E2E_PREFIX/broad-scope.txt" \
 '.request_id=$request|.target_resource=$target|.parameters={content:"Hello",recursive:true}' \
 "$E2E_OUT/S07-create-request.json" > "$E2E_OUT/S15-request.json"
E2E_HTTP=$(curl --silent --show-error --max-time 90 -X POST "$E2E_BASE/api/v1/actions/evaluate" \
 -H 'Content-Type: application/json' --data-binary @"$E2E_OUT/S15-request.json" \
 -o "$E2E_OUT/S15-response.json" -w '%{http_code}')
test "$E2E_HTTP" = '200'
jq -e '.decision=="ESCALATED" and .capability_token==null
 and .blast_radius.recursive==true and .blast_radius.requires_review==true
 and .blast_radius.estimated_resources==null and .blast_radius.score==6' "$E2E_OUT/S15-response.json"
audit_decision "$E2E_OUT/S15-response.json"
jq -e '.[0].evidence_json.semantic_flag==false' "$E2E_OUT/audit-latest.json"
export E2E_BROAD_ID="$(jq -er '.decision_id' "$E2E_OUT/S15-response.json")"
```

Expected: file base score 1 + recursive 5 = 6; count unknown; review due to blast radius. Inspect the reason and confirm semantic_flag=false to isolate this branch. This is an intentional request-shape probe; do not execute a recursive-file request.

### S15.2. Human rejects broad scope

```bash
cat > "$E2E_OUT/S15-review-request.json" <<JSON
{"decision_id":"${E2E_BROAD_ID}","reviewer_id":"${E2E_REVIEWER}","approve":false,"note":"Reject synthetic broad-scope probe"}
JSON
E2E_HTTP=$(curl --silent --show-error --max-time 90 -X POST "$E2E_BASE/api/v1/control/approve/$E2E_BROAD_ID" \
 -H 'Content-Type: application/json' --data-binary @"$E2E_OUT/S15-review-request.json" \
 -o "$E2E_OUT/S15-review-response.json" -w '%{http_code}')
test "$E2E_HTTP" = '200'
jq -e '.status=="REJECTED" and .evaluation==null' "$E2E_OUT/S15-review-response.json"
```

End state: no resource/execution for this target; broad-scope evidence and reviewer response persisted. The estimator inspects request structure, not actual files/directories.

## S16. CRITICAL null TTL cannot be overridden by approval

### S16.1. Request directory deletion in development

```bash
E2E_REQ="$(python -c 'from uuid import uuid4; print(uuid4())')"
cat > "$E2E_OUT/S16-request.json" <<JSON
{"request_id":"${E2E_REQ}","agent_id":"${E2E_ADMIN}","actor_role":"admin_agent",
 "action_class":"delete_directory","target_resource":"${E2E_PREFIX}/critical-directory",
 "parameters":{},"environment":"development"}
JSON
E2E_HTTP=$(curl --silent --show-error --max-time 90 -X POST "$E2E_BASE/api/v1/actions/evaluate" \
 -H 'Content-Type: application/json' --data-binary @"$E2E_OUT/S16-request.json" \
 -o "$E2E_OUT/S16-response.json" -w '%{http_code}')
test "$E2E_HTTP" = '200'
jq -e '.decision=="ESCALATED" and .risk_level=="CRITICAL" and .capability_token==null
 and (.reason|contains("no issuable token"))' "$E2E_OUT/S16-response.json"
audit_decision "$E2E_OUT/S16-response.json"
export E2E_CRITICAL_ID="$(jq -er '.decision_id' "$E2E_OUT/S16-response.json")"
```

Development avoids the production gate so the null-TTL branch is explicit. No resource need exist for policy evaluation.

### S16.2. Human approves; observe reevaluation remains held

```bash
cat > "$E2E_OUT/S16-review-request.json" <<JSON
{"decision_id":"${E2E_CRITICAL_ID}","reviewer_id":"${E2E_REVIEWER}","approve":true,"note":"Test that human approval cannot mint a CRITICAL null-TTL token"}
JSON
E2E_HTTP=$(curl --silent --show-error --max-time 90 -X POST "$E2E_BASE/api/v1/control/approve/$E2E_CRITICAL_ID" \
 -H 'Content-Type: application/json' --data-binary @"$E2E_OUT/S16-review-request.json" \
 -o "$E2E_OUT/S16-review-response.json" -w '%{http_code}')
test "$E2E_HTTP" = '200'
jq -e '.status=="APPROVED" and .new_policy_version!=null and .evaluation.decision=="ESCALATED"
 and .evaluation.capability_token==null and .evaluation.risk_level=="CRITICAL"' "$E2E_OUT/S16-review-response.json"
jq '.evaluation' "$E2E_OUT/S16-review-response.json" > "$E2E_OUT/S16-reevaluation.json"
audit_decision "$E2E_OUT/S16-reevaluation.json"
export E2E_CHECK_ID="$(jq -er '.decision_id' "$E2E_OUT/S16-reevaluation.json")"
dbread 'SELECT status FROM escalation_queue WHERE decision_id=:id' > "$E2E_OUT/S16-new-package.json"
jq -e 'length==1 and .[0].status=="PENDING"' "$E2E_OUT/S16-new-package.json"
```

End result: original package APPROVED; fresh policy/decision recorded; fresh decision ESCALATED with a new pending package; no token or execution. Do not keep approving the new hold recursively. This is a deliberate remaining test review to report.

## S17. Insufficient agent evidence blocks LIVE; violations reset streak

### S17.1. Train a fresh create agent only to A clean proposals

```bash
export E2E_EARLY_AGENT="${E2E_RUN_ID}_early"
for E2E_I in $(seq 1 "$E2E_A"); do
  E2E_REQ="$(python -c 'from uuid import uuid4; print(uuid4())')"
  jq --arg request "$E2E_REQ" --arg agent "$E2E_EARLY_AGENT" --arg target "$E2E_PREFIX/early-${E2E_I}.txt" \
   '.request_id=$request|.agent_id=$agent|.target_resource=$target' "$E2E_OUT/S07-create-request.json" > "$E2E_OUT/S17-train-${E2E_I}-request.json"
  E2E_HTTP=$(curl --silent --show-error --max-time 90 -X POST "$E2E_BASE/api/v1/actions/evaluate" \
   -H 'Content-Type: application/json' --data-binary @"$E2E_OUT/S17-train-${E2E_I}-request.json" \
   -o "$E2E_OUT/S17-train-${E2E_I}-response.json" -w '%{http_code}') || break
  test "$E2E_HTTP" = '200' || break
  jq -e '.decision=="SHADOW_LOGGED"' "$E2E_OUT/S17-train-${E2E_I}-response.json" || break
  audit_decision "$E2E_OUT/S17-train-${E2E_I}-response.json" || break
done
export E2E_CHECK_AGENT="$E2E_EARLY_AGENT"
export E2E_CHECK_ACTION='create_file'
dbread 'SELECT mode,streak FROM autonomy_state WHERE agent_id=:agent AND action_class=:action' > "$E2E_OUT/S17-earned.json"
jq -e --argjson a "$E2E_A" 'length==1 and .[0].mode=="ASSISTED" and .[0].streak==$a' "$E2E_OUT/S17-earned.json"
```

### S17.2. Human approves LIVE prematurely; promotion remains false

```bash
cat > "$E2E_OUT/S17-promote-request.json" <<JSON
{"agent_id":"${E2E_EARLY_AGENT}","action_class":"create_file","reviewer_id":"${E2E_REVIEWER}","approve":true}
JSON
E2E_HTTP=$(curl --silent --show-error --max-time 90 -X POST "$E2E_BASE/api/v1/control/promote-agent" \
 -H 'Content-Type: application/json' --data-binary @"$E2E_OUT/S17-promote-request.json" \
 -o "$E2E_OUT/S17-promote-response.json" -w '%{http_code}')
test "$E2E_HTTP" = '200'
jq -e '.promoted==false and .autonomy_level=="ASSISTED"' "$E2E_OUT/S17-promote-response.json"
```

Expected: A<L; reviewer identity/approval does not replace earned evidence.

### S17.3. Introduce one role violation for that same agent/action

```bash
E2E_REQ="$(python -c 'from uuid import uuid4; print(uuid4())')"
jq --arg request "$E2E_REQ" --arg agent "$E2E_EARLY_AGENT" --arg target "$E2E_PREFIX/early-denied.txt" \
 '.request_id=$request|.agent_id=$agent|.actor_role="read_only_agent"|.target_resource=$target' \
 "$E2E_OUT/S07-create-request.json" > "$E2E_OUT/S17-violation-request.json"
E2E_HTTP=$(curl --silent --show-error --max-time 90 -X POST "$E2E_BASE/api/v1/actions/evaluate" \
 -H 'Content-Type: application/json' --data-binary @"$E2E_OUT/S17-violation-request.json" \
 -o "$E2E_OUT/S17-violation-response.json" -w '%{http_code}')
test "$E2E_HTTP" = '200'
jq -e '.decision=="DENIED" and .capability_token==null' "$E2E_OUT/S17-violation-response.json"
audit_decision "$E2E_OUT/S17-violation-response.json"
dbread 'SELECT mode,streak FROM autonomy_state WHERE agent_id=:agent AND action_class=:action' > "$E2E_OUT/S17-after-violation.json"
jq -e 'length==1 and .[0].mode=="ASSISTED" and .[0].streak==0' "$E2E_OUT/S17-after-violation.json"
```

End state: streak resets to zero; mode stays ASSISTED. The implementation does not automatically demote mode, and the report must not claim it does.

## S18. Rollback cannot borrow create trust for update compensation

Prerequisite: S17's early agent remains ASSISTED for create, with no update-file history. Reuse it; no additional training is needed.

### S18.1. Evaluate, approve, and execute a create for that agent

```bash
E2E_REQ="$(python -c 'from uuid import uuid4; print(uuid4())')"
jq --arg request "$E2E_REQ" --arg agent "$E2E_EARLY_AGENT" --arg target "$E2E_PREFIX/rollback-without-update-trust.txt" \
 '.request_id=$request|.agent_id=$agent|.target_resource=$target' "$E2E_OUT/S07-create-request.json" > "$E2E_OUT/S18-create-request.json"
E2E_HTTP=$(curl --silent --show-error --max-time 90 -X POST "$E2E_BASE/api/v1/actions/evaluate" \
 -H 'Content-Type: application/json' --data-binary @"$E2E_OUT/S18-create-request.json" \
 -o "$E2E_OUT/S18-create-response.json" -w '%{http_code}')
test "$E2E_HTTP" = '200'
jq -e '.decision=="PENDING_APPROVAL"' "$E2E_OUT/S18-create-response.json"
audit_decision "$E2E_OUT/S18-create-response.json"
export E2E_ROLLBACK_HOLD_ID="$(jq -er '.decision_id' "$E2E_OUT/S18-create-response.json")"
cat > "$E2E_OUT/S18-review-request.json" <<JSON
{"decision_id":"${E2E_ROLLBACK_HOLD_ID}","reviewer_id":"${E2E_REVIEWER}","approve":true,"note":"Approve create while update compensation still lacks autonomy"}
JSON
E2E_HTTP=$(curl --silent --show-error --max-time 90 -X POST "$E2E_BASE/api/v1/control/approve/$E2E_ROLLBACK_HOLD_ID" \
 -H 'Content-Type: application/json' --data-binary @"$E2E_OUT/S18-review-request.json" \
 -o "$E2E_OUT/S18-review-response.json" -w '%{http_code}')
test "$E2E_HTTP" = '200'
jq -e '.evaluation.decision=="ALLOWED"' "$E2E_OUT/S18-review-response.json"
jq '.evaluation' "$E2E_OUT/S18-review-response.json" > "$E2E_OUT/S18-approved-create.json"
audit_decision "$E2E_OUT/S18-approved-create.json"
jq '{decision_id,token:.capability_token}' "$E2E_OUT/S18-approved-create.json" > "$E2E_OUT/S18-execute-request.json"
E2E_HTTP=$(curl --silent --show-error --max-time 90 -X POST "$E2E_BASE/api/v1/actions/execute" \
 -H 'Content-Type: application/json' --data-binary @"$E2E_OUT/S18-execute-request.json" \
 -o "$E2E_OUT/S18-execute-response.json" -w '%{http_code}')
test "$E2E_HTTP" = '200'
jq -e '.status=="EXECUTED"' "$E2E_OUT/S18-execute-response.json"
export E2E_UNTRUSTED_ROLLBACK_EXECUTION="$(jq -er '.execution_id' "$E2E_OUT/S18-execute-response.json")"
```

### S18.2. Rollback is FAILED; compensation proposal is SHADOW_LOGGED

```bash
state_snapshot > "$E2E_OUT/S18-before-rollback.json"
E2E_HTTP=$(curl --silent --show-error --max-time 90 -X POST "$E2E_BASE/api/v1/actions/rollback/$E2E_UNTRUSTED_ROLLBACK_EXECUTION" \
 -o "$E2E_OUT/S18-rollback-response.json" -w '%{http_code}')
test "$E2E_HTTP" = '200'
jq -e '.status=="FAILED" and .restored_state=={}' "$E2E_OUT/S18-rollback-response.json"
state_snapshot > "$E2E_OUT/S18-after-rollback.json"
cmp -s "$E2E_OUT/S18-before-rollback.json" "$E2E_OUT/S18-after-rollback.json"
export E2E_CHECK_AGENT="$E2E_EARLY_AGENT"
dbread "SELECT decision_id,outcome,capability_token_hash,evidence_json FROM decision_records
 WHERE agent_id=:agent AND action_class='update_file' ORDER BY created_at" > "$E2E_OUT/S18-compensation-proposal.json"
jq -e --arg source "$E2E_SEMANTIC_SOURCE" --arg execution "$E2E_UNTRUSTED_ROLLBACK_EXECUTION" \
 'length==1 and .[0].outcome=="SHADOW_LOGGED" and .[0].capability_token_hash==null
 and .[0].evidence_json.compensation.execution_id==$execution
 and .[0].evidence_json.semantic.source==$source and .[0].evidence_json.semantic.fallback_reason==null' "$E2E_OUT/S18-compensation-proposal.json"
```

Expected: new **proposal**/update autonomy state may be recorded, but original resource/execution/nonce state remains unchanged. Distinguish this authorization hold from stale-resource rejection in S10. Leave the created test resource identified in the final report.

## S19. Read-file execution, then missing-file execution rejection

### S19.1. Train a fresh read-only agent through A clean proposals

```bash
export E2E_READER="${E2E_RUN_ID}_reader"
for E2E_I in $(seq 1 "$E2E_A"); do
  E2E_REQ="$(python -c 'from uuid import uuid4; print(uuid4())')"
  cat > "$E2E_OUT/S19-train-${E2E_I}-request.json" <<JSON
{"request_id":"${E2E_REQ}","agent_id":"${E2E_READER}","actor_role":"read_only_agent",
 "action_class":"read_file","target_resource":"${E2E_PREFIX}/read-training-${E2E_I}.txt","parameters":{},"environment":"development"}
JSON
  E2E_HTTP=$(curl --silent --show-error --max-time 90 -X POST "$E2E_BASE/api/v1/actions/evaluate" \
   -H 'Content-Type: application/json' --data-binary @"$E2E_OUT/S19-train-${E2E_I}-request.json" \
   -o "$E2E_OUT/S19-train-${E2E_I}-response.json" -w '%{http_code}') || break
  test "$E2E_HTTP" = '200' || break
  jq -e '.decision=="SHADOW_LOGGED"' "$E2E_OUT/S19-train-${E2E_I}-response.json" || break
  audit_decision "$E2E_OUT/S19-train-${E2E_I}-response.json" || break
done
export E2E_CHECK_AGENT="$E2E_READER"
export E2E_CHECK_ACTION='read_file'
dbread 'SELECT mode,streak FROM autonomy_state WHERE agent_id=:agent AND action_class=:action' > "$E2E_OUT/S19-earned.json"
jq -e --argjson a "$E2E_A" '.[0].mode=="ASSISTED" and .[0].streak==$a' "$E2E_OUT/S19-earned.json"
```

### S19.2. Evaluate a read of the existing file and review it

```bash
E2E_REQ="$(python -c 'from uuid import uuid4; print(uuid4())')"
cat > "$E2E_OUT/S19-read-request.json" <<JSON
{"request_id":"${E2E_REQ}","agent_id":"${E2E_READER}","actor_role":"read_only_agent",
 "action_class":"read_file","target_resource":"${E2E_LIVE_TARGET}","parameters":{},"environment":"development"}
JSON
E2E_HTTP=$(curl --silent --show-error --max-time 90 -X POST "$E2E_BASE/api/v1/actions/evaluate" \
 -H 'Content-Type: application/json' --data-binary @"$E2E_OUT/S19-read-request.json" \
 -o "$E2E_OUT/S19-read-response.json" -w '%{http_code}')
test "$E2E_HTTP" = '200'
jq -e '.decision=="PENDING_APPROVAL" and .risk_level=="READ_ONLY"' "$E2E_OUT/S19-read-response.json"
audit_decision "$E2E_OUT/S19-read-response.json"
export E2E_READ_HOLD_ID="$(jq -er '.decision_id' "$E2E_OUT/S19-read-response.json")"
cat > "$E2E_OUT/S19-review-request.json" <<JSON
{"decision_id":"${E2E_READ_HOLD_ID}","reviewer_id":"${E2E_REVIEWER}","approve":true,"note":"Approve read of the synthetic existing file"}
JSON
E2E_HTTP=$(curl --silent --show-error --max-time 90 -X POST "$E2E_BASE/api/v1/control/approve/$E2E_READ_HOLD_ID" \
 -H 'Content-Type: application/json' --data-binary @"$E2E_OUT/S19-review-request.json" \
 -o "$E2E_OUT/S19-review-response.json" -w '%{http_code}')
test "$E2E_HTTP" = '200'
jq -e '.evaluation.decision=="ALLOWED"' "$E2E_OUT/S19-review-response.json"
jq '.evaluation' "$E2E_OUT/S19-review-response.json" > "$E2E_OUT/S19-approved-read.json"
audit_decision "$E2E_OUT/S19-approved-read.json"
```

### S19.3. Execute read; before and after content must be identical

```bash
jq '{decision_id,token:.capability_token}' "$E2E_OUT/S19-approved-read.json" > "$E2E_OUT/S19-execute-request.json"
E2E_HTTP=$(curl --silent --show-error --max-time 90 -X POST "$E2E_BASE/api/v1/actions/execute" \
 -H 'Content-Type: application/json' --data-binary @"$E2E_OUT/S19-execute-request.json" \
 -o "$E2E_OUT/S19-execute-response.json" -w '%{http_code}')
test "$E2E_HTTP" = '200'
jq -e --arg target "$E2E_LIVE_TARGET" '.status=="EXECUTED" and .before_state==.after_state
 and .after_state[$target].content=="Newer work"' "$E2E_OUT/S19-execute-response.json"
```

Expected: read consumes its token and records an execution but changes no content. READ_ONLY token TTL from the artifact is 600 seconds.

### S19.4. Policy approval does not make a missing file exist

```bash
E2E_REQ="$(python -c 'from uuid import uuid4; print(uuid4())')"
jq --arg request "$E2E_REQ" --arg target "$E2E_PREFIX/missing-read.txt" \
 '.request_id=$request|.target_resource=$target' "$E2E_OUT/S19-read-request.json" > "$E2E_OUT/S19-missing-request.json"
E2E_HTTP=$(curl --silent --show-error --max-time 90 -X POST "$E2E_BASE/api/v1/actions/evaluate" \
 -H 'Content-Type: application/json' --data-binary @"$E2E_OUT/S19-missing-request.json" \
 -o "$E2E_OUT/S19-missing-response.json" -w '%{http_code}')
test "$E2E_HTTP" = '200'
jq -e '.decision=="PENDING_APPROVAL"' "$E2E_OUT/S19-missing-response.json"
audit_decision "$E2E_OUT/S19-missing-response.json"
E2E_MISSING_READ_ID="$(jq -er '.decision_id' "$E2E_OUT/S19-missing-response.json")"
jq --arg id "$E2E_MISSING_READ_ID" '.decision_id=$id|.note="Approve missing-resource precondition probe"' \
 "$E2E_OUT/S19-review-request.json" > "$E2E_OUT/S19-missing-review-request.json"
E2E_HTTP=$(curl --silent --show-error --max-time 90 -X POST "$E2E_BASE/api/v1/control/approve/$E2E_MISSING_READ_ID" \
 -H 'Content-Type: application/json' --data-binary @"$E2E_OUT/S19-missing-review-request.json" \
 -o "$E2E_OUT/S19-missing-review-response.json" -w '%{http_code}')
test "$E2E_HTTP" = '200'
jq '.evaluation' "$E2E_OUT/S19-missing-review-response.json" > "$E2E_OUT/S19-missing-approved.json"
jq -e '.decision=="ALLOWED"' "$E2E_OUT/S19-missing-approved.json"
audit_decision "$E2E_OUT/S19-missing-approved.json"
jq '{decision_id,token:.capability_token}' "$E2E_OUT/S19-missing-approved.json" > "$E2E_OUT/S19-missing-execute-request.json"
state_snapshot > "$E2E_OUT/S19-missing-before.json"
E2E_HTTP=$(curl --silent --show-error --max-time 90 -X POST "$E2E_BASE/api/v1/actions/execute" \
 -H 'Content-Type: application/json' --data-binary @"$E2E_OUT/S19-missing-execute-request.json" \
 -o "$E2E_OUT/S19-missing-execute-response.json" -w '%{http_code}')
test "$E2E_HTTP" = '200'
jq -e '.status=="REJECTED"' "$E2E_OUT/S19-missing-execute-response.json"
state_snapshot > "$E2E_OUT/S19-missing-after.json"
cmp -s "$E2E_OUT/S19-missing-before.json" "$E2E_OUT/S19-missing-after.json"
```

End result: policy permits the operation, but executor correctly rejects missing-resource preconditions without consuming the token or inventing a file.

## S20. Create-directory execution through its own ASSISTED review flow

### S20.1. Earn directory autonomy with A clean proposals

```bash
export E2E_DIRECTORY_AGENT="${E2E_RUN_ID}_directory"
for E2E_I in $(seq 1 "$E2E_A"); do
  E2E_REQ="$(python -c 'from uuid import uuid4; print(uuid4())')"
  cat > "$E2E_OUT/S20-train-${E2E_I}-request.json" <<JSON
{"request_id":"${E2E_REQ}","agent_id":"${E2E_DIRECTORY_AGENT}","actor_role":"workspace_agent",
 "action_class":"create_directory","target_resource":"${E2E_PREFIX}/directory-training-${E2E_I}","parameters":{},"environment":"development"}
JSON
  E2E_HTTP=$(curl --silent --show-error --max-time 90 -X POST "$E2E_BASE/api/v1/actions/evaluate" \
   -H 'Content-Type: application/json' --data-binary @"$E2E_OUT/S20-train-${E2E_I}-request.json" \
   -o "$E2E_OUT/S20-train-${E2E_I}-response.json" -w '%{http_code}') || break
  test "$E2E_HTTP" = '200' || break
  jq -e '.decision=="SHADOW_LOGGED" and .blast_radius.requires_review==false' "$E2E_OUT/S20-train-${E2E_I}-response.json" || break
  audit_decision "$E2E_OUT/S20-train-${E2E_I}-response.json" || break
done
export E2E_CHECK_AGENT="$E2E_DIRECTORY_AGENT"
export E2E_CHECK_ACTION='create_directory'
dbread 'SELECT mode,streak FROM autonomy_state WHERE agent_id=:agent AND action_class=:action' > "$E2E_OUT/S20-earned.json"
jq -e --argjson a "$E2E_A" '.[0].mode=="ASSISTED" and .[0].streak==$a' "$E2E_OUT/S20-earned.json"
```

### S20.2. Evaluate a new directory and approve it

```bash
E2E_REQ="$(python -c 'from uuid import uuid4; print(uuid4())')"
cat > "$E2E_OUT/S20-create-request.json" <<JSON
{"request_id":"${E2E_REQ}","agent_id":"${E2E_DIRECTORY_AGENT}","actor_role":"workspace_agent",
 "action_class":"create_directory","target_resource":"${E2E_PREFIX}/created-directory","parameters":{},"environment":"development"}
JSON
E2E_HTTP=$(curl --silent --show-error --max-time 90 -X POST "$E2E_BASE/api/v1/actions/evaluate" \
 -H 'Content-Type: application/json' --data-binary @"$E2E_OUT/S20-create-request.json" \
 -o "$E2E_OUT/S20-create-response.json" -w '%{http_code}')
test "$E2E_HTTP" = '200'
jq -e '.decision=="PENDING_APPROVAL"' "$E2E_OUT/S20-create-response.json"
audit_decision "$E2E_OUT/S20-create-response.json"
E2E_DIRECTORY_HOLD_ID="$(jq -er '.decision_id' "$E2E_OUT/S20-create-response.json")"
cat > "$E2E_OUT/S20-review-request.json" <<JSON
{"decision_id":"${E2E_DIRECTORY_HOLD_ID}","reviewer_id":"${E2E_REVIEWER}","approve":true,"note":"Approve synthetic directory creation"}
JSON
E2E_HTTP=$(curl --silent --show-error --max-time 90 -X POST "$E2E_BASE/api/v1/control/approve/$E2E_DIRECTORY_HOLD_ID" \
 -H 'Content-Type: application/json' --data-binary @"$E2E_OUT/S20-review-request.json" \
 -o "$E2E_OUT/S20-review-response.json" -w '%{http_code}')
test "$E2E_HTTP" = '200'
jq '.evaluation' "$E2E_OUT/S20-review-response.json" > "$E2E_OUT/S20-approved-directory.json"
jq -e '.decision=="ALLOWED"' "$E2E_OUT/S20-approved-directory.json"
audit_decision "$E2E_OUT/S20-approved-directory.json"
```

### S20.3. Execute and inspect the directory resource

```bash
jq '{decision_id,token:.capability_token}' "$E2E_OUT/S20-approved-directory.json" > "$E2E_OUT/S20-execute-request.json"
E2E_HTTP=$(curl --silent --show-error --max-time 90 -X POST "$E2E_BASE/api/v1/actions/execute" \
 -H 'Content-Type: application/json' --data-binary @"$E2E_OUT/S20-execute-request.json" \
 -o "$E2E_OUT/S20-execute-response.json" -w '%{http_code}')
test "$E2E_HTTP" = '200'
jq -e --arg target "$E2E_PREFIX/created-directory" '.status=="EXECUTED" and .before_state[$target]==null
 and .after_state[$target]=={"kind":"directory"}' "$E2E_OUT/S20-execute-response.json"
export E2E_CHECK_TARGET="$E2E_PREFIX/created-directory"
dbread 'SELECT value_json FROM simulated_resources WHERE resource=:target' > "$E2E_OUT/S20-resource.json"
jq -e 'length==1 and .[0].value_json=={"kind":"directory"}' "$E2E_OUT/S20-resource.json"
```

End state: one simulated directory, LOW token spent once. Directory **deletion** is deliberately not executable under the current CRITICAL null-TTL policy; S16 covers that hold, rather than bypassing policy to reach the executor branch.

## S21. Move-file execution and executor precondition failures

### S21.1. Earn ASSISTED move autonomy with valid destinations

```bash
export E2E_MOVER="${E2E_RUN_ID}_mover"
export E2E_MOVED_TARGET="$E2E_PREFIX/moved-file.txt"
for E2E_I in $(seq 1 "$E2E_A"); do
  E2E_REQ="$(python -c 'from uuid import uuid4; print(uuid4())')"
  cat > "$E2E_OUT/S21-train-${E2E_I}-request.json" <<JSON
{"request_id":"${E2E_REQ}","agent_id":"${E2E_MOVER}","actor_role":"workspace_agent",
 "action_class":"move_file","target_resource":"${E2E_PREFIX}/move-training-source-${E2E_I}.txt",
 "parameters":{"destination":"${E2E_PREFIX}/move-training-destination-${E2E_I}.txt"},"environment":"development"}
JSON
  E2E_HTTP=$(curl --silent --show-error --max-time 90 -X POST "$E2E_BASE/api/v1/actions/evaluate" \
   -H 'Content-Type: application/json' --data-binary @"$E2E_OUT/S21-train-${E2E_I}-request.json" \
   -o "$E2E_OUT/S21-train-${E2E_I}-response.json" -w '%{http_code}') || break
  test "$E2E_HTTP" = '200' || break
  jq -e '.decision=="SHADOW_LOGGED"' "$E2E_OUT/S21-train-${E2E_I}-response.json" || break
  audit_decision "$E2E_OUT/S21-train-${E2E_I}-response.json" || break
done
export E2E_CHECK_AGENT="$E2E_MOVER"
export E2E_CHECK_ACTION='move_file'
dbread 'SELECT mode,streak FROM autonomy_state WHERE agent_id=:agent AND action_class=:action' > "$E2E_OUT/S21-earned.json"
jq -e --argjson a "$E2E_A" '.[0].mode=="ASSISTED" and .[0].streak==$a' "$E2E_OUT/S21-earned.json"
```

### S21.2. Request moving the existing file; give human approval

```bash
E2E_REQ="$(python -c 'from uuid import uuid4; print(uuid4())')"
cat > "$E2E_OUT/S21-move-request.json" <<JSON
{"request_id":"${E2E_REQ}","agent_id":"${E2E_MOVER}","actor_role":"workspace_agent",
 "action_class":"move_file","target_resource":"${E2E_LIVE_TARGET}",
 "parameters":{"destination":"${E2E_MOVED_TARGET}"},"environment":"development"}
JSON
E2E_HTTP=$(curl --silent --show-error --max-time 90 -X POST "$E2E_BASE/api/v1/actions/evaluate" \
 -H 'Content-Type: application/json' --data-binary @"$E2E_OUT/S21-move-request.json" \
 -o "$E2E_OUT/S21-move-response.json" -w '%{http_code}')
test "$E2E_HTTP" = '200'
jq -e '.decision=="PENDING_APPROVAL" and .risk_level=="MEDIUM"' "$E2E_OUT/S21-move-response.json"
audit_decision "$E2E_OUT/S21-move-response.json"
E2E_MOVE_HOLD_ID="$(jq -er '.decision_id' "$E2E_OUT/S21-move-response.json")"
cat > "$E2E_OUT/S21-review-request.json" <<JSON
{"decision_id":"${E2E_MOVE_HOLD_ID}","reviewer_id":"${E2E_REVIEWER}","approve":true,"note":"Approve moving the synthetic file"}
JSON
E2E_HTTP=$(curl --silent --show-error --max-time 90 -X POST "$E2E_BASE/api/v1/control/approve/$E2E_MOVE_HOLD_ID" \
 -H 'Content-Type: application/json' --data-binary @"$E2E_OUT/S21-review-request.json" \
 -o "$E2E_OUT/S21-review-response.json" -w '%{http_code}')
test "$E2E_HTTP" = '200'
jq '.evaluation' "$E2E_OUT/S21-review-response.json" > "$E2E_OUT/S21-approved-move.json"
jq -e '.decision=="ALLOWED"' "$E2E_OUT/S21-approved-move.json"
audit_decision "$E2E_OUT/S21-approved-move.json"
```

### S21.3. Execute move and compare both resource keys

```bash
jq '{decision_id,token:.capability_token}' "$E2E_OUT/S21-approved-move.json" > "$E2E_OUT/S21-execute-request.json"
E2E_HTTP=$(curl --silent --show-error --max-time 90 -X POST "$E2E_BASE/api/v1/actions/execute" \
 -H 'Content-Type: application/json' --data-binary @"$E2E_OUT/S21-execute-request.json" \
 -o "$E2E_OUT/S21-execute-response.json" -w '%{http_code}')
test "$E2E_HTTP" = '200'
jq -e --arg source "$E2E_LIVE_TARGET" --arg dest "$E2E_MOVED_TARGET" \
 '.status=="EXECUTED" and .before_state[$source].content=="Newer work"
 and .before_state[$dest]==null and .after_state[$source]==null
 and .after_state[$dest]=={"kind":"file","content":"Newer work"}' "$E2E_OUT/S21-execute-response.json"
export E2E_CHECK_TARGET="$E2E_LIVE_TARGET"
dbread 'SELECT resource FROM simulated_resources WHERE resource=:target' > "$E2E_OUT/S21-source-after.json"
jq -e 'length==0' "$E2E_OUT/S21-source-after.json"
export E2E_CHECK_TARGET="$E2E_MOVED_TARGET"
dbread 'SELECT value_json FROM simulated_resources WHERE resource=:target' > "$E2E_OUT/S21-destination-after.json"
jq -e 'length==1 and .[0].value_json.content=="Newer work"' "$E2E_OUT/S21-destination-after.json"
```

Expected: source removed, destination preserves content, two-key snapshots persisted atomically. Later tests must refer to MOVED_TARGET, since LIVE_TARGET no longer exists.

### S21.4. Create an occupied destination through the authorized API

```bash
export E2E_OCCUPIED_TARGET="$E2E_PREFIX/occupied-destination.txt"
E2E_REQ="$(python -c 'from uuid import uuid4; print(uuid4())')"
jq --arg request "$E2E_REQ" --arg target "$E2E_OCCUPIED_TARGET" \
 '.request_id=$request|.target_resource=$target' "$E2E_OUT/S07-create-request.json" > "$E2E_OUT/S21-occupied-request.json"
E2E_HTTP=$(curl --silent --show-error --max-time 90 -X POST "$E2E_BASE/api/v1/actions/evaluate" \
 -H 'Content-Type: application/json' --data-binary @"$E2E_OUT/S21-occupied-request.json" \
 -o "$E2E_OUT/S21-occupied-response.json" -w '%{http_code}')
test "$E2E_HTTP" = '200'
jq -e '.decision=="ALLOWED"' "$E2E_OUT/S21-occupied-response.json"
audit_decision "$E2E_OUT/S21-occupied-response.json"
jq '{decision_id,token:.capability_token}' "$E2E_OUT/S21-occupied-response.json" > "$E2E_OUT/S21-occupied-execute-request.json"
E2E_HTTP=$(curl --silent --show-error --max-time 90 -X POST "$E2E_BASE/api/v1/actions/execute" \
 -H 'Content-Type: application/json' --data-binary @"$E2E_OUT/S21-occupied-execute-request.json" \
 -o "$E2E_OUT/S21-occupied-execute-response.json" -w '%{http_code}')
test "$E2E_HTTP" = '200'
jq -e '.status=="EXECUTED"' "$E2E_OUT/S21-occupied-execute-response.json"
```

### S21.5. Approve moving into the occupied destination; execution rejects it

```bash
E2E_REQ="$(python -c 'from uuid import uuid4; print(uuid4())')"
jq --arg request "$E2E_REQ" --arg source "$E2E_MOVED_TARGET" --arg dest "$E2E_OCCUPIED_TARGET" \
 '.request_id=$request|.target_resource=$source|.parameters.destination=$dest' \
 "$E2E_OUT/S21-move-request.json" > "$E2E_OUT/S21-collision-request.json"
E2E_HTTP=$(curl --silent --show-error --max-time 90 -X POST "$E2E_BASE/api/v1/actions/evaluate" \
 -H 'Content-Type: application/json' --data-binary @"$E2E_OUT/S21-collision-request.json" \
 -o "$E2E_OUT/S21-collision-response.json" -w '%{http_code}')
test "$E2E_HTTP" = '200'
jq -e '.decision=="PENDING_APPROVAL"' "$E2E_OUT/S21-collision-response.json"
audit_decision "$E2E_OUT/S21-collision-response.json"
E2E_COLLISION_ID="$(jq -er '.decision_id' "$E2E_OUT/S21-collision-response.json")"
jq --arg id "$E2E_COLLISION_ID" '.decision_id=$id|.note="Approve occupied-destination precondition probe"' \
 "$E2E_OUT/S21-review-request.json" > "$E2E_OUT/S21-collision-review-request.json"
E2E_HTTP=$(curl --silent --show-error --max-time 90 -X POST "$E2E_BASE/api/v1/control/approve/$E2E_COLLISION_ID" \
 -H 'Content-Type: application/json' --data-binary @"$E2E_OUT/S21-collision-review-request.json" \
 -o "$E2E_OUT/S21-collision-review-response.json" -w '%{http_code}')
test "$E2E_HTTP" = '200'
jq '.evaluation' "$E2E_OUT/S21-collision-review-response.json" > "$E2E_OUT/S21-collision-approved.json"
jq -e '.decision=="ALLOWED"' "$E2E_OUT/S21-collision-approved.json"
audit_decision "$E2E_OUT/S21-collision-approved.json"
jq '{decision_id,token:.capability_token}' "$E2E_OUT/S21-collision-approved.json" > "$E2E_OUT/S21-collision-execute-request.json"
state_snapshot > "$E2E_OUT/S21-collision-before.json"
E2E_HTTP=$(curl --silent --show-error --max-time 90 -X POST "$E2E_BASE/api/v1/actions/execute" \
 -H 'Content-Type: application/json' --data-binary @"$E2E_OUT/S21-collision-execute-request.json" \
 -o "$E2E_OUT/S21-collision-execute-response.json" -w '%{http_code}')
test "$E2E_HTTP" = '200'
jq -e '.status=="REJECTED"' "$E2E_OUT/S21-collision-execute-response.json"
state_snapshot > "$E2E_OUT/S21-collision-after.json"
cmp -s "$E2E_OUT/S21-collision-before.json" "$E2E_OUT/S21-collision-after.json"
```

Expected: source and occupied destination both preserved; no token consumption/execution row for this rejected move.

### S21.6. Duplicate-create and missing-update executor preconditions

The following are two separate cases. Both are policy-ALLOWED for the main agent's independently earned LIVE action modes; both must be execution-REJECTED.

```bash
cat > "$E2E_OUT/S21-precondition-cases.json" <<JSON
[
 {"name":"duplicate-create","action":"create_file","target":"${E2E_MOVED_TARGET}","parameters":{"content":"Overwrite attempt"}},
 {"name":"missing-update","action":"update_file","target":"${E2E_PREFIX}/never-created-update.txt","parameters":{"content":"Hello"}}
]
JSON
for E2E_CASE in duplicate-create missing-update; do
  E2E_REQ="$(python -c 'from uuid import uuid4; print(uuid4())')"
  jq --arg name "$E2E_CASE" --arg agent "$E2E_MAIN_AGENT" --arg request "$E2E_REQ" \
   '.[]|select(.name==$name)|{request_id:$request,agent_id:$agent,actor_role:"workspace_agent",
      action_class:.action,target_resource:.target,parameters:.parameters,environment:"development"}' \
   "$E2E_OUT/S21-precondition-cases.json" > "$E2E_OUT/S21-${E2E_CASE}-request.json"
  E2E_HTTP=$(curl --silent --show-error --max-time 90 -X POST "$E2E_BASE/api/v1/actions/evaluate" \
   -H 'Content-Type: application/json' --data-binary @"$E2E_OUT/S21-${E2E_CASE}-request.json" \
   -o "$E2E_OUT/S21-${E2E_CASE}-response.json" -w '%{http_code}') || break
  test "$E2E_HTTP" = '200' || break
  jq -e '.decision=="ALLOWED"' "$E2E_OUT/S21-${E2E_CASE}-response.json" || break
  audit_decision "$E2E_OUT/S21-${E2E_CASE}-response.json" || break
  jq '{decision_id,token:.capability_token}' "$E2E_OUT/S21-${E2E_CASE}-response.json" > "$E2E_OUT/S21-${E2E_CASE}-execute-request.json"
  state_snapshot > "$E2E_OUT/S21-${E2E_CASE}-before.json"
  E2E_HTTP=$(curl --silent --show-error --max-time 90 -X POST "$E2E_BASE/api/v1/actions/execute" \
   -H 'Content-Type: application/json' --data-binary @"$E2E_OUT/S21-${E2E_CASE}-execute-request.json" \
   -o "$E2E_OUT/S21-${E2E_CASE}-execute-response.json" -w '%{http_code}') || break
  test "$E2E_HTTP" = '200' || break
  jq -e '.status=="REJECTED"' "$E2E_OUT/S21-${E2E_CASE}-execute-response.json" || break
  state_snapshot > "$E2E_OUT/S21-${E2E_CASE}-after.json"
  cmp -s "$E2E_OUT/S21-${E2E_CASE}-before.json" "$E2E_OUT/S21-${E2E_CASE}-after.json" || break
done
```

Observe both cases; do not treat loop exit as proof of completion. End state: moved file content still Newer work; missing-update target still absent. Policy permission does not override resource feasibility.

## S22. Observe actual expiry of the NEVER-CONSUMED token from S08

### S22.1. Check the real expiration time; wait only if necessary

```bash
E2E_EXPIRES_AT="$(jq -er '.expires_at' "$E2E_OUT/S08-expiry.json")"
printf 'Current UTC epoch: %s; token expiration: %s\n' "$(date +%s)" "$E2E_EXPIRES_AT"
while [ "$(date +%s)" -le "$((E2E_EXPIRES_AT + 2))" ]; do
  printf 'Waiting for actual expiry; current UTC epoch: %s\n' "$(date +%s)"
  sleep 30
done
```

If executing through an agent tool, start this with a short tool yield and poll/update the user at intervals <=60 seconds. No individual sleep exceeds 30 seconds. Do not alter the token/clock/TTL; the wait must reflect genuine expiration.

### S22.2. Verify token was never consumed and target never created

```bash
export E2E_CHECK_ID="$(jq -er '.decision_id' "$E2E_OUT/expiry-create.json")"
dbread 'SELECT nonce FROM consumed_tokens WHERE decision_id=:id' > "$E2E_OUT/S22-nonces-before.json"
dbread 'SELECT execution_id FROM execution_records WHERE decision_id=:id' > "$E2E_OUT/S22-executions-before.json"
export E2E_CHECK_TARGET="$E2E_PREFIX/expiry-must-remain-absent.txt"
dbread 'SELECT resource FROM simulated_resources WHERE resource=:target' > "$E2E_OUT/S22-resource-before.json"
jq -e 'length==0' "$E2E_OUT/S22-nonces-before.json"
jq -e 'length==0' "$E2E_OUT/S22-executions-before.json"
jq -e 'length==0' "$E2E_OUT/S22-resource-before.json"
```

### S22.3. Attempt execution after expiry

```bash
jq '{decision_id,token:.capability_token}' "$E2E_OUT/expiry-create.json" > "$E2E_OUT/S22-execute-request.json"
state_snapshot > "$E2E_OUT/S22-state-before.json"
E2E_HTTP=$(curl --silent --show-error --max-time 90 -X POST "$E2E_BASE/api/v1/actions/execute" \
 -H 'Content-Type: application/json' --data-binary @"$E2E_OUT/S22-execute-request.json" \
 -o "$E2E_OUT/S22-execute-response.json" -w '%{http_code}')
test "$E2E_HTTP" = '200'
jq -e '.status=="REJECTED"' "$E2E_OUT/S22-execute-response.json"
state_snapshot > "$E2E_OUT/S22-state-after.json"
cmp -s "$E2E_OUT/S22-state-before.json" "$E2E_OUT/S22-state-after.json"
```

End result: actual expired, previously unused token cannot execute; no resource/nonce/execution row added. Combined with S06 and S07, this proves expiry, replay rejection, and decision binding separately.

## S23. True shadow disagreement and rejection despite enough later clean runs

This scenario demonstrates that **active policy decides**, the candidate only logs, and past candidate violations cannot be erased by later clean runs. It uses a second distinct candidate so intentional disagreement does not ruin S03's successful-promotion demonstration.

Prerequisites: S03 passed and no shadow candidate currently exists. If a preexisting candidate was deliberately left in place, mark this scenario BLOCKED instead of silently retiring it. This section is an explicitly recorded synthetic policy fixture; all old policies/history remain intact.

### S23.1. Create a new shadow candidate with a unique deny marker

```bash
export E2E_SHADOW_MARKER="${E2E_RUN_ID}_candidate_marker_2"
python - <<'PY' > "$E2E_OUT/S23-candidate-fixture.json"
import copy, json, os, re
from sqlmodel import Session, select
from src.db.database import engine
from src.models.policy import Policy
from src.services.policy_loader import canonical_policy_hash, validate_policy
from src.services.policy_store import validate_stored
with Session(engine) as session:
    active = session.exec(select(Policy).where(Policy.is_active == True).with_for_update()).one()
    validate_stored(active)
    old = session.exec(select(Policy).where(Policy.is_shadow == True).with_for_update()).one_or_none()
    assert old is None, "STOP: unexpected existing candidate; do not replace it silently"
    major, minor, _ = map(int, active.version.split("."))
    versions = set(session.exec(select(Policy.version)).all())
    version = f"{major}.{minor+1}.0"
    while version in versions:
        minor += 1
        version = f"{major}.{minor+1}.0"
    rules = copy.deepcopy(active.rules_json)
    rules["version"] = version
    rules["regex_deny_rules"].append({"pattern":re.escape(os.environ["E2E_SHADOW_MARKER"]),
                                    "reason":"E2E second candidate marker forbidden"})
    validate_policy(rules)
    candidate = Policy(version=version, policy_hash=canonical_policy_hash(rules), rules_json=rules, is_shadow=True)
    session.add(candidate)
    session.commit()
    print(json.dumps({"candidate_hash":candidate.policy_hash,"version":version,
                      "active_hash":active.policy_hash}, indent=2))
PY
show_json "$E2E_OUT/S23-candidate-fixture.json"
export E2E_CANDIDATE_2="$(jq -er '.candidate_hash' "$E2E_OUT/S23-candidate-fixture.json")"
export E2E_ACTIVE_BEFORE_DIFF="$(jq -er '.active_hash' "$E2E_OUT/S23-candidate-fixture.json")"
```

Expected: fresh candidate2, same active policy and rules except candidate version/new marker deny. No autonomy manipulation or threshold changes.

### S23.2. Active policy ALLOWS the marker; candidate DENIES it

```bash
E2E_REQ="$(python -c 'from uuid import uuid4; print(uuid4())')"
jq --arg request "$E2E_REQ" --arg target "$E2E_PREFIX/shadow-difference.txt" --arg marker "$E2E_SHADOW_MARKER" \
 '.request_id=$request|.target_resource=$target|.parameters={content:$marker}' \
 "$E2E_OUT/S07-create-request.json" > "$E2E_OUT/S23-difference-request.json"
state_snapshot > "$E2E_OUT/S23-state-before.json"
E2E_HTTP=$(curl --silent --show-error --max-time 90 -X POST "$E2E_BASE/api/v1/actions/evaluate" \
 -H 'Content-Type: application/json' --data-binary @"$E2E_OUT/S23-difference-request.json" \
 -o "$E2E_OUT/S23-difference-response.json" -w '%{http_code}')
test "$E2E_HTTP" = '200'
jq -e --arg active "$E2E_ACTIVE_BEFORE_DIFF" '.decision=="ALLOWED" and .policy_hash==$active
 and .capability_token!=null' "$E2E_OUT/S23-difference-response.json"
audit_decision "$E2E_OUT/S23-difference-response.json"
export E2E_CHECK_ID="$(jq -er '.decision_id' "$E2E_OUT/S23-difference-response.json")"
dbread 'SELECT d.outcome,d.policy_hash,s.candidate_policy_hash,s.would_have_decided
 FROM decision_records d JOIN shadow_records s ON s.decision_id=d.decision_id WHERE d.decision_id=:id' > "$E2E_OUT/S23-difference-db.json"
jq -e --arg candidate "$E2E_CANDIDATE_2" 'length==1 and .[0].outcome=="ALLOWED"
 and .[0].candidate_policy_hash==$candidate and .[0].would_have_decided=="DENIED"' "$E2E_OUT/S23-difference-db.json"
state_snapshot > "$E2E_OUT/S23-state-after.json"
cmp -s "$E2E_OUT/S23-state-before.json" "$E2E_OUT/S23-state-after.json"
```

Do not execute this token: this case observes dual evaluation only. Expected: primary response follows active policy; candidate's denial is linked and logged, does not change the primary response, and does not cause candidate execution.

### S23.3. Add P clean real evaluations without executing them

```bash
for E2E_I in $(seq 1 "$E2E_P"); do
  E2E_REQ="$(python -c 'from uuid import uuid4; print(uuid4())')"
  jq --arg request "$E2E_REQ" --arg target "$E2E_PREFIX/candidate2-clean-${E2E_I}.txt" \
   '.request_id=$request|.target_resource=$target' "$E2E_OUT/S07-create-request.json" > "$E2E_OUT/S23-clean-${E2E_I}-request.json"
  E2E_HTTP=$(curl --silent --show-error --max-time 90 -X POST "$E2E_BASE/api/v1/actions/evaluate" \
   -H 'Content-Type: application/json' --data-binary @"$E2E_OUT/S23-clean-${E2E_I}-request.json" \
   -o "$E2E_OUT/S23-clean-${E2E_I}-response.json" -w '%{http_code}') || break
  test "$E2E_HTTP" = '200' || break
  jq -e '.decision=="ALLOWED"' "$E2E_OUT/S23-clean-${E2E_I}-response.json" || break
  audit_decision "$E2E_OUT/S23-clean-${E2E_I}-response.json" || break
done
export E2E_CANDIDATE="$E2E_CANDIDATE_2"
dbread "SELECT count(*) AS runs, count(*) FILTER (WHERE s.would_have_decided NOT IN ('SHADOW_LOGGED','PENDING_APPROVAL','ALLOWED')
 OR d.outcome NOT IN ('SHADOW_LOGGED','PENDING_APPROVAL','ALLOWED')) AS violations
 FROM shadow_records s JOIN decision_records d ON d.decision_id=s.decision_id WHERE s.candidate_policy_hash=:candidate" > "$E2E_OUT/S23-evidence.json"
jq -e --argjson p "$E2E_P" '.[0].runs >= $p and .[0].violations==1' "$E2E_OUT/S23-evidence.json"
```

Expected: enough total runs, exactly the intentional marker violation still present. This isolates lifetime violation rejection from mere below-threshold rejection.

### S23.4. Human promotion still returns false

```bash
cat > "$E2E_OUT/S23-promote-request.json" <<JSON
{"candidate_policy_hash":"${E2E_CANDIDATE_2}","reviewer_id":"${E2E_REVIEWER}","approve":true}
JSON
E2E_HTTP=$(curl --silent --show-error --max-time 90 -X POST "$E2E_BASE/api/v1/control/promote-policy" \
 -H 'Content-Type: application/json' --data-binary @"$E2E_OUT/S23-promote-request.json" \
 -o "$E2E_OUT/S23-promote-response.json" -w '%{http_code}')
test "$E2E_HTTP" = '200'
jq -e --arg active "$E2E_ACTIVE_BEFORE_DIFF" '.promoted==false and .active_policy_hash==$active' "$E2E_OUT/S23-promote-response.json"
dbread 'SELECT approved,promoted,evidence_json FROM policy_promotion_events WHERE policy_hash=:candidate ORDER BY created_at DESC LIMIT 1' > "$E2E_OUT/S23-promotion-audit.json"
jq -e --argjson p "$E2E_P" 'length==1 and .[0].approved and (.[0].promoted|not)
 and .[0].evidence_json.runs >= $p and (.[0].evidence_json.violations|length)==1' "$E2E_OUT/S23-promotion-audit.json"
```

End state: active unchanged; candidate2 remains shadow and intentionally disqualified; no history erased. Report this test candidate as a leftover. Later API validation failures should not add shadow/decision rows.

## S24. Validation, duplicate request identity, depth guard, and forged control fields

### S24.1. Required field missing → HTTP 422

Capture ledger/autonomy state before the validation-only cases:

```bash
dbread 'SELECT decision_id,outcome FROM decision_records WHERE left(agent_id,length(:run_id))=:run_id ORDER BY decision_id' > "$E2E_OUT/S24-ledger-before.json"
dbread 'SELECT agent_id,action_class,mode,streak,updated_at FROM autonomy_state WHERE left(agent_id,length(:run_id))=:run_id ORDER BY agent_id,action_class' > "$E2E_OUT/S24-autonomy-before.json"
cat > "$E2E_OUT/S24-missing-field-request.json" <<JSON
{"agent_id":"${E2E_MAIN_AGENT}"}
JSON
E2E_HTTP=$(curl --silent --show-error --max-time 30 -X POST "$E2E_BASE/api/v1/actions/evaluate" \
 -H 'Content-Type: application/json' --data-binary @"$E2E_OUT/S24-missing-field-request.json" \
 -o "$E2E_OUT/S24-missing-field-response.json" -w '%{http_code}')
test "$E2E_HTTP" = '422'
jq -e '.detail|type=="array"' "$E2E_OUT/S24-missing-field-response.json"
```

Expected: schema validation error, no provider/evaluation/execution.

### S24.2. Recursive parameter has wrong type → HTTP 400

```bash
E2E_REQ="$(python -c 'from uuid import uuid4; print(uuid4())')"
jq --arg request "$E2E_REQ" '.request_id=$request|.parameters.recursive="true"' \
 "$E2E_OUT/S07-create-request.json" > "$E2E_OUT/S24-recursive-type-request.json"
E2E_HTTP=$(curl --silent --show-error --max-time 90 -X POST "$E2E_BASE/api/v1/actions/evaluate" \
 -H 'Content-Type: application/json' --data-binary @"$E2E_OUT/S24-recursive-type-request.json" \
 -o "$E2E_OUT/S24-recursive-type-response.json" -w '%{http_code}')
test "$E2E_HTTP" = '400'
jq -e '.detail=="parameters.recursive must be a boolean"' "$E2E_OUT/S24-recursive-type-response.json"
```

Expected: string `"true"` is not silently treated as boolean true; transaction rolls back with no ledger/autonomy changes.

### S24.3. Empty prompt → HTTP 400

```bash
cat > "$E2E_OUT/S24-empty-prompt-request.json" <<JSON
{"agent_id":"${E2E_RUN_ID}_empty_prompt","actor_role":"workspace_agent","raw_prompt":""}
JSON
E2E_HTTP=$(curl --silent --show-error --max-time 30 -X POST "$E2E_BASE/api/v1/actions/evaluate-prompt" \
 -H 'Content-Type: application/json' --data-binary @"$E2E_OUT/S24-empty-prompt-request.json" \
 -o "$E2E_OUT/S24-empty-prompt-response.json" -w '%{http_code}')
test "$E2E_HTTP" = '400'
jq -e '.detail|contains("1-8192")' "$E2E_OUT/S24-empty-prompt-response.json"
```

### S24.4. Repeat the original request UUID → HTTP 409

This deliberately reuses the exact S01 request file. Do not generate a new UUID for this subcase.

```bash
E2E_HTTP=$(curl --silent --show-error --max-time 90 -X POST "$E2E_BASE/api/v1/actions/evaluate" \
 -H 'Content-Type: application/json' --data-binary @"$E2E_OUT/S01-request.json" \
 -o "$E2E_OUT/S24-duplicate-request-response.json" -w '%{http_code}')
test "$E2E_HTTP" = '409'
jq -e '.detail=="duplicate request or conflicting database state"' "$E2E_OUT/S24-duplicate-request-response.json"
dbread 'SELECT decision_id,outcome FROM decision_records WHERE left(agent_id,length(:run_id))=:run_id ORDER BY decision_id' > "$E2E_OUT/S24-ledger-after-validation.json"
dbread 'SELECT agent_id,action_class,mode,streak,updated_at FROM autonomy_state WHERE left(agent_id,length(:run_id))=:run_id ORDER BY agent_id,action_class' > "$E2E_OUT/S24-autonomy-after-validation.json"
cmp -s "$E2E_OUT/S24-ledger-before.json" "$E2E_OUT/S24-ledger-after-validation.json"
cmp -s "$E2E_OUT/S24-autonomy-before.json" "$E2E_OUT/S24-autonomy-after-validation.json"
```

Expected: no second decision, no streak inflation, no partial committed audit. An online advisory call may have occurred before the duplicate was discovered; no provider trace is expected in the ledger for this rolled-back request.

### S24.5. Excessive nested parameter depth → committed DENIED decision

```bash
E2E_TEMPLATE="$E2E_OUT/S07-create-request.json" python - <<'PY' > "$E2E_OUT/S24-depth-request.json"
import json, os
from uuid import uuid4
with open(os.environ["E2E_TEMPLATE"]) as f:
    request = json.load(f)
value = "Hello"
for _ in range(25):
    value = {"nested":value}
request.update(request_id=str(uuid4()), agent_id=os.environ["E2E_RUN_ID"]+"_depth",
 target_resource=os.environ["E2E_PREFIX"]+"/depth-denial.txt", parameters={"metadata":value})
print(json.dumps(request))
PY
E2E_HTTP=$(curl --silent --show-error --max-time 90 -X POST "$E2E_BASE/api/v1/actions/evaluate" \
 -H 'Content-Type: application/json' --data-binary @"$E2E_OUT/S24-depth-request.json" \
 -o "$E2E_OUT/S24-depth-response.json" -w '%{http_code}')
test "$E2E_HTTP" = '200'
jq -e '.decision=="DENIED" and .capability_token==null and (.reason|contains("supported scan depth"))' "$E2E_OUT/S24-depth-response.json"
audit_decision "$E2E_OUT/S24-depth-response.json"
```

Expected: inability to inspect deep parameters fails closed and is logged; this is distinct from transport/schema validation errors.

### S24.6. Client cannot declare itself LIVE or supply trusted review evidence

```bash
E2E_REQ="$(python -c 'from uuid import uuid4; print(uuid4())')"
jq --arg request "$E2E_REQ" --arg agent "$E2E_RUN_ID" --arg target "$E2E_PREFIX/forged-controls.txt" \
 --arg reviewer "$E2E_REVIEWER" '.request_id=$request|.agent_id=($agent+"_forged_controls")|.target_resource=$target
 |.autonomy_level="LIVE"|.review_approval={approved:true,reviewer_id:$reviewer,original_decision_id:"invented"}
 |.compensation={execution_id:"invented"}' "$E2E_OUT/S07-create-request.json" > "$E2E_OUT/S24-forged-request.json"
E2E_HTTP=$(curl --silent --show-error --max-time 90 -X POST "$E2E_BASE/api/v1/actions/evaluate" \
 -H 'Content-Type: application/json' --data-binary @"$E2E_OUT/S24-forged-request.json" \
 -o "$E2E_OUT/S24-forged-response.json" -w '%{http_code}')
test "$E2E_HTTP" = '200'
jq -e '.decision=="SHADOW_LOGGED" and .autonomy_level=="SHADOW" and .capability_token==null
 and (.action_request|has("review_approval")|not) and (.action_request|has("compensation")|not)' "$E2E_OUT/S24-forged-response.json"
audit_decision "$E2E_OUT/S24-forged-response.json"
jq -e '.[0].evidence_json|has("review_approval")|not' "$E2E_OUT/audit-latest.json"
```

Expected for the current schema: unrecognized top-level fields are ignored; trusted server evidence is not taken from the caller. The fresh agent starts SHADOW. This tests action-payload trust boundaries; it does **not** imply that control endpoints authenticate reviewer identities (they currently do not).

### S24.7. Unknown execution, unknown rollback, unknown replay

```bash
export E2E_UNKNOWN_ID="$(python -c 'from uuid import uuid4; print(uuid4())')"
cat > "$E2E_OUT/S24-unknown-execute-request.json" <<JSON
{"decision_id":"${E2E_UNKNOWN_ID}","token":"invalid-token"}
JSON
state_snapshot > "$E2E_OUT/S24-unknown-before.json"
E2E_HTTP=$(curl --silent --show-error --max-time 30 -X POST "$E2E_BASE/api/v1/actions/execute" \
 -H 'Content-Type: application/json' --data-binary @"$E2E_OUT/S24-unknown-execute-request.json" \
 -o "$E2E_OUT/S24-unknown-execute-response.json" -w '%{http_code}')
test "$E2E_HTTP" = '200'
jq -e '.status=="REJECTED"' "$E2E_OUT/S24-unknown-execute-response.json"
E2E_HTTP=$(curl --silent --show-error --max-time 30 -X POST "$E2E_BASE/api/v1/actions/rollback/$E2E_UNKNOWN_ID" \
 -o "$E2E_OUT/S24-unknown-rollback-response.json" -w '%{http_code}')
test "$E2E_HTTP" = '200'
jq -e '.status=="FAILED"' "$E2E_OUT/S24-unknown-rollback-response.json"
E2E_HTTP=$(curl --silent --show-error --max-time 30 "$E2E_BASE/api/v1/replay/$E2E_UNKNOWN_ID" \
 -o "$E2E_OUT/S24-unknown-replay-response.json" -w '%{http_code}')
test "$E2E_HTTP" = '404'
jq -e '.detail|contains("not found")' "$E2E_OUT/S24-unknown-replay-response.json"
state_snapshot > "$E2E_OUT/S24-unknown-after.json"
cmp -s "$E2E_OUT/S24-unknown-before.json" "$E2E_OUT/S24-unknown-after.json"
```

Expected: documented distinction between business rejection (HTTP 200) and missing replay (HTTP 404), with no invented resource/execution/token-consumption rows.

## S25. Replay EVERY run decision after policy changes, reviews, and compensation

### S25.1. Capture decision inventory and prove all five outcomes were exercised

```bash
dbread 'SELECT decision_id,outcome,policy_hash FROM decision_records
 WHERE left(agent_id,length(:run_id))=:run_id ORDER BY decision_id' > "$E2E_OUT/S25-decisions.json"
jq -e '([.[].outcome]|unique|sort)==(["ALLOWED","DENIED","ESCALATED","PENDING_APPROVAL","SHADOW_LOGGED"]|sort)' "$E2E_OUT/S25-decisions.json"
```

Expected: all five outcomes observed. The inventory includes internally generated approval and compensation decisions because their agent IDs share the run prefix.

### S25.2. Save state before replay

Avoid other activity against the shared app during this comparison. Concurrent legitimate writes would require investigating differences rather than blaming replay.

```bash
state_snapshot > "$E2E_OUT/S25-state-before.json"
dbread 'SELECT * FROM autonomy_state WHERE left(agent_id,length(:run_id))=:run_id ORDER BY agent_id,action_class' > "$E2E_OUT/S25-autonomy-before.json"
dbread 'SELECT q.* FROM escalation_queue q JOIN decision_records d ON d.decision_id=q.decision_id
 WHERE left(d.agent_id,length(:run_id))=:run_id ORDER BY q.escalation_id' > "$E2E_OUT/S25-queue-before.json"
dbread 'SELECT policy_hash,version,is_active,is_shadow,reviewer_id,activated_at,deactivated_at FROM policies ORDER BY policy_hash' > "$E2E_OUT/S25-policies-before.json"
dbread 'SELECT d.* FROM decision_records d WHERE left(d.agent_id,length(:run_id))=:run_id ORDER BY decision_id' > "$E2E_OUT/S25-ledger-before.json"
```

### S25.3. Call replay separately for each recorded decision

```bash
E2E_REPLAYED=0
E2E_TOTAL_REPLAYS="$(jq 'length' "$E2E_OUT/S25-decisions.json")"
for E2E_ID in $(jq -r '.[].decision_id' "$E2E_OUT/S25-decisions.json"); do
  E2E_EXPECTED_OUTCOME="$(jq -r --arg id "$E2E_ID" '.[]|select(.decision_id==$id)|.outcome' "$E2E_OUT/S25-decisions.json")"
  E2E_EXPECTED_HASH="$(jq -r --arg id "$E2E_ID" '.[]|select(.decision_id==$id)|.policy_hash' "$E2E_OUT/S25-decisions.json")"
  E2E_HTTP=$(curl --silent --show-error --max-time 90 "$E2E_BASE/api/v1/replay/$E2E_ID" \
    -o "$E2E_OUT/S25-replay-${E2E_ID}.json" -w '%{http_code}') || break
  test "$E2E_HTTP" = '200' || break
  jq -e --arg id "$E2E_ID" --arg outcome "$E2E_EXPECTED_OUTCOME" --arg hash "$E2E_EXPECTED_HASH" \
    '.decision_id==$id and .original_outcome==$outcome and .replayed_outcome==$outcome
     and .is_consistent==true and .policy_hash==$hash and .discrepancy_reason==null' \
    "$E2E_OUT/S25-replay-${E2E_ID}.json" || break
  E2E_REPLAYED=$((E2E_REPLAYED + 1))
  printf 'Replay matched %s/%s: %s (%s)\n' "$E2E_REPLAYED" "$E2E_TOTAL_REPLAYS" "$E2E_ID" "$E2E_EXPECTED_OUTCOME"
done
test "$E2E_REPLAYED" = "$E2E_TOTAL_REPLAYS"
```

Expected per request: HTTP 200, original outcome equals replayed outcome, same original policy hash, is_consistent=true, no discrepancy reason. Old policy decisions remain reproducible even when another policy is now active. Review rejection does not rewrite the historical decision.

### S25.4. Prove replay changed no persisted run state

```bash
state_snapshot > "$E2E_OUT/S25-state-after.json"
dbread 'SELECT * FROM autonomy_state WHERE left(agent_id,length(:run_id))=:run_id ORDER BY agent_id,action_class' > "$E2E_OUT/S25-autonomy-after.json"
dbread 'SELECT q.* FROM escalation_queue q JOIN decision_records d ON d.decision_id=q.decision_id
 WHERE left(d.agent_id,length(:run_id))=:run_id ORDER BY q.escalation_id' > "$E2E_OUT/S25-queue-after.json"
dbread 'SELECT policy_hash,version,is_active,is_shadow,reviewer_id,activated_at,deactivated_at FROM policies ORDER BY policy_hash' > "$E2E_OUT/S25-policies-after.json"
dbread 'SELECT d.* FROM decision_records d WHERE left(d.agent_id,length(:run_id))=:run_id ORDER BY decision_id' > "$E2E_OUT/S25-ledger-after.json"
cmp -s "$E2E_OUT/S25-state-before.json" "$E2E_OUT/S25-state-after.json"
cmp -s "$E2E_OUT/S25-autonomy-before.json" "$E2E_OUT/S25-autonomy-after.json"
cmp -s "$E2E_OUT/S25-queue-before.json" "$E2E_OUT/S25-queue-after.json"
cmp -s "$E2E_OUT/S25-policies-before.json" "$E2E_OUT/S25-policies-after.json"
cmp -s "$E2E_OUT/S25-ledger-before.json" "$E2E_OUT/S25-ledger-after.json"
```

End result: replay reproduces all recorded outcomes with no resource, execution, nonce, autonomy, queue, policy-pointer, or ledger mutation. It uses frozen evidence; source inspection shows no fresh LLM/embedding lookup in this path. Without provider/network telemetry, do not separately claim you measured absence of network traffic.

Do not corrupt a shared policy/hash to exercise replay divergence. Shared-DB corruption and database-write-outage fault injection are outside this online run. Mark them NOT EXECUTED rather than inferring their success from normal replay. They require a separately isolated fault-testing environment.

## S26. Final cross-scenario audit: expected final resources, queues, provenance

### S26.1. Compare the entire run's resources with the expected end state

For a complete successful run of all preceding scenarios, **exactly four** synthetic resources remain:

| Resource suffix | Expected value | Why |
|---|---|---|
| `created-directory` | directory | S20 executed creation. |
| `moved-file.txt` | file, content `Newer work` | S10 updated; S21 moved; collision/duplicate attempts rejected. |
| `occupied-destination.txt` | file, content `Hello` | S21 created; rejected move preserved it. |
| `rollback-without-update-trust.txt` | file, content `Hello` | S18 create executed; untrusted compensation held. |

Original approved file was compensated; original live-file path moved away; production deletion target was deleted. Expired, denied, SHADOW, unapproved, semantic, broad, CRITICAL, training and shadow-comparison targets were never executed.

```bash
dbread 'SELECT resource,value_json FROM simulated_resources WHERE left(resource,length(:prefix))=:prefix ORDER BY resource' > "$E2E_OUT/S26-resources-actual.json"
cat > "$E2E_OUT/S26-resources-expected.json" <<JSON
[
 {"resource":"${E2E_PREFIX}/created-directory","value_json":{"kind":"directory"}},
 {"resource":"${E2E_PREFIX}/moved-file.txt","value_json":{"kind":"file","content":"Newer work"}},
 {"resource":"${E2E_PREFIX}/occupied-destination.txt","value_json":{"kind":"file","content":"Hello"}},
 {"resource":"${E2E_PREFIX}/rollback-without-update-trust.txt","value_json":{"kind":"file","content":"Hello"}}
]
JSON
jq -S 'sort_by(.resource)' "$E2E_OUT/S26-resources-actual.json" > "$E2E_OUT/S26-resources-actual-sorted.json"
jq -S 'sort_by(.resource)' "$E2E_OUT/S26-resources-expected.json" > "$E2E_OUT/S26-resources-expected-sorted.json"
cmp -s "$E2E_OUT/S26-resources-actual-sorted.json" "$E2E_OUT/S26-resources-expected-sorted.json"
show_json "$E2E_OUT/S26-resources-actual.json"
```

If any prerequisite/scenario was blocked, derive the expected residual state from the **actually completed** steps, list the deviations, and do not claim this full-run four-resource assertion passed.

### S26.2. Audit online provenance for every recorded decision

```bash
dbread "SELECT decision_id,outcome,policy_hash,
 evidence_json->'semantic'->>'source' AS source,
 evidence_json->'semantic'->>'fallback_reason' AS fallback_reason,
 capability_token_hash IS NOT NULL AS token_hash_present
 FROM decision_records WHERE left(agent_id,length(:run_id))=:run_id ORDER BY decision_id" > "$E2E_OUT/S26-provenance.json"
jq -e --arg source "$E2E_SEMANTIC_SOURCE" \
 'length>0 and all(.[]; .source==$source and .fallback_reason==null
  and .token_hash_present==(.outcome=="ALLOWED"))' "$E2E_OUT/S26-provenance.json"
dbread "SELECT decision_id,request_json->'parameters'->'parser_provenance' AS parser
 FROM decision_records WHERE left(agent_id,length(:run_id))=:run_id
 AND request_json->'parameters'->'parser_provenance' IS NOT NULL" > "$E2E_OUT/S26-parser-provenance.json"
jq -e --arg parser "$E2E_PARSER_SOURCE" 'length>=1 and all(.[]; .parser.source==$parser and .parser.fallback_reason==null)' "$E2E_OUT/S26-parser-provenance.json"
```

Expected: zero semantic/parser fallbacks including approval/compensation decisions. Token hash only for ALLOWED. An ALLOWED decision can remain unexecuted or have execution rejected; that does not retroactively change its policy outcome.

### S26.3. Check every persisted execution consumed exactly one token

```bash
dbread 'SELECT e.execution_id,e.decision_id,e.status,count(c.nonce) AS nonces
 FROM execution_records e JOIN decision_records d ON d.decision_id=e.decision_id
 LEFT JOIN consumed_tokens c ON c.decision_id=e.decision_id
 WHERE left(d.agent_id,length(:run_id))=:run_id
 GROUP BY e.execution_id,e.decision_id,e.status ORDER BY e.execution_id' > "$E2E_OUT/S26-execution-nonces.json"
jq -e 'length==11 and all(.[]; .nonces==1 and (.status=="EXECUTED" or .status=="COMPENSATED"))' "$E2E_OUT/S26-execution-nonces.json"
dbread 'SELECT c.nonce,c.decision_id FROM consumed_tokens c JOIN decision_records d ON d.decision_id=c.decision_id
 LEFT JOIN execution_records e ON e.decision_id=c.decision_id
 WHERE left(d.agent_id,length(:run_id))=:run_id AND e.execution_id IS NULL' > "$E2E_OUT/S26-orphan-nonces.json"
jq -e 'length==0' "$E2E_OUT/S26-orphan-nonces.json"
```

For the full successful run, expected **11 persisted execution records and 11 consumed tokens**: S04 create; S07 create; S10 compensation and update; S13 seed create and delete; S18 create; S19 read; S20 directory create; S21 move and occupied-target create. Exactly one original execution is COMPENSATED; the other 10 remain EXECUTED. The compensation itself has its own successful execution record. Rejected attempts insert neither nonce nor execution record. For a partially blocked run, reconcile counts against its actual completed steps instead of using 11.

```bash
jq -e '[.[]|select(.status=="COMPENSATED")]|length==1' "$E2E_OUT/S26-execution-nonces.json"
jq -e '[.[]|select(.status=="EXECUTED")]|length==10' "$E2E_OUT/S26-execution-nonces.json"
```

### S26.4. Inventory remaining pending reviews and human attribution

```bash
dbread 'SELECT q.decision_id,q.status,q.reviewer_id,q.package_json FROM escalation_queue q
 JOIN decision_records d ON d.decision_id=q.decision_id WHERE left(d.agent_id,length(:run_id))=:run_id
 ORDER BY q.decision_id' > "$E2E_OUT/S26-reviews.json"
jq -e --arg reviewer "$E2E_REVIEWER" \
 'all(.[]; if .status=="PENDING" then .reviewer_id==null else .reviewer_id==$reviewer end)' "$E2E_OUT/S26-reviews.json"
jq '[.[]|select(.status=="PENDING")]|length' "$E2E_OUT/S26-reviews.json"
E2E_EXPECTED_PENDING=$((E2E_N + E2E_L - 2 * E2E_A - 1))
jq -e --argjson n "$E2E_EXPECTED_PENDING" '[.[]|select(.status=="PENDING")]|length==$n' "$E2E_OUT/S26-reviews.json"
```

For the full default run: 13 unreviewed main create training packages + 15 unreviewed update training packages + 1 renewed CRITICAL package = **29 pending test reviews**. They are intentional leftovers, not proof that the reviewed actions failed. Do not mass-approve/reject them to tidy the report. With blocked/skipped steps, report actual queue inventory instead of using the full-run formula.

### S26.5. Validate final policy integrity and inspect promotion events

```bash
python - <<'PY' > "$E2E_OUT/S26-policy-inventory.json"
import json
from sqlalchemy import text
from sqlmodel import Session, select
from src.db.database import engine
from src.models.policy import Policy
from src.services.policy_store import validate_stored
with Session(engine) as session:
    session.execute(text("SET TRANSACTION READ ONLY"))
    policies = session.exec(select(Policy).order_by(Policy.created_at)).all()
    for p in policies:
        validate_stored(p)
    print(json.dumps([{"version":p.version,"hash":p.policy_hash,"active":p.is_active,
                      "shadow":p.is_shadow,"reviewer":p.reviewer_id} for p in policies], indent=2))
PY
show_json "$E2E_OUT/S26-policy-inventory.json"
jq -e '[.[]|select(.active)]|length==1' "$E2E_OUT/S26-policy-inventory.json"
dbread 'SELECT e.* FROM autonomy_promotion_events e JOIN autonomy_state a ON a.id=e.autonomy_state_id
 WHERE left(a.agent_id,length(:run_id))=:run_id ORDER BY e.created_at,e.event_id' > "$E2E_OUT/S26-autonomy-promotions.json"
dbread 'SELECT * FROM policy_promotion_events WHERE reviewer_id=:reviewer ORDER BY created_at,event_id' > "$E2E_OUT/S26-policy-promotion-events.json"
show_json "$E2E_OUT/S26-policy-promotion-events.json"
```

Expected: exactly one active pointer, valid hashes/versions, preserved old policies, attributed control events. If S23 completed, candidate2 remains shadow and disqualified. Approval patch bumps and agent promotion events must remain distinct from candidate activation events.

### S26.6. Compare unrelated data with the pre-run snapshots

```bash
dbread 'SELECT * FROM simulated_resources WHERE left(resource,length(:prefix))<>:prefix ORDER BY resource' > "$E2E_OUT/unrelated-resources-after.json"
dbread 'SELECT * FROM autonomy_state WHERE left(agent_id,length(:run_id))<>:run_id ORDER BY agent_id,action_class' > "$E2E_OUT/unrelated-autonomy-after.json"
dbread 'SELECT q.* FROM escalation_queue q JOIN decision_records d ON d.decision_id=q.decision_id
 WHERE left(d.agent_id,length(:run_id))<>:run_id ORDER BY q.escalation_id' > "$E2E_OUT/unrelated-reviews-after.json"
cmp -s "$E2E_OUT/unrelated-resources-before.json" "$E2E_OUT/unrelated-resources-after.json"
cmp -s "$E2E_OUT/unrelated-autonomy-before.json" "$E2E_OUT/unrelated-autonomy-after.json"
cmp -s "$E2E_OUT/unrelated-reviews-before.json" "$E2E_OUT/unrelated-reviews-after.json"
jq -e --slurpfile before "$E2E_OUT/policies-before.json" \
 '[.[].hash] as $hashes | all($before[0][]; .hash as $hash | $hashes | index($hash)!=null)' "$E2E_OUT/S26-policy-inventory.json"
```

Expected: preexisting resources/autonomy/reviews unchanged, all original policy rows retained with valid original hashes. Recorded active/shadow flag changes are intentional workflow/fixture changes. If other users made concurrent legitimate changes, inspect the differences and document them instead of automatically claiming this comparison passed.

### S26.7. Reinspect Qdrant and compare collection retention explicitly

```bash
python - <<'PY' > "$E2E_OUT/qdrant-inventory-after.json"
import json
from uuid import NAMESPACE_URL, uuid5
from qdrant_client import QdrantClient, models
from src.config import settings as s
from src.services.embeddings import specification
from src.services.vector_setup import PATTERNS
client = QdrantClient(url=s.QDRANT_URL,api_key=s.QDRANT_API_KEY,timeout=10,check_compatibility=False)
try:
    names = [c.name for c in client.get_collections().collections]
    info = client.get_collection(s.QDRANT_COLLECTION)
    vectors = info.config.params.vectors
    assert isinstance(vectors,models.VectorParams)
    points = client.retrieve(s.QDRANT_COLLECTION,with_vectors=False,with_payload=True,
       ids=[str(uuid5(NAMESPACE_URL,specification()+p)) for p,_ in PATTERNS])
    found = {(p.payload or {}).get("text") for p in points
             if (p.payload or {}).get("embedding_spec")==specification()}
    print(json.dumps({"collections":names,"configured_collection":s.QDRANT_COLLECTION,
      "dimensions":vectors.size,"cosine":vectors.distance==models.Distance.COSINE,
      "keyword_index":"embedding_spec" in info.payload_schema and info.payload_schema["embedding_spec"].data_type==models.PayloadSchemaType.KEYWORD,
      "missing_patterns":[p for p,_ in PATTERNS if p not in found]},indent=2))
finally:
    client.close()
PY
show_json "$E2E_OUT/qdrant-inventory-after.json"
```

```bash
jq -e --slurpfile before "$E2E_OUT/qdrant-inventory.json" \
 '.collections as $names | all($before[0].collections[]; . as $name | $names | index($name)!=null)' "$E2E_OUT/qdrant-inventory-after.json"
jq -e --argjson dims "$(jq '.embedding_dimensions' "$E2E_OUT/config.json")" \
 '.dimensions==$dims and .cosine and .keyword_index and .missing_patterns==[]' "$E2E_OUT/qdrant-inventory-after.json"
```

Expected: original collections, including `items` if present, still exist; configured advisory collection retains its vector contract/patterns. This verifies collection retention and the configured collection's metadata; it does not claim a byte-for-byte comparison of unrelated stored vectors. No step is authorized to delete/recreate those collections.

## D. Create the handoff report and redacted evidence bundle

### D1. Copy only redacted JSON evidence into the workspace

```bash
export E2E_REPORT_DIR="artifacts/online-e2e/${E2E_RUN_ID}"
mkdir -p "$E2E_REPORT_DIR"
for E2E_FILE in "$E2E_OUT"/*.json; do
  show_json "$E2E_FILE" > "$E2E_REPORT_DIR/$(basename "$E2E_FILE")" || break
done
printf 'Redacted evidence directory: %s\n' "$E2E_REPORT_DIR"
```

Do not copy private raw response files by another route. Keep their temporary location available until token-dependent steps finish; report only the redacted directory.

### D2. Write `REPORT.md` in that directory

The executing model must fill this from observations, not from expected values in this document:

```text
Run ID / UTC start-end / API URL / virtual environment
LLM provider and model actually observed
Embedding specification / Qdrant collection / cutoff / A,L,P
Database migration revision and baseline counts
Initial active and candidate hashes; baseline candidate qualification
Conditional setup actions performed (migrations, seeds, pointer/config changes)

Step or scenario | PASS/FAIL/BLOCKED/SKIPPED | expected | observed | evidence file / IDs
B1-B7 ...
C1-C2 ...
S00 ...
S01 ...
...
S26 ...

EVAL coverage:
EVAL-1: S04 and S07 (ALLOWED + valid scoped token + execution)
EVAL-2: S12 (hard denials)
EVAL-3: S13-S16 (holds and packages)
EVAL-4: S06/S07/S22 (replay, binding, real unused-token expiry)
EVAL-5: S09/S10/S18 (authorization, compensation, repeat/stale protection)
EVAL-6: S00/S03/S23 (candidate shadow, qualification, human promotion, disagreement)
EVAL-7: S04/S13/S16 (review response, patch version/hash, reevaluation)
EVAL-8: S25 (every stored outcome; state unchanged)
EVAL-9: S01/S11 (new agent SHADOW, no execution)
EVAL-10: S02 (ASSISTED pending approval)

Additional CRUD and input-boundary coverage: S19-S21/S24
Representative decision IDs for all five outcomes
Successful execution IDs; compensated original and compensation decision IDs
Online fallback count and affected IDs/reasons (must be zero for online acceptance)
Final active/candidate pointers; final resource and pending-review inventories
Not executed: unit tests/offline harness; shared-DB corruption/outage fault injection;
Docker/deployment; real filesystem execution; reviewer authentication (not implemented);
other LLM provider unless separately repeated
Failures/blocked prerequisites and exact next diagnostic action
Redacted evidence directory
```

Acceptance requires observed matches, not merely that commands were issued. A skipped/blocked candidate setup means candidate success coverage is incomplete. A fallback means the affected scenario did not pass the requested online run. A test that was never executed must remain unexecuted in the report.

### D3. Expected full-run end-to-end outcome

If every required prerequisite/scenario passes: real configured providers were used; fresh agents earned trust; active/shadow policies were evaluated separately; reviewer responses were recorded and versioned; only ALLOWED, valid, correctly bound, unexpired, unconsumed tokens executed; resource preconditions still applied; authorized compensation restored unchanged work; newer work survived; all historical decisions replayed consistently. The four resources and 29 default pending test reviews above remain identified. Existing unrelated data/collections remain preserved; policy-pointer fixture/workflow changes are explicitly reported.

Do not reset active/shadow pointers, remove test policies, delete vectors, or delete audit records automatically after the run. Such cleanup changes the demonstrated history and is a separate task.

## Instruction to Luna / Sol light

> Read `docs/ONLINE_E2E_GRANULAR_RUNBOOK.md`. Execute its shell commands one numbered step at a time in the same Bash session, starting with read-only discovery. Do not use the earlier Python session helper. Compare each HTTP status, response field, and database assertion with the expected result before continuing. Record failures and stop dependent chains; never modify application logic or expectations to force success. Keep credentials/tokens redacted. Complete the final cross-scenario audit and write the report/evidence bundle. Do not run unit tests, the offline harness, Docker, or deployment.

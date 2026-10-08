"""Interactive helpers for docs/ONLINE_E2E_PLAN.md; never runs scenarios automatically.

Start from the project root with: PYTHONPATH=. python -i scripts/online_e2e_session.py
Requires the application's existing dependencies. Tokens remain in memory;
the saved journal redacts them. All resource mutations go through HTTP.
"""

import hashlib
import json
from datetime import datetime, timezone
from uuid import uuid4

import httpx
from alembic.config import Config
from alembic.script import ScriptDirectory
from sqlalchemy import inspect, text
from sqlmodel import Session, select

from src.config import PROJECT_ROOT, settings
from src.db.database import engine
from src.models.decision import DecisionRecord
from src.models.policy import Policy
from src.services import embeddings, policy_promotion, semantic_engine
from src.services.llm_parser import ParsedAction
from src.services.llm_providers import structured_parser
from src.services.policy_store import validate_stored


TABLES = (
    "policies", "decision_records", "shadow_records", "autonomy_state",
    "autonomy_promotion_events", "escalation_queue", "policy_promotion_events",
    "simulated_resources", "execution_records", "consumed_tokens",
)


def redacted(value):
    if isinstance(value, dict):
        return {
            key: "<redacted>" if key.lower() in {
                "token", "capability_token", "authorization", "api_key", "secret_key"
            } and item else redacted(item)
            for key, item in value.items()
        }
    if isinstance(value, (list, tuple)):
        return [redacted(item) for item in value]
    return value


class OnlineSession:
    def __init__(self, base_url="http://127.0.0.1:8000"):
        self.base_url = base_url.rstrip("/")
        self.run_id = "e2e_" + datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ") + "_" + uuid4().hex[:6]
        self.agent = self.run_id + "_main"
        self.reviewer = self.run_id + "_reviewer"
        self.root = None
        self.prefix = None
        self.candidate = None
        self.A = settings.PROMOTION_THRESHOLD_ASSISTED
        self.L = settings.PROMOTION_THRESHOLD_LIVE
        self.P = settings.PROMOTION_THRESHOLD_POLICY
        self.journal = []
        self.decisions = {}
        self.representatives = {}
        self.output = PROJECT_ROOT / "artifacts" / "online-e2e" / self.run_id / "journal.json"

    def log(self, label, value):
        event = {"time": datetime.now(timezone.utc).isoformat(), "label": label,
                 "value": redacted(value)}
        self.journal.append(event)
        self.output.parent.mkdir(parents=True, exist_ok=True)
        self.output.write_text(json.dumps({"run_id": self.run_id, "events": self.journal},
                                          indent=2, default=str))
        print(json.dumps(event, default=str))
        return value

    def check(self, label, condition, evidence=None):
        self.log(label, {"passed": bool(condition), "evidence": evidence})
        if not condition:
            raise AssertionError(label)

    def read(self, sql, params=None):
        # Discovery and evidence checks cannot commit application mutations.
        with engine.connect() as connection:
            assert connection.dialect.name == "postgresql", "Online plan requires PostgreSQL"
            connection.execute(text("SET TRANSACTION READ ONLY"))
            return [dict(row) for row in connection.execute(text(sql), params or {}).mappings()]

    def counts(self):
        # Identifiers come exclusively from this fixed allowlist.
        return {name: self.read(f'SELECT count(*) AS n FROM "{name}"')[0]["n"]
                for name in TABLES}

    def discover(self):
        self.log("safe configuration", {
            "offline": settings.OFFLINE_MODE, "llm_provider": settings.LLM_PROVIDER,
            "llm_model": settings.GEMINI_LLM_MODEL if settings.LLM_PROVIDER == "gemini" else settings.GROQ_LLM_MODEL,
            "embedding_provider": settings.EMBEDDING_PROVIDER,
            "embedding_spec": embeddings.specification(),
            "collection": settings.QDRANT_COLLECTION, "semantic_threshold": settings.SEMANTIC_THRESHOLD,
            "gemini_key_present": bool(settings.GEMINI_API_KEY),
            "groq_key_present": bool(settings.GROQ_API_KEY),
            "qdrant_key_present": bool(settings.QDRANT_API_KEY),
            "signing_key_changed_from_default": settings.SECRET_KEY != "dev-insecure-secret-change-me",
            "thresholds": {"assisted": self.A, "live": self.L, "policy": self.P},
        })
        self.check("online Gemini embeddings configured", not settings.OFFLINE_MODE
                   and settings.EMBEDDING_PROVIDER == "gemini" and bool(settings.GEMINI_API_KEY))
        self.check("valid thresholds", 0 < self.A <= self.L and self.P > 0)
        self.api("GET", "/health")
        with httpx.Client(timeout=10) as client:
            response = client.get(self.base_url + "/openapi.json")
            response.raise_for_status()
            paths = response.json()["paths"]
        self.check("expected API routes", all(path in paths for path in (
            "/api/v1/actions/evaluate", "/api/v1/actions/evaluate-prompt",
            "/api/v1/control/approve/{decision_id}", "/api/v1/actions/execute",
            "/api/v1/actions/rollback/{execution_id}", "/api/v1/replay/{decision_id}")))
        with engine.connect() as connection:
            existing = set(inspect(connection).get_table_names())
        self.check("required database tables", set(TABLES).issubset(existing), sorted(existing))
        cfg = Config(str(PROJECT_ROOT / "alembic.ini"))
        cfg.set_main_option("script_location", str(PROJECT_ROOT / "alembic"))
        heads = set(ScriptDirectory.from_config(cfg).get_heads())
        current = {row["version_num"] for row in self.read("SELECT version_num FROM alembic_version")}
        self.check("database at migration head", current == heads,
                   {"database": sorted(current), "code": sorted(heads)})
        with Session(engine) as session:
            session.execute(text("SET TRANSACTION READ ONLY"))
            policies = session.exec(select(Policy)).all()
            for policy in policies:
                validate_stored(policy)
            active = [p for p in policies if p.is_active]
            shadow = [p for p in policies if p.is_shadow]
            self.check("exactly one active, at most one candidate", len(active) == 1 and len(shadow) <= 1)
            self.root = active[0].rules_json["sandbox_root"].rstrip("/")
            self.initial_active = active[0].policy_hash
            self.prefix = self.root + "/" + self.run_id
            self.candidate = shadow[0].policy_hash if shadow else None
            self.rules = active[0].rules_json
            self.log("policy inventory", [{"version": p.version, "hash": p.policy_hash,
                      "active": p.is_active, "shadow": p.is_shadow} for p in policies])
            self.log("active policy rules", self.rules)
        self.log("baseline table counts", self.counts())
        self.log("existing autonomy summary", self.read(
            "SELECT mode, count(*) AS n FROM autonomy_state GROUP BY mode"))
        self.log("existing review summary", self.read(
            "SELECT status, count(*) AS n FROM escalation_queue GROUP BY status"))
        self.log("existing execution summary", self.read(
            "SELECT status, count(*) AS n FROM execution_records GROUP BY status"))
        self.check("fresh agent namespace", not self.read(
            "SELECT agent_id FROM autonomy_state WHERE left(agent_id, :n) = :prefix",
            {"n": len(self.run_id), "prefix": self.run_id}))
        self.check("fresh resource namespace", not self.read(
            "SELECT resource FROM simulated_resources WHERE left(resource, :n) = :prefix",
            {"n": len(self.prefix), "prefix": self.prefix}))
        if self.candidate:
            self.candidate_evidence()
        else:
            self.log("candidate discovery", {"status": "MISSING", "policy_promotion_scenario": "BLOCKED"})
        self.log("test namespace", {"agent": self.agent, "reviewer": self.reviewer, "prefix": self.prefix})

    def candidate_evidence(self):
        assert self.candidate, "No shadow candidate"
        with Session(engine) as session:
            session.execute(text("SET TRANSACTION READ ONLY"))
            result = policy_promotion.evidence(session, self.candidate)
        return self.log("candidate evidence", result)

    def cloud_inventory(self):
        from qdrant_client import models
        client = semantic_engine._cloud_client()
        try:
            names = [c.name for c in client.get_collections().collections]
            self.log("Qdrant collections", names)
            self.check("configured collection exists", settings.QDRANT_COLLECTION in names)
            info = client.get_collection(settings.QDRANT_COLLECTION)
            params = info.config.params.vectors
            self.check("unnamed cosine vectors and matching dimensions",
                       isinstance(params, models.VectorParams)
                       and params.distance == models.Distance.COSINE
                       and params.size == settings.GEMINI_EMBEDDING_DIMENSIONS)
            self.check("embedding_spec keyword payload index", "embedding_spec" in info.payload_schema
                       and info.payload_schema["embedding_spec"].data_type == models.PayloadSchemaType.KEYWORD)
            from src.services.vector_setup import point_id
            from src.services.semantic_corpus import load_corpus
            corpus = load_corpus()
            points = client.retrieve(settings.QDRANT_COLLECTION,
                ids=[point_id(example.id) for example in corpus.examples],
                with_payload=True, with_vectors=False)
            payloads = [point.payload or {} for point in points]
            self.log("configured Qdrant collection", {"collection": settings.QDRANT_COLLECTION,
                "dimensions": params.size, "distance": str(params.distance),
                "points_count": info.points_count, "matching_sample": payloads})
            self.check("all versioned contrasting examples seeded", {p.id for p in corpus.examples}.issubset(
                {p.get("example_id") for p in payloads if p.get("embedding_spec") == embeddings.specification()
                 and p.get("corpus_hash") == corpus.fingerprint}))
            self.check("semantic filter indexes", all(key in info.payload_schema
                for key in ("corpus_hash", "action_family", "label")))
        finally:
            client.close()

    def provider_probe(self):
        parsed = structured_parser(ParsedAction).invoke([
            {"role": "system", "content": "Return the requested file action exactly. Do not execute it."},
            {"role": "user", "content": "create file " + self.prefix + "/probe.txt with content Hello in development"},
        ])
        parsed = ParsedAction.model_validate(parsed)
        self.check("live structured LLM response", parsed.action_class == "create_file"
                   and parsed.target_resource == self.prefix + "/probe.txt"
                   and parsed.environment == "development" and parsed.parameters.get("content") == "Hello",
                   parsed.model_dump())
        from src.models.request import ActionRequest
        request = ActionRequest(agent_id=self.agent, actor_role="workspace_agent", action_class="create_file",
                                target_resource=self.prefix + "/probe.txt", environment="development",
                                parameters={"content": "Hello"})
        assessment = semantic_engine.assess(request)
        self.check("live embedding and cloud query, benign probe", assessment.source == "qdrant:" + embeddings.specification()
                   and assessment.fallback_reason is None and not assessment.flag, assessment.as_evidence())

    def api(self, method, path, payload=None, expected_http=200):
        with httpx.Client(timeout=90) as client:
            response = client.request(method, self.base_url + "/api/v1" + path, json=payload)
        try:
            body = response.json()
        except ValueError:
            body = {"detail": "non-JSON response", "body_omitted": True}
        self.log(method + " " + path, {"http_status": response.status_code, "body": body})
        self.check("expected HTTP status", response.status_code == expected_http,
                   {"expected": expected_http, "actual": response.status_code})
        return body

    def audit(self, response):
        decision_id = response["decision_id"]
        with Session(engine) as session:
            session.execute(text("SET TRANSACTION READ ONLY"))
            row = session.get(DecisionRecord, decision_id)
            self.check("API and local tooling use the same database", row is not None, decision_id)
            semantic = row.evidence_json.get("semantic", {})
            self.check("decision uses online semantic evidence", semantic.get("source") == "qdrant:" + embeddings.specification()
                       and semantic.get("fallback_reason") is None, semantic)
            self.check("stored decision matches response", row.outcome == response["decision"]
                       and row.request_id == response["request_id"] and row.policy_hash == response["policy_hash"])
            token = response.get("capability_token")
            self.check("token issued only for ALLOWED", bool(token) == (row.outcome == "ALLOWED"))
            self.check("only token hash is persisted", row.capability_token_hash == (
                hashlib.sha256(token.encode()).hexdigest() if token else None))
            self.log("decision evidence", {"decision_id": decision_id, "reason": row.reason,
                                          "evidence": row.evidence_json})
        parser = response["action_request"]["parameters"].get("parser_provenance")
        if parser:
            expected = settings.LLM_PROVIDER + ":" + (settings.GEMINI_LLM_MODEL if settings.LLM_PROVIDER == "gemini" else settings.GROQ_LLM_MODEL)
            self.check("prompt parsed by configured live provider", parser.get("source") == expected
                       and parser.get("fallback_reason") is None, parser)
        self.decisions[decision_id] = response
        self.representatives.setdefault(response["decision"], decision_id)
        return response

    def evaluate(self, agent=None, action="create_file", name=None, parameters=None,
                 role="workspace_agent", environment="development", expected=None, target=None):
        assert self.prefix, "Run discover() first"
        result = self.api("POST", "/actions/evaluate", {
            "agent_id": agent or self.agent, "actor_role": role, "action_class": action,
            "target_resource": target or self.prefix + "/" + (name or uuid4().hex + ".txt"),
            "parameters": parameters if parameters is not None else {"content": "Hello"},
            "environment": environment,
        })
        self.audit(result)
        if expected:
            self.check("expected decision", result["decision"] == expected,
                       {"expected": expected, "actual": result["decision"]})
        return result

    def autonomy(self, agent=None, action="create_file"):
        rows = self.read("SELECT mode, streak, reviewer_id FROM autonomy_state WHERE agent_id=:agent AND action_class=:action",
                         {"agent": agent or self.agent, "action": action})
        return rows[0] if rows else None

    def train(self, agent=None, action="create_file", n=None, role="workspace_agent"):
        agent = agent or self.agent
        self.check("training starts with fresh agent/action", self.autonomy(agent, action) is None)
        n = n if n is not None else max(self.L, self.P)
        results = []
        for index in range(n):
            result = self.evaluate(agent=agent, action=action, role=role,
                name=f"training-{action}-{index}.txt",
                parameters={"content": "Hello"} if action in {"create_file", "update_file"} else {},
                expected="SHADOW_LOGGED" if index < self.A else "PENDING_APPROVAL")
            results.append(result)
        state = self.autonomy(agent, action)
        self.check("earned ASSISTED state", state["mode"] == "ASSISTED" and state["streak"] == n, state)
        return results

    def promote_agent(self, agent=None, action="create_file", approve=True, expected=True):
        result = self.api("POST", "/control/promote-agent", {"agent_id": agent or self.agent,
            "action_class": action, "reviewer_id": self.reviewer, "approve": approve})
        self.check("expected agent promotion result", result["promoted"] is expected, result)
        return result

    def review(self, response, approve, expected=None):
        decision_id = response["decision_id"]
        original_before = self.read(
            "SELECT outcome, policy_hash, request_json, evidence_json, created_at "
            "FROM decision_records WHERE decision_id=:id", {"id": decision_id})[0]
        policies_before = self.read("SELECT policy_hash FROM policies ORDER BY policy_hash")
        result = self.api("POST", "/control/approve/" + decision_id,
            {"decision_id": decision_id, "reviewer_id": self.reviewer,
             "approve": approve, "note": "Online E2E test reviewer response"})
        self.check("review status", result["status"] == ("APPROVED" if approve else "REJECTED"))
        original = self.read("SELECT outcome, policy_hash, reviewer_id, request_json, evidence_json, created_at FROM decision_records WHERE decision_id=:id", {"id": decision_id})[0]
        self.check("original outcome and policy hash preserved", original["outcome"] == response["decision"]
                   and original["policy_hash"] == response["policy_hash"] and original["reviewer_id"] == self.reviewer)
        self.check("original request, evidence and timestamp preserved",
                   all(original[key] == value for key, value in original_before.items()))
        package = self.read("SELECT status, reviewer_id, package_json FROM escalation_queue WHERE decision_id=:id",
                            {"id": decision_id})[0]
        self.check("review package records human response", package["status"] == result["status"]
                   and package["reviewer_id"] == self.reviewer
                   and package["package_json"].get("review_note") == "Online E2E test reviewer response")
        self.log("reviewed package", package)
        if approve:
            self.check("approval creates new policy and decision", bool(result["new_policy_version"])
                       and result["evaluation"]["decision_id"] != decision_id
                       and result["evaluation"]["policy_hash"] != response["policy_hash"])
            self.audit(result["evaluation"])
            if expected:
                self.check("reviewed decision", result["evaluation"]["decision"] == expected)
        else:
            self.check("rejection creates no evaluation/version", result["evaluation"] is None
                       and result["new_policy_version"] is None)
            self.check("rejection creates no policy", self.read(
                "SELECT policy_hash FROM policies ORDER BY policy_hash") == policies_before)
        return result

    def execution_state(self, response):
        request = response["action_request"]
        target = request["target_resource"]
        destination = request["parameters"].get("destination")
        resources = {target: self.resource(target)}
        if destination:
            resources[destination] = self.resource(destination)
        return {
            "resources": resources,
            "executions": self.read("SELECT execution_id, status, before_state, after_state FROM execution_records "
                                    "WHERE decision_id=:id ORDER BY execution_id", {"id": response["decision_id"]}),
            "nonces": self.read("SELECT nonce FROM consumed_tokens WHERE decision_id=:id ORDER BY nonce",
                                {"id": response["decision_id"]}),
        }

    def execute(self, response, expected="EXECUTED"):
        before = self.execution_state(response)
        result = self.api("POST", "/actions/execute", {
            "decision_id": response["decision_id"], "token": response["capability_token"]})
        self.check("expected execution result", result["status"] == expected, result)
        after = self.execution_state(response)
        if expected == "REJECTED":
            self.check("rejected execution has no resource, execution or nonce mutation", after == before)
        elif expected == "EXECUTED":
            self.check("successful execution consumes one token and persists one execution",
                       len(after["executions"]) == len(before["executions"]) + 1
                       and len(after["nonces"]) == len(before["nonces"]) + 1)
            self.check("persisted execution matches returned snapshots",
                       after["executions"][0]["before_state"] == result["before_state"]
                       and after["executions"][0]["after_state"] == result["after_state"])
        self.log("execution database before/after", {"decision_id": response["decision_id"],
                 "before": before, "after": after})
        return result

    def resource(self, target):
        rows = self.read("SELECT value_json FROM simulated_resources WHERE resource=:target", {"target": target})
        return rows[0]["value_json"] if rows else None

    def replay_all(self):
        required = {"SHADOW_LOGGED", "PENDING_APPROVAL", "ALLOWED", "DENIED", "ESCALATED"}
        self.check("all five decision outcomes observed", required.issubset(self.representatives), self.representatives)
        before = self.counts()
        snapshot_sql = {
            "resources": "SELECT resource, value_json FROM simulated_resources WHERE left(resource,:n)=:prefix ORDER BY resource",
            "executions": "SELECT e.* FROM execution_records e JOIN decision_records d ON d.decision_id=e.decision_id "
                          "WHERE left(d.agent_id,:n)=:prefix ORDER BY e.execution_id",
            "autonomy": "SELECT * FROM autonomy_state WHERE left(agent_id,:n)=:prefix ORDER BY agent_id, action_class",
        }
        def snapshots():
            return {key: self.read(sql, {"n": len(self.prefix if key == "resources" else self.run_id),
                                         "prefix": self.prefix if key == "resources" else self.run_id})
                    for key, sql in snapshot_sql.items()}
        state_before = snapshots()
        rows = self.read("SELECT decision_id FROM decision_records WHERE left(agent_id, :n)=:prefix",
                         {"n": len(self.run_id), "prefix": self.run_id})
        for row in rows:
            result = self.api("GET", "/replay/" + row["decision_id"])
            self.check("consistent replay", result["is_consistent"] is True, result)
        self.check("replay creates no rows", self.counts() == before)
        self.check("replay preserves resource, execution and autonomy state", snapshots() == state_before)


if __name__ == "__main__":
    t = OnlineSession()
    print("Session prepared; no network calls or scenarios have run.")
    print("Follow docs/ONLINE_E2E_PLAN.md. Start with t.discover().")
    print("Journal destination:", t.output)

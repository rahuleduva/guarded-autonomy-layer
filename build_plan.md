# Build Plan — Guarded Action & Autonomy Control Layer

**Assignment:** Option 2 — Guarded Action and Autonomy Control Layer, with Autonomy Levels and Decision Replay.
**Scope:** A complete, step-by-step build plan from an empty repo to a submittable, reviewable project.
**Companion document:** `review.md` (the fuller findings register). Every phase below names the register items it closes, so nothing on that list can be quietly dropped. **This document is standalone** — the full assignment text is reproduced in Appendix A, and the finding definitions in Appendix B, so it can be picked up without the surrounding conversation.

> **Readability note.** This document is written to be read by both humans and AI agents. Every term is defined the first time it is used, and the "Background & concepts" section exists so a reader with no prior context can understand the system before the step-by-step begins. If you are an AI agent picking this up cold: read Part A and the Ground Rules first — they are the invariants everything else obeys.

---

## How to use this plan

- Phases run in order. Each phase has **prerequisites**, **steps**, **files**, **tests**, and a **definition of done (DoD)**.
- Do not start a phase until its prerequisites are green.
- Each phase ends with its tests passing **and** the traceability matrix updated. A phase with no test is not done.
- Everything must run **offline by default**. Cloud services (Neon, Qdrant, Gemini) are optional accelerators, never prerequisites for the core path.
- One schema change = one Alembic migration. One logical step = one commit.

---

# Part A — Background & concepts

This part explains *what* we are building and *why*, in plain language, before any steps. Read it once and the rest of the plan will make sense.

## A.1 The problem in one paragraph

An AI agent (a program that decides what to do on its own) is useful precisely because it can take actions — read a file, update a record, delete a directory, send a notification. But an agent is also fallible and untrusted: it can hallucinate, loop, or be manipulated by a crafted input ("prompt injection"). If we hand it raw access to the real world, its mistakes are *our* mistakes. The safe pattern is to put a **control layer** between the agent and the world: the agent proposes an action, the control layer decides whether it may happen, and only then does anything real occur.

## A.2 What we are building

A **control layer** (also called a guardrail, a policy gateway, or a "policy decision + enforcement" layer) that answers one question for every proposed action:

> *May this agent perform this action, on this resource, in this context — right now?*

It answers with one of a small set of outcomes:

- **ALLOWED** — proceed, and here is a short-lived key (a *capability token*) authorising exactly this action.
- **DENIED** — refuse, with a reason.
- **ESCALATED** — the *policy* forced human review (critical risk or an advisory semantic warning).
- **PENDING_APPROVAL** — the *autonomy* layer held an otherwise-allowed action (agent in ASSISTED).
- **SHADOW_LOGGED** — the *autonomy* layer recorded an allowed action without executing it (agent in SHADOW).

## A.3 Why a separate layer (and not logic inside the agent)

- **The agent cannot be trusted to police itself.** An agent that wants to finish a task may rationalise a dangerous step. Safety logic must live outside it.
- **The decision must be independent of the agent's words.** If an LLM judged its own risk, a clever prompt could talk it out of a "deny". We keep the *decision* deterministic (see A.4).
- **Separation of concerns.** The agent generates *intent*; the control layer governs *permission*; a separate executor performs the *action*. Three roles, three places.

## A.4 Concept glossary

Each entry: **what it is** → *why it matters here*.

**Determinism** — the property that the same inputs always produce the same output.
*Why:* a security decision must be predictable and testable. If the decision varied run to run, you could never prove what it did, replay it, or trust a test. Determinism is the backbone of this whole design.

**Pure function / the decision core** — a function with no side effects: no database call, no network, no reading the clock inside it. Everything it needs is passed in as arguments.
*Why:* a pure function is trivially testable and reproducible. The heart of this system (`policy_engine.evaluate`) is pure; all I/O happens around it.

**Policy Decision Point (PDP) and Policy Enforcement Point (PEP)** — the PDP *decides*; the PEP *acts on the decision* (e.g. an executor that checks the token before touching anything).
*Why:* separating them means the decision can be tested alone, and enforcement cannot be bypassed by skipping the decision.

**Policy-as-data (versioned & hashed)** — the rules (who may do what, how risky each action is, what is forbidden) live in a **JSON artifact**, not as constants in code. Each artifact has a `version` and a content **hash** (a short fingerprint of the exact rules).
*Why:* with a hash you can (a) replay an old decision against the *exact* rules that produced it, and (b) run a *candidate* ruleset in shadow. This single decision unlocks replay and shadow mode — see C1, B1.

**Capability token** — a short-lived, cryptographically signed "permission slip" for one specific action on one specific resource. It carries an expiry (`exp`) and a unique id.
*Why:* this is *capability-based* security — access is granted by holding a specific, expiring token, rather than by ambient trust. A signed token cannot be forged or tampered with; an expiring one cannot be replayed forever; a single-use one cannot be spent twice.

**Risk classification & blast radius** — *risk* is a category (read-only, low, medium, high, critical). *Blast radius* is the concrete amount of damage an action could cause (one file vs an entire directory deleted recursively).
*Why:* these drive the decision — low risk may auto-allow, high blast radius escalates. Blast radius must be computed **deterministically** (by rules), never guessed by an LLM (C3).

**RBAC, scope, sandbox** — Role-Based Access Control: which *role* may perform which *action*. *Scope*: which *resources* (e.g. a path prefix). *Sandbox*: a boundary the agent may not step outside.
*Why:* least privilege. An agent allowed to edit `./workspace/` must not reach `/etc`. This is the "out-of-scope prevention" the assignment requires (A1).

**Autonomy levels — SHADOW → ASSISTED → LIVE** — how much freedom an agent has earned. Every agent/action-class starts in SHADOW (recorded, not executed). Promotion is a **two-rung ladder**: SHADOW → ASSISTED is automatic at the evidence threshold (N clean shadow runs); ASSISTED → LIVE additionally requires an explicit, recorded human promotion. ASSISTED means a held action needs sign-off; LIVE means it auto-executes within bounds.
*Why:* trust should be *earned from recorded evidence*, never granted by default (B6).

**Shadow mode — two distinct meanings.** This is the single most-confused idea in the project:
- *Agent shadow*: run a candidate **agent** alongside the trusted one and compare their outputs.
- *Policy shadow*: run a candidate **ruleset** alongside the active one and record what it *would have decided*, executing nothing.
*Why:* the assignment needs **policy shadow** — you change the rules safely, gather evidence, then promote the ruleset. Building agent-shadow instead is the drift we are correcting (B1).

**Replay** — re-running a past decision from its stored inputs and its policy version, and checking the outcome reproduces.
*Why:* auditability. "Why did the system deny this three weeks ago?" must have a verifiable answer. Replay is honest only if *every* input is stored and the *whole* pipeline is re-run (A2).

**Event log / ledger** — an append-only record of every decision and state change (the "audit spine").
*Why:* it is the single source of truth for audit and replay, and it lets current state (autonomy levels, pending queue) be *derived* rather than trusted.

**Escalation / human-in-the-loop (HITL)** — risky or denied actions are packaged and sent to a human review queue.
*Why:* a safety valve and an accountability point. The reviewer's decision is itself recorded and can update the policy (B3).

**Advisory vs authoritative signals** — some signals *raise scrutiny* (semantic similarity, regex deny-patterns); others *grant* access (path containment, RBAC).
*Why:* a fuzzy signal must never become the thing that *allows* an action. Semantic and regex layers may deny or flag; only deterministic path/role logic may grant (C2, C8).

**Fail closed** — when in doubt, deny.
*Why:* the safe default. An error, an ambiguous token, or a failed audit write must never let an action through (A3).

**Single-use / idempotency** — a token can be spent once; a duplicate request is recognised rather than re-executed.
*Why:* prevents replay attacks and double execution (A4).

**Evidence / provenance** — every material fact in a decision carries where it came from and when.
*Why:* reviewability — the assignment grades whether a human can follow your reasoning.

## A.5 The lifecycle of one request

This narrative ties the concepts to the phases. A request travels:

1. **Ingest & normalise** (Phase 1–2): accept structured JSON or natural language; turn both into one clean `ActionRequest`. (Natural language is parsed by an LLM, but only *here* — never in the decision.)
2. **Advisory checks** (Phase 4): semantic similarity and blast radius *raise scrutiny*; they never grant access.
3. **Deterministic decision** (Phase 2): the pure policy engine evaluates role, scope, environment, risk, and the frozen evidence — **taking the agent's autonomy state as an input** — and returns one of ALLOWED / DENIED / ESCALATED (policy-forced review) / PENDING_APPROVAL (autonomy-held) / SHADOW_LOGGED (autonomy shadow). The autonomy gate is *inside* this step, not after it.
4. **Shadow evaluation** (Phase 3): a candidate policy also evaluates the same request; its outcome is logged as *would-have-decided*, nothing executes.
5. **Escalation** (Phase 6): ESCALATED or PENDING_APPROVAL builds a package and enters the human queue; a reviewer decision updates the policy.
6. **Token & execution** (Phase 2, 9): ALLOWED issues a single-use capability token (minted by the orchestrator); the executor verifies and consumes it, then performs the simulated action — with a rollback path.
7. **Ledger & replay** (Phase 2, 8): every decision and its evidence is written immutably; later, replay reproduces it.
8. **Policy promotion** (Phase 7): once a candidate policy has enough recorded shadow evidence it **qualifies**, and a reviewer promotes it to active.

```
propose ─▶ ingest/normalise ─▶ advisory(blast, semantic)
        ─▶ DECIDE (active policy) ─┬─▶ shadow (candidate)  → ledger
                                   ├─▶ (ALLOWED) token ─▶ executor ─▶ action
                                   ├─▶ escalate → human → policy bump
                                   └─▶ deny
        ─▶ ledger (evidence frozen) ─▶ replay / promote
```

## A.6 How to read the phases

The phases build outward from a foundation: first a running skeleton (Phase 0), then the sharp edges are removed (Phase 1), then the deterministic core (Phase 2) and the features that depend on it (3–9), then the offline fallback and the graded harness (10), then the documentation a reviewer needs (11). Each phase lists a **Why** line so the purpose is never lost in the steps.

---

# Part B — Rules & reference

## Ground rules (the invariants)

1. **Determinism:** the decision core is pure — no LLM, no network, no clock inside it. All inputs are passed in. Token minting needs the clock and the secret, so it happens in the **orchestrator**, never inside the core.
2. **Policy as data:** rules live in versioned, hashed artifacts, never as code constants.
3. **Fail closed:** default deny; deny on engine error; deny on ambiguous token state; refuse to proceed if the audit write fails.
4. **Evidence frozen:** every input a decision consumed (semantic flag, blast radius, autonomy state, environment) is stored with the decision, so replay reproduces it exactly.
5. **Advisory vs authoritative:** semantic similarity and regex deny-lists may *raise scrutiny* or *deny*; only path algebra and RBAC may *grant* access.
6. **No client-trusted control flags:** shadow mode, autonomy level, and risk are assigned by the control layer, never asserted by the caller.
7. **Tests per phase:** each scenario's test is written as its phase lands, not reconstructed at the end.
8. **Secrets stay out of the repo:** only `.env.example` is tracked.
9. **Replay is read-only:** re-running a decision must never mutate state (streaks, autonomy, policies).

## 1. Final repository layout

```
guarded-autonomy-layer/
├── README.md
├── AGENT_WORKFLOW.md
├── Dockerfile
├── docker-compose.yml
├── requirements.txt
├── pyproject.toml
├── pytest.ini
├── alembic.ini
├── .env.example
├── .gitignore
│
├── alembic/
│   ├── env.py
│   ├── script.py.mako        # revision template — required by `alembic revision`
│   └── versions/
│
├── policies/
│   ├── policy_v1.0.0.json          # baseline active policy
│   └── policy_v1.1.0.json          # candidate policy for shadow
│
├── src/
│   ├── __init__.py
│   ├── main.py                     # unified FastAPI gateway (single entry point)
│   ├── config.py                   # settings incl. OFFLINE_MODE
│   ├── cli.py                      # CLI validation runner
│   │
│   ├── models/
│   │   ├── __init__.py
│   │   ├── request.py              # pure Pydantic API contracts (no tables)
│   │   ├── policy.py               # policy artifact + SQLModel table
│   │   ├── decision.py             # decision ledger table
│   │   ├── autonomy.py             # autonomy state table
│   │   ├── escalation.py           # escalation queue table
│   │   ├── policy_shadow.py        # candidate-policy shadow records table
│   │   └── token.py                # consumed-tokens table
│   │
│   ├── db/
│   │   ├── __init__.py
│   │   └── database.py             # engine/session (Postgres or SQLite)
│   │
│   └── services/
│       ├── __init__.py
│       ├── path_guard.py           # deterministic sandbox containment
│       ├── decision_ledger.py      # fail-closed decision append (audit spine)
│       ├── llm_parser.py           # NL -> ActionRequest (+ offline stub)
│       ├── semantic_engine.py      # Qdrant advisor (+ offline stub)
│       ├── policy_engine.py        # pure interpreter of versioned policy
│       ├── shadow_engine.py        # active vs candidate dual evaluation
│       ├── policy_promotion.py     # evidence-based candidate -> active promotion
│       ├── blast_radius.py         # deterministic blast radius estimator
│       ├── token_issuer.py         # JWT capability token issue/verify
│       ├── autonomy_service.py     # DB-backed autonomy state & streaks
│       ├── escalation_service.py   # review queue & reviewer policy bump
│       ├── replay_engine.py        # full-pipeline deterministic replay
│       └── executor.py             # token-consuming executor & rollback
│
├── examples/
│   ├── sample_requests.json
│   └── sample_outputs.json
│
├── docs/
│   └── session_excerpt.md
│
├── workspace/                # sandbox fixtures (the policy declares sandbox_root; this holds test files)
│   └── .gitkeep
│
└── tests/
    ├── __init__.py
    ├── conftest.py
    ├── test_phase1_security.py
    ├── test_phase2_policy.py
    ├── test_phase3_shadow.py
    ├── test_phase4_blast_radius.py
    ├── test_phase5_autonomy.py
    ├── test_phase6_escalation.py
    ├── test_phase7_policy_promotion.py
    ├── test_phase8_replay.py
    ├── test_phase9_executor.py
    ├── test_phase10_offline.py
    └── test_evaluation_scenarios.py   # consolidated EVAL-1..10 (reviewer entry point)
```

**Why this layout:** each service owns one concept (a decision, a shadow run, a blast radius, a token…), so the code mirrors the mental model. API contracts (`models/request.py`) are kept separate from DB tables so a schema migration never drags the request shape with it.

## 2. Traceability matrix (capabilities)

| ID | Requirement / finding | Phase | Register items | Test |
|---|---|---|---|---|
| REQ-1 | Classify by risk **& blast radius** | 2, 4 | B2, C3 | test_phase2, test_phase4 |
| REQ-2 | Evaluate actor, role, resource, scope, **environment**, policy | 2 | B7 | test_phase2 |
| REQ-3 | Short-lived capability token + unified response | 2 | A6 (fixed), A5 (fixed) | test_phase2 |
| REQ-4 | Distinguish read-only / low / approval-required / denied | 2, 6 | B8 | test_phase2, test_phase6 |
| REQ-5 | Human review queue & escalation package | 6 | B3 | test_phase6 |
| REQ-6 | Prevent expired / duplicated / tampered / out-of-scope execution | 1, 9 | A1, A4 | test_phase1, test_phase9 |
| REQ-7 | Log policy version, reason, evidence, approval, outcome | 2, 4, 6 | B4 | test_phase2/4/6 |
| REQ-8 | Executor verifying tokens & simulated rollback | 9 | B5 | test_phase9 |
| REQ-9 | Autonomy levels per agent/action class; promotion on recorded evidence | 5 | B6 | test_phase5 |
| REQ-10 | Run changed policy in shadow first | 3, 7 | B1 | test_phase3, test_phase7 |
| REQ-11 | Replay past decision from logged version + evidence | 8 | A2 | test_phase8 |
| REQ-12 | Offline / mock fallback (keyless) | 10 | C4 | test_phase10 |
| C2 | Semantic threshold advisory, not authoritative | 4 | C2 | test_phase4 |

## 3. Graded evaluation scenarios

| EVAL | Scenario | Pass criterion | Phase |
|---|---|---|---|
| EVAL-1 | Allow one low-risk action | `ALLOWED` + valid token with correct TTL | 2 |
| EVAL-2 | Deny one out-of-scope action | `DENIED` via sandbox containment | 1, 2 |
| EVAL-3 | Hold one high-impact action | `ESCALATED` (policy-forced) held in the queue with a package | 6 |
| EVAL-4 | Reject one expired **or replayed** authorization | verifier rejects expired and already-consumed tokens | 9 |
| EVAL-5 | Demonstrate a rollback / compensating action | action executes, then compensates successfully | 9 |
| EVAL-6 | Run changed policy in shadow, then promote on evidence | shadow outcome logged (nothing executed); candidate **qualifies** at threshold and is promoted to active by a reviewer | 3, 7 |
| EVAL-7 | Escalation package + reviewer updates policy version | package produced; approval triggers a policy version bump | 6 |
| EVAL-8 | Replay a stored decision and show it reproduces | `is_consistent == True` for the full pipeline | 8 |
| EVAL-9 | New agent's first action must not execute | `SHADOW_LOGGED` — recorded, nothing executed (the "no agent starts live" claim) | 5 |
| EVAL-10 | ASSISTED agent's allowed action is held | `PENDING_APPROVAL` — the "approval-required" class (REQ-4) | 6 |

> EVAL-1..10 are all **required**. EVAL-9 and EVAL-10 evidence explicit assignment claims ("no agent or action class should start live"; "distinguish read-only, low-risk, approval-required, and denied"), so they are protected from the §20 cut order alongside the original eight.

## 4. Submission deliverables tracker

| Deliverable | Phase | Notes |
|---|---|---|
| GitHub repository | throughout | clean history, one commit per step |
| README (problem, users, architecture, setup, demo, assumptions, trade-offs, limitations, next steps) | 11 | |
| Working CLI / API | 10 | `src/cli.py` + FastAPI |
| Synthetic sample inputs/outputs | 11 | `examples/` |
| Persistent state | 0–6 | Postgres/SQLite tables |
| Tests / validation script (all scenarios) | per phase + 10 | `tests/` |
| Evidence, provenance, approval handling | 4, 6 | ledger fields + queue |
| Keyless mock fallback | 10 | `OFFLINE_MODE=True` |
| Dockerfile / compose | 11 | |
| `AGENT_WORKFLOW.md` | throughout, assembled 11 | AI tools, where helped/failed, manual changes, audit guide |
| Redacted session excerpt | 11 | `docs/session_excerpt.md` |

---

# Part C — The phases

## 5. Phase 0 — Foundation & environment

**Goal:** an empty-but-running skeleton where migrations apply and the app boots offline.
**Why:** Alembic needs `config.py` and the model modules to exist before it can generate anything; and an offline-first foundation means nothing downstream is blocked by cloud credentials.
**Prerequisites:** none.

1. **Scaffold the tree** from Section 1: create directories and all `__init__.py` files.
2. **`requirements.txt` / `pyproject.toml`:** fastapi, uvicorn, pydantic, pydantic-settings, sqlmodel, alembic, psycopg[binary], pyjwt, sentence-transformers (optional), qdrant-client (optional), google-genai (optional), pytest, httpx.
3. **`pytest.ini`:** set testpaths and a `unit`/`integration` marker.
4. **`.gitignore`:** `.env`, `*.db`, `__pycache__/`, `.pytest_cache/`, `.venv/`.
5. **`.env.example`:** placeholder `DATABASE_URL`, `SECRET_KEY`, `QDRANT_URL`, `QDRANT_API_KEY`, `GEMINI_API_KEY`, `OFFLINE_MODE`, thresholds.
6. **`src/config.py`:** `Settings` (pydantic-settings) with `DATABASE_URL` (local Postgres default; SQLite when `OFFLINE_MODE=True`), `SECRET_KEY`, `QDRANT_*`, `GEMINI_API_KEY`, `PROMOTION_THRESHOLD_ASSISTED`, `PROMOTION_THRESHOLD_LIVE`, `PROMOTION_THRESHOLD_POLICY`, `SEMANTIC_THRESHOLD`. **Every field needs a default** so `Settings()` constructs without secrets. `sandbox_root` is **not** a setting — it is declared by the policy artifact (C.4). Token TTL is **also not** a setting: the TTL is consumed by the decision (it becomes `ttl_seconds` and the token's `exp`), so the artifact's `ttl_map` is the single source and a config override would let config silently disagree with the recorded decision. Export `settings`.
7. **`src/db/database.py`:** engine + session factory; branch on `OFFLINE_MODE` (SQLite) vs Postgres.
8. **`src/models/request.py`:** corrected contracts — see Section 6.
9. **SQLModel tables:** `models/policy.py`, `decision.py`, `autonomy.py`, `escalation.py`, `shadow.py`, `token.py`. Create the ledger table in its **full final shape now** — `request_json` (the complete `ActionRequest`), `policy_hash`, `reason`, `evidence_json`, `capability_token_hash`, `reviewer_id` — so no later migration is needed (see C.3).
10. **`docker-compose.yml`:** local Postgres service for development.
11. **Alembic:** confirm `env.py` imports `settings` and all `src.models.*`; bring up local Postgres; run `alembic revision --autogenerate -m "initial schema"`; `alembic upgrade head`; confirm tables exist.

**DoD:** app imports cleanly; `alembic upgrade head` creates all tables on local Postgres; the same runs against SQLite with `OFFLINE_MODE=True`.

## 6. The API contracts (`models/request.py`) — built in Phase 0

**Why:** the earlier draft dropped fields the rest of the system depends on; this pins the contract so RBAC, sandboxing, and blast radius all have what they need.

Restore the load-bearing fields (do not invent a new schema):

- `ActionRequest`: `request_id`, `agent_id`, `actor_role`, `action_class`, `target_resource`, `parameters`, `environment`, `timestamp`.
- `NaturalLanguageRequest`: `agent_id`, `actor_role`, `raw_prompt`.
- Response contract: one unified object both entry points return (`status`, `request_id`, `action_request`, plus decision fields).

Rules:
- Keep this file **pure Pydantic** — no `table=True`. It is the API contract only.
- Do **not** put `shadow_mode` on the request — that is control-layer config.
- Do **not** let `context: Dict` absorb `target_resource` or `actor_role`.
- Define the decision enum **once** (`ALLOWED`, `DENIED`, `ESCALATED`, `PENDING_APPROVAL`, `SHADOW_LOGGED`) and import it everywhere — no `ALLOW` vs `ALLOWED` drift.

## 7. Phase 1 — Security & ledger hygiene

**Goal:** remove the sharp edges before any feature work. Closes A1, A3, A7, A8.
**Why:** a control layer with a broken boundary check or a silently-dropped audit log cannot be trusted regardless of what features sit on top.

1. **`services/path_guard.py`:** deterministic, **absolute-path-only**, **pure algebraic** containment. `is_within_sandbox(target, sandbox_root)` requires **both** arguments to be absolute; if either is relative it returns `False` (fail closed) — the guard never resolves a relative path against the process cwd. It normalises with `os.path.normpath` and compares components with `os.path.commonpath`, rejecting `..` traversal and prefix-lookalikes (`workspace-evil`). **No filesystem access at all** — no `realpath`, no existence checks, no directory creation — so the answer depends only on the two strings, and the same check works for any path-like key space (POSIX paths or object-storage keys). The `sandbox_root` value comes from the **evaluated policy** (C.4) — not from config, and not from the app's location. A separate helper, `to_absolute(base_dir, target)`, is the *only* place a relative target is resolved — anchored to the policy's absolute `sandbox_root`, never cwd. **Symlink resolution is deliberately out of scope** (see Phase 11 limitations): the guard treats the path as a pure string, so symlink escapes are not detected — accepted, because A1's core is the prefix bug.
2. **Wire the guard** as the out-of-scope check (used by the policy engine later).
3. **Fail-closed ledger (A3):** make `log_decision` raise/propagate on failure so the request fails rather than silently unlogged.
4. **`request_id` entropy (A7):** generate full UUIDv4; never 8 hex chars.
5. **Unify entry points (A8):** `main.py` is the single gateway; both structured and NL routes return the same response contract.
6. **Tests (`test_phase1_security.py`):** path escapes rejected (`..`, prefix lookalikes); legit paths allowed; ledger failure aborts; duplicate `request_id` handled explicitly, not silently.

**DoD:** EVAL-2 passes; no path outside the sandbox can be reached; a failed audit write cannot let an action through.

## 8. Phase 2 — Versioned policy engine & schema

**Goal:** policy becomes versioned data; the engine interprets it. Closes C1, C8, B7, B8, and the REQ-7 hash.
**Why:** this is the load-bearing decision — versioned, hashed policy is what makes replay and shadow mode possible at all (see A.4, "Policy-as-data").

1. **Policy artifact schema:** JSON with `version`, `risk_map`, `role_permissions`, `sandbox_root`, `environment_rules`, `regex_deny_rules`, `ttl_map`. (Promotion thresholds live in `settings`, not the artifact — see C.4.)
2. **Loader + validation + content hash:** the loader **validates** the artifact before storing it, then canonicalizes (sorted keys) → SHA-256 → `policy_hash`, and stores it in the `policies` table (seeded from `policies/*.json` on startup; idempotent — insert a version only if its hash is absent). Validation **rejects** (raises) on any failure, so an invalid policy is never stored or activated: required keys present (`version`, `sandbox_root`, `risk_map`, `role_permissions`, `environment_rules`, `ttl_map`, `regex_deny_rules`); **no unknown keys** (the schema is closed — unknown keys are rejected, not ignored); `sandbox_root` **absolute**; every `risk_map` value a valid `RiskLevel`; every action named in `role_permissions` present in `risk_map`; every `regex_deny_rules` pattern compiles. Validation runs **before** hashing and storage. A policy with no scope is rejected so it can never reach a decision.
3. **`services/policy_engine.py`:** pure `evaluate(request, policy, evidence, autonomy_state) -> EvaluationResult`. Interprets the artifact; **no code constants**.
4. **Regex guardrails (C8):** deny-patterns from the artifact; path containment stays path algebra. Define rule precedence (deny → allow, most-specific first) and validate patterns at load (guard ReDoS).
5. **Environment evaluation (B7):** `environment` participates in policy decisions (e.g. destructive ops allowed in dev, escalated in prod).
6. **Decision vocabulary (B8):** single enum; clean `PENDING_APPROVAL` path distinct from autonomy.
7. **Token issuer:** JWT HS256 with TTL from `ttl_map`, binding `decision_id`, `policy_hash`, resource, and a nonce.
8. **Migration:** none — the ledger's full shape was created in Phase 0 (see C.3). Add a migration only if a column genuinely changes later.
9. **Tests (`test_phase2_policy.py`):** EVAL-1 (allow + token); EVAL-2 (deny); policy hash stability; environment branch; regex deny; precedence; **loader rejects an invalid policy** (missing key, **unknown key**, relative `sandbox_root`, **invalid `risk_map` value**, **role action absent from `risk_map`**, uncompilable regex) **and an invalid policy is never stored or activated** — asserted by checking the `policies` table is unchanged after each rejection.

**DoD:** identical request + identical policy → identical decision; `policy_hash` recorded on every decision.

## 9. Phase 3 — True policy shadow mode

**Goal:** a candidate policy is evaluated without executing. Closes B1 (first half).
**Why:** you cannot safely change the rules if you cannot first observe what the new rules *would* do (see A.4, "Shadow mode").

1. **`services/shadow_engine.py`:** load active + candidate policies; evaluate each request against both.
2. Active policy's outcome drives execution; candidate's outcome recorded as `would_have_decided` with the candidate `policy_hash`.
3. **`shadow_records`:** store the candidate's hash + would-have outcome as its own row (not in the decision ledger), linked to the decision by `decision_id`.
4. **Tests (`test_phase3_shadow.py`):** EVAL-6 first half — candidate logged, nothing executed, active unaffected.

**DoD:** shadow evaluation has zero side effects; candidate outcomes are queryable.

## 10. Phase 4 — Blast radius, evidence & semantic advisor

**Goal:** deterministic blast radius; semantic search as advisor. Closes B2, C2, C3.
**Why:** risk needs a concrete measure of damage, and a fuzzy "looks dangerous" signal must inform — never override — the deterministic decision.

1. **`services/blast_radius.py`:** deterministic score from target type (file vs dir), recursion flag, wildcards, path depth; optionally a **recorded** file count as evidence (not an LLM decision).
2. **`services/semantic_engine.py`:** Qdrant query with `all-MiniLM-L6-v2`; **offline stub** (keyword/regex) when unreachable. Returns advisory flag + score; **never** the sole basis for allow/deny.
3. **Evidence bundling:** attach blast-radius metrics + semantic warning to the decision; persist in `evidence_json`. The engine must **echo** `evidence["blast_radius"]` verbatim and never recompute it — recomputation would drift on replay (A2).
4. **Threshold calibration:** measure real similarity scores on seeded patterns; set the cutoff from evidence, not the arbitrary 0.85.
5. **Tests (`test_phase4_blast_radius.py`):** file vs recursive-dir scores; semantic hit raises scrutiny but doesn't override a hard deny; offline stub parity.

**DoD:** blast radius is reproducible; semantic layer is advisory and fails open (to deterministic-only) when offline.

## 11. Phase 5 — Persisted autonomy & evidence promotion

**Goal:** autonomy is DB-backed and earned. Closes B6.
**Why:** trust must survive a restart and be backed by recorded evidence, not held in memory.

1. **`services/autonomy_service.py`:** read/write autonomy per `(agent_id, action_class)` from the `autonomy_state` table, recording `reviewer_id` + `promoted_at` on a human LIVE grant (attribution).
2. Streaks derived from recorded decisions (or maintained in the table), with configurable thresholds.
3. Promotion ladder on recorded evidence: SHADOW→ASSISTED automatic at `PROMOTION_THRESHOLD_ASSISTED`; ASSISTED→LIVE requires `PROMOTION_THRESHOLD_LIVE` clean runs **and** an explicit recorded human promotion. Any violation resets the streak.
4. **Tests (`test_phase5_autonomy.py`):** streak survives restart; promotion at threshold; reset on violation.

**DoD:** promotion is provably backed by ledger evidence; no trust is held in memory.

## 12. Phase 6 — Human escalation loop & reviewer policy bump

**Goal:** held actions reach a human; the decision updates the policy. Closes B3.
**Why:** high-impact actions need a human in the loop, and that human's decision must have a durable effect (a policy version bump).

1. **`services/escalation_service.py`:** build `EscalationPackage` (request, risk, blast radius, semantic warning, recommended decision); insert into the `escalation_queue` table with queue `status = PENDING`. The queue accepts **both** held outcomes — `ESCALATED` (policy-forced) and `PENDING_APPROVAL` (autonomy-held).
2. **Reviewer endpoint** `/api/v1/control/approve/{decision_id}`: approve/reject, record `reviewer_id`, and — on approval — perform a **mandatory** policy version bump (new artifact + new hash).
3. **Tests (`test_phase6_escalation.py`):** EVAL-3 (hold + package); EVAL-7 (reviewer decision bumps the policy version, and the bump is recorded).

**DoD:** a high-impact action cannot execute without a recorded human decision; that decision changes the active policy version.

## 13. Phase 7 — Evidence-based policy promotion

**Goal:** a *candidate policy* that has met the evidence threshold is promoted to active by a deliberate human decision. Closes B1 (second half) — the gap that kept getting conflated with agent autonomy.
**Why:** the second half of the shadow requirement is *promoting the ruleset on evidence*. The evidence **qualifies** the candidate; a **reviewer** promotes it — mirroring the agent ladder, where the threshold qualifies ASSISTED→LIVE and a human grants it. A policy change has global blast radius (every request is affected at once), so the highest-consequence step is human-gated.

1. **`services/policy_promotion.py`:** count the candidate's shadow outcomes in the ledger. Meeting the threshold (N shadow runs, zero violations) makes the candidate **qualify** — it does **not** auto-promote. A reviewer then promotes it via `promote_candidate(candidate_policy_hash, reviewer_id, approved)`, which flips the pointers in one transaction: old active → `is_active=false, deactivated_at=now, activated_at=null`; candidate → `is_active=true, activated_at=now, deactivated_at=null, is_shadow=false, reviewer_id`. The promotion is attributed (who, when).
2. Keep this **separate** from agent-autonomy promotion (Phase 5) and from reviewer bumps (Phase 6).
3. **Tests (`test_phase7_policy_promotion.py`):** EVAL-6 second half — below threshold the candidate stays shadow and cannot be promoted; once it qualifies, a reviewer's promotion makes it active (with `reviewer_id` recorded).

**DoD:** the "run a changed policy in shadow, then promote it only on evidence" scenario is demonstrable end to end.

## 14. Phase 8 — Full-pipeline replay

**Goal:** replay reproduces the whole decision, not a sub-step. Closes A2.
**Why:** replay is only meaningful if it re-runs everything the original decision did, against the exact stored rules and inputs.

1. **`services/replay_engine.py`:** load a decision row; reconstruct the request **and** all frozen inputs (semantic flag, blast radius, autonomy snapshot, environment); load the policy artifact **by stored hash**; re-run the full pipeline. Replay is **read-only** — it must not mutate streaks, autonomy, or policies (Ground Rule 9).
2. Compare full-pipeline outcome to the stored outcome across **all** outcomes (`ALLOWED`, `DENIED`, `ESCALATED`, `PENDING_APPROVAL`, `SHADOW_LOGGED`) — no special-case hacks.
3. **Tests (`test_phase8_replay.py`):** EVAL-8 — replay of a stored decision reproduces; a deliberately changed policy hash reports divergence with a reason.

**DoD:** replay is honest — it either reproduces or explains divergence; never a false "consistent".

## 15. Phase 9 — Executor, single-use tokens & rollback

**Goal:** execution verifies and spends the token; rollback works. Closes A4, B5.
**Why:** the decision is only real if enforcement actually checks the token — and an action you cannot undo is an action you should be able to compensate.

1. **`services/executor.py`:** verify signature + TTL; check the `consumed_tokens` table; **consume** (insert nonce) before executing; execute the simulated action against a mock state store.
2. **Single-use (A4):** a second presentation of the same token is rejected as already-consumed.
3. **Rollback/compensation:** record before/after state; a compensating action (itself routed through the control layer) restores it.
4. **Tests (`test_phase9_executor.py`):** EVAL-4 (expired + replayed rejected); EVAL-5 (execute then compensate).

**DoD:** no token can be spent twice; every executed action has a demonstrable compensation path.

## 16. Phase 10 — Offline fallback & validation harness

**Goal:** the whole thing runs with no keys; the graded checks run in one command. Closes C4.
**Why:** the assignment requires a reviewer to run the core path with no paid or secret key — and a single-command harness makes the graded checks obvious.

1. **`OFFLINE_MODE` wiring:** SQLite ledger, stub parser (regex NL→ActionRequest), null/stub vector matcher.
2. **`src/cli.py`:** `run-all-scenarios` executes EVAL-1..10 and prints a report.
3. **`tests/test_evaluation_scenarios.py`:** consolidated EVAL-1..10 as the reviewer-facing entry point — including the required `SHADOW_LOGGED` (EVAL-9) and `PENDING_APPROVAL` (EVAL-10) cases.
4. **Tests (`test_phase10_offline.py`):** full pipeline with all API keys unset.

**DoD:** `pytest` and `python -m src.cli run-all-scenarios` both pass with no secrets and no network.

## 17. Phase 11 — Documentation & submission assembly

**Goal:** the reviewer can understand and run it. Closes the checklist.
**Why:** the assignment explicitly grades reviewability — the docs are part of the deliverable, not an afterthought.

1. **README.md:** problem, users, architecture, setup, run/demo path, assumptions, trade-offs, limitations, next steps.
2. **`AGENT_WORKFLOW.md`:** AI tools used, where they helped, where they failed (the Gemini episode is a fair, honest example), what was changed manually, how a teammate audits the work. Draft this from the start; assemble here.
3. **`docs/session_excerpt.md`:** one redacted excerpt that influenced a real decision (e.g. the request-schema regression or the policy-promotion split).
4. **`examples/`:** synthetic sample inputs + representative outputs.
5. **Dockerfile + docker-compose:** one-command local run.
6. **Final pass:** re-check the traceability matrix; write the limitations section honestly (semantic threshold calibration, single-node replay, simulated execution only, and **no symlink handling** — the guard is pure path algebra that treats paths as strings, so symlink escapes are out of scope).

**DoD:** a fresh clone runs offline, the demo path works, every matrix cell is filled.

---

# Part D — Risks & sequencing

## 18. Risk register / gotchas

- **Alembic dialect drift:** autogenerate against **Postgres**, not SQLite, or types (`JSONB`, timezone) come out wrong. Use local Postgres for dev; Neon for the demo.
- **`create_all` vs migrations:** once Alembic is in use, never mix in `create_all` — it won't `ALTER` existing tables and will desync the schema.
- **Replay determinism:** versioning the policy is necessary but not sufficient — store *every* input and re-run the *whole* pipeline.
- **Two promotions, don't merge:** agent autonomy (Phase 5) vs policy promotion (Phase 7) vs reviewer bump (Phase 6) are three distinct mechanisms.
- **Enum vocabulary:** one set of decision strings everywhere, or replay mismatches on strings.
- **Semantic layer:** advisory only; must fail open to deterministic-only when offline.
- **Secrets:** `.env` never committed; `.env.example` only.
- **Regex:** deny-patterns only; never the sandbox boundary; validate at load (ReDoS).
- **Client-trusted flags:** shadow mode and autonomy are assigned by the control layer, not the request.
- **The sandbox is policy-dependent, not location-dependent:** the guard reasons about **absolute paths only**, and `sandbox_root` comes from the evaluated **policy** (C.4) — never from config or the process working directory. A relative `target_resource` is anchored to the policy's absolute `sandbox_root` at the boundary (`to_absolute`). Otherwise the same request could be judged differently depending on where the app was launched — breaking determinism and replay.
- **Policy language is closed (data vs code):** a policy expresses data changes (roles, risks, rules, scope); a change to the grammar (e.g. POSIX paths → object-store keys) is a code change — surfaced loudly at load, never a silent misread.

## 19. Critical path

`Phase 0 → 1 → 2 → 3 → 4 → 5 → 6 → 7 → 8 → 9 → 10 → 11`

Hard dependencies: 2 before 3/7 (versioned policy); 4 before 6 (evidence in the package); 5 before 8 (autonomy state is a replay input); 9 is needed for EVAL-4/5 but can slip if 10's harness is built with a stubbed executor. Do **not** reorder 2 before 1, or 7 before 3.

## 20. Time budget & where to cut

The assignment budgets roughly **6–8 focused hours**. The table below sums to about **10.25 h**, so it is deliberately *above* the ceiling — build the required scenarios first, then apply the cut order below to land inside 8 h. The ten graded scenarios (EVAL-1..10) are the non-negotiables; everything else can be trimmed (see §3 for why EVAL-9 and EVAL-10 are protected).

| Phase | Rough time | Priority |
|---|---|---|
| 0 Foundation | 1.0 h | required |
| 1 Security & ledger | 0.75 h | required |
| 2 Policy engine | 1.25 h | required |
| 3 Policy shadow | 0.75 h | required (EVAL-6) |
| 4 Blast radius + semantic | 1.0 h | required; semantic may be offline-stub-only |
| 5 Autonomy | 0.5 h | required |
| 6 Escalation loop | 1.0 h | required (EVAL-3, EVAL-7) |
| 7 Policy promotion | 0.5 h | required (EVAL-6) |
| 8 Replay | 1.0 h | required (EVAL-8) |
| 9 Executor + rollback | 0.75 h | required (EVAL-4, EVAL-5) |
| 10 Offline + harness | 0.75 h | required |
| 11 Docs | 1.0 h | compress to essentials |

**If time runs short, in this order:** (1) run the semantic layer as the offline stub only — skip Qdrant; (2) trim docs to README + `AGENT_WORKFLOW.md` + the session excerpt; (3) reduce `examples/` to the minimum that demonstrates each EVAL. Do **not** cut Phases 1, 2, 5, 6, 8, or 9 — they carry the guarantees the assignment grades (including EVAL-9's "no agent starts live" and EVAL-10's "approval-required").

---

# Appendices

## Appendix A — The assignment (verbatim)

Reproduced exactly as given, so this document is self-contained. **The project builds Option 2.**

> this isn't a school-style test. we want to understand how you frame an open-ended Agentic AI problem, make trade-offs, build something reviewable, validate its behaviour, and use AI tools with clear human judgement.
>
> please choose one of the two options below.

### Option 1: source-grounded context gateway for agents, with context lifecycle management

design and build a small context gateway through which an AI agent retrieves task-specific information from multiple sources. use at least one structured source, such as tasks or records, and one unstructured source, such as documents, messages, or notes.

the gateway should also keep the context it serves healthy over a long-running or many-step session. context that is left alone tends to bloat, rot, and drift, and the gateway is where that should be caught.

show how the gateway can:

- ingest and normalize multiple source types;
- preserve the original source reference, timestamp, freshness, confidence, and access scope;
- resolve a task into the smallest useful context package instead of loading everything;
- combine related structured and unstructured evidence;
- detect stale, missing, duplicated, or conflicting facts;
- filter private or out-of-scope material before retrieval;
- return citations or evidence references with every material fact;
- explain why each context item was included;
- track and cap context growth: detect when the working context is carrying low-value material, and compress or evict it without losing task-critical facts;
- detect rot: identify context items that have gone stale, been superseded, or turned out to be wrong, and refresh or retire them instead of carrying them forward;
- detect drift: notice when the agent's focus, assumptions, or output direction has moved away from its instructions or goal across a long run, and re-anchor - a soft correction when that is enough, a lean rebuilt context pack when it is not;
- escalate: hand the situation to a human when rot or drift is material and cannot be safely resolved automatically.

required evaluation:

- run at least four task queries;
- include one query where a tempting but private or irrelevant item must be excluded;
- include one stale or conflicting fact and show the returned warning or resolution;
- run one longer multi-turn or multi-task session and show a measurable reduction in retained context (items or tokens) while task quality is preserved;
- include one case where stale, superseded, or contradicted information would cause a wrong action if it were kept, and show rot detection preventing it;
- include one case where drift is detected and corrected, and one where it must escalate to a human;
- measure retrieval relevance and source-attribution completeness;
- show that the same gateway contract can serve at least two simple downstream agent tasks.

### Option 2: guarded action and autonomy control layer, with autonomy levels and decision replay

design and build a small control layer that decides whether an AI agent may perform a requested action. use simulated actions such as updating a record, sending a draft notification, changing a workflow state, or applying a configuration change.

the layer should also govern how much autonomy the agent holds over time. no agent or action class should start live: it starts in shadow mode, and wider autonomy is earned from recorded evidence rather than granted by default.

show how the control layer can:

- classify actions by risk or blast radius;
- evaluate actor, role, resource, scope, environment, and action policy;
- issue a short-lived capability decision or token for an allowed action;
- distinguish read-only, low-risk, approval-required, and denied actions;
- place approval-required actions in a human review queue with evidence;
- prevent expired, duplicated, tampered, or out-of-scope execution;
- log the policy version, decision reason, evidence, approval, and outcome;
- support a simulated rollback or compensating action;
- assign an autonomy level such as shadow, assisted, or live per agent or action class, and allow promotion only against recorded evidence;
- run a new or changed policy in shadow mode first, recording what it would have decided without executing anything;
- estimate the blast radius of a high-risk action before it is allowed or queued;
- produce an escalation package for a denied or approval-required action: what was requested, the risk, the evidence, and the decision a human must make;
- replay a past decision from its logged policy version and evidence, and show whether it reproduces.

required evaluation:

- allow one valid low-risk action;
- deny one out-of-scope action;
- hold one high-impact action until explicit approval;
- reject one expired or replayed authorization;
- demonstrate one simulated rollback or compensation path;
- run one policy or threshold change in shadow mode first, show the recorded would-have-decided outcomes with nothing executed, then promote it only on evidence;
- produce one escalation package for a held or denied action, and show the reviewer's decision updating the policy version;
- replay one stored decision from its logged evidence and show that it reproduces, or explain the divergence.

**what we expect in your submission:**

- a GitHub repository;
- a README covering the problem, users, architecture, setup, run or demo path, assumptions, trade-offs, limitations, and next steps;
- a small working CLI, API, notebook, or lightweight application;
- synthetic sample inputs and representative outputs;
- persistent data or state where the selected option requires it;
- tests, evaluations, or a structured validation script covering the required scenarios;
- clear evidence, provenance, confidence, and human-approval handling where relevant;
- a mock or deterministic fallback so the reviewer can run the core path without a paid or secret API key;
- Dockerfile or docker-compose where practical, or an equally simple local run path;
- AGENT_WORKFLOW.md, or an equivalent note, describing the AI tools used, where they helped, where they failed, what was manually changed, and how a teammate should audit the work;
- one redacted prompt or agent-session excerpt that influenced a real schema, workflow, feature, test, or architecture decision.

**boundaries:**

- use only synthetic or openly licensed data. no employer, customer, personal, or confidential information;
- no required paid API; no production cloud deployment;
- no full user-authentication system required - a simple scope or role model is enough;
- no autonomous consequential action - use a simulated action and a human approval boundary;
- no polished frontend needed; a clear CLI or API is acceptable;
- choose depth over number of features.

expected effort: approximately 6 to 8 focused hours. a polished production system is not required. a smaller working solution with clear reasoning, tests and limitations is stronger than a broad but incomplete design. keep each check bounded - one honest scenario per check is worth more than five shallow ones.

keep it clear and practical. we care more about your problem-solving approach and how reviewable your thinking is.

## Appendix B — Findings register (definitions)

These are the finding IDs referenced throughout the plan (e.g. "Closes A1, A3"). Fuller diagnosis lives in `review.md`; this is the one-line definition of each, so the plan is self-explanatory.

**Correctness bugs**

- **A1** — Sandbox path check passes anything starting with "workspace" (prefix bug).
- **A2** — Replay reproduces only the policy sub-step, not the whole pipeline.
- **A3** — Ledger silently drops failed writes (audit hole).
- **A4** — Reused (still-valid) capability token isn't blocked (no single-use).
- **A5** — Response-key mismatch between the two gateway routes (fixed).
- **A6** — First capability token was a truncated hash with no secret/TTL (fixed → JWT).
- **A7** — `request_id` was 8 hex chars and `UNIQUE` → silent insert drops.
- **A8** — Two inconsistent entry points (gateway endpoints vs pipeline).

**Requirement gaps**

- **B1** — Policy-shadow mode missing (agent-shadow built instead).
- **B2** — Blast-radius estimation absent.
- **B3** — Escalation package / review queue / reviewer→policy-version bump missing.
- **B4** — Decision reason, evidence, approval not logged.
- **B5** — No executor verifying tokens; no simulated rollback.
- **B6** — Autonomy promotion not backed by recorded (persisted) evidence.
- **B7** — `environment` field never evaluated.
- **B8** — "Approval-required" not a clean distinct decision class.

**Design & robustness**

- **C1** — Policy not versioned/hashed (rules as code constants).
- **C2** — Semantic threshold uncalibrated; semantic hit force-overrides risk (should be advisory).
- **C3** — Blast radius / risk must be deterministic, not LLM-decided.
- **C4** — No deterministic offline fallback (hard requirement).
- **C5** — Ledger stores the raw capability token (hash preferred).
- **C6** — Few-shot example deletes `/tmp/cache` (outside the sandbox).
- **C7** — Secrets were default placeholders.
- **C8** — Regex guardrails belong in the policy artifact but must not own the sandbox boundary.

**Checklist**

- **D** — Submission deliverables not yet built (README, CLI, tests, samples, Dockerfile, `AGENT_WORKFLOW.md`, session excerpt).

**Process notes**

- **E1** — Model names inconsistent/wrong.
- **E2** — Sycophantic agreement on rate limits without verification.
- **E3** — Assistant claimed to read screenshots it may not have seen.

---

*This plan supersedes the earlier phased sketches. It folds in every item from `review.md`: the sandbox fix, fail-closed ledger, replay scope, single-use tokens, policy promotion, and the offline fallback, with a test attached to each. It is standalone: the assignment is in Appendix A and the finding definitions in Appendix B.*

---

# Appendix C — Data & interface contracts

**Purpose:** pin the concrete artifacts so the build is deterministic and two builders produce the *same* thing. Everything below is **normative** unless marked *"refine in Phase X"*. Where a value here conflicts with prose elsewhere in the document, this appendix wins.

## C.1 Enums (single source of truth)

These string values are the **only** vocabulary the system uses. The returned decision, the stored outcome, and the replayed outcome must all use these exact values — enum consistency is what lets replay compare outcomes by string (see finding A2 and Ground Rule 4).

```
RiskLevel:        READ_ONLY | LOW | MEDIUM | HIGH | CRITICAL
AutonomyLevel:    SHADOW | ASSISTED | LIVE
DecisionOutcome:  ALLOWED | DENIED | ESCALATED | PENDING_APPROVAL | SHADOW_LOGGED
EscalationStatus: PENDING | APPROVED | REJECTED
```

**When each outcome is used** (do not conflate — this is finding B8):

- `ALLOWED` — policy permits the action and the autonomy gate releases it.
- `DENIED` — policy refuses it (RBAC, scope, environment, or a regex deny).
- `ESCALATED` — the **policy** forced human review (critical risk, or an advisory semantic flag).
- `PENDING_APPROVAL` — the **autonomy** layer held an otherwise-allowed action (agent in ASSISTED mode).
- `SHADOW_LOGGED` — the **autonomy** layer recorded an allowed action without executing it (agent in SHADOW mode).

**Two shadow mechanisms — do not merge** (this is finding B1):

- *Autonomy shadow* → a `decision_records` row whose `outcome` is `SHADOW_LOGGED` (an agent's action, recorded but not executed).
- *Policy shadow* → a row in `shadow_records` (a candidate ruleset's `would_have_decided`).

## C.2 API contracts — Pydantic models (`src/models/request.py`)

Pure Pydantic only (no `table=True`). These are the API contracts; DB tables are separate (C.3).

**`ActionRequest`**

| field | type | notes |
|---|---|---|
| `request_id` | `str` | UUIDv4 (never 8 hex chars — A7) |
| `agent_id` | `str` | |
| `actor_role` | `str` | must be a key of `role_permissions` |
| `action_class` | `str` | must be a key of `risk_map` |
| `target_resource` | `str` | file/dir path; subject to sandbox check (A1) |
| `parameters` | `Dict[str, Any]` | default `{}` |
| `environment` | `str` | default `"production"` |
| `timestamp` | `datetime` | default `now(utc)` |

**`NaturalLanguageRequest`**

| field | type | notes |
|---|---|---|
| `agent_id` | `str` | |
| `actor_role` | `str` | |
| `raw_prompt` | `str` | parsed to an `ActionRequest` by `llm_parser` |

**`EvaluationResponse`** — the *single* response shape both entry points return (closes A5/A8)

| field | type |
|---|---|
| `status` | `str` |
| `request_id` | `str` |
| `action_request` | `ActionRequest` |
| `decision` | `DecisionOutcome` |
| `risk_level` | `RiskLevel` |
| `blast_radius` | `Dict[str, Any]` |
| `policy_hash` | `str` |
| `reason` | `str` |
| `capability_token` | `Optional[str]` |
| `autonomy_level` | `AutonomyLevel` |

**`EscalationPackage`**

| field | type |
|---|---|
| `escalation_id` | `str` |
| `decision_id` | `str` |
| `request` | `ActionRequest` |
| `risk_level` | `RiskLevel` |
| `blast_radius` | `Dict[str, Any]` |
| `semantic_warning` | `Optional[str]` |
| `recommended_decision` | `str` |
| `status` | `EscalationStatus` |

**`ApprovalDecision`** (reviewer payload)

| field | type |
|---|---|
| `decision_id` | `str` |
| `reviewer_id` | `str` |
| `approve` | `bool` |
| `note` | `Optional[str]` |

**Internal service result types** (returned by services; distinct from the API response above)

**`EvaluationResult`** — returned by `policy_engine.evaluate`

| field | type |
|---|---|
| `outcome` | `DecisionOutcome` |
| `risk_level` | `RiskLevel` |
| `blast_radius` | `Dict[str, Any]` |
| `reason` | `str` |
| `policy_hash` | `str` |
| `ttl_seconds` | `Optional[int]` | engine decides the TTL; `None` = no token issuable (CRITICAL). The **orchestrator** mints the token (clock + secret live outside the pure core) |
| `autonomy_level` | `AutonomyLevel` |
| `evidence` | `Dict[str, Any]` |

**`ReplayResult`**

| field | type |
|---|---|
| `decision_id` | `str` |
| `original_outcome` | `DecisionOutcome` |
| `replayed_outcome` | `DecisionOutcome` |
| `is_consistent` | `bool` |
| `policy_hash` | `str` |
| `discrepancy_reason` | `Optional[str]` |

**`ExecutionResult`**

| field | type |
|---|---|
| `execution_id` | `str` |
| `decision_id` | `str` |
| `status` | `str` (`EXECUTED` / `REJECTED`) |
| `before_state` | `Dict[str, Any]` |
| `after_state` | `Dict[str, Any]` |

**`RollbackResult`**

| field | type |
|---|---|
| `execution_id` | `str` |
| `status` | `str` (`COMPENSATED` / `FAILED`) |
| `restored_state` | `Dict[str, Any]` |

## C.3 Database tables — SQLModel (`src/models/*.py`)

Types shown are logical; use `JSON`/`JSONB` per dialect (Postgres `JSONB`).

**`policies`**

| column | type | notes |
|---|---|---|
| `id` | int PK | |
| `version` | str | e.g. `"1.0.0"` |
| `policy_hash` | str | unique; SHA-256 of canonical rules |
| `rules_json` | JSON | the full artifact (C.4) |
| `is_active` | bool | exactly one active |
| `is_shadow` | bool | the candidate under evaluation; at most one (partial unique index) |
| `activated_at` | datetime, null | set **only while active**; nulled on deactivation (see note) |
| `deactivated_at` | datetime, null | set **only while deactivated**; nulled on re-activation (see note) |
| `reviewer_id` | str, null | who promoted it to active (attribution) |
| `created_at` | datetime | |

> The `policies` table is **append-only**: every version is retained forever and `is_active` only moves the pointer. Never delete a row, or old decisions can no longer be replayed. **Exactly one active** must be enforced in the database — a partial unique index (`UNIQUE (is_active) WHERE is_active`) or a single-row pointer updated in one transaction — because three writers move it (seed, reviewer bump, policy promotion — the last two human-triggered).
>
> **Activation timestamps (decided):** `activated_at` and `deactivated_at` are **symmetric** — exactly one is set at a time, so the pair always reflects the *current* stint: `(t, null)` = active, `(null, t)` = deactivated, `(null, null)` = never activated. Deactivation nulls `activated_at`; activation — whether promoting a candidate or **re-activating a previously-retired policy (rollback)** — nulls `deactivated_at`. This keeps only the most-recent stint by design; a full activation/deactivation **history table is deferred** (out of scope for now).

**`decision_records`** (the ledger)

| column | type | notes |
|---|---|---|
| `decision_id` | str PK | |
| `request_id` | str | unique |
| `agent_id` | str | |
| `actor_role` | str | |
| `action_class` | str | |
| `target_resource` | str | |
| `request_json` | JSON | the **full** `ActionRequest` (parameters, environment, timestamp) — required so replay can reconstruct the request |
| `risk_level` | str | `RiskLevel` |
| `autonomy_level` | str | `AutonomyLevel` |
| `outcome` | str | `DecisionOutcome` |
| `policy_hash` | str | the exact policy used (enables replay) |
| `reason` | str | decision reason (B4) |
| `evidence_json` | JSON | frozen inputs: blast radius, semantic warning, **autonomy snapshot (mode + streak)**, environment |
| `capability_token_hash` | str, null | store a **hash**, not the raw token (C5) |
| `reviewer_id` | str, null | set when a human decides |
| `created_at` | datetime | |

**`shadow_records`** (candidate-policy evaluation, no execution)

| column | type | notes |
|---|---|---|
| `shadow_id` | str PK | |
| `decision_id` | str | the active decision this shadows |
| `candidate_policy_hash` | str | which candidate was evaluated |
| `would_have_decided` | str | `DecisionOutcome` |
| `created_at` | datetime | |

**`autonomy_state`**

| column | type | notes |
|---|---|---|
| `id` | int PK | |
| `agent_id` | str | unique with `action_class` |
| `action_class` | str | |
| `mode` | str | `AutonomyLevel`, default `SHADOW` |
| `streak` | int | consecutive clean runs |
| `reviewer_id` | str, null | set when a human grants ASSISTED→LIVE (attribution for REQ-9 / B6) |
| `promoted_at` | datetime, null | when the current mode was granted |
| `updated_at` | datetime | |

**`escalation_queue`**

| column | type | notes |
|---|---|---|
| `escalation_id` | str PK | |
| `decision_id` | str | |
| `package_json` | JSON | the `EscalationPackage` |
| `status` | str | `EscalationStatus`, default `PENDING` |
| `reviewer_id` | str, null | |
| `decided_at` | datetime, null | |
| `created_at` | datetime | |

**`consumed_tokens`** (single-use enforcement — A4)

| column | type | notes |
|---|---|---|
| `nonce` | str PK | the token's `jti` |
| `decision_id` | str | |
| `consumed_at` | datetime | |

---

### Database Constraints

**Foreign Keys** (all `ON DELETE RESTRICT`):
- `shadow_records.decision_id` → `decision_records.decision_id`
- `shadow_records.candidate_policy_hash` → `policies.policy_hash`
- `decision_records.policy_hash` → `policies.policy_hash`
- `escalation_queue.decision_id` → `decision_records.decision_id`

SQLite foreign key enforcement is enabled on every connection via `PRAGMA foreign_keys=ON` event listener.

**CHECK Constraints** (vocabulary enforcement):
- `policies`: activation invariant — `(is_active AND activated_at IS NOT NULL AND deactivated_at IS NULL) OR (NOT is_active AND ((activated_at IS NULL AND deactivated_at IS NULL) OR (activated_at IS NULL AND deactivated_at IS NOT NULL)))`
- `decision_records`: `risk_level`, `autonomy_level`, `outcome` enums
- `shadow_records`: `would_have_decided` enum — **all five**, mirroring `decision_records.outcome` (the candidate runs the same full pipeline, autonomy gate included)
- `autonomy_state`: `mode` enum
- `escalation_queue`: `status` enum

## C.4 Policy artifact schema (`policies/policy_v1.0.0.json`)

*Initial shape — finalised in Phase 2.*

```json
{
  "version": "1.0.0",
  "sandbox_root": "/abs/path/to/workspace",
  "risk_map": {
    "read_file": "READ_ONLY",
    "create_file": "LOW",
    "create_directory": "LOW",
    "update_file": "MEDIUM",
    "move_file": "MEDIUM",
    "delete_file": "HIGH",
    "delete_directory": "CRITICAL"
  },
  "role_permissions": {
    "read_only_agent": ["read_file"],
    "workspace_agent": ["read_file", "create_file", "create_directory", "update_file", "move_file"],
    "admin_agent": ["read_file", "create_file", "create_directory", "update_file", "move_file", "delete_file", "delete_directory"]
  },
  "environment_rules": {
    "destructive_allowed_environments": ["development"],
    "prod_escalates": ["delete_file", "delete_directory"]
  },
  "regex_deny_rules": [
    { "pattern": "(?i)rm\\s+-rf\\s+/", "reason": "destructive shell payload" },
    { "pattern": "(?i)/etc/(passwd|shadow)", "reason": "system credential file" }
  ],
  "ttl_map": { "READ_ONLY": 600, "LOW": 300, "MEDIUM": 120, "HIGH": 60, "CRITICAL": null }
}
```

Rules: `regex_deny_rules` are **deny-patterns only** — never the sandbox boundary (C8). Path containment is path algebra. Validate patterns at load (guard ReDoS). Rule precedence: **deny before allow, most-specific first**. A `ttl_map` value of `null` means **no token is issuable** — the action must be escalated, never minted a born-expired token. Promotion thresholds are **not** in the artifact; they live in `settings` (C.8), because anything in the artifact is hashed into `policy_hash` and a threshold tweak must not mint a new policy version (see A2). `sandbox_root` is the policy's declared **scope** — an absolute path (validated at load) and, being part of the artifact, hashed into `policy_hash`. The sandbox is therefore **policy-dependent**, not a function of where the app runs: replay uses the exact scope the policy recorded. (Trade-off: the artifact then carries a deployment-specific absolute path — accepted, because scope belongs to the policy.) The loader enforces this: it **rejects** (raises) a policy with a missing required key, a non-absolute `sandbox_root`, or an uncompilable regex pattern — an invalid policy is never stored or activated. Validation is closed: unknown keys are rejected rather than ignored, and every risk_map value and role_permissions action must be valid — so a policy can never smuggle in semantics the engine doesn't implement; the loader fails loudly instead of misreading it.

## C.5 Capability token payload (JWT, HS256)

Claims carried in the signed token:

| claim | meaning |
|---|---|
| `jti` | unique nonce; checked against `consumed_tokens` for single-use |
| `decision_id` | the decision that issued this token |
| `policy_hash` | the policy under which it was issued |
| `agent_id` | |
| `actor_role` | |
| `action_class` | the one action this token authorises |
| `target_resource` | the one resource this token authorises |
| `iat` | issued-at |
| `exp` | expiry (from `ttl_map`) |

Signed with `SECRET_KEY` (HS256). Verification checks signature, `exp`, and `jti` not already consumed. If the policy's TTL is `null` (CRITICAL), **no token is issued** — the orchestrator skips minting entirely rather than producing an already-expired token.

## C.6 Service interfaces (function signatures)

```python
# src/services/path_guard.py   (ABSOLUTE-ONLY; pure algebraic — no filesystem access, no symlink resolution)
is_within_sandbox(target: str, sandbox_root: str) -> bool      # both must be absolute; relative => False (fail closed)
to_absolute(base_dir: str, target: str) -> Optional[str]       # boundary helper: resolve a relative target against the policy's absolute sandbox_root

# src/services/policy_engine.py   (PURE — no I/O; returns ttl_seconds, never a token)
evaluate(request: ActionRequest, policy: dict, evidence: dict, autonomy_state: AutonomyState) -> EvaluationResult

# src/services/blast_radius.py
estimate(request: ActionRequest) -> dict            # deterministic metrics

# src/services/semantic_engine.py   (advisory only; never grants access)
evaluate(request: ActionRequest) -> tuple[bool, float, Optional[str]]

# src/services/token_issuer.py
issue(decision_id: str, policy_hash: str, request: ActionRequest, ttl_seconds: int) -> str
verify(token: str) -> Optional[dict]

# src/services/autonomy_service.py
get(agent_id: str, action_class: str) -> AutonomyState
record_outcome(agent_id: str, action_class: str, violated: bool) -> AutonomyState
maybe_auto_promote(agent_id: str, action_class: str) -> bool          # SHADOW→ASSISTED, automatic at threshold
promote_to_live(agent_id: str, action_class: str, reviewer_id: str, approved: bool) -> bool  # ASSISTED→LIVE; records approve OR decline

# src/services/escalation_service.py
raise_escalation(decision_id: str, package: EscalationPackage) -> str
decide(decision_id: str, reviewer_id: str, approve: bool, note: Optional[str]) -> str  # returns new policy version

# src/services/shadow_engine.py
evaluate(request: ActionRequest, active_policy: dict, candidate_policy: dict) -> tuple[EvaluationResult, dict]

# src/services/policy_promotion.py
check_and_promote(candidate_policy_hash: str) -> bool   # threshold met? qualifies only — never auto-promotes
promote_candidate(candidate_policy_hash: str, reviewer_id: str, approved: bool) -> bool  # human promotion; records approve OR decline

# src/services/replay_engine.py
replay(decision_id: str) -> ReplayResult

# src/services/executor.py
execute(decision_id: str, token: str) -> ExecutionResult
rollback(execution_id: str) -> RollbackResult
```

## C.7 API surface (FastAPI, `src/main.py`)

| method | path | input | output |
|---|---|---|---|
| POST | `/api/v1/actions/evaluate` | `ActionRequest` | `EvaluationResponse` |
| POST | `/api/v1/actions/evaluate-prompt` | `NaturalLanguageRequest` | `EvaluationResponse` |
| POST | `/api/v1/control/approve/{decision_id}` | `ApprovalDecision` | status + new policy version |
| POST | `/api/v1/control/promote-policy` | `{candidate_policy_hash, reviewer_id, approve}` | status + new active policy |
| POST | `/api/v1/control/promote-agent` | `{agent_id, action_class, reviewer_id, approve}` | status + new autonomy level |
| POST | `/api/v1/actions/execute` | `decision_id` + token | `ExecutionResult` |
| POST | `/api/v1/actions/rollback/{execution_id}` | — | `RollbackResult` |
| GET | `/api/v1/escalations` | — | list of `EscalationPackage` |
| GET | `/api/v1/replay/{decision_id}` | — | `ReplayResult` |
| GET | `/api/v1/health` | — | `{ "status": "ok" }` |

## C.8 Configuration settings (`src/config.py`)

| setting | default | purpose |
|---|---|---|
| `DATABASE_URL` | local Postgres; SQLite when `OFFLINE_MODE` | DB target |
| `OFFLINE_MODE` | `False` | keyless local run |
| `SECRET_KEY` | (required) | JWT signing |
| `QDRANT_URL` / `QDRANT_API_KEY` / `QDRANT_COLLECTION` | empty | semantic engine (optional) |
| `GEMINI_API_KEY` | empty | NL parser (optional) |
| `PROMOTION_THRESHOLD_ASSISTED` | `5` | clean shadow runs to promote SHADOW→ASSISTED |
| `PROMOTION_THRESHOLD_LIVE` | `20` | clean runs to *qualify* for ASSISTED→LIVE (still human-gated) |
| `PROMOTION_THRESHOLD_POLICY` | `20` | candidate→active shadow runs |
| `SEMANTIC_THRESHOLD` | `0.5` (placeholder) | advisory cutoff; recalibrated in Phase 4 |
| `TOKEN_TTL_*` | (removed — no longer settings) | token lifetime is **not** configurable. It is consumed by the decision (`ttl_seconds` → the issued token's `exp`), so the artifact's `ttl_map` is the single source, hashed into `policy_hash`. A config override would let config silently disagree with the recorded decision and break exact replay (C.4, C.8). This row is kept to record the decision, not to invite the override back. |

---

*End of document. Sections above C are the build plan; Appendix C is the pinned contract layer.*

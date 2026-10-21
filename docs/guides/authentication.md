# Authentication setup and API usage

Authentication is required for application endpoints except health and human
login. Agents use `X-API-Key`. Humans can use either a personal `X-API-Key` or
username/password login followed by `Authorization: Bearer <access_token>`.
Sending both authentication headers is rejected. Production connections must
use HTTPS; loopback HTTP is suitable for local testing.

## Deployment prerequisites

Install `requirements.txt`, including `argon2-cffi`. Add a separate random
`AUTH_JWT_SECRET` of at least 32 bytes to your private `.env`. It must differ
from `SECRET_KEY`, which signs execution capabilities. Never commit either.
Startup rejects whitespace and recognizable placeholder values; these checks
cannot establish randomness. Generate the secret with the command below.
`AUTH_ACCESS_TOKEN_TTL_SECONDS` defaults to 900. Optional access-token issuer
and audience settings are `AUTH_JWT_ISSUER` and `AUTH_JWT_AUDIENCE`.

Generate a secret locally when configuring `.env`:

```bash
python -c 'import secrets; print(secrets.token_urlsafe(48))'
```

Redis is required for human login throttling. Configure `REDIS_URL` and
`REDIS_NAMESPACE`; unavailable Redis returns 503 for login without disabling
valid API-key or JWT authentication. Username/address rate limits default to
5/30 attempts per minute and can be configured through
`AUTH_LOGIN_USERNAME_LIMIT` / `AUTH_LOGIN_ADDRESS_LIMIT`. Password verification
uses Argon2id, outside database transactions, with bounded concurrency through
`AUTH_PASSWORD_MAX_CONCURRENCY` (default 4).

The models add `auth_principals`, `auth_credentials`, `auth_events`, and nullable
`workflow_operations.caller_principal_id`. Generate, review, and apply an
Alembic migration before starting the updated API. Migration review must check
all authentication constraints, including the PostgreSQL permissions check;
autogeneration does not reliably detect every check-constraint change.
No credentials are automatically provisioned. Existing agents require explicit
`--adopt-existing` when binding a principal to their historical records. Legacy
workflow responses stay inaccessible; adoption does not transfer their caches.

## Provision callers locally

The local command requires PostgreSQL and uses the configured database. It does
not expose a remote administration API. Choose agent roles from the active
policy; `workspace_agent` is used in these examples.

```bash
python -m src.auth_cli create-agent --principal-id dev-agent --agent-id dev-agent --actor-role workspace_agent \
  --permission actions:evaluate --permission actions:execute \
  --permission actions:rollback --permission records:read_own
python -m src.auth_cli issue-key --principal-id dev-agent

python -m src.auth_cli create-human --principal-id dev-reviewer --username dev-reviewer \
  --permission evidence:read --permission escalations:prepare --permission escalations:review \
  --permission policies:promote --permission agents:promote
python -m src.auth_cli issue-key --principal-id dev-reviewer
```

Human creation prompts twice for a password without echo. API keys are shown
once on issuance; keep them privately. Keys default to 90 days of validity;
`issue-key --expires-days` customizes expiration. Permissions default to none.

```bash
python -m src.auth_cli revoke-key --credential-id THE_ISSUED_CREDENTIAL_ID
python -m src.auth_cli reset-password --principal-id dev-reviewer
python -m src.auth_cli revoke-sessions --principal-id dev-reviewer
python -m src.auth_cli disable --principal-id dev-agent
python -m src.auth_cli enable --principal-id dev-agent
python -m src.auth_cli set-permissions --principal-id dev-reviewer \
  --permission evidence:read --permission escalations:review
```

`set-permissions` replaces the complete permission list. Password reset and
session revocation invalidate existing access JWTs; API keys are independently
revocable. Disabling a principal denies both methods. Permissions always come
from current database records, including when returning cached results.

Start FastAPI locally without trusting forwarded addresses:

```bash
uvicorn src.main:app --host 127.0.0.1 --port 8000 --no-proxy-headers
```

When deploying behind a proxy, explicitly configure Uvicorn's trusted proxy
addresses. Login rate limits use the resulting client address.

## Login and caller identity

`POST /api/v1/auth/login` accepts only:

```json
{"username": "dev-reviewer", "password": "your privately configured password"}
```

It returns `access_token`, `token_type: "bearer"`, and `expires_in` with
`Cache-Control: no-store`. Login failures do not reveal whether the user exists.
Expired JWTs require login again; refresh tokens are not implemented.

`GET /api/v1/auth/me` returns the verified principal ID, kind, agent binding,
policy role, and permissions. Use that principal ID as `reviewer_id` when
submitting a review. Existing identity fields in request bodies remain required
for compatibility, and must match the verified caller. For agent requests use
its configured `agent_id` and `actor_role`.

## Permissions and response protection

See [the permission matrix](../plans/authentication_plan.md#permissions). Human kind
alone grants no access. Agents cannot approve escalations or promote anything;
humans cannot submit agent action requests. Object access is checked before
work and before protected changes commit, including execution and rollback.
All database transactions close before provider calls or password verification.

Missing/invalid credentials return 401. Conflicting headers return 400.
Insufficient route permissions or spoofed identity fields return 403. Missing
or inaccessible objects return 404. Current permission and revocation checks
also apply to idempotent retries. Each caller has a distinct workflow namespace.

Approval responses retain their review and authorization information but omit
execution capabilities, including the token inside `evaluation`. The owning
agent polls `GET /api/v1/actions/reviews/{original_decision_id}` with its own
API key and `records:read_own` permission. It uses the decision ID from its
original evaluation; it does not need information passed back by the reviewer.
The response contains `original_decision_id`, `status`, `reason`, optional
`authorization_decision_id`, and optional `evaluation`:

| Status | Meaning |
| --- | --- |
| `PREPARING` | The escalation package is being prepared. |
| `AWAITING_REVIEW` | The package is ready and no human decision has been accepted. |
| `REJECTED` | The human rejected the request; no authorization is returned. |
| `APPROVED_BLOCKED` | Human approval was recorded but final authorization was blocked; includes the evaluation and reason. |
| `APPROVED_ALLOWED` | Human approval and authorization succeeded; includes the original evaluation and capability. |

These reads return HTTP 200 for all five states. Poll pending states at a
reasonable interval; terminal results remain available. Both retrieval endpoints
send `Cache-Control: no-store`. Unknown decisions,
another agent's decisions, and decisions with no escalation return 404. Missing
or inconsistent saved authorization evidence returns 503 rather than pretending
review is pending. Expired or consumed capabilities are not renewed by polling.

`GET /api/v1/actions/authorizations/{authorization_decision_id}` remains available
when the agent already knows that ID. Neither endpoint mints a replacement
token or extends its expiry. Execution still needs
`actions:execute`, ownership, and the separate capability in the existing
execute request body. A human JWT cannot serve as an execution capability.

Other humans with `evidence:read` can inspect only safe workflow status, never
the checkpoint, cached response, error input, or capability. Human reviewers
use escalation and replay routes for evidence. Existing prompt and response
audit details remain recorded; authentication secrets are excluded.

Successful workflow stages and local credential changes record `auth_events`
atomically with their writes. Denials and read access use separate short audit
writes. An audit failure cannot authorize a denied request. Mutation audit
failure rolls back the mutation. Denial audit failure keeps access denied and
returns a generic 503 with a correlation ID; server logs contain only that safe
identifier. Successful-read audit failure logs the safe correlation ID and
preserves the authorized read response. Existing domain audits remain in place.

## Live test clients

The updated interactive `scripts/e2e/legacy/online_e2e_session.py` reads:

- `E2E_MAIN_AGENT`: provisioned agent ID.
- `E2E_AGENT_API_KEYS`: private JSON mapping of agent IDs to their API keys.
- `E2E_REVIEWER`: provisioned human principal ID.
- `E2E_REVIEWER_API_KEY`: that human's key.
- Optional `E2E_REVIEWER_API_KEYS`: JSON mapping for additional human IDs. The
  concurrent review helper requires separately provisioned `<reviewer>_approve`
  and `<reviewer>_reject` identities and keys.

Each additional scenario agent must be provisioned and added to the mapping;
changing a body identity cannot create an authenticated agent. The three-case
`scripts/e2e/phase12_online_e2e.py` instead uses one fresh provisioned agent through
`E2E_MAIN_AGENT` and `E2E_AGENT_API_KEY`, plus the two reviewer variables above.
Reports redact keys, passwords, access tokens, and capabilities.

`scripts/e2e/auth_live_smoke.py` checks authentication and permission boundaries
against the running API without executing resource actions. Set the variables
documented in that script and enter the human password when prompted. Run it
after migrations, provisioning, and restart. It verifies caller kinds and
permissions before checking boundaries, and optionally verifies expected
identities through `E2E_MAIN_AGENT` and `E2E_REVIEWER`. Its agent needs
`actions:evaluate` and `records:read_own`; its human needs `evidence:read`,
`escalations:review`, and `agents:promote`. Full revocation/concurrency tests
remain a separate live verification step; no SQLite is used.

For local E2E runs, after applying the auth migration, seed dedicated test
identities with:

```bash
PYTHONPATH=. python scripts/seeding/seed_e2e_auth.py
```

The idempotent seeder provisions one `workspace_agent` and one reviewer, then
saves their generated credentials in the ignored `.env` with owner-only file
permissions. It does not print keys or passwords. The live smoke and Phase 12
scripts load those E2E settings from `.env` automatically.

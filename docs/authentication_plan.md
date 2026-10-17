# Agent and reviewer authentication

Status: code implemented; migration generation/application, credential
provisioning, and live verification remain pending. No migration was generated
or applied as part of this implementation. See [setup instructions](authentication.md).

## Identity and credential choice

Agents authenticate with a unique random API key through `X-API-Key: <key>`.
Human reviewers can use either of these methods:

- Log in with a username and password, receive a short-lived JWT access token,
  then send `Authorization: Bearer <access_token>` on subsequent requests.
- Send their personal API key through `X-API-Key: <key>`.

Both methods resolve to the same server-side principal and permissions. Reject
requests supplying both authentication headers, including an invalid credential
alongside a valid one. Never silently fall back between supplied credentials.
Bearer authentication accepts human access JWTs only; API keys belong in
`X-API-Key`. Agents cannot obtain human login tokens.

A locally operated administrative command provisions humans, sets or resets
passwords, and issues, rotates, or revokes API keys. No public registration or
API-key issuance endpoint is planned. Human login is the public credential
exchange endpoint; it does not grant permissions based on submitted identity
labels. The same login and bearer-token flow will serve a future approval UI.

Store only a SHA-256 digest of a cryptographically random, high-entropy key.
Reveal the raw key once on issuance; never put it in monitoring events, workflow
payloads, prompts, or committed files. Store human passwords only as secure
password hashes, using a password-specific hashing algorithm such as Argon2id.
Passwords, access tokens, and both authentication headers must also stay out of
logs, request-body captures, reports, and LLM inputs.

Proposed new tables:

- `auth_principals`: immutable caller ID, agent or human kind, enabled state,
  and validated permissions. Agent identity maps to an existing `agent_id` and
  its allowed policy role. Authentication permissions and policy roles are
  distinct concepts. Human principals additionally have a unique login username,
  password hash, and authentication version for invalidating issued JWTs after
  password reset or explicit session revocation. Agents have no login password.
  Enforce a unique agent-to-`agent_id` mapping and unique normalized human login
  usernames. Principal kind and historical identity bindings cannot be changed
  through ordinary API requests.
- `auth_credentials`: credential ID, principal ID, key digest, creation,
  expiration, and revocation timestamps. Multiple credentials allow rotation.
- `auth_events`: correlation ID, timestamp, authenticated principal when known,
  route, outcome, and reason code. Record login success/failure and access
  denials without recording supplied passwords, keys, or tokens.

Use matching Pydantic write contracts and database constraints for principal
kind, credential ownership, identity uniqueness, and valid human/agent fields.
Do not permit assigning human review permissions to an agent principal.

## Permissions

Define permissions explicitly and deny access unless the required permission
is present. Principal kind alone grants no permissions.

| Permission | Principal kind | Scope |
| --- | --- | --- |
| `actions:evaluate` | Agent | Own requests and clarification submissions |
| `actions:execute` | Agent | Own allowed decisions, with execution capability |
| `actions:rollback` | Agent | Own executions, with existing compensation checks |
| `records:read_own` | Agent | Own clarification records and workflow responses |
| `evidence:read` | Human | Escalation evidence, recorded prompts, replay, and safe workflow status |
| `escalations:prepare` | Human | Prepare review explanations |
| `escalations:review` | Human | Approve or reject prepared escalations |
| `policies:promote` | Human | Policy promotion decisions |
| `agents:promote` | Human | Agent promotion decisions |

Review submission requires both `evidence:read` and `escalations:review`.
Administrative provisioning and permission changes use the local administrative
command; no remote administration permission or public provisioning API is
introduced in this phase.

## Human login contract

`POST /api/v1/auth/login` accepts JSON containing only `username` and `password`.
Normalize usernames consistently at provisioning and login; never normalize or
silently truncate passwords. Reject blank or oversized inputs before hashing.
Use Argon2id for password hashes and bound hashing resource usage. For an unknown
username, perform a dummy password-hash verification to reduce timing differences.

Successful login returns `access_token`, `token_type: "bearer"`, and
`expires_in`. Configure `AUTH_ACCESS_TOKEN_TTL_SECONDS`, default 900 seconds.
Return `Cache-Control: no-store` on login responses. Invalid passwords, unknown
users, disabled users, and agent identities return the same generic 401 response.
Validation errors must not echo passwords or supplied credential values.

Apply distributed login throttling using the configured Redis namespace, with
separate limits per normalized username and source address. Initial configurable
limits: five attempts per username per minute and thirty per address per minute;
return 429 with `Retry-After` when exceeded. Hash username values in limiter keys.
Trust forwarded client addresses only from explicitly configured reverse proxies.
If the limiter is unavailable, return a generic 503 for login; existing API-key
and JWT requests continue through their normal authentication checks.

Load the password hash in a short read transaction, close it, and then verify the
password. Before issuing a JWT, use a new short transaction to confirm the same
password hash, enabled state, and authentication version remain current. This
prevents a reset or disable during hashing from issuing a usable new token.

Access JWTs use a separate signing configuration and explicit issuer, audience,
token type, subject, issued-at, expiration, and authentication version claims.
Pin the accepted signing algorithm. Verify the current principal is enabled and
the authentication version still matches; permissions come from current
server-side records. Capability JWTs must never pass access-token validation,
and access JWTs must never pass capability-token validation. Expired access
tokens require login again initially; refresh tokens are outside this first
implementation. Protect login against brute-force attempts with bounded rate
limits and generic invalid-credentials responses.

Add nullable `caller_principal_id` to `workflow_operations` for existing rows.
Require it for new API operations. Historical operations with no authenticated
owner expose only safe status to humans with `evidence:read`; do not expose their
cached response through the operation-status endpoint. Existing decision and human
review records retain their current agent and reviewer identity fields.

## API rules

Health and the human login endpoint stay public. Other application endpoints
require authentication.
Missing or invalid credentials return 401; authenticated but unauthorized
requests return 403 (or consistent 404 for inaccessible object IDs).

| Route | Required access |
| --- | --- |
| Human login (proposed `POST /api/v1/auth/login`) | Username/password for an enabled human; returns an access JWT |
| `GET /api/v1/auth/me` | Any verified caller; returns its own identity and permissions |
| `GET /api/v1/actions/reviews/{original_decision_id}` | Owning agent with `records:read_own`; discovers pending/rejected/approved review outcomes and its authorization using the original decision ID |
| `GET /api/v1/actions/authorizations/{decision_id}` | Owning agent with `records:read_own`; retrieves its original post-review evaluation and capability |
| Evaluate structured action or prompt | Agent with `actions:evaluate`; use its configured agent ID and policy role |
| Answer clarification | Owning agent with `actions:evaluate` |
| Read clarification | Owning agent with `records:read_own` |
| Execute | Owning agent with `actions:execute` plus valid capability token |
| Rollback | Owning agent with `actions:rollback`; preserve compensation checks |
| List escalation packages | Human with `evidence:read` |
| Prepare escalation package | Human with `evidence:read` and `escalations:prepare` |
| Approve/reject escalation | Human with `evidence:read` and `escalations:review`; record verified principal ID |
| Promote policy | Human with `policies:promote` |
| Promote agent | Human with `agents:promote` |
| Replay decision | Human with `evidence:read` |
| Operation status or cached response | Original caller with current permission for the operation; other humans with `evidence:read` receive safe status only |

For compatibility, submitted `agent_id`, `actor_role`, and `reviewer_id` can
remain in the existing request contracts initially, but must agree with the
verified principal. Mismatches are rejected before workflow lookup or mutation.
Services receive identity values taken from the principal, never trusted from
the supplied fields. Reviewer credentials cannot be used to submit agent
requests, and agent credentials cannot acquire review or promotion permissions.

Check ownership before returning cached workflow results as well as before
starting new work. Include authenticated caller identity in workflow identity
and payload binding so two callers cannot share a cached result accidentally.
Object permissions must also protect historical decision, interpretation,
execution, and workflow records. A supplied object ID is not authorization.

Safe workflow status contains only operation ID, kind, status, stage, and
timestamps. It excludes cached response, checkpoint, errors containing input,
and capability tokens. Reviewers inspect evidence through authorized review or
replay routes; serialization must explicitly exclude bearer capabilities from
those responses. Full cached evaluation responses belong to the owning agent.
Reviewers may retrieve their own review or promotion results subject to current
permissions. Do not make legacy cached results accessible based only on an
old agent identity label; administrative adoption must bind ownership explicitly.

Use 404 consistently for missing or inaccessible object IDs. Use 403 for
authenticated callers lacking route permission or supplying a mismatched caller
identity. Authentication runs before workflows, provider calls, and sensitive
request-validation capture. OpenAPI must document both authentication methods
and the required permissions; documentation does not grant API access.

## Transaction boundaries

Credential verification and object access checks use short database reads that
finish before LLM, embedding, or other slow work starts. Do not introduce a
request-wide Session transaction through an authentication dependency.
Permission, ownership, caller-enabled, and credential-validity checks are
rechecked in the short transaction that commits a protected mutation, including
evaluation finalization, review, promotion, execution, and rollback. Check again
before returning cached results. A successful initial check is not authorization
for every later stage of a long-running operation.
For API keys, verify expiration/revocation; for JWTs, verify expiration and the
principal's current authentication version.

Use a consistent locking order: workflow row when applicable, then principal,
then API-key credential when applicable, then domain rows. Administrative
revocation and permission updates lock the same principal before credentials.
Keep these locks within short database-only transactions. If revocation commits
first, the mutation is rejected; if the authorized mutation obtains the shared
locks first, it can commit before revocation. This establishes an explicit order
without holding locks across provider calls. Expiration is checked at final
authorization; already committed execution is never undone by later revocation.

Record successful protected mutations in `auth_events` in the same transaction
as the mutation and existing domain audits. Audit failure rolls back that
mutation. Denied authentication/access attempts use independent short audit
writes. If such a denial audit cannot be persisted, keep access denied and emit
a credential-free server error with a correlation ID; never grant access as a
fallback. Keep existing domain audit detail and prompt/response recording intact,
while excluding authentication secrets from those records.

Capability JWT issuance and verification remain separate from caller
authentication. An authentication key cannot authorize execution by itself;
a capability token cannot authenticate a caller by itself.

## Implementation and verification

1. Confirm the proposed storage tables and implement the agreed authentication
   methods: human username/password login with bearer JWTs; agent and human
   API keys through `X-API-Key`.
2. Add SQLModel tables and matching validated write contracts; administrative
   credential management; login and rate limits; separate access-JWT settings;
   FastAPI authentication/permission dependencies.
3. Bind all exposed routes to verified identity, ownership, and permissions;
   update workflow ownership and authentication audit recording.
4. Update CLI, live E2E clients, README, and runbooks to use separate agent and
   reviewer credentials. Ensure no credentials enter captured reports.
5. Generate and review Alembic migration when authorized, then apply to live
   PostgreSQL. Provision explicit credentials before starting the protected API.
6. Verify on configured live infrastructure: human login success and failure;
   password reset invalidating JWTs; login rate limits; missing/invalid/expired/
   revoked credentials; conflicting authentication headers; human API-key and
   JWT access resolving to the same identity; rejection of capability JWTs as
   login tokens and vice versa; spoofed agent/role/reviewer; cross-agent reads and execution;
   agent approval/promotion attempts; authorized human review; idempotent replay
   ownership; capability verification; credential rotation; audit persistence;
   no transaction held during provider work; revocation racing with mutations;
   password reset during login; no capability leakage through reviewer reads;
   login limiter outages; audit-write failures. Do not use SQLite.

Existing historical IDs are audit labels and are not proof of identity. Any
mapping from a newly provisioned principal to historical agent records must be
explicitly configured by the administrator.

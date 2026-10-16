# Request clarification and grounding

An unclear natural-language request returns HTTP 202 with `status: needs_clarification`,
a durable `interpretation_id`, a `revision`, questions, and field-specific error JSON.
It creates no policy decision and grants no execution capability.

Missing user facts and incorrect model output are different. A missing resource type,
missing content, missing move destination, unclear operation, or conflicting negation
can require clarification. A model that changes a supplied destination or truncates
supplied content gets validation feedback and can correct its response within the
configured parser call limit. Provider failures remain failures, not successful
clarification. Unsupported or compound requests retain their existing rejection behavior.

## Database setup

The new SQLModel is `src/models/clarification.py`, registered in `alembic/env.py`.
No migration file was generated and no cloud schema was changed. Generate the migration,
review its `request_interpretations` table, and apply it before starting this version:

```sh
alembic revision --autogenerate -m "Add request interpretation and clarification history"
alembic upgrade head
```

Restart FastAPI after migrating.

## Example conversation

Submit the instruction:

```sh
curl -sS http://localhost:8000/api/v1/actions/evaluate-prompt \
  -H 'Content-Type: application/json' \
  -d '{"agent_id":"demo-agent","actor_role":"workspace_agent","raw_prompt":"Create /srv/agent/workspace/hotfix using emergency privileges."}'
```

The response asks whether the target is a file or directory. Copy its
`interpretation_id` and `revision`. Submit a **complete revised instruction**, including
all intended operations, restrictions, content, paths, and environment. This endpoint
accepts a restatement, not a one-word answer or a patch to executable arguments.
For example, the caller can explicitly revise the request to creating an empty file:

```sh
curl -sS http://localhost:8000/api/v1/actions/clarifications/INTERPRETATION_ID \
  -H 'Content-Type: application/json' \
  -d '{"agent_id":"demo-agent","actor_role":"workspace_agent","revision":1,"clarified_prompt":"Create an empty file /srv/agent/workspace/hotfix using emergency privileges."}'
```

The full revised instruction becomes the current request. Older wording remains in
the audit history; it is not silently merged into the current instruction. The revised
instruction is parsed and evaluated from scratch, including its claimed privileges and
risks. If it is still unclear, the response contains another question and an incremented
revision. Caller clarification is not human policy approval. A valid interpretation can
still be denied, escalated, or shadow logged by the existing policy/autonomy flow.

Successful evaluation responses include `interpretation_id` alongside the existing
`decision_id` and `request_id`. Read the conversation with:

```sh
curl -sS 'http://localhost:8000/api/v1/actions/clarifications/INTERPRETATION_ID?agent_id=demo-agent&actor_role=workspace_agent'
```

The answer must repeat the original caller identifiers. This is consistency checking
within the existing caller-supplied identity API; it does not introduce authenticated
user identity. PostgreSQL row locking and revision checks prevent stale/repeated answers
from being evaluated twice. An evaluated or rejected conversation is closed; submit a
new request for further changes. Provider-failed conversations may be retried through
the same endpoint with the current revision.

## What is recorded

| Table | Information |
| --- | --- |
| `request_interpretations` | Original prompt, caller identifiers, revision, current status/questions, every submitted instruction, errors, available analysis, correlation IDs, and resulting decision link |
| `request_attempt_events` | Per-call model messages/responses, error JSON, correction feedback, parser attempt outcomes, interpretation events, rejected stale/incorrect-caller answers, and other monitored stages |
| `decision_records` | Policy decision and saved evidence once a grounded action reaches evaluation |
| `escalation_queue` | Human-review package and reviewer response when policy requires escalation |

Interpretation changes and resulting decisions commit before a response is returned.
Database commit failures roll back those changes. Operational events use the existing
independent best-effort persistence path and structured log fallback, so a failed main
transaction can still leave diagnostic evidence. Failed commits do not produce a
successful clarification or authorization response. Monitoring is not a guarantee of
persistence when the database and log sink are both unavailable.

## Grounding checks and scope

The validator checks affirmative operation wording, target evidence in the operation,
resource type, source/destination direction, exact supplied content, supported parameters,
explicit recursion, and environment. Content and quoted sentences are excluded from
operation evidence; quoted path tokens remain usable. Composed paths need an explicit
name/parent relationship. Extensionless files remain supported when explicitly called
files; suffixes such as `.txt.bak` remain part of the filename.

These are conservative checks over recognized instruction forms, not a complete natural
language proof. Unsupported phrasing can require a clearer restatement. Filesystem state,
sandbox restrictions, permissions, and approvals remain downstream checks. Similarity and
LLM judgments never replace those checks.

Local tests use temporary SQLite databases and fake parser responses. No provider calls,
Qdrant changes, Redis changes, or cloud migration are needed to run them.

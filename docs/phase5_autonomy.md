# Phase 5: persisted autonomy

Autonomy belongs to an `(agent_id, action_class)` pair. `autonomy_service.get`
creates SHADOW state with streak zero for a new pair. PostgreSQL locks existing
state rows while an evaluation or promotion transaction is in progress. The
unique pair constraint prevents duplicate state on simultaneous first use;
constraint failures propagate and the caller must roll back/retry. SQLite is
appropriate for the single-writer offline demonstration.

`shadow_engine.evaluate_and_record` loads persisted autonomy when no explicit
trusted snapshot is supplied. It freezes mode and streak before evaluation,
then writes the active decision and candidate record. Only after both writes
succeed does it refresh the streak and consider automatic promotion. The
caller commits the records, state change, and promotion event together.

The optional explicit snapshot argument remains available for controlled
service tests/comparisons; that path does not update persisted autonomy. An API
must always use the default database-managed path and never obtain control
state from request fields. Replay calls the pure policy evaluator with its
stored snapshot and does not call this mutating service.

## Recorded evidence

The streak is derived from consecutive ledger decisions, not incremented from
a caller's assertion. Repeating `record_outcome` cannot count a run twice, and
promotion recalculates evidence rather than trusting the cached streak field.

- `SHADOW_LOGGED`, `PENDING_APPROVAL`, and `ALLOWED` count as clean policy evaluations.
- `DENIED` and `ESCALATED` reset the streak. This conservative definition stops
  unresolved policy-forced review from earning greater freedom.
- Clean proposals are not proof of execution or approval; human approval is
  independently required before LIVE promotion.
- If decision timestamps tie, a violation in that group takes precedence.
- Violations reset evidence without automatically demoting the current mode;
  automatic demotion is outside the current plan.

## Promotion ladder

SHADOW -> ASSISTED is automatic once `PROMOTION_THRESHOLD_ASSISTED` consecutive
clean shadow decisions are recorded (default 5). The decision reaching the
threshold still uses SHADOW; the next decision uses ASSISTED.

ASSISTED -> LIVE requires `PROMOTION_THRESHOLD_LIVE` consecutive clean decisions
(default 20, including the qualifying shadow runs) and an explicit reviewer
approval. ASSISTED alone still produces `PENDING_APPROVAL` for otherwise allowed
requests. A LIVE grant stores reviewer ID and promotion time on the state row.

`autonomy_promotion_events` records automatic grants and human approve/decline
attempts, including unsuccessful approvals below threshold. Events preserve
previous/requested mode, reviewer, result, reason, threshold, streak, and the
decision IDs used as evidence. Declines do not replace the current grant's
reviewer attribution.

## Migration and use

Migration `c85a51d74e02` adds the audit table on top of `8696a72fef7b` and has been
checked with SQLite upgrade/downgrade. Apply it to the development database
before using the service:

```sh
alembic upgrade head
```

Service functions accept the caller's SQLModel session. They flush and never
commit. For example, after recording enough qualifying decisions:

```python
with Session(engine) as session:
    promoted = autonomy_service.promote_to_live(
        session, "agent-1", "read_file", "reviewer-1", approved=True
    )
    session.commit()
```

The project currently trusts an internal reviewer ID; endpoint authorization is
a later integration concern. No promotion or migration has been applied to
the connected cloud database by this implementation.

## Requirement coverage

REQ-9 / B6 and EVAL-9 are covered by `tests/test_phase5_autonomy.py`: first action
is shadow, agent/action isolation, session restart, threshold promotion, held
assisted requests, human LIVE promotion, decline attribution, reset on denial
or escalation, protection against repeated counting/faked cached streaks,
transaction rollback, and the additive audit migration.

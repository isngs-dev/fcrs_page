"""Round-robin lead assignment (SR-20 D1/D3/D4) -- the sprint's highest-risk
module, kept OUT of ``leads/repository.py`` so it stays independently
testable and ``create_lead`` stays a single-purpose insert (M2 unchanged).

Two entry points:

- ``select_next_agent(db, claims) -> str | None`` -- pure selection logic.
  Returns ``None`` when the tenant has round-robin disabled (D1's default)
  or the active-agent pool is empty (D3/D4: the ordinary, non-error case).
  Otherwise atomically advances the tenant's persisted rotation cursor and
  returns the chosen agent id.

- ``assign_lead_fail_open(db, claims, *, lead_id) -> None`` -- the ONLY
  entry point the three lead-creation call sites use (D3). Calls
  ``select_next_agent`` and, if it returns an agent id, persists it onto
  the already-created lead row. ANY exception anywhere in this path --
  the config read, the candidate query, the cursor UPDATE, or the final
  lead UPDATE -- is caught, logged at WARNING with
  ``event``/``tenant_id``/``lead_id``/``reason`` (never PII), and NEVER
  propagates. The lead row must already exist before this is called; this
  function never creates or rolls back a lead (D3's ordering: write first,
  assign second, as a separate step).

Concurrency (D4): the rotation cursor is advanced by a SINGLE atomic
``UPDATE tenant_assignment_configs ... RETURNING`` statement wrapped around
an ordered, wrapping selection over the current active-agent pool -- never
a read-then-write across two statements/round-trips. Two simultaneous
callers therefore each get their own atomically-committed row version;
Postgres serializes the two UPDATEs against the same tenant_id row (row-
level lock), so the second writer always computes "next after" the first
writer's already-committed cursor value, not a stale value both read
before either wrote. This is the same shape as
``api.scheduling.reminder_repository.claim_due_reminders``'s
``UPDATE ... RETURNING`` exactly-once claim (this codebase's precedent).

Candidate pool (D4/M9): ``users WHERE tenant_id = $1 AND active = true AND
role IN ('CLIENT_ADMIN', 'CLIENT_AGENT')``, ordered by ``id`` for a
deterministic, stable rotation order.
"""
from __future__ import annotations

from common.auth import AuthClaims
from common.db import Database
from common.errors import ValidationError
from common.logging import get_logger, log_context

from api.notifications.emit import emit_event_safe

_log = get_logger(__name__)


def _reject_global(claims: AuthClaims) -> None:
    """Raise ``ValidationError`` for global callers (PLATFORM_ADMIN).

    Assignment is always tenant-scoped; a global caller has no tenant_id
    and therefore cannot be filtered to a tenant's agent pool.
    """
    if claims.tenant_id is None:
        raise ValidationError(
            "Lead assignment is tenant-scoped; PLATFORM_ADMIN callers are "
            "not permitted.",
            code="GLOBAL_CALLER_NOT_PERMITTED",
        )


async def select_next_agent(db: Database, claims: AuthClaims) -> str | None:
    """Return the next agent to assign a new lead to, or ``None``.

    ``None`` is returned (never an exception) when:
    - the tenant has ``round_robin_enabled = False`` (D1's default -- the
      exact today's-behavior case, M2), or
    - the tenant's active-agent pool
      (``role IN ('CLIENT_ADMIN','CLIENT_AGENT') AND active = true``) is
      empty (D3/D4 -- the ordinary case of a small/new tenant).

    Raises ``ValidationError`` (``GLOBAL_CALLER_NOT_PERMITTED``) for a
    global caller, and propagates any other exception (a query failure,
    etc.) -- callers on the lead-creation path MUST use
    ``assign_lead_fail_open`` rather than call this directly, so that a
    failure here never costs a lead (D3).
    """
    _reject_global(claims)

    config_row = await db.fetchrow(
        "SELECT round_robin_enabled, last_assigned_agent_id "
        "FROM tenant_assignment_configs WHERE tenant_id = $1",
        claims.tenant_id,
    )
    if config_row is None or not config_row["round_robin_enabled"]:
        return None

    candidates = await db.fetch(
        "SELECT id FROM users "
        "WHERE tenant_id = $1 AND active = true "
        "AND role IN ('CLIENT_ADMIN', 'CLIENT_AGENT') "
        "ORDER BY id",
        claims.tenant_id,
    )
    if not candidates:
        return None

    # Atomic advance (D4): a single UPDATE ... RETURNING computes "the
    # active-pool member immediately after the current cursor, wrapping to
    # the first when the cursor is NULL, missing (deactivated agent), or
    # the last in order" and persists it as the new cursor in the same
    # statement. Postgres's row-level lock on the tenant_assignment_configs
    # row being UPDATEd serializes concurrent callers for the same tenant --
    # the second writer's subquery runs against the first writer's
    # already-committed cursor, never a value both read before either wrote.
    row = await db.fetchrow(
        "WITH pool AS ( "
        "    SELECT id, ROW_NUMBER() OVER (ORDER BY id) AS rn "
        "    FROM users "
        "    WHERE tenant_id = $1 AND active = true "
        "    AND role IN ('CLIENT_ADMIN', 'CLIENT_AGENT') "
        "), "
        "current_pos AS ( "
        "    SELECT COALESCE( "
        "        (SELECT rn FROM pool WHERE id = "
        "            (SELECT last_assigned_agent_id FROM tenant_assignment_configs "
        "             WHERE tenant_id = $1)"
        "        ), 0 "
        "    ) AS rn "
        "), "
        "next_agent AS ( "
        "    SELECT id FROM pool "
        "    WHERE rn = (SELECT (current_pos.rn % (SELECT COUNT(*) FROM pool)) + 1 "
        "                FROM current_pos) "
        ") "
        "UPDATE tenant_assignment_configs "
        "SET last_assigned_agent_id = (SELECT id FROM next_agent), updated_at = now() "
        "WHERE tenant_id = $1 "
        "RETURNING last_assigned_agent_id",
        claims.tenant_id,
    )
    if row is None:
        # No tenant_assignment_configs row exists to UPDATE (shouldn't
        # happen -- the config read above required round_robin_enabled=True,
        # which implies a row exists -- but fail closed to None rather than
        # KeyError if it does).
        return None
    next_agent_id = row["last_assigned_agent_id"]
    return str(next_agent_id) if next_agent_id is not None else None


async def assign_lead_fail_open(
    db: Database, claims: AuthClaims, *, lead_id: str
) -> None:
    """Attempt to assign ``lead_id`` to the next round-robin agent; NEVER
    raises (D3 -- the single most important behavior in this sprint).

    Callers MUST have already durably written the lead row before calling
    this (D3's ordering: create first, assign second, as a separate step
    -- never inside the same transaction, never as a precondition). Any
    failure here -- disabled toggle (no-op, not a failure), empty pool
    (no-op, not a failure -- see ``select_next_agent``), or a genuine
    error (query failure, missing config row, anything) -- results in the
    lead staying exactly as created (``assigned_agent_id = NULL``, M2's
    honest default) with, for genuine errors only, a warning-level
    structured log (``event``, ``tenant_id``, ``lead_id``, ``reason`` --
    never a lead name/email/phone). No exception ever propagates to the
    caller.
    """
    try:
        agent_id = await select_next_agent(db, claims)
        if agent_id is None:
            return
        await db.execute(
            "UPDATE leads SET assigned_agent_id = $1, updated_at = now() "
            "WHERE tenant_id = $2 AND lead_id = $3",
            agent_id,
            claims.tenant_id,
            lead_id,
        )
        _log.info(
            "lead auto-assigned",
            extra={
                "event": "lead_auto_assigned",
                "lead_id": lead_id,
                "assigned_agent_id": agent_id,
            },
        )

        # -- Feed emit (SR-21 D3): fail-open (emit_event_safe never raises),
        # AFTER the durable assignment UPDATE above. payload is ids only --
        # never PII. Placed here (the single source of truth for a
        # successful auto-assignment) rather than at each of the three
        # create_lead call sites, so it cannot be missed regardless of how
        # many callers eventually invoke assign_lead_fail_open.
        await emit_event_safe(
            db,
            claims,
            kind="lead_auto_assigned",
            category="leads",
            target_type="lead",
            target_id=lead_id,
            payload={"lead_id": lead_id, "assigned_agent_id": agent_id},
            actor_id=None,
        )
    except Exception as exc:  # noqa: BLE001 -- fail-open by design (D3)
        # log_context (not just extra=) so tenant_id -- a ContextVar-sourced
        # field, per common.logging -- is present even when this runs outside
        # an active request's own bound context (e.g. a background/worker
        # call site added later). event/lead_id/reason ride extra=, both are
        # in _ALLOWED_EXTRA. Never a lead name/email/phone -- this function
        # only ever receives lead_id, never PII.
        with log_context(tenant_id=claims.tenant_id):
            _log.warning(
                "lead auto-assignment failed; lead remains unassigned",
                extra={
                    "event": "lead_auto_assignment_failed",
                    "lead_id": lead_id,
                    "reason": str(exc),
                },
            )

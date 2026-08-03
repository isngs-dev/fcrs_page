"""Identity resolution for the SR-9.3 customer-360 timeline.

Resolves a Contact or a Lead to an ``IdentitySet`` -- the list of
``visitor_id``s, ``email``s, and ``lead_id``s the timeline's fan-out queries
each source module by (D2). This is the ONLY place identity resolution
happens; ``service.py`` fans out to the four source modules using the result
without ever re-deriving or widening it.

D3 (LOCKED): NEVER widen an identity set by inference. Only values
physically present in ``contact_identities`` rows (contact route) or on the
lead row itself (lead route) enter the set -- no fuzzy/domain/name matching,
no transitive hops through a matched lead's *other* identities.

Per D6, a failure in this module is ALWAYS a hard error (the caller lets the
exception propagate to a 500) -- never a degraded 200. If we don't know
*whose* timeline this is, we must not return a partial list under their
name. This module raises no ``AppException`` for a missing/cross-tenant
subject; it returns ``None`` and the route layer maps that to 404 (mirrors
the ``leads``/``contacts`` precedent of a 404, not 403, for cross-tenant --
no existence leak).
"""
from __future__ import annotations

from dataclasses import dataclass

from common.auth import AuthClaims
from common.db import Database

from api.contacts.repository import get_contact, list_identities
from api.leads.repository import get_lead, list_lead_ids_converted_to_contact


@dataclass(frozen=True)
class IdentitySet:
    """The identity values a timeline fan-out queries each source by.

    Frozen/immutable so nothing downstream can accidentally widen it after
    resolution (D3).
    """

    visitor_ids: tuple[str, ...]
    emails: tuple[str, ...]
    lead_ids: tuple[str, ...]


async def resolve_contact_identities(
    db: Database,
    claims: AuthClaims,
    contact_id: str,
) -> IdentitySet | None:
    """Resolve a Contact's identity set (D2's contact-route rule).

    Collects, from ``contact_identities`` rows only (D3 -- no inference):
    - ``identity_type='visitor_id'`` values -> ``visitor_ids``.
    - ``identity_type='email'`` values -> ``emails``.

    Plus ``lead_ids``, from exactly two physically-present sources
    (belt-and-braces, per D2 -- SR-9.2 D6's partial unique index makes this
    at most one in practice, but this does not assume that):
    - the contact's own ``contacts.lead_id`` (if set).
    - any lead whose ``converted_to_contact_id`` equals this ``contact_id``.

    Returns ``None`` if the contact is missing or belongs to another tenant
    (caller maps this to 404).
    """
    contact = await get_contact(db, claims, contact_id)
    if contact is None:
        return None

    identities = await list_identities(db, claims, contact_id)
    visitor_ids = {i.identity_value for i in identities if i.identity_type == "visitor_id"}
    emails = {i.identity_value for i in identities if i.identity_type == "email"}

    lead_ids: set[str] = set()
    if contact.lead_id is not None:
        lead_ids.add(contact.lead_id)
    converted_lead_ids = await list_lead_ids_converted_to_contact(db, claims, contact_id)
    lead_ids.update(converted_lead_ids)

    return IdentitySet(
        visitor_ids=tuple(sorted(visitor_ids)),
        emails=tuple(sorted(emails)),
        lead_ids=tuple(sorted(lead_ids)),
    )


async def resolve_lead_identities(
    db: Database,
    claims: AuthClaims,
    lead_id: str,
) -> IdentitySet | None:
    """Resolve a Lead's identity set (D2's lead-route rule).

    The identity set is exactly the lead's own ``lead_id`` plus its
    ``visitor_id``/``email`` where non-NULL (D3 -- no inference, no reach
    into ``contact_identities`` even if the lead is already converted; a
    converted lead's route returns ITS OWN view, not the contact's fuller
    one -- see ``service.py``'s subject-building for
    ``converted_to_contact_id`` exposure).

    Returns ``None`` if the lead is missing or belongs to another tenant
    (caller maps this to 404).
    """
    lead = await get_lead(db, claims, lead_id)
    if lead is None:
        return None

    visitor_ids = (lead.visitor_id,) if lead.visitor_id else ()
    emails = (lead.email,) if lead.email else ()

    return IdentitySet(
        visitor_ids=visitor_ids,
        emails=emails,
        lead_ids=(lead_id,),
    )

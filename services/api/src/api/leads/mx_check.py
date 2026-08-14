"""Live + cached MX-record check for email qualification (``api.leads.email_classification``).

The one network-I/O layer of the classification pipeline, kept in its own
module (mirrors ``api.leads.pipeline`` staying pure by pushing all DB I/O
into ``api.leads.repository`` -- same separation, applied to DNS I/O here).

Uses ``dnspython`` directly rather than ``email_validator``'s bundled
deliverability check so ``NXDOMAIN``/``NoAnswer`` (the domain or its mail
routing genuinely does not exist -- "absent") can be told apart from
``Timeout``/``NoNameservers``/any other resolver failure (a transient
infrastructure hiccup -- "error", never treated as "absent").

No RFC 5321 §5.1 implicit-MX-via-A-record fallback: an earlier version of
this module treated "no MX record, but SOME A record exists" as "ok", since
that is technically mail-deliverable per the RFC. In practice this let
parked/squatted typo domains (e.g. ``gmel.com`` -- a typo of ``gmail.com``
with an A record for a parking page but no mail server at all) slip through
as a qualified lead. For LEAD qualification (not strict mail-deliverability
compliance), "no MX record" is itself treated as a strong enough signal that
this domain almost certainly cannot actually receive mail -- a genuine
small-business domain that skips MX entirely is rare in practice (virtually
all real email hosting sets MX records), so this trade favors catching junk
leads over the rare false disqualification, which an agent can always
manually re-qualify.
"""
from __future__ import annotations

import dns.resolver
from common.auth import AuthClaims
from common.cache import Cache, cache_key

from api.leads.email_classification import MxResult

# MX/A records are stable; a day-long TTL keeps repeat domains (gmail.com,
# yahoo.com, a client's own company domain reused across many leads) at
# near-zero marginal DNS cost after the first lookup for a given tenant.
MX_CACHE_TTL_SECONDS = 86_400

# A "error" (ambiguous) outcome is NEVER cached -- caching a transient
# resolver hiccup for a day would silently push every subsequent lead with
# that domain to needs_review for the full TTL over what may have been a
# one-off blip. Always re-checked live.
_CACHEABLE_RESULTS = {"ok", "absent"}


def _check_mx_live(domain_ascii: str, *, timeout_seconds: float) -> MxResult:
    """Synchronous, blocking DNS check. Never raises -- every failure mode
    maps to a ``MxResult``."""
    try:
        dns.resolver.resolve(domain_ascii, "MX", lifetime=timeout_seconds)
        return "ok"
    except (dns.resolver.NXDOMAIN, dns.resolver.NoAnswer):
        # Domain doesn't exist, or exists but has no MX record -- either way,
        # treated as "absent" (see module docstring: no implicit-MX-via-A
        # fallback for lead qualification purposes).
        return "absent"
    except Exception:  # noqa: BLE001 -- Timeout, NoNameservers, or anything else: ambiguous
        return "error"


async def check_mx_cached(
    cache: Cache,
    claims: AuthClaims,
    domain_ascii: str,
    *,
    timeout_seconds: float,
) -> MxResult:
    """Cache-aside MX check, tenant-scoped per CLAUDE.md §3's cache-key rule.

    A per-tenant cache (rather than a single global one) means the DNS win
    only compounds within a tenant's own lead volume, not across tenants --
    a deliberate trade against this codebase's "cache keys always include
    tenant_id" invariant, which is treated as strict here rather than
    special-cased for this one arguably-tenant-agnostic fact.
    """
    key = cache_key(claims, "email_mx", domain_ascii)
    cached = await cache.get(key)
    if cached in _CACHEABLE_RESULTS:
        return cached  # type: ignore[return-value]

    result = _check_mx_live(domain_ascii, timeout_seconds=timeout_seconds)
    if result in _CACHEABLE_RESULTS:
        await cache.set(key, result, MX_CACHE_TTL_SECONDS)
    return result

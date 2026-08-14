"""Email qualification -- classify a lead's email as qualified/disqualified/needs_review.

No database or task-queue I/O here (mirrors ``api.leads.pipeline``'s "pure
function, unit-testable without infrastructure" philosophy). The one
unavoidable I/O layer -- checking whether a domain can receive mail at all --
is injected as ``mx_checker`` rather than performed here directly, so this
module stays a plain function of its inputs. The real, cached, network-backed
checker lives in ``api.leads.mx_check``; production callers (``api.leads.tasks
.classify_lead_email``) wire it in, tests inject a fake.

Layered checks, cheapest/most-certain first, short-circuiting on the first
definitive verdict (empty/malformed/whitespace/uppercase are all handled by
normalization + the syntax layer; Unicode/IDNA domains are handled by
``email_validator``'s own IDNA encoding, never rejected just for being
non-ASCII):

1. Normalize + presence check.
2. RFC-subset syntax check (via the ``email-validator`` package).
3. Disposable/temp-mail domain block list.
4. Common-typo-of-a-major-provider domain list (a curated direct lookup, NOT
   fuzzy distance-matching against arbitrary domains -- fuzzy matching risks
   "correcting" a real small-business domain into a wrong guess; a short,
   curated table of known typos of a SHORT list of major consumer providers
   has near-zero false-positive risk).
5. MX/domain-existence check (network I/O, injected).
6. A low-confidence "obviously fake local part" heuristic -- NEVER a hard
   disqualify (real names can coincidentally match), only ``needs_review``.

Ambiguous outcomes (DNS timeout/resolver error) are NEVER treated as
disqualifying -- they always resolve to ``needs_review`` so a possibly-good
lead is never silently thrown away on an infrastructure hiccup (CLAUDE.md §3,
no silent fallbacks -- the inverse failure mode applies just as much here:
never *fabricate* a disqualification either).
"""
from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Literal

from email_validator import EmailNotValidError, validate_email

Verdict = Literal["qualified", "disqualified", "needs_review"]
MxResult = Literal["ok", "absent", "error"]

MxChecker = Callable[[str], Awaitable[MxResult]]


@dataclass(frozen=True)
class EmailClassification:
    """The result of classifying one email address."""

    verdict: Verdict
    reason: str
    """Machine-readable reason code, e.g. ``"typo_domain"``, ``"no_mx_record"``,
    ``"ok"`` (for a qualified verdict)."""
    suggested_correction: str | None = None
    """Populated only for ``reason == "typo_domain"`` -- the corrected email,
    e.g. ``"jane@gmail.com"`` for input ``"jane@gmial.com"``."""


# -- Layer 3: disposable / temporary-mail domains --------------------------
# A curated starting list of well-known disposable-email providers. Not
# exhaustive (no free list is) -- intended as a maintained, periodically
# refreshed set, not a one-time exhaustive enumeration. All lowercase,
# ASCII (disposable providers do not typically register IDNA domains).
DISPOSABLE_DOMAINS: frozenset[str] = frozenset(
    {
        "mailinator.com", "mailinator.net", "mailinator.org",
        "yopmail.com", "yopmail.fr", "yopmail.net",
        "10minutemail.com", "10minutemail.net", "10minutemail.co.za",
        "guerrillamail.com", "guerrillamail.net", "guerrillamail.org",
        "guerrillamail.biz", "guerrillamail.de", "guerrillamailblock.com",
        "sharklasers.com", "grr.la", "spam4.me", "pokemail.net",
        "tempmail.com", "temp-mail.org", "temp-mail.io", "tempmailo.com",
        "throwawaymail.com", "trashmail.com", "trashmail.net", "trashmail.io",
        "getnada.com", "getairmail.com", "dispostable.com", "fakeinbox.com",
        "maildrop.cc", "mintemail.com", "mohmal.com", "moakt.com", "moakt.cc",
        "spamgourmet.com", "spamgourmet.net", "mailnesia.com",
        "33mail.com", "emailondeck.com", "mytemp.email", "tempinbox.com",
        "discard.email", "discardmail.com", "mailcatch.com", "inboxbear.com",
        "burnermail.io", "mailsac.com", "tempr.email", "fakemailgenerator.com",
        "crazymailing.com", "mail-temporaire.fr", "tempmailaddress.com",
        "throwam.com", "anonbox.net", "safetymail.info", "tempmail.dev",
        "luxusmail.org", "einrot.com", "wegwerfmail.de", "wegwerfmail.net",
        "trbvm.com", "tmpeml.com", "tmpmail.org", "tmpmail.net",
        "harakirimail.com", "jetable.org", "meltmail.com",
    }
)

# -- Layer 4: known single-typo corrections of major providers -------------
# Direct lookup, not fuzzy matching (see module docstring). Keys/values are
# already-lowercased ASCII domains.
_TYPO_CORRECTIONS: dict[str, str] = {
    # gmail.com
    "gmial.com": "gmail.com", "gmal.com": "gmail.com", "gmai.com": "gmail.com",
    "gmailc.om": "gmail.com", "gmail.co": "gmail.com", "gmail.cm": "gmail.com",
    "gmail.con": "gmail.com", "gnail.com": "gmail.com", "gmaill.com": "gmail.com",
    "gamil.com": "gmail.com", "gmail.comm": "gmail.com",
    "gmel.com": "gmail.com", "gmeil.com": "gmail.com", "gmail.om": "gmail.com",
    "gmali.com": "gmail.com", "gamail.com": "gmail.com", "gmailo.com": "gmail.com",
    "gmall.com": "gmail.com", "gmayl.com": "gmail.com",
    # yahoo.com
    "yaho.com": "yahoo.com", "yahooo.com": "yahoo.com", "yhoo.com": "yahoo.com",
    "yahoo.co": "yahoo.com", "yahoo.cm": "yahoo.com", "yahoo.con": "yahoo.com",
    "yahoo.comm": "yahoo.com",
    # hotmail.com
    "hotmal.com": "hotmail.com", "hotmial.com": "hotmail.com",
    "hotmai.com": "hotmail.com", "hotmail.co": "hotmail.com",
    "hotmali.com": "hotmail.com", "hotmail.cm": "hotmail.com",
    "hotmail.con": "hotmail.com", "hotmailc.om": "hotmail.com",
    # outlook.com
    "outlok.com": "outlook.com", "outllook.com": "outlook.com",
    "outlook.co": "outlook.com", "outlook.cm": "outlook.com",
    "outlok.co": "outlook.com", "outloo.com": "outlook.com",
    # icloud.com
    "iclould.com": "icloud.com", "icloud.co": "icloud.com",
    "icoud.com": "icloud.com", "iclud.com": "icloud.com",
    # aol.com
    "aol.co": "aol.com", "aoll.com": "aol.com", "aol.con": "aol.com",
    # protonmail.com
    "protonmai.com": "protonmail.com", "protomail.com": "protonmail.com",
    "protonmail.co": "protonmail.com",
    # live.com / msn.com (Microsoft's other consumer domains)
    "live.co": "live.com", "msn.co": "msn.com",
}

# -- Layer 6: low-confidence "obviously fake" local-part heuristic ---------
# Soft signal only -- a hit here NEVER hard-disqualifies (a real person could
# genuinely be named/nicknamed one of these), only flags for human review.
_OBVIOUS_FAKE_LOCAL_PARTS: frozenset[str] = frozenset(
    {
        "test", "testing", "asdf", "asdfasdf", "qwerty", "xxx", "xxxx", "xxxxx",
        "example", "none", "n/a", "na", "noemail", "no-email", "notreal",
        "fake", "fakeemail", "abc", "abc123", "aaa", "aaaa", "111", "1234",
        "12345", "dummy", "placeholder", "sample", "foo", "foobar",
    }
)


async def classify_email(
    raw_email: str | None,
    *,
    mx_checker: MxChecker | None = None,
) -> EmailClassification:
    """Classify a single email address.

    Parameters
    ----------
    raw_email:
        The lead's email as stored (or about to be stored). ``None`` or
        blank -> ``disqualified("empty_email")``. Callers that want to skip
        classification entirely for a lead with no email at all (rather than
        record a verdict) should not call this function for that lead --
        see ``api.leads.tasks.classify_lead_email``'s NULL-email skip.
    mx_checker:
        Async callable resolving an ASCII domain to ``"ok"``/``"absent"``/
        ``"error"``. ``None`` skips the MX layer entirely (syntax + list
        checks only) -- used only where a live DNS check is genuinely
        unavailable/undesired; production code always supplies one.

    Returns
    -------
    EmailClassification
        Never raises for a malformed/garbage input -- every input maps to a
        verdict.
    """
    if raw_email is None or not raw_email.strip():
        return EmailClassification("disqualified", "empty_email")

    candidate = raw_email.strip()
    if candidate.count("@") != 1:
        return EmailClassification("disqualified", "invalid_syntax")

    try:
        result = validate_email(candidate, check_deliverability=False)
    except EmailNotValidError:
        return EmailClassification("disqualified", "invalid_syntax")

    domain_ascii = result.ascii_domain.lower() if result.ascii_domain else result.domain.lower()
    local_part = result.local_part

    if domain_ascii in DISPOSABLE_DOMAINS:
        return EmailClassification("disqualified", "disposable_domain")

    correction = _TYPO_CORRECTIONS.get(domain_ascii)
    if correction is not None:
        return EmailClassification(
            "disqualified", "typo_domain", suggested_correction=f"{local_part}@{correction}"
        )

    if mx_checker is not None:
        mx_result = await mx_checker(domain_ascii)
        if mx_result == "absent":
            return EmailClassification("disqualified", "no_mx_record")
        if mx_result == "error":
            return EmailClassification("needs_review", "dns_check_failed")
        # mx_result == "ok" falls through to the remaining checks.

    if local_part.lower() in _OBVIOUS_FAKE_LOCAL_PARTS:
        return EmailClassification("needs_review", "suspicious_local_part")

    return EmailClassification("qualified", "ok")

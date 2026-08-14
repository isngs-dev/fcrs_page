"""Unit tests for api.leads.email_classification.classify_email.

Covers every edge case from the approved Leads > Board email-qualification
design: empty/malformed/whitespace/uppercase/Unicode-domain handling, the
disposable/typo/obvious-fake lists, and the ambiguous-MX-result contract
(absent -> disqualified, error -> needs_review, never the reverse).
"""
from __future__ import annotations

from api.leads.email_classification import (
    DISPOSABLE_DOMAINS,
    EmailClassification,
    MxResult,
    classify_email,
)


async def _mx_ok(_domain: str) -> MxResult:
    return "ok"


async def _mx_absent(_domain: str) -> MxResult:
    return "absent"


async def _mx_error(_domain: str) -> MxResult:
    return "error"


# ==============================================================================
# Empty / malformed / whitespace / case
# ==============================================================================


async def test_none_email_is_disqualified_empty() -> None:
    result = await classify_email(None, mx_checker=_mx_ok)
    assert result == EmailClassification("disqualified", "empty_email")


async def test_empty_string_is_disqualified_empty() -> None:
    result = await classify_email("", mx_checker=_mx_ok)
    assert result.verdict == "disqualified"
    assert result.reason == "empty_email"


async def test_whitespace_only_is_disqualified_empty() -> None:
    result = await classify_email("   \t  ", mx_checker=_mx_ok)
    assert result.reason == "empty_email"


async def test_leading_trailing_whitespace_is_trimmed_before_classification() -> None:
    result = await classify_email("  jane@example.com  ", mx_checker=_mx_ok)
    assert result.verdict == "qualified"


async def test_missing_at_sign_is_invalid_syntax() -> None:
    result = await classify_email("janeexample.com", mx_checker=_mx_ok)
    assert result == EmailClassification("disqualified", "invalid_syntax")


async def test_multiple_at_signs_is_invalid_syntax() -> None:
    result = await classify_email("jane@doe@example.com", mx_checker=_mx_ok)
    assert result.reason == "invalid_syntax"


async def test_no_domain_dot_is_invalid_syntax() -> None:
    result = await classify_email("jane@example", mx_checker=_mx_ok)
    assert result.reason == "invalid_syntax"


async def test_uppercase_domain_is_normalized_and_still_classified() -> None:
    result = await classify_email("Jane.Doe@EXAMPLE.COM", mx_checker=_mx_ok)
    assert result.verdict == "qualified"


async def test_uppercase_disposable_domain_still_matches_the_lowercase_list() -> None:
    result = await classify_email("someone@MAILINATOR.COM", mx_checker=_mx_ok)
    assert result.reason == "disposable_domain"


# ==============================================================================
# Unicode / IDNA domains
# ==============================================================================


async def test_unicode_domain_is_punycode_encoded_and_classified_normally() -> None:
    # muenchen.de-equivalent Unicode domain -- must not be rejected merely
    # for being non-ASCII; IDNA-encodes to a normal-looking ascii domain and
    # proceeds through the same layers as any other domain.
    result = await classify_email("user@münchen.de", mx_checker=_mx_ok)
    assert result.verdict == "qualified"


# ==============================================================================
# Disposable domains
# ==============================================================================


async def test_known_disposable_domain_is_disqualified() -> None:
    result = await classify_email("someone@mailinator.com", mx_checker=_mx_ok)
    assert result == EmailClassification("disqualified", "disposable_domain")


async def test_disposable_domain_list_is_nonempty_and_lowercase() -> None:
    assert len(DISPOSABLE_DOMAINS) > 20
    assert all(d == d.lower() for d in DISPOSABLE_DOMAINS)


# ==============================================================================
# Typo domains
# ==============================================================================


async def test_gmial_typo_is_disqualified_with_a_suggested_correction() -> None:
    result = await classify_email("jane@gmial.com", mx_checker=_mx_ok)
    assert result.verdict == "disqualified"
    assert result.reason == "typo_domain"
    assert result.suggested_correction == "jane@gmail.com"


async def test_gmel_typo_is_disqualified_with_a_suggested_correction() -> None:
    """Regression guard: 'gmel.com' is a real, registered (parked) domain
    with an A record but no MX record, so before this domain was added to
    the typo table it fell through the disposable/typo checks and only got
    caught (or not) by the MX layer's now-removed implicit-MX-via-A
    fallback -- letting it slip through as qualified. Catching it here, at
    the typo layer, means it never even reaches the MX check."""
    result = await classify_email("sainath@gmel.com", mx_checker=_mx_ok)
    assert result.verdict == "disqualified"
    assert result.reason == "typo_domain"
    assert result.suggested_correction == "sainath@gmail.com"


async def test_hotmal_typo_is_disqualified_with_a_suggested_correction() -> None:
    result = await classify_email("bob@hotmal.com", mx_checker=_mx_ok)
    assert result.suggested_correction == "bob@hotmail.com"


async def test_typo_check_never_fuzzy_matches_an_unrelated_small_domain() -> None:
    # A legitimate small-business domain that happens to be short/unusual
    # must NEVER be "corrected" -- only exact matches against the curated
    # table trigger a typo verdict.
    result = await classify_email("jane@joesroofingco.com", mx_checker=_mx_ok)
    assert result.reason != "typo_domain"


# ==============================================================================
# MX / domain-existence layer
# ==============================================================================


async def test_mx_absent_is_disqualified() -> None:
    result = await classify_email("jane@example.com", mx_checker=_mx_absent)
    assert result == EmailClassification("disqualified", "no_mx_record")


async def test_mx_error_is_needs_review_never_disqualified() -> None:
    """The core ambiguous-case contract: a DNS timeout/resolver error must
    NEVER produce a disqualified verdict -- only needs_review."""
    result = await classify_email("jane@example.com", mx_checker=_mx_error)
    assert result.verdict == "needs_review"
    assert result.reason == "dns_check_failed"


async def test_mx_ok_proceeds_to_qualified_for_a_normal_email() -> None:
    result = await classify_email("jane@example.com", mx_checker=_mx_ok)
    assert result == EmailClassification("qualified", "ok")


async def test_no_mx_checker_skips_the_mx_layer_entirely() -> None:
    result = await classify_email("jane@example.com", mx_checker=None)
    assert result.verdict == "qualified"


# ==============================================================================
# Obvious-fake local part (soft signal, never a hard disqualify)
# ==============================================================================


async def test_obviously_fake_local_part_is_needs_review_not_disqualified() -> None:
    result = await classify_email("test@example.com", mx_checker=_mx_ok)
    assert result.verdict == "needs_review"
    assert result.reason == "suspicious_local_part"


async def test_obvious_fake_check_is_case_insensitive() -> None:
    result = await classify_email("TEST@example.com", mx_checker=_mx_ok)
    assert result.reason == "suspicious_local_part"


async def test_a_real_looking_local_part_is_not_flagged_as_suspicious() -> None:
    result = await classify_email("jane.doe@example.com", mx_checker=_mx_ok)
    assert result.verdict == "qualified"


# ==============================================================================
# Layer precedence (short-circuit ordering)
# ==============================================================================


async def test_disposable_domain_short_circuits_before_the_mx_check_runs() -> None:
    calls: list[str] = []

    async def _tracking_mx(domain: str) -> MxResult:
        calls.append(domain)
        return "ok"

    await classify_email("someone@mailinator.com", mx_checker=_tracking_mx)
    assert calls == [], "MX layer must not run once a disposable domain already disqualifies"


async def test_typo_domain_short_circuits_before_the_mx_check_runs() -> None:
    calls: list[str] = []

    async def _tracking_mx(domain: str) -> MxResult:
        calls.append(domain)
        return "ok"

    await classify_email("jane@gmial.com", mx_checker=_tracking_mx)
    assert calls == [], "MX layer must not run once a typo domain already disqualifies"

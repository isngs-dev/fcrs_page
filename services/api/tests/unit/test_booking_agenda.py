"""Plan 1.6: "What we'll cover" in their own words, from what Rebecca remembers."""
from api.agents.memory import agenda


def _need(text: str, kind: str = "need") -> dict[str, str]:
    return {"kind": kind, "text": text, "channel": "x", "at": "2026-11-18T10:00:00Z"}


def test_form_service_and_message_then_what_they_said() -> None:
    entries = [_need("service: meta_ads"), _need("message: More leads in Miami"),
               _need("company: Lee Roofing"), _need("How much is it?", kind="asked"),
               _need("we want more roof replacement jobs")]
    assert agenda(entries) == ["Meta ads", "More leads in Miami",
                               "we want more roof replacement jobs"]


def test_short_services_read_as_acronyms_and_repeats_and_corrections_are_left_out() -> None:
    entries = [_need("service: seo"), _need("service: SEO"),
               _need("Their details, corrected on the call: my name is Jan")]
    assert agenda(entries) == ["SEO"]


def test_the_newest_three_are_kept_and_long_ones_trimmed() -> None:
    entries = [_need(f"need number {i}") for i in range(5)] + [_need("x" * 200)]
    got = agenda(entries)
    assert got[:2] == ["need number 3", "need number 4"] and len(got) == 3
    assert len(got[2]) == 140 and got[2].endswith("…")

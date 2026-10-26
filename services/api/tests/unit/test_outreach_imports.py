"""A5 pure pieces: CSV parsing and row rules, rejected-rows export, and the
'Talk to Rebecca' command schema."""
from __future__ import annotations

import csv
import io
from datetime import date

import pytest
from common.errors import ValidationError

from api.agents.commands import parse_model_output
from api.agents.imports import MAX_ROWS, parse_csv, rejected_csv

_TODAY = date(2026, 10, 16)


def _csv(text: str) -> bytes:
    return text.encode("utf-8")


def test_header_aliases_and_normalisation() -> None:
    parsed = parse_csv(_csv(
        "First Name,Last Name,E-mail,Mobile,Time Zone,Favourite Colour\n"
        "Jane,Doe, Jane@Example.COM ,(786) 823-3553,America/New_York,blue\n"), today=_TODAY)
    [row] = parsed.rows
    assert (row.name, row.email, row.phone, row.timezone) == (
        "Jane Doe", "jane@example.com", "+17868233553", "America/New_York")
    assert row.row == 2  # spreadsheet line, header is line 1
    assert parsed.unknown_columns == ["Favourite Colour"]


def test_bom_and_semicolons() -> None:
    parsed = parse_csv("﻿name;email\nAna;ana@x.com\n".encode(), today=_TODAY)
    assert [r.email for r in parsed.rows] == ["ana@x.com"]


@pytest.mark.parametrize(("line", "reasons"), [
    ("Bob,,", ["NO_CONTACT_DETAILS"]),
    ("Bob,not-an-email,", ["INVALID_EMAIL"]),
    ("Bob,,12345", ["INVALID_PHONE"]),
    ("Bob,bob@x.com,7868233553,Mars/Olympus", ["INVALID_TIMEZONE"]),
])
def test_row_rules(line: str, reasons: list[str]) -> None:
    parsed = parse_csv(_csv(f"name,email,phone,timezone\n{line}\n"), today=_TODAY)
    assert parsed.rows == []
    assert parsed.rejected[0].reasons == reasons
    assert parsed.rejected[0].row == 2


def test_duplicates_in_file_are_rejected_after_the_first() -> None:
    parsed = parse_csv(_csv("name,email,phone\nA,a@x.com,\nB,A@X.com,\nC,,7868233553\n"
                            "D,,(786) 823-3553\n"), today=_TODAY)
    assert [r.name for r in parsed.rows] == ["A", "C"]
    assert [(r.row, r.reasons) for r in parsed.rejected] == [
        (3, ["DUPLICATE_IN_FILE"]), (5, ["DUPLICATE_IN_FILE"])]


def test_call_consent_needs_complete_evidence() -> None:
    parsed = parse_csv(_csv(
        "name,phone,call_consent,consent_date,consent_source,consent_type\n"
        "Full,7868233553,yes,2026-09-01,web form acme.com/quote,express_written\n"
        "NoDate,7868233554,yes,,web form,\n"
        "Future,7868233555,yes,2027-01-01,web form,\n"
        "NoSource,7868233556,yes,2026-09-01,,\n"
        "Express,7868233557,yes,2026-09-01,phone call,express\n"
        "NoConsent,7868233558,no,,,\n"), today=_TODAY)
    by_name = {r.name: r for r in parsed.rows}
    assert by_name["Full"].call_consent == {"date": "2026-09-01",
                                            "source": "web form acme.com/quote", "wording": "",
                                            "type": "express_written"}
    for incomplete in ("NoDate", "Future", "NoSource"):
        assert by_name[incomplete].call_consent is None
        assert by_name[incomplete].warnings == ["CALL_CONSENT_INCOMPLETE"]
    assert by_name["Express"].call_consent is not None
    assert by_name["Express"].call_consent["type"] == "express"
    assert by_name["NoConsent"].call_consent is None and by_name["NoConsent"].warnings == []
    report = parsed.report()
    assert report["call_consent_rows"] == 2
    # Phone rows without WRITTEN consent would need a DNC scrub for any human calling.
    assert report["needs_dnc_scrub"] == 5


@pytest.mark.parametrize(("data", "code"), [
    (b"", "IMPORT_EMPTY"),
    (b"name,company\nA,B\n", "IMPORT_NO_CONTACT_COLUMNS"),
    (b"\xff\xfename,email\n", "IMPORT_NOT_UTF8"),
    (b"x" * (2 * 1024 * 1024 + 1), "IMPORT_TOO_LARGE"),
], ids=["empty", "no_contact_columns", "not_utf8", "too_large"])  # Windows caps env vars at 32 KB
def test_file_level_errors(data: bytes, code: str) -> None:
    with pytest.raises(ValidationError) as exc:
        parse_csv(data, today=_TODAY)
    assert exc.value.code == code


def test_row_limit() -> None:
    body = "".join(f"p{i},p{i}@x.com\n" for i in range(MAX_ROWS + 1))
    with pytest.raises(ValidationError) as exc:
        parse_csv(_csv("name,email\n" + body), today=_TODAY)
    assert exc.value.code == "IMPORT_TOO_MANY_ROWS"


def test_rejected_csv_is_formula_injection_safe() -> None:
    out = rejected_csv([{"row": 2, "reasons": ["INVALID_EMAIL"],
                         "raw": {"name": "=HYPERLINK(\"http://evil\")", "email": "+bad"}}])
    rows = list(csv.reader(io.StringIO(out)))
    assert rows[0] == ["row", "reasons", "name", "email"]
    assert rows[1][2].startswith("'=") and rows[1][3] == "'+bad"


# -- Talk to Rebecca schema --------------------------------------------------------------


def test_command_schema_accepts_a_well_formed_request() -> None:
    parsed = parse_model_output('Sure! {"action": "start_outreach", "audience": {"emails": '
                                '["a@x.com"], "names": [], "created_within_days": 7, '
                                '"last_import": false}, "channels": ["email"], "goal": "demo"}')
    assert parsed is not None
    assert parsed.channels == ["email"] and parsed.audience.created_within_days == 7


@pytest.mark.parametrize("text", [
    "no json here",
    '{"action": null}',
    '{"action": "delete_everything", "audience": {}}',
    '{"action": "start_outreach", "audience": {"tenant_id": "other"}}',   # unknown field
    '{"action": "start_outreach", "audience": {}, "channels": ["sms"]}',
    '{"action": "start_outreach", "audience": {"created_within_days": 9999}}',
])
def test_command_schema_rejects_anything_else(text: str) -> None:
    assert parse_model_output(text) is None

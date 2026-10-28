"""CSV import parsing and row validation (A5 J2/J3/J7) -- pure, no I/O.

``parse_csv`` turns an uploaded file into accepted rows (normalised email/phone,
timezone, call-consent evidence) and rejected rows with reasons. Database
checks (suppression, existing contacts) happen in the service on commit/preview.
"""
from __future__ import annotations

import csv
import io
import re
from dataclasses import asdict, dataclass, field
from datetime import UTC, date, datetime
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from common.csv_safe import escape_csv_cell
from common.errors import ValidationError
from common.phone import to_e164
from email_validator import EmailNotValidError, validate_email

MAX_BYTES = 2 * 1024 * 1024
MAX_ROWS = 5000

_ALIASES = {
    "name": "name", "full_name": "name", "fullname": "name", "contact_name": "name",
    "first_name": "first_name", "firstname": "first_name", "first": "first_name",
    "last_name": "last_name", "lastname": "last_name", "last": "last_name", "surname": "last_name",
    "email": "email", "email_address": "email", "e_mail": "email",
    "phone": "phone", "phone_number": "phone", "mobile": "phone", "cell": "phone",
    "telephone": "phone",
    "timezone": "timezone", "time_zone": "timezone", "tz": "timezone",
    "call_consent": "call_consent", "consent_to_call": "call_consent",
    "consent_date": "consent_date", "consent_source": "consent_source",
    "consent_wording": "consent_wording", "consent_type": "consent_type",
}
_YES = {"yes", "y", "true", "1", "granted", "x"}


@dataclass
class ImportRow:
    row: int  # the line number in the user's spreadsheet (the header is line 1)
    name: str | None
    email: str | None
    phone: str | None
    timezone: str | None
    call_consent: dict[str, str] | None = None  # date, source, wording, type
    warnings: list[str] = field(default_factory=list)


@dataclass
class Rejected:
    row: int
    reasons: list[str]
    raw: dict[str, str]


@dataclass
class ParsedImport:
    rows: list[ImportRow]
    rejected: list[Rejected]
    unknown_columns: list[str]
    total: int

    def report(self) -> dict[str, Any]:
        return {
            "total": self.total,
            "valid": len(self.rows),
            "rejected": [asdict(r) | {"raw": {}} for r in self.rejected],
            "rejected_count": len(self.rejected),
            "unknown_columns": self.unknown_columns,
            "call_consent_rows": sum(1 for r in self.rows if r.call_consent),
            "warnings": sum(len(r.warnings) for r in self.rows),
            # Phone rows the AI will never call (no written consent); a human-call
            # workflow would need a National DNC scrub first (compliance §2.4).
            "needs_dnc_scrub": sum(1 for r in self.rows if r.phone and not (
                r.call_consent and r.call_consent["type"] == "express_written")),
        }


def _key(header: str) -> str:
    return re.sub(r"[\s\-]+", "_", header.strip().lower())


def _decode(data: bytes) -> str:
    if len(data) > MAX_BYTES:
        raise ValidationError("The file is larger than 2 MB.", code="IMPORT_TOO_LARGE")
    try:
        return data.decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        raise ValidationError("The file must be UTF-8 CSV.", code="IMPORT_NOT_UTF8") from exc


def _email(value: str) -> str | None:
    try:
        return validate_email(value, check_deliverability=False).normalized.lower()
    except EmailNotValidError:
        return None


def _consent(values: dict[str, str], phone: str | None, today: date) -> tuple[
        dict[str, str] | None, list[str]]:
    if values.get("call_consent", "").strip().lower() not in _YES:
        return None, []
    raw_date = values.get("consent_date", "").strip()
    source = values.get("consent_source", "").strip()
    kind = values.get("consent_type", "").strip().lower().replace("-", "_") or "express_written"
    try:
        when = date.fromisoformat(raw_date[:10]) if raw_date else None
    except ValueError:
        when = None
    if (not phone or when is None or when > today or not source
            or kind not in ("express_written", "express")):
        return None, ["CALL_CONSENT_INCOMPLETE"]
    return {"date": when.isoformat(), "source": source[:300],
            "wording": values.get("consent_wording", "").strip()[:2000],
            "type": kind}, []


def check_row(values: dict[str, str], number: int, today: date) -> tuple[
        ImportRow | None, list[str]]:
    """One row's rules (J3): the accepted row, or ``None`` and the reasons."""
    reasons: list[str] = []
    name = values.get("name") or " ".join(
        p for p in (values.get("first_name"), values.get("last_name")) if p) or None
    email = phone = tz = None
    if values.get("email"):
        email = _email(values["email"])
        if email is None:
            reasons.append("INVALID_EMAIL")
    if values.get("timezone"):
        try:
            ZoneInfo(values["timezone"])
            tz = values["timezone"]
        except (ZoneInfoNotFoundError, ValueError):
            reasons.append("INVALID_TIMEZONE")
    if values.get("phone"):
        phone = to_e164(values["phone"])
        if phone is None:
            reasons.append("INVALID_PHONE")
        elif (tz and not tz.startswith("America/") and tz != "Pacific/Honolulu"
              and not values["phone"].strip().startswith("+")):
            # Without '+' a 10-digit number is read as US; for someone outside the US
            # that silently dials the wrong country (live: an Indian mobile -> +1, 403).
            reasons.append("PHONE_NEEDS_COUNTRY_CODE")
    if not values.get("email") and not values.get("phone"):
        reasons.append("NO_CONTACT_DETAILS")
    if reasons:
        return None, reasons
    consent, warnings = _consent(values, phone, today)
    return ImportRow(row=number, name=name[:200] if name else None, email=email, phone=phone,
                     timezone=tz, call_consent=consent, warnings=warnings), []


def parse_csv(data: bytes, *, today: date | None = None) -> ParsedImport:
    text = _decode(data)
    try:
        dialect: Any = csv.Sniffer().sniff(text[:4096], delimiters=",;")
    except csv.Error:
        dialect = csv.excel
    reader = csv.reader(io.StringIO(text), dialect)
    try:
        header = next(reader)
    except StopIteration as exc:
        raise ValidationError("The file is empty.", code="IMPORT_EMPTY") from exc
    keys = [_ALIASES.get(_key(h)) for h in header]
    if not {"email", "phone"} & set(k for k in keys if k):
        raise ValidationError("The file needs an email or phone column.",
                              code="IMPORT_NO_CONTACT_COLUMNS")
    unknown = [h.strip() for h, k in zip(header, keys, strict=True) if not k and h.strip()]
    today = today or datetime.now(UTC).date()

    rows: list[ImportRow] = []
    rejected: list[Rejected] = []
    seen_email: set[str] = set()
    seen_phone: set[str] = set()
    total = 0
    for number, cells in enumerate(reader, start=2):  # line 1 is the header
        if not any(c.strip() for c in cells):
            continue
        total += 1
        if total > MAX_ROWS:
            raise ValidationError(f"The file has more than {MAX_ROWS} rows.",
                                  code="IMPORT_TOO_MANY_ROWS")
        values = {k: c.strip() for k, c in zip(keys, cells, strict=False) if k}
        raw = {h.strip(): c for h, c in zip(header, cells, strict=False)}
        row, reasons = check_row(values, number, today)
        if row is not None and ((row.email and row.email in seen_email)
                                or (row.phone and row.phone in seen_phone)):
            row, reasons = None, ["DUPLICATE_IN_FILE"]
        if row is None:
            rejected.append(Rejected(row=number, reasons=reasons, raw=raw))
            continue
        if row.email:
            seen_email.add(row.email)
        if row.phone:
            seen_phone.add(row.phone)
        rows.append(row)
    return ParsedImport(rows=rows, rejected=rejected, unknown_columns=unknown, total=total)


def rejected_csv(rejected: list[dict[str, Any]]) -> str:
    """Rejected rows as CSV (row, reasons, original cells), formula-injection safe (J11)."""
    columns: list[str] = []
    for r in rejected:
        columns += [c for c in r.get("raw", {}) if c not in columns]
    out = io.StringIO()
    writer = csv.writer(out)
    writer.writerow(["row", "reasons", *columns])
    for r in rejected:
        writer.writerow([r["row"], " ".join(r["reasons"]),
                         *(escape_csv_cell(r.get("raw", {}).get(c, "")) for c in columns)])
    return out.getvalue()

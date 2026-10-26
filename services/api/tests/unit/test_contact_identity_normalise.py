"""contacts.repository.normalise_identity -- email/phone identities are stored
and looked up in one canonical form (A1 D6), so "Foo@Bar.com" and
"foo@bar.com" are the same contact and a phone matches however it was typed."""
from __future__ import annotations

from typing import Any

import pytest
from common.auth import AuthClaims, Role
from common.errors import ValidationError

from api.contacts.repository import (
    add_identity,
    get_contact_id_by_identity,
    normalise_identity,
)

_CLAIMS = AuthClaims(subject="u-1", role=Role.CLIENT_ADMIN, tenant_id="tenant-a")


def test_email_is_trimmed_and_lower_cased() -> None:
    assert normalise_identity("email", "  Jane.Doe@Example.COM ") == "jane.doe@example.com"


def test_phone_becomes_e164() -> None:
    assert normalise_identity("phone", "(786) 823-3553") == "+17868233553"


def test_undialable_phone_is_rejected() -> None:
    with pytest.raises(ValidationError) as exc:
        normalise_identity("phone", "12345")
    assert exc.value.code == "INVALID_PHONE"


def test_other_identity_types_are_stored_as_given() -> None:
    assert normalise_identity("visitor_id", "Visitor-ABC") == "Visitor-ABC"


class _RecordingDB:
    def __init__(self) -> None:
        self.calls: list[tuple[str, tuple[Any, ...]]] = []

    async def execute(self, query: str, *args: Any) -> str:
        self.calls.append((query, args))
        return "INSERT 0 1"

    async def fetchrow(self, query: str, *args: Any) -> dict[str, Any] | None:
        self.calls.append((query, args))
        return None


async def test_add_and_lookup_use_the_normalised_value() -> None:
    db = _RecordingDB()
    await add_identity(db, _CLAIMS, "c-1", identity_type="email", identity_value="Jane@Example.com")  # type: ignore[arg-type]
    await get_contact_id_by_identity(db, _CLAIMS, identity_type="phone", identity_value="786-823-3553")  # type: ignore[arg-type]

    assert db.calls[0][1][4] == "jane@example.com"
    assert db.calls[1][1][2] == "+17868233553"

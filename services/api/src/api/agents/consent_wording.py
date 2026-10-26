"""Channel-consent wording shown by the widget (A4 H2), versioned and server-owned.

The widget sends only ``{email, call, version}``; the stored consent text is
looked up here, so what's on record is exactly what that widget build showed
and a client can't inject its own wording. Never edit a published version --
add a new one (the widget mirrors the text in ``apps/widget/src/channelConsent.ts``).
"""
from __future__ import annotations

from typing import Any

from common.errors import ValidationError
from pydantic import BaseModel, Field

CURRENT_VERSION = "2026-10-a"

WORDING: dict[str, dict[str, str]] = {
    "2026-10-a": {
        "email": "Email me about my enquiry, including follow-ups. I can unsubscribe at any time.",
        "call": ("You may call me at the number I provided about my enquiry, including with "
                 "automated technology and an AI assistant. Consent is not a condition of "
                 "purchase; I can say stop at any time."),
    },
    # Chat capture (orchestrator/identity_capture.py): the visitor typed their
    # email after Rebecca asked for the best email so the team can follow up.
    # Server-only -- the widget can't submit this version.
    "chat-2026-10-a": {
        "email": ("Typed their email in the chat after the assistant asked for the best email "
                  "so the team could follow up about their enquiry."),
        # Must equal orchestrator.identity_capture.PHONE_ASK (the exact disclosure shown).
        "call": ("And what's the best number to reach you? If you share it, our team may call "
                 "you about your enquiry, including with our AI assistant, and you can ask us "
                 "to stop anytime."),
    },
}
BASIS = {"email": "express_consent", "call": "express_written_consent"}


class ChannelConsentsIn(BaseModel):
    """The widget's per-channel boxes (both unticked by default)."""

    email: bool = False
    call: bool = False
    version: str = Field(min_length=1, max_length=40)


def granted_channels(consents: ChannelConsentsIn | None, *, has_phone: bool) -> list[str]:
    """Validate and return the ticked channels -- raises before anything is stored."""
    if consents is None:
        return []
    if consents.version not in WORDING or consents.version.startswith("chat-"):
        raise ValidationError("Unknown consent wording version.", code="CONSENT_VERSION_UNKNOWN")
    if consents.call and not has_phone:
        raise ValidationError("Call consent needs a phone number.",
                              code="CALL_CONSENT_NEEDS_PHONE")
    return [ch for ch in ("email", "call") if getattr(consents, ch)]


def consent_record(channel: str, version: str) -> dict[str, Any]:
    # A number typed in the chat after the disclosure is express (not written) consent --
    # still enough for AI calls under A1 D5 (express_consent from the widget).
    basis = "express_consent" if version.startswith("chat-") else BASIS[channel]
    return {"channel": channel, "basis": basis, "wording": WORDING[version][channel],
            "wording_version": version}

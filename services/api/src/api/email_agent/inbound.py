"""Pure inbound-email helpers (A3): parse raw MIME, strip quoted history,
deterministic classification rules, plus-address thread ids, and SendGrid
event-webhook signature verification. No I/O.
"""
from __future__ import annotations

import base64
import hashlib
import re
from dataclasses import dataclass
from email import message_from_bytes, policy
from email.message import EmailMessage
from email.utils import getaddresses, parseaddr

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.serialization import load_der_public_key

_MSGID = re.compile(r"<[^<>\s]+>")


@dataclass(frozen=True)
class InboundEmail:
    rfc_message_id: str
    in_reply_to: str | None
    references: list[str]
    from_addr: str
    to_addrs: list[str]
    subject: str
    text: str
    auto_submitted: bool
    is_dsn: bool
    attachments: int = 0


def _ids(value: str | None) -> list[str]:
    return _MSGID.findall(value or "")


def _text_part(msg: EmailMessage) -> str:
    body = msg.get_body(preferencelist=("plain",))
    if body is not None:
        return str(body.get_content())
    html_part = msg.get_body(preferencelist=("html",))
    if html_part is None:
        return ""
    html = str(html_part.get_content())
    html = re.sub(r"(?is)<(script|style).*?</\1>", " ", html)
    html = re.sub(r"(?i)<br\s*/?>|</p>|</div>", "\n", html)
    return re.sub(r"<[^>]+>", "", html)


def parse_raw(raw: bytes) -> InboundEmail:
    msg = message_from_bytes(raw, policy=policy.default)
    assert isinstance(msg, EmailMessage)  # noqa: S101 -- policy.default always builds EmailMessage
    message_ids = _ids(msg.get("Message-ID"))
    rfc_id = message_ids[0] if message_ids else (
        f"<{hashlib.sha256(raw).hexdigest()}@inbound.invalid>")
    auto = msg.get("Auto-Submitted", "no").strip().lower()
    precedence = msg.get("Precedence", "").strip().lower()
    content_type = msg.get_content_type()
    report_type = (msg.get_param("report-type") or "")
    attachments = sum(1 for _ in msg.iter_attachments())
    return InboundEmail(
        rfc_message_id=rfc_id,
        in_reply_to=next(iter(_ids(msg.get("In-Reply-To"))), None),
        references=_ids(msg.get("References")),
        from_addr=parseaddr(str(msg.get("From", "")))[1].strip().lower(),
        to_addrs=[a.strip().lower() for _, a in getaddresses(
            [str(msg.get("To", "")), str(msg.get("Cc", ""))]) if a],
        subject=" ".join(str(msg.get("Subject", "")).split())[:300],
        text=_text_part(msg).strip(),
        auto_submitted=(auto not in ("", "no")) or precedence in ("auto_reply", "bulk", "junk")
        or msg.get("X-Autoreply") is not None or msg.get("X-Autorespond") is not None,
        is_dsn=(content_type == "multipart/report"
                and str(report_type).lower() == "delivery-status"),
        attachments=attachments,
    )


_QUOTE_START = re.compile(
    r"^(On .{3,200}wrote:\s*$|-{2,}\s*Original Message\s*-{2,}|_{5,}\s*$|From:\s.+$|"
    r"Sent from my \w+)", re.IGNORECASE | re.MULTILINE)


def strip_quoted(text: str) -> str:
    """The new part of a reply: cut at the first quote header, drop ``>`` lines."""
    match = _QUOTE_START.search(text)
    if match is not None:
        text = text[:match.start()]
    lines = [line for line in text.splitlines() if not line.lstrip().startswith(">")]
    return "\n".join(lines).strip()[:5000]


_OOO_SUBJECT = re.compile(r"out of (the )?office|auto(matic)?[- ]?reply|on vacation|away from",
                          re.IGNORECASE)
_UNSUB = re.compile(
    r"\b(unsubscribe|stop emailing|stop sending|remove me|take me off|opt[- ]?out|"
    r"do not (contact|email) me|don'?t (contact|email) me)\b", re.IGNORECASE)
_BOUNCE_SENDERS = ("mailer-daemon@", "postmaster@")


def classify_by_rules(email: InboundEmail, reply_text: str) -> str | None:
    """Deterministic labels that never need the LLM (F7), or ``None``."""
    if email.is_dsn or email.from_addr.startswith(_BOUNCE_SENDERS):
        return "bounce"
    if email.auto_submitted or _OOO_SUBJECT.search(email.subject):
        return "out_of_office"
    if _UNSUB.search(reply_text) or _UNSUB.search(email.subject):
        return "unsubscribe"
    return None


def plus_thread_id(addresses: list[str], reply_domain: str | None) -> str | None:
    """``reply+{thread_id}@{reply_domain}`` -> thread_id (F2/F3)."""
    if not reply_domain:
        return None
    for address in addresses:
        local, _, domain = address.partition("@")
        if domain == reply_domain.lower() and local.startswith("reply+"):
            candidate = local[len("reply+"):]
            if re.fullmatch(r"[0-9a-f]{32}", candidate):
                return candidate
    return None


def sendgrid_auth(spf: str | None, dkim: str | None, from_addr: str) -> dict[str, bool]:
    """Did the sender's domain pass SPF or DKIM, as SendGrid reported it?"""
    domain = from_addr.rpartition("@")[2]
    dkim_pass = bool(domain) and bool(re.search(rf"@{re.escape(domain)}\s*:\s*pass",
                                                dkim or "", re.IGNORECASE))
    return {"spf": (spf or "").strip().lower() == "pass", "dkim": dkim_pass}


def verify_event_signature(public_key_b64: str, signature_b64: str, timestamp: str,
                           payload: bytes) -> bool:
    """SendGrid signed Event Webhook: ECDSA P-256 / SHA-256 over ``timestamp + body``."""
    try:
        key = load_der_public_key(base64.b64decode(public_key_b64))
        if not isinstance(key, ec.EllipticCurvePublicKey):
            return False
        key.verify(base64.b64decode(signature_b64), timestamp.encode() + payload,
                   ec.ECDSA(hashes.SHA256()))
    except (InvalidSignature, ValueError, TypeError):
        return False
    return True

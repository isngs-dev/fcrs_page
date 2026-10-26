"""TransactionalEmailProvider (A2 E2): SendGrid v3 API and an SMTP adapter.

Both send the SAME headers we control: ``Message-ID`` (ours, stable across
retries), ``In-Reply-To``/``References`` when threading, ``Reply-To``, and
RFC 8058 one-click ``List-Unsubscribe`` + ``List-Unsubscribe-Post``.

Errors: deterministic (bad key, rejected payload, auth failure) ->
``EmailDeliveryError`` (a ``ValidationError``, never retried); transient
(429, 5xx, timeouts, connection errors) -> ``TransientExecutorError``.
Recipients, subjects and bodies are never logged.
"""
from __future__ import annotations

import asyncio
import re
import smtplib
from dataclasses import dataclass, field
from email.message import EmailMessage
from email.utils import formataddr, formatdate
from typing import Any, Protocol

import httpx
from common.errors import ValidationError

from api.agents.executors import TransientExecutorError
from api.notifications.providers import _text_to_html

_SENDGRID_URL = "https://api.sendgrid.com/v3/mail/send"


class EmailDeliveryError(ValidationError):
    code = "EMAIL_DELIVERY_FAILED"


@dataclass(frozen=True)
class OutboundEmail:
    from_address: str
    from_name: str | None
    to: str
    subject: str
    body_text: str
    message_id: str  # "<...@domain>"
    reply_to: str | None = None
    in_reply_to: str | None = None
    references: list[str] = field(default_factory=list)
    unsubscribe_url: str | None = None
    custom_args: dict[str, str] = field(default_factory=dict)


@dataclass(frozen=True)
class SentRef:
    provider: str
    provider_message_id: str | None


class TransactionalEmailProvider(Protocol):
    async def send(self, email: OutboundEmail) -> SentRef: ...


def _clean(value: str) -> str:
    """No CR/LF in header values (header injection)."""
    return re.sub(r"[\r\n]+", " ", value).strip()


def thread_headers(email: OutboundEmail) -> dict[str, str]:
    headers = {"Message-ID": email.message_id}
    if email.in_reply_to:
        headers["In-Reply-To"] = email.in_reply_to
    if email.references:
        headers["References"] = " ".join(email.references)
    if email.unsubscribe_url:
        headers["List-Unsubscribe"] = f"<{email.unsubscribe_url}>"
        headers["List-Unsubscribe-Post"] = "List-Unsubscribe=One-Click"
    return {k: _clean(v) for k, v in headers.items()}


def sendgrid_payload(email: OutboundEmail) -> dict[str, Any]:
    sender: dict[str, str] = {"email": email.from_address}
    if email.from_name:
        sender["name"] = _clean(email.from_name)
    payload: dict[str, Any] = {
        "personalizations": [{"to": [{"email": email.to}]}],
        "from": sender,
        "subject": _clean(email.subject),
        "content": [
            {"type": "text/plain", "value": email.body_text},
            {"type": "text/html", "value": _text_to_html(email.body_text)},
        ],
        "headers": thread_headers(email),
        # SendGrid's own click/open tracking rewrites links and adds pixels;
        # off, so the unsubscribe link and Message-ID stay ours.
        "tracking_settings": {"click_tracking": {"enable": False},
                              "open_tracking": {"enable": False}},
    }
    if email.reply_to:
        payload["reply_to"] = {"email": email.reply_to}
    if email.custom_args:
        payload["custom_args"] = email.custom_args
    return payload


class SendGridEmailProvider:
    def __init__(self, api_key: str, *, timeout: float,
                 transport: httpx.AsyncBaseTransport | None = None) -> None:
        self._api_key = api_key
        self._timeout = timeout
        self._transport = transport

    async def send(self, email: OutboundEmail) -> SentRef:
        try:
            async with httpx.AsyncClient(timeout=self._timeout,
                                         transport=self._transport) as client:
                response = await client.post(
                    _SENDGRID_URL, json=sendgrid_payload(email),
                    headers={"Authorization": f"Bearer {self._api_key}"})
        except httpx.HTTPError as exc:
            raise TransientExecutorError(f"SendGrid unreachable: {type(exc).__name__}") from exc
        if response.status_code == 429 or response.status_code >= 500:
            raise TransientExecutorError(f"SendGrid HTTP {response.status_code}")
        if response.status_code >= 400:
            raise EmailDeliveryError(
                f"SendGrid rejected the message (HTTP {response.status_code}).",
                code=f"SENDGRID_{response.status_code}")
        return SentRef(provider="sendgrid",
                       provider_message_id=response.headers.get("X-Message-Id"))


_DETERMINISTIC_SMTP = (smtplib.SMTPAuthenticationError, smtplib.SMTPRecipientsRefused,
                       smtplib.SMTPSenderRefused, smtplib.SMTPDataError,
                       smtplib.SMTPNotSupportedError)


def smtp_message(email: OutboundEmail) -> EmailMessage:
    msg = EmailMessage()
    name = _clean(email.from_name or "")
    msg["From"] = formataddr((name, email.from_address)) if name else email.from_address
    msg["To"] = email.to
    msg["Subject"] = _clean(email.subject)
    msg["Date"] = formatdate(usegmt=True)
    if email.reply_to:
        msg["Reply-To"] = email.reply_to
    for key, value in thread_headers(email).items():
        msg[key] = value
    msg.set_content(email.body_text)
    msg.add_alternative(_text_to_html(email.body_text), subtype="html")
    return msg


class SmtpAgentEmailProvider:
    """The tenant's OWN SMTP server (its notification email config), never PLATFORM_SMTP."""

    def __init__(self, *, host: str, port: int, use_tls: bool, username: str | None,
                 password: str, timeout: float) -> None:
        self._host, self._port, self._use_tls = host, port, use_tls
        self._username, self._password, self._timeout = username, password, timeout

    async def send(self, email: OutboundEmail) -> SentRef:
        return await asyncio.to_thread(self._send_sync, email)

    def _send_sync(self, email: OutboundEmail) -> SentRef:
        try:
            with smtplib.SMTP(self._host, self._port, timeout=self._timeout) as smtp:
                if self._use_tls:
                    smtp.starttls()
                if self._username:
                    smtp.login(self._username, self._password)
                smtp.send_message(smtp_message(email))
        except _DETERMINISTIC_SMTP as exc:
            raise EmailDeliveryError(f"SMTP rejected the message: {type(exc).__name__}",
                                     code="SMTP_REJECTED") from exc
        except (OSError, smtplib.SMTPException) as exc:
            raise TransientExecutorError(f"SMTP unavailable: {type(exc).__name__}") from exc
        return SentRef(provider="smtp", provider_message_id=None)

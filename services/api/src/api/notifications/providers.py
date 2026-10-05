"""NotificationProvider Protocol + Notification/DeliveryRef + impls (S9.1).

``NotificationProvider`` is a ``typing.Protocol`` with two implementations
this sprint: ``LogNotificationProvider`` (deterministic, PII-safe logging
stub -- dev/live-testable without a mail server) and ``SmtpEmailProvider``
(stdlib ``smtplib`` against the tenant's configured SMTP host/port, e.g.
MailHog in dev). This mirrors S8.2's ``StubCalendarProvider`` /
``GoogleCalendarProvider`` split and ``calendar_provider_for`` selection
exactly.

Deterministic vs transient (CLAUDE.md background-task discipline, S9.1
decision 5): a deterministic send failure (SMTP auth failure, permanent 5xx,
malformed address, unknown/misconfigured provider) is raised as
``NotificationDeliveryError``/``NotificationConfigError`` -- both
``ValidationError`` subtypes, exactly like ``CalendarConfigError`` -- so the
Celery task (``api.notifications.tasks``) can catch ``ValidationError`` to
mark the job ``failed`` WITHOUT retrying, mirroring
``api.crm.tasks``/``api.scheduling.tasks``. A transient failure (connect
error, timeout, disconnect) is left to propagate as its native exception type
so the task does NOT catch it -- Celery retries with backoff/jitter.

PII discipline: ``Notification.recipient``/``.subject``/``.body`` are never
logged. ``LogNotificationProvider`` logs only ``channel``/``tenant_id``/
``recipient_domain`` (the part after ``@``) -- never the recipient localpart,
subject, or body.
"""
from __future__ import annotations

import re
import smtplib
from dataclasses import dataclass
from email.message import EmailMessage
from email.utils import formatdate, make_msgid
from typing import Protocol
from uuid import uuid4

import httpx
from common.auth import AuthClaims
from common.errors import ValidationError
from common.logging import get_logger

from api.notifications.config_repository import NotificationConfig

_log = get_logger(__name__)

_TWILIO_API_BASE = "https://api.twilio.com/2010-04-01"

# Loose E.164-ish phone validator (Decision 5) -- no carrier lookup, no
# phone-number library; Twilio's own 400 is the authoritative validation.
_PHONE_RE = re.compile(r"^\+?[0-9]{7,15}$")


@dataclass(frozen=True)
class Notification:
    """A single outbound notification message.

    ``channel`` is ``"email"``, ``"sms"``, or ``"whatsapp"`` (S9.3). For
    sms/whatsapp ``recipient`` is an E.164 phone number and ``subject`` is
    ignored by ``TwilioNotificationProvider`` (Twilio's Messages API has no
    subject field) -- documented, not silently dropped.
    """

    channel: str             # "email" | "sms" | "whatsapp"
    recipient: str          # the "to" address/number; PII -- never logged
    subject: str            # PII/content -- never logged; ignored for sms/whatsapp
    body: str                # plain text this sprint; PII/content -- never logged


@dataclass(frozen=True)
class DeliveryRef:
    """The result of a successful ``NotificationProvider.send`` call."""

    provider: str           # "log" | "smtp" | "twilio"
    ref: str                # provider message id / job_id echo


class NotificationProvider(Protocol):
    """Outbound notification delivery contract. Selected per tenant config."""

    async def send(self, claims: AuthClaims, message: Notification) -> DeliveryRef: ...


class NotificationConfigError(ValidationError):
    """Deterministic notification config error -- raised before any send."""

    code = "NOTIFICATION_CONFIG_ERROR"


class NotificationDeliveryError(ValidationError):
    """Deterministic send failure (auth failure, permanent 5xx, malformed address).

    A ``ValidationError`` subtype so the Celery task's deterministic-vs-
    transient split (catch ``ValidationError`` => no retry) mirrors
    ``api.crm.tasks``/``api.scheduling.tasks`` exactly.
    """

    code = "NOTIFICATION_DELIVERY_FAILED"


def _recipient_domain(recipient: str) -> str:
    """Return the domain part of an email address, PII-safe for logging."""
    if "@" not in recipient:
        return ""
    return recipient.rsplit("@", 1)[-1]


class LogNotificationProvider:
    """``NotificationProvider`` implementation: deterministic, PII-safe logging stub.

    Never raises -- the whole enqueue -> task -> provider -> ``sent`` path is
    live-testable with no mail server and no secret (S9.1 decision 6).
    """

    async def send(self, claims: AuthClaims, message: Notification) -> DeliveryRef:
        ref = uuid4().hex
        _log.info(
            "notification_dispatched",
            extra={
                "event": "notification_dispatched",
                "channel": message.channel,
                "tenant_id": claims.tenant_id,
                "recipient_domain": _recipient_domain(message.recipient),
            },
        )
        return DeliveryRef(provider="log", ref=ref)


# Deterministic (permanent) SMTP failures -- caught and re-raised as
# NotificationDeliveryError so the task does NOT retry.
_DETERMINISTIC_SMTP_ERRORS = (
    smtplib.SMTPAuthenticationError,
    smtplib.SMTPRecipientsRefused,
    smtplib.SMTPSenderRefused,
    smtplib.SMTPHeloError,
    smtplib.SMTPNotSupportedError,
)


class SmtpEmailProvider:
    """``NotificationProvider`` implementation: stdlib ``smtplib`` (MailHog-testable).

    Runs the blocking ``smtplib.SMTP`` call via ``asyncio.to_thread`` so it
    never blocks the event loop (S9.1 decision 1). STARTTLS iff
    ``smtp_use_tls``; ``login`` iff a username is configured (MailHog dev =
    no TLS, no auth).
    """

    def __init__(
        self,
        *,
        host: str,
        port: int,
        use_tls: bool,
        username: str | None,
        password: str,
        from_address: str,
        from_name: str | None,
        timeout: float,
    ) -> None:
        self._host = host
        self._port = port
        self._use_tls = use_tls
        self._username = username
        self._password = password
        self._from_address = from_address
        self._from_name = from_name
        self._timeout = timeout

    async def send(self, claims: AuthClaims, message: Notification) -> DeliveryRef:
        import asyncio

        return await asyncio.to_thread(self._send_sync, message)

    def _send_sync(self, message: Notification) -> DeliveryRef:
        from_header = (
            f"{self._from_name} <{self._from_address}>" if self._from_name else self._from_address
        )
        msg = EmailMessage()
        msg["From"] = from_header
        msg["To"] = message.recipient
        msg["Subject"] = message.subject
        # Spam filters penalise a missing Date and a Message-Id on a bare
        # container hostname, so stamp both on the sender's own domain.
        message_id = make_msgid(domain=self._from_address.rpartition("@")[2] or None)
        msg["Message-Id"] = message_id
        msg["Date"] = formatdate(usegmt=True)
        msg.set_content(message.body)

        try:
            with smtplib.SMTP(self._host, self._port, timeout=self._timeout) as smtp:
                if self._use_tls:
                    smtp.starttls()
                if self._username:
                    smtp.login(self._username, self._password)
                smtp.send_message(msg)
        except _DETERMINISTIC_SMTP_ERRORS as exc:
            raise NotificationDeliveryError(
                f"SMTP send failed (deterministic): {exc.__class__.__name__}",
                code="NOTIFICATION_DELIVERY_FAILED",
            ) from exc
        # Any other smtplib/socket/OSError (SMTPConnectError, SMTPServerDisconnected,
        # socket.timeout, ...) is transient -- propagates unchanged so Celery retries.

        return DeliveryRef(provider="smtp", ref=message_id)


# Twilio status codes that are transient (rate-limited / server-side) --
# raised as a plain RuntimeError so Celery retries with backoff/jitter.
_TWILIO_TRANSIENT_STATUS_FLOOR = 500


class TwilioNotificationProvider:
    """``NotificationProvider`` implementation: Twilio SMS/WhatsApp via raw ``httpx``.

    One channel-aware provider (NOT two) -- reads ``message.channel`` to
    decide sms vs whatsapp To/From shaping (Decision 3). Mirrors
    ``api.scheduling.calendar.GoogleCalendarProvider``'s raw-httpx-over-
    vendor-SDK precedent exactly: no ``twilio`` package, HTTP Basic Auth,
    deterministic-vs-transient classification of the response.

    ``message.subject`` is ignored -- Twilio's Messages API has no subject
    field; only ``message.body`` is sent.
    """

    def __init__(
        self, *, account_sid: str, auth_token: str, from_number: str, timeout: float
    ) -> None:
        self._account_sid = account_sid
        self._auth_token = auth_token
        self._from_number = from_number
        self._timeout = timeout

    async def send(self, claims: AuthClaims, message: Notification) -> DeliveryRef:
        if message.channel == "whatsapp":
            to_value = f"whatsapp:{message.recipient}"
            from_value = f"whatsapp:{self._from_number}"
        else:
            to_value = message.recipient
            from_value = self._from_number

        async with httpx.AsyncClient(timeout=self._timeout) as client:
            try:
                response = await client.post(
                    f"{_TWILIO_API_BASE}/Accounts/{self._account_sid}/Messages.json",
                    auth=(self._account_sid, self._auth_token),
                    data={"To": to_value, "From": from_value, "Body": message.body},
                )
            except httpx.HTTPError as exc:
                # Transient (connect error / timeout / read error) -- caught
                # and re-raised as RuntimeError exactly like
                # GoogleCalendarProvider's network-error handling. NEVER
                # include the auth token, phone number, or body.
                raise RuntimeError(
                    f"Twilio Messages request failed: {exc.__class__.__name__}"
                ) from exc

        if response.status_code == 429 or response.status_code >= _TWILIO_TRANSIENT_STATUS_FLOOR:
            # Transient: rate-limited or server-side -- propagates so Celery
            # retries under the same job_id.
            raise RuntimeError(
                f"Twilio Messages returned transient status: {response.status_code}"
            )

        if not (200 <= response.status_code < 300):
            # Deterministic: any other 4xx (401 invalid credentials, 400
            # malformed To/From, 403). Include Twilio's numeric `code` when
            # available -- NEVER the phone number or body.
            twilio_code = None
            try:
                twilio_code = response.json().get("code")
            except Exception:  # noqa: BLE001 -- malformed/non-JSON body is still deterministic
                twilio_code = None
            code_suffix = f" (twilio code={twilio_code})" if twilio_code is not None else ""
            raise NotificationDeliveryError(
                f"Twilio Messages request failed with status {response.status_code}"
                f"{code_suffix}",
                code="NOTIFICATION_DELIVERY_FAILED",
            )

        data = response.json()
        return DeliveryRef(provider="twilio", ref=str(data["sid"]))


def validate_recipient_for_channel(channel: str, recipient: str) -> None:
    """Loose per-channel recipient validation (Decision 5).

    ``email`` -> must contain ``@``. ``sms``/``whatsapp`` -> must match a
    loose E.164 shape. Deliberately loose -- no phone-number libraries, no
    carrier lookup; the authoritative validation is Twilio's own ``400``
    (handled as deterministic by ``TwilioNotificationProvider``). Raises
    ``ValidationError`` (``code="INVALID_RECIPIENT"``, -> 422) on mismatch.
    """
    if channel == "email":
        if "@" not in recipient:
            raise ValidationError(
                "Invalid recipient for channel 'email': expected an email address.",
                code="INVALID_RECIPIENT",
            )
        return

    if channel in ("sms", "whatsapp"):
        if not _PHONE_RE.match(recipient):
            raise ValidationError(
                f"Invalid recipient for channel {channel!r}: expected a loose E.164 "
                "phone number.",
                code="INVALID_RECIPIENT",
            )
        return

    raise ValidationError(
        f"Unsupported notification channel: {channel!r}.",
        code="INVALID_RECIPIENT",
    )


def notification_provider_for(
    config: NotificationConfig, *, timeout: float
) -> NotificationProvider:
    """Select a ``NotificationProvider`` implementation for the tenant's config.

    Raises ``NotificationConfigError`` (a ``ValidationError``) for an unknown
    ``provider`` value or missing required fields -- deterministic, never
    retried, never a network call. Mirrors
    ``api.scheduling.calendar.calendar_provider_for``.
    """
    if config.provider == "log":
        return LogNotificationProvider()

    if config.provider == "smtp":
        if not config.from_address or not config.smtp_host or not config.smtp_port:
            raise NotificationConfigError(
                "SMTP provider requires from_address, smtp_host, and smtp_port.",
                code="NOTIFICATION_CONFIG_ERROR",
            )
        return SmtpEmailProvider(
            host=config.smtp_host,
            port=config.smtp_port,
            use_tls=config.smtp_use_tls,
            username=config.smtp_username,
            password=config.credentials,
            from_address=config.from_address,
            from_name=config.from_name,
            timeout=timeout,
        )

    if config.provider == "twilio":
        if not config.twilio_account_sid or not config.credentials or not config.twilio_from:
            raise NotificationConfigError(
                "Twilio provider requires twilio_account_sid, credentials (Auth "
                "Token), and twilio_from.",
                code="NOTIFICATION_CONFIG_ERROR",
            )
        return TwilioNotificationProvider(
            account_sid=config.twilio_account_sid,
            auth_token=config.credentials,
            from_number=config.twilio_from,
            timeout=timeout,
        )

    raise NotificationConfigError(
        f"Unsupported notification provider: {config.provider!r}.",
        code="NOTIFICATION_PROVIDER_NOT_SUPPORTED",
    )

"""Application exception hierarchy.

One base ``AppException`` plus a small set of typed subclasses. Each carries a
stable ``code`` (UPPER_SNAKE_CASE, part of the API contract) and an ``http_status``.
The gateway's centralized error middleware (see the ``api-gateway-bff`` skill) catches
these, attaches a correlation id, logs full detail server-side, and returns the
user-safe ``{error_code, message, correlation_id}`` payload built from ``to_dict()``.

Rules (CLAUDE.md §3):
- Messages are user-safe; never embed secrets/PII.
- ``details`` is server-side context only and is NOT included in ``to_dict()``.
"""
from __future__ import annotations

from typing import Any


class AppException(Exception):
    """Base for every expected, mapped application error."""

    code: str = "INTERNAL_ERROR"
    http_status: int = 500
    default_message: str = "An unexpected error occurred."

    def __init__(
        self,
        message: str | None = None,
        *,
        code: str | None = None,
        details: dict[str, Any] | None = None,
    ) -> None:
        self.message: str = message if message is not None else self.default_message
        if code is not None:
            self.code = code
        self.details: dict[str, Any] = details or {}
        super().__init__(self.message)

    def to_dict(self) -> dict[str, Any]:
        """User-safe payload. The middleware adds ``correlation_id``."""
        return {"error_code": self.code, "message": self.message}


class NotFoundError(AppException):
    code = "NOT_FOUND"
    http_status = 404
    default_message = "The requested resource was not found."


class ValidationError(AppException):
    code = "VALIDATION_ERROR"
    http_status = 422
    default_message = "The request was invalid."


class AuthenticationError(AppException):
    """No / invalid credentials — the request is unauthenticated (401)."""

    code = "UNAUTHENTICATED"
    http_status = 401
    default_message = "Authentication is required."


class AuthorizationError(AppException):
    """Authenticated but not permitted — including cross-tenant access (403)."""

    code = "FORBIDDEN"
    http_status = 403
    default_message = "You are not authorized to perform this action."


class ConflictError(AppException):
    """The request conflicts with the current state of the target resource (409).

    Distinct from ``ValidationError`` (422, malformed/invalid input): a
    ``ConflictError`` is well-formed but cannot be honored because the
    target already exists in an incompatible state (e.g. a Lead that has
    already been converted to a Contact) -- an honest, informative error
    rather than a silent no-op or 200 (no-silent-fallback, CLAUDE.md §3).
    """

    code = "CONFLICT"
    http_status = 409
    default_message = "The request conflicts with the current state of the resource."


class RateLimitError(AppException):
    code = "RATE_LIMITED"
    http_status = 429
    default_message = "Too many requests. Please try again later."

    def __init__(
        self,
        message: str | None = None,
        *,
        code: str | None = None,
        details: dict[str, Any] | None = None,
        retry_after: int | None = None,
    ) -> None:
        super().__init__(message, code=code, details=details)
        self.retry_after: int | None = retry_after


class InternalServerError(AppException):
    code = "INTERNAL_ERROR"
    http_status = 500
    default_message = "An unexpected error occurred."

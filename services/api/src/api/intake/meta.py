"""Meta Lead Ads: webhook signature check and the Graph API lead fetch (A10 I7).

Meta signs each webhook body with the app secret (``X-Hub-Signature-256:
sha256=<hex>``) and sends only ids; the lead's answers are fetched from the Graph
API with the page's access token. Network errors and 5xx/429 are transient (the
outbox retries); other errors are final.
"""
from __future__ import annotations

import hashlib
import hmac
from typing import Protocol

import httpx
from common.errors import ValidationError

from api.agents.executors import TransientExecutorError
from api.config import get_api_settings


def signature_ok(raw: bytes, header: str | None, app_secret: str) -> bool:
    if not header or not header.startswith("sha256="):
        return False
    expected = hmac.new(app_secret.encode(), raw, hashlib.sha256).hexdigest()
    return hmac.compare_digest(header.removeprefix("sha256="), expected)


class MetaLeadError(ValidationError):
    code = "META_LEAD_FETCH_FAILED"


class GraphClient(Protocol):
    async def fetch_lead(self, leadgen_id: str, page_token: str) -> dict[str, str]: ...


class HttpGraphClient:
    """``{field name: first value}`` of one lead, plus ``form_id`` / ``campaign_name``."""

    def __init__(self, *, transport: httpx.AsyncBaseTransport | None = None) -> None:
        self._transport = transport

    async def fetch_lead(self, leadgen_id: str, page_token: str) -> dict[str, str]:
        version = get_api_settings().meta_graph_version
        url = f"https://graph.facebook.com/{version}/{leadgen_id}"
        try:
            async with httpx.AsyncClient(timeout=10.0, transport=self._transport) as client:
                response = await client.get(url, params={
                    "access_token": page_token,
                    "fields": "field_data,form_id,campaign_name,created_time"})
        except httpx.HTTPError as exc:
            raise TransientExecutorError(f"Meta unreachable: {type(exc).__name__}") from exc
        if response.status_code == 429 or response.status_code >= 500:
            raise TransientExecutorError(f"Meta HTTP {response.status_code}")
        if response.status_code >= 400:
            raise MetaLeadError(f"Meta refused the lead fetch (HTTP {response.status_code}).",
                                code=f"META_{response.status_code}")
        body = response.json()
        fields = {str(f.get("name")): str((f.get("values") or [""])[0])
                  for f in body.get("field_data", []) if f.get("name")}
        if body.get("form_id"):
            fields["form"] = str(body["form_id"])
        if body.get("campaign_name"):
            fields["campaign"] = str(body["campaign_name"])
        return fields


# Swappable for tests (a fake instead of Meta's API).
graph_client: GraphClient = HttpGraphClient()

"""HTTP client with retries/backoff used by REST-based connectors."""

from __future__ import annotations

import asyncio
import random
from typing import Any

import httpx

from ontology_platform.config import Settings
from ontology_platform.errors import ConnectorError, TransientConnectorError
from ontology_platform.observability import get_logger

log = get_logger(__name__)

RETRYABLE_STATUS = {408, 425, 429, 500, 502, 503, 504}


class RetryingHttpClient:
    def __init__(
        self,
        base_url: str,
        settings: Settings,
        headers: dict[str, str] | None = None,
        transport: httpx.AsyncBaseTransport | None = None,
        auth: httpx.Auth | tuple[str, str] | None = None,
        verify: bool | str = True,
    ) -> None:
        self.settings = settings
        self._client = httpx.AsyncClient(
            base_url=base_url,
            headers={"User-Agent": "ontology-platform-connector/0.1", **(headers or {})},
            timeout=settings.http_timeout_seconds,
            transport=transport,
            auth=auth,
            verify=verify,
            follow_redirects=True,
        )

    async def request(self, method: str, url: str, **kwargs: Any) -> httpx.Response:
        attempts = self.settings.http_max_retries + 1
        last_error: Exception | None = None
        for attempt in range(1, attempts + 1):
            try:
                resp = await self._client.request(method, url, **kwargs)
            except (httpx.TransportError, httpx.TimeoutException) as exc:
                last_error = exc
                log.warning("http transport error", extra={"url": url, "attempt": attempt, "error": str(exc)})
            else:
                if resp.status_code < 400:
                    return resp
                if resp.status_code not in RETRYABLE_STATUS:
                    raise ConnectorError(f"{method} {url} failed with HTTP {resp.status_code}: {resp.text[:300]}")
                last_error = TransientConnectorError(f"HTTP {resp.status_code} from {url}")
                log.warning("retryable http status", extra={"url": url, "status": resp.status_code, "attempt": attempt})
                retry_after = _retry_after_seconds(resp)
                if retry_after is not None and attempt < attempts:
                    await asyncio.sleep(min(retry_after, 60.0))
                    continue
            if attempt < attempts:
                delay = self.settings.http_backoff_base_seconds * (2 ** (attempt - 1))
                await asyncio.sleep(delay + random.uniform(0, delay / 4))
        raise TransientConnectorError(f"{method} {url} failed after {attempts} attempts: {last_error}")

    async def get_json(self, url: str, **kwargs: Any) -> Any:
        resp = await self.request("GET", url, **kwargs)
        try:
            return resp.json()
        except ValueError as exc:
            raise ConnectorError(f"non-JSON response from {url}") from exc

    async def get_text(self, url: str, **kwargs: Any) -> str | None:
        """GET returning text, or None on 404 (optional files)."""
        try:
            resp = await self.request("GET", url, **kwargs)
        except ConnectorError as exc:
            if "HTTP 404" in str(exc):
                return None
            raise
        return resp.text

    async def post_json(self, url: str, json: Any, **kwargs: Any) -> Any:
        resp = await self.request("POST", url, json=json, **kwargs)
        return resp.json()

    async def aclose(self) -> None:
        await self._client.aclose()


def _retry_after_seconds(resp: httpx.Response) -> float | None:
    value = resp.headers.get("Retry-After")
    if value is None:
        return None
    try:
        return max(0.0, float(value))
    except ValueError:
        return None


def parse_link_next(link_header: str | None) -> str | None:
    """Return the rel="next" URL of an RFC 5988 Link header (GitHub pagination)."""
    if not link_header:
        return None
    for part in link_header.split(","):
        section = part.split(";")
        if len(section) < 2:
            continue
        url = section[0].strip().strip("<>")
        if any(s.strip() == 'rel="next"' for s in section[1:]):
            return url
    return None

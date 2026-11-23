"""Confluence (Atlassian Cloud / Data Center) connector using the REST content search API."""

from __future__ import annotations

import base64
import html
import re
from collections.abc import AsyncIterator
from datetime import datetime

from pydantic import BaseModel, Field

from ontology_platform.connectors.base import Connector, DocumentPayload, RecordKind, SourceRecord, register_connector
from ontology_platform.connectors.http import RetryingHttpClient

_TAG = re.compile(r"<[^>]+>")
_BLOCK = re.compile(r"</?(p|div|h[1-6]|li|tr|br|table|ul|ol|pre)[^>]*>", re.I)
_WS = re.compile(r"[ \t]+")


def html_to_text(markup: str) -> str:
    text = _BLOCK.sub("\n", markup)
    text = _TAG.sub(" ", text)
    text = html.unescape(text)
    lines = [_WS.sub(" ", line).strip() for line in text.splitlines()]
    return "\n".join(line for line in lines if line)


class ConfluenceConfig(BaseModel):
    base_url: str = Field(description="e.g. https://acme.atlassian.net (Cloud) or https://confluence.acme.internal")
    spaces: list[str] = Field(min_length=1)
    cloud: bool = True
    page_size: int = Field(default=50, ge=1, le=200)


@register_connector
class ConfluenceConnector(Connector):
    """Secret: ``email:api_token`` (Cloud basic auth) or a personal access token (Data Center)."""

    type = "confluence"
    source_system = "confluence"
    config_model = ConfluenceConfig
    supports_incremental = True
    config: ConfluenceConfig

    def _headers(self) -> dict[str, str]:
        secret = self.secret or ""
        if ":" in secret:
            return {"Authorization": "Basic " + base64.b64encode(secret.encode()).decode()}
        return {"Authorization": f"Bearer {secret}"}

    @property
    def _api_prefix(self) -> str:
        return "/wiki/rest/api" if self.config.cloud else "/rest/api"

    async def fetch(self, since: str | None = None) -> AsyncIterator[SourceRecord]:
        http = RetryingHttpClient(self.config.base_url, self.settings, headers=self._headers(), transport=self._transport)
        self._cursor = since
        try:
            for space in self.config.spaces:
                cql = f'space="{space}" and type=page'
                if since:
                    ts = datetime.fromisoformat(since.replace("Z", "+00:00")).strftime("%Y/%m/%d %H:%M")
                    cql += f' and lastmodified > "{ts}"'
                url: str | None = f"{self._api_prefix}/content/search"
                params: dict | None = {
                    "cql": cql,
                    "limit": self.config.page_size,
                    "expand": "body.storage,version,metadata.labels,space",
                }
                while url:
                    data = await http.get_json(url, params=params)
                    params = None
                    base = (data.get("_links") or {}).get("base") or self.config.base_url
                    for page in data.get("results", []):
                        body = ((page.get("body") or {}).get("storage") or {}).get("value", "")
                        labels = [
                            lbl.get("name")
                            for lbl in (((page.get("metadata") or {}).get("labels") or {}).get("results") or [])
                            if lbl.get("name")
                        ]
                        webui = (page.get("_links") or {}).get("webui")
                        page_url = f"{base}{webui}" if webui else None
                        updated = (page.get("version") or {}).get("when")
                        payload = DocumentPayload(
                            source="confluence",
                            external_id=str(page["id"]),
                            title=page.get("title", ""),
                            url=page_url,
                            space=space,
                            labels=labels,
                            body=html_to_text(body),
                        )
                        if updated and (self._cursor is None or updated > self._cursor):
                            self._cursor = updated
                        yield SourceRecord(
                            kind=RecordKind.DOCUMENT,
                            external_id=f"confluence:{page['id']}",
                            source_system="confluence",
                            source_url=page_url,
                            payload=payload.model_dump(),
                        )
                    nxt = (data.get("_links") or {}).get("next")
                    if nxt and self.config.cloud and nxt.startswith("/rest/"):
                        nxt = "/wiki" + nxt
                    url = nxt
        finally:
            await http.aclose()

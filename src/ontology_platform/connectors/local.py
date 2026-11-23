"""File-based connectors.

* ``catalog_file`` — production connector for a curated enterprise catalog (customers, products,
  ownership) exported from a CMDB/CRM as YAML/JSON on disk.
* ``fixture`` — **local test adapter**. Replays pre-recorded normalized records (as a real
  connector would emit them) from a directory. Used for the sample dataset, demos and tests when
  credentials for the real systems are unavailable. Records keep their declared
  ``source_system`` but provenance always shows ``connector_type = fixture``.
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from datetime import datetime
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, Field

from ontology_platform.connectors.base import Connector, RecordKind, SourceRecord, register_connector
from ontology_platform.errors import ConnectorError


def _load_structured(path: Path) -> Any:
    text = path.read_text(encoding="utf-8")
    if path.suffix.lower() == ".json":
        return json.loads(text)
    return yaml.safe_load(text)


def _resolve_path(raw: str) -> Path:
    path = Path(raw).expanduser()
    if not path.exists():
        raise ConnectorError(f"path '{raw}' does not exist")
    return path


class CatalogFileConfig(BaseModel):
    path: str = Field(description="Catalog file or directory of *.yaml/*.json catalog files")


@register_connector
class CatalogFileConnector(Connector):
    type = "catalog_file"
    source_system = "catalog"
    config_model = CatalogFileConfig
    requires_secret = False
    config: CatalogFileConfig

    async def fetch(self, since: str | None = None) -> AsyncIterator[SourceRecord]:
        root = _resolve_path(self.config.path)
        files = sorted(p for p in root.rglob("*") if p.suffix.lower() in {".yaml", ".yml", ".json"}) if root.is_dir() else [root]
        for f in files:
            data = _load_structured(f) or {}
            if not isinstance(data, dict):
                raise ConnectorError(f"catalog file {f} must contain a mapping")
            yield SourceRecord(
                kind=RecordKind.CATALOG,
                external_id=f"catalog:{f.name}",
                source_system="catalog",
                source_url=f.resolve().as_uri(),
                payload={"entities": data.get("entities", []), "relationships": data.get("relationships", [])},
            )


class FixtureConfig(BaseModel):
    path: str = Field(description="Directory (or file) containing fixture YAML with a top-level 'records' list")
    source_systems: list[str] = Field(default_factory=list, description="Only replay records of these systems")


@register_connector
class FixtureConnector(Connector):
    type = "fixture"
    source_system = "fixture"
    config_model = FixtureConfig
    requires_secret = False
    supports_incremental = True
    config: FixtureConfig

    async def fetch(self, since: str | None = None) -> AsyncIterator[SourceRecord]:
        root = _resolve_path(self.config.path)
        files = sorted(p for p in root.rglob("*") if p.suffix.lower() in {".yaml", ".yml", ".json"}) if root.is_dir() else [root]
        since_dt = datetime.fromisoformat(since) if since else None
        self._cursor = since
        for f in files:
            data = _load_structured(f) or {}
            for raw in data.get("records", []):
                record = SourceRecord.model_validate(raw)
                if self.config.source_systems and record.source_system not in self.config.source_systems:
                    continue
                if since_dt and record.updated_at and record.updated_at <= since_dt:
                    continue
                if record.updated_at:
                    iso = record.updated_at.isoformat()
                    if self._cursor is None or datetime.fromisoformat(iso) > datetime.fromisoformat(self._cursor):
                        self._cursor = iso
                yield record


class PushConfig(BaseModel):
    description: str | None = None


@register_connector
class PushConnector(Connector):
    """Receives normalized records via ``POST /api/v1/connectors/{id}/records`` (webhooks, CI jobs).

    It has nothing to pull; ``fetch`` refuses so a scheduled full sync cannot wipe pushed facts.
    """

    type = "push"
    source_system = "push"
    config_model = PushConfig
    requires_secret = False
    config: PushConfig

    async def fetch(self, since: str | None = None) -> AsyncIterator[SourceRecord]:
        raise ConnectorError("push connectors receive records via the API; they cannot be synced")
        yield  # pragma: no cover  (makes this an async generator)

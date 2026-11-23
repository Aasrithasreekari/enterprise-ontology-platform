"""Connector framework.

A connector talks to one enterprise system and yields *normalized* ``SourceRecord``s. The
record ``kind`` (not the source system) decides which extractor runs, so e.g. GitHub, GitLab and
Bitbucket repositories share one extraction path.
"""

from __future__ import annotations

import hashlib
import json
import os
from abc import ABC, abstractmethod
from collections.abc import AsyncIterator
from datetime import datetime
from enum import StrEnum
from pathlib import Path
from typing import Any, ClassVar

import httpx
from pydantic import BaseModel, ConfigDict, Field, ValidationError as PydanticValidationError

from ontology_platform.config import Settings
from ontology_platform.errors import ConnectorError, ValidationError


class RecordKind(StrEnum):
    REPOSITORY = "repository"
    DOCUMENT = "document"
    K8S_WORKLOAD = "k8s_workload"
    CLOUD_RESOURCE = "cloud_resource"
    CATALOG = "catalog"


class SourceRecord(BaseModel):
    kind: RecordKind
    external_id: str = Field(min_length=1)
    source_system: str
    source_url: str | None = None
    updated_at: datetime | None = None
    payload: dict[str, Any]

    @property
    def record_key(self) -> str:
        return f"{self.kind.value}:{self.external_id}"

    def content_hash(self) -> str:
        body = json.dumps({"kind": self.kind, "payload": self.payload}, sort_keys=True, default=str)
        return hashlib.sha256(body.encode()).hexdigest()


# ----------------------------------------------------------------------------- normalized payloads
class RepositoryPayload(BaseModel):
    """Normalized repository shape shared by GitHub / GitLab / Bitbucket."""

    model_config = ConfigDict(extra="allow")
    provider: str
    path: str  # e.g. "acme/payments-service"
    name: str
    url: str | None = None
    description: str | None = None
    topics: list[str] = Field(default_factory=list)
    default_branch: str | None = None
    language: str | None = None
    archived: bool = False
    files: dict[str, str] = Field(default_factory=dict, description="Selected file path → text content")


class DocumentPayload(BaseModel):
    model_config = ConfigDict(extra="allow")
    source: str  # confluence, readme, ...
    external_id: str
    title: str
    url: str | None = None
    space: str | None = None
    labels: list[str] = Field(default_factory=list)
    body: str = ""


class K8sWorkloadPayload(BaseModel):
    model_config = ConfigDict(extra="allow")
    cluster: str
    cluster_provider: str | None = None
    namespace: str
    name: str
    kind: str = "Deployment"
    labels: dict[str, str] = Field(default_factory=dict)
    annotations: dict[str, str] = Field(default_factory=dict)
    images: list[str] = Field(default_factory=list)
    replicas: int | None = None


class CloudResourcePayload(BaseModel):
    model_config = ConfigDict(extra="allow")
    provider: str  # aws | azure | gcp
    resource_id: str
    resource_type: str
    name: str
    region: str | None = None
    account: str | None = None
    tags: dict[str, str] = Field(default_factory=dict)


PAYLOAD_MODELS: dict[RecordKind, type[BaseModel]] = {
    RecordKind.REPOSITORY: RepositoryPayload,
    RecordKind.DOCUMENT: DocumentPayload,
    RecordKind.K8S_WORKLOAD: K8sWorkloadPayload,
    RecordKind.CLOUD_RESOURCE: CloudResourcePayload,
}


def validate_payload(record: SourceRecord) -> BaseModel | dict[str, Any]:
    model = PAYLOAD_MODELS.get(record.kind)
    if model is None:
        return record.payload
    try:
        return model.model_validate(record.payload)
    except PydanticValidationError as exc:
        raise ValidationError(f"invalid {record.kind.value} payload for {record.external_id}: {exc}") from exc


# ----------------------------------------------------------------------------- secrets
def resolve_secret(secret_ref: str | None) -> str | None:
    """Resolve a secret reference. Supported: ``env:NAME`` and ``file:/path``. Secrets never live in the DB."""
    if not secret_ref:
        return None
    scheme, _, target = secret_ref.partition(":")
    if scheme == "env":
        value = os.environ.get(target)
        if value is None:
            raise ConnectorError(f"secret env var '{target}' is not set")
        return value
    if scheme == "file":
        path = Path(target)
        if not path.is_file():
            raise ConnectorError(f"secret file '{target}' not found")
        return path.read_text(encoding="utf-8").strip()
    raise ValidationError(f"unsupported secret reference scheme '{scheme}' (use env: or file:)")


# ----------------------------------------------------------------------------- connector base
class Connector(ABC):
    type: ClassVar[str]
    source_system: ClassVar[str]
    config_model: ClassVar[type[BaseModel]]
    supports_incremental: ClassVar[bool] = False
    requires_secret: ClassVar[bool] = True

    def __init__(
        self,
        config: dict[str, Any],
        secret: str | None,
        settings: Settings,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        try:
            self.config = self.config_model.model_validate(config)
        except PydanticValidationError as exc:
            raise ValidationError(f"invalid {self.type} connector config: {exc}") from exc
        if self.requires_secret and not secret:
            raise ConnectorError(f"{self.type} connector requires a secret (set secret_ref)")
        self.secret = secret
        self.settings = settings
        self._transport = transport
        self._cursor: str | None = None

    @abstractmethod
    def fetch(self, since: str | None = None) -> AsyncIterator[SourceRecord]:
        """Yield normalized records. ``since`` is the cursor from the previous incremental run."""

    def next_cursor(self) -> str | None:
        """Cursor to persist after a successful run (incremental connectors)."""
        return self._cursor

    async def aclose(self) -> None:  # pragma: no cover - default no-op
        return None


_REGISTRY: dict[str, type[Connector]] = {}


def register_connector(cls: type[Connector]) -> type[Connector]:
    _REGISTRY[cls.type] = cls
    return cls


def connector_class(connector_type: str) -> type[Connector]:
    _ensure_builtin_connectors_loaded()
    try:
        return _REGISTRY[connector_type]
    except KeyError as exc:
        raise ValidationError(f"unknown connector type '{connector_type}'; known: {sorted(_REGISTRY)}") from exc


def available_connector_types() -> list[str]:
    _ensure_builtin_connectors_loaded()
    return sorted(_REGISTRY)


def _ensure_builtin_connectors_loaded() -> None:
    from ontology_platform.connectors import builtin  # noqa: F401  (registers on import)

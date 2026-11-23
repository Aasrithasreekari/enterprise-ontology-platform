"""Administrative operations: tenants and connector registrations."""

from __future__ import annotations

import re
from typing import Any

from pydantic import ValidationError as PydanticValidationError
from sqlalchemy import select

from ontology_platform.connectors.base import connector_class
from ontology_platform.errors import ConflictError, NotFoundError, ValidationError
from ontology_platform.storage.db import Database
from ontology_platform.storage.orm import ConnectorConfig, IngestionRun, Tenant

_TENANT_ID = re.compile(r"^[a-z0-9][a-z0-9-]{1,62}$")
_SECRET_REF = re.compile(r"^(env:[A-Za-z_][A-Za-z0-9_]*|file:.+)$")


def create_tenant(db: Database, tenant_id: str, name: str) -> dict[str, Any]:
    if not _TENANT_ID.match(tenant_id):
        raise ValidationError("tenant id must be 2-63 chars of lowercase letters, digits and '-'")
    with db.session() as s:
        if s.get(Tenant, tenant_id) is not None:
            raise ConflictError(f"tenant '{tenant_id}' already exists")
        s.add(Tenant(id=tenant_id, name=name))
    return {"id": tenant_id, "name": name}


def ensure_tenant(db: Database, tenant_id: str, name: str | None = None) -> None:
    with db.session() as s:
        if s.get(Tenant, tenant_id) is None:
            s.add(Tenant(id=tenant_id, name=name or tenant_id))


def connector_to_dict(c: ConnectorConfig) -> dict[str, Any]:
    return {
        "id": c.id,
        "name": c.name,
        "type": c.type,
        "config": c.config,
        "secret_ref": c.secret_ref,  # a reference (env var / file path), never the secret value
        "enabled": c.enabled,
        "cursor": c.cursor,
        "created_at": c.created_at.isoformat() if c.created_at else None,
    }


def register_connector(
    db: Database,
    tenant_id: str,
    name: str,
    connector_type: str,
    config: dict[str, Any],
    secret_ref: str | None = None,
    enabled: bool = True,
) -> dict[str, Any]:
    cls = connector_class(connector_type)
    try:
        cls.config_model.model_validate(config)
    except PydanticValidationError as exc:
        raise ValidationError(f"invalid {connector_type} config: {exc}") from exc
    if secret_ref and not _SECRET_REF.match(secret_ref):
        raise ValidationError("secret_ref must be 'env:VAR_NAME' or 'file:/path' — never the secret itself")
    if cls.requires_secret and not secret_ref:
        raise ValidationError(f"{connector_type} connector requires secret_ref")
    with db.session() as s:
        if s.get(Tenant, tenant_id) is None:
            raise NotFoundError(f"tenant '{tenant_id}' not found")
        existing = s.scalars(select(ConnectorConfig).where(ConnectorConfig.tenant_id == tenant_id, ConnectorConfig.name == name)).first()
        if existing is not None:
            raise ConflictError(f"connector '{name}' already exists")
        c = ConnectorConfig(tenant_id=tenant_id, name=name, type=connector_type, config=config, secret_ref=secret_ref, enabled=enabled)
        s.add(c)
        s.flush()
        return connector_to_dict(c)


def list_connectors(db: Database, tenant_id: str) -> list[dict[str, Any]]:
    with db.session() as s:
        rows = s.scalars(select(ConnectorConfig).where(ConnectorConfig.tenant_id == tenant_id).order_by(ConnectorConfig.name))
        return [connector_to_dict(c) for c in rows]


def get_connector_by_name(db: Database, tenant_id: str, name: str) -> dict[str, Any]:
    with db.session() as s:
        c = s.scalars(select(ConnectorConfig).where(ConnectorConfig.tenant_id == tenant_id, ConnectorConfig.name == name)).first()
        if c is None:
            raise NotFoundError(f"connector '{name}' not found")
        return connector_to_dict(c)


def run_to_dict(r: IngestionRun) -> dict[str, Any]:
    return {
        "id": r.id,
        "connector_id": r.connector_id,
        "mode": r.mode,
        "status": r.status,
        "stats": r.stats,
        "error": r.error,
        "triggered_by": r.triggered_by,
        "started_at": r.started_at.isoformat() if r.started_at else None,
        "finished_at": r.finished_at.isoformat() if r.finished_at else None,
    }


def list_runs(db: Database, tenant_id: str, connector_id: str | None = None, limit: int = 50) -> list[dict[str, Any]]:
    with db.session() as s:
        stmt = select(IngestionRun).where(IngestionRun.tenant_id == tenant_id)
        if connector_id:
            stmt = stmt.where(IngestionRun.connector_id == connector_id)
        return [run_to_dict(r) for r in s.scalars(stmt.order_by(IngestionRun.started_at.desc()).limit(limit))]


def get_run(db: Database, tenant_id: str, run_id: str) -> dict[str, Any]:
    with db.session() as s:
        r = s.get(IngestionRun, run_id)
        if r is None or r.tenant_id != tenant_id:
            raise NotFoundError(f"run '{run_id}' not found")
        return run_to_dict(r)

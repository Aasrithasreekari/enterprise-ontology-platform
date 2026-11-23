from __future__ import annotations

import asyncio
import logging
from pathlib import Path

import pytest

from ontology_platform import admin
from ontology_platform.auth.principal import Principal
from ontology_platform.config import Settings
from ontology_platform.service import PlatformService
from ontology_platform.storage.db import Database

ROOT = Path(__file__).resolve().parents[1]
SAMPLE = ROOT / "sample_data"

logging.disable(logging.INFO)


def make_settings(**overrides) -> Settings:
    base = {"database_url": "sqlite://", "http_backoff_base_seconds": 0.0, "http_max_retries": 2, "log_json": False}
    base.update(overrides)
    return Settings(_env_file=None, **base)


def new_service(settings: Settings | None = None) -> PlatformService:
    settings = settings or make_settings()
    db = Database(settings.database_url)
    db.create_all()
    return PlatformService(db, settings)


def operator(tenant: str, roles=("admin",), customers=None) -> Principal:
    return Principal(subject=f"test:{tenant}", tenant_id=tenant, roles=frozenset(roles), allowed_customers=frozenset(customers) if customers is not None else None)


def sync(svc: PlatformService, tenant: str, connector_id: str, mode: str = "full") -> dict:
    p = operator(tenant)
    run_id = svc.start_sync(p, connector_id, mode)
    return asyncio.run(svc.execute_sync(p, connector_id, run_id, mode))


def seed(svc: PlatformService) -> dict[str, str]:
    """Load the sample dataset; returns connector ids."""
    ids = {}
    admin.create_tenant(svc.db, "northwind", "Northwind")
    admin.create_tenant(svc.db, "contoso", "Contoso")
    for tenant, name, ctype, path in [
        ("northwind", "crm", "catalog_file", SAMPLE / "northwind" / "catalog"),
        ("northwind", "fixtures", "fixture", SAMPLE / "northwind" / "fixtures"),
        ("contoso", "fixtures", "fixture", SAMPLE / "contoso" / "fixtures"),
    ]:
        c = admin.register_connector(svc.db, tenant, name, ctype, {"path": str(path)})
        result = sync(svc, tenant, c["id"])
        assert result["status"] == "succeeded", result
        ids[f"{tenant}/{name}"] = c["id"]
    return ids


@pytest.fixture(scope="session")
def seeded() -> PlatformService:
    """Read-only shared platform with the sample dataset loaded. Do not mutate."""
    svc = new_service()
    svc.connector_ids = seed(svc)  # type: ignore[attr-defined]
    return svc


@pytest.fixture
def fresh() -> PlatformService:
    svc = new_service()
    admin.create_tenant(svc.db, "t1", "Tenant One")
    return svc


@pytest.fixture
def nw_admin() -> Principal:
    return operator("northwind")


@pytest.fixture
def nw_viewer() -> Principal:
    return operator("northwind", roles=("viewer",))


@pytest.fixture
def acme_viewer() -> Principal:
    return operator("northwind", roles=("viewer",), customers=["acme"])


@pytest.fixture
def contoso_viewer() -> Principal:
    return operator("contoso", roles=("viewer",))

from __future__ import annotations

import asyncio
import contextlib
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


# ----------------------------------------------------------------------------- security / concurrency helpers
def auth_header(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


@contextlib.asynccontextmanager
async def mcp_http(svc: PlatformService):
    """Serve the MCP server over streamable HTTP in-process (no default principal, like production).

    Yields ``connect(token)``, an async context manager returning an MCP client that sends
    ``Authorization: Bearer <token>`` (or no Authorization header when token is None)."""
    from mcp.client import Client
    from mcp.client.streamable_http import httpx2, streamable_http_client
    from mcp.server.transport_security import TransportSecuritySettings

    from ontology_platform.mcp_server.server import build_mcp_server

    security = TransportSecuritySettings(allowed_hosts=["testserver"], allowed_origins=["http://testserver"])
    app = build_mcp_server(svc).streamable_http_app(stateless_http=True, transport_security=security)

    @contextlib.asynccontextmanager
    async def connect(token: str | None):
        headers = auth_header(token) if token is not None else {}
        async with httpx2.AsyncClient(transport=httpx2.ASGITransport(app=app), base_url="http://testserver", headers=headers) as http:
            async with Client(streamable_http_client("http://testserver/mcp", http_client=http)) as client:
                yield client

    async with app.router.lifespan_context(app):
        yield connect


def seed_tenants(svc: PlatformService, prefix: str) -> dict[str, str]:
    """Like ``seed`` but with tenant ids ``<prefix>northwind`` / ``<prefix>contoso`` (for shared databases)."""
    ids = {}
    for tenant in ("northwind", "contoso"):
        admin.create_tenant(svc.db, f"{prefix}{tenant}", tenant.title())
    for tenant, name, ctype, path in [
        ("northwind", "crm", "catalog_file", SAMPLE / "northwind" / "catalog"),
        ("northwind", "fixtures", "fixture", SAMPLE / "northwind" / "fixtures"),
        ("contoso", "fixtures", "fixture", SAMPLE / "contoso" / "fixtures"),
    ]:
        c = admin.register_connector(svc.db, f"{prefix}{tenant}", name, ctype, {"path": str(path)})
        result = sync(svc, f"{prefix}{tenant}", c["id"])
        assert result["status"] == "succeeded", result
        ids[f"{tenant}/{name}"] = c["id"]
    return ids


def purge_tenants(db: Database, tenant_ids: list[str]) -> None:
    """Delete every row belonging to the given tenants (children first), then the tenants."""
    from sqlalchemy import inspect as sa_inspect

    from ontology_platform.storage.orm import Base

    if not tenant_ids:
        return
    existing = set(sa_inspect(db.engine).get_table_names())
    with db.engine.begin() as conn:
        for table in reversed(Base.metadata.sorted_tables):
            if table.name in existing and table.name != "tenants" and "tenant_id" in table.c:
                conn.execute(table.delete().where(table.c.tenant_id.in_(tenant_ids)))
        tenants = Base.metadata.tables["tenants"]
        conn.execute(tenants.delete().where(tenants.c.id.in_(tenant_ids)))

"""PostgreSQL integration tests. Run only when OP_TEST_DATABASE_URL is set, e.g.

    OP_TEST_DATABASE_URL=postgresql+psycopg://user:pass@localhost:5432/ontology pytest tests/test_postgres.py

The tests use their own tenant ids (prefix ``pgt<pid>-``) and delete those rows afterwards, so they
can run against a shared development database without touching other data.
"""

from __future__ import annotations

import asyncio
import os
from concurrent.futures import ThreadPoolExecutor

import pytest
from fastapi.testclient import TestClient

from conftest import auth_header, make_settings, operator, purge_tenants, seed_tenants
from ontology_platform.api.app import create_app
from ontology_platform.cli import migrate
from ontology_platform.errors import ConflictError, NotFoundError
from ontology_platform.service import PlatformService
from ontology_platform.storage.db import Database

DATABASE_URL = os.environ.get("OP_TEST_DATABASE_URL", "").strip()
PREFIX = f"pgt{os.getpid()}-"
NW, CO, STRANGER = f"{PREFIX}northwind", f"{PREFIX}contoso", f"{PREFIX}nobody"

pytestmark = pytest.mark.skipif(not DATABASE_URL, reason="OP_TEST_DATABASE_URL not set")


@pytest.fixture(scope="module")
def pg():
    migrate(DATABASE_URL)
    migrate(DATABASE_URL)  # idempotent
    settings = make_settings(database_url=DATABASE_URL)
    db = Database.from_settings(settings)
    purge_tenants(db, [NW, CO, STRANGER])  # leftovers from an interrupted run with the same pid
    svc = PlatformService(db, settings)
    try:
        svc.connector_ids = seed_tenants(svc, PREFIX)  # type: ignore[attr-defined]
        yield svc
    finally:
        purge_tenants(db, [NW, CO, STRANGER])
        db.engine.dispose()


def test_core_queries(pg):
    v = operator(NW, roles=("viewer",))
    assert pg.get_entity_details(v, "service:ledger-service")["entity"]["name"] == "ledger-service"
    rel = pg.get_related(v, "application:ledger", ["contains"], "out")
    assert {r["entity"]["name"] for r in rel["related"]} == {"ledger-service", "ledger-db"}
    dep = pg.deployment_information(v, "application:ledger", None, "prod")
    assert {d["customer"] for d in dep["deployments"]} == {"acme", "globex"}
    assert pg.find_relationships(v, "customer:initech", "service:ocr-worker")["connected"]
    upstream = pg.trace_dependencies(v, "service:auth-service", "upstream")
    assert "acme" in upstream["impact"]["affected_customers"]
    assert pg.search_entities(v, "fraud", ["service"])[0]["name"] == "fraud-detector"
    assert pg.search_documentation(v, "OCR throughput")["results"][0]["title"] == "Document Intake OCR Tuning"
    a = pg.ask(v, "Which APIs are exposed by claims-portal application?")
    assert {x["entity"]["name"] for x in a["results"]} == {"claims-api"}


def test_tenant_isolation(pg):
    nw, co = operator(NW, roles=("viewer",)), operator(CO, roles=("viewer",))
    nw_id = pg.get_entity_details(nw, "service:ledger-service")["entity"]["id"]
    with pytest.raises(NotFoundError):
        pg.get_entity_details(co, nw_id)
    with pytest.raises(NotFoundError):
        pg.get_related(co, nw_id, depth=3)
    assert pg.search_entities(co, "ledger") == []
    assert pg.search_documentation(co, "failover")["results"] == []
    deps = pg.deployment_information(co, "application:payments-platform")["deployments"]
    assert [d["customer"] for d in deps] == ["fabrikam"]
    # tenants that are not ours are invisible as well
    stranger = operator(STRANGER, roles=("viewer",))
    assert pg.search_entities(stranger, "ledger") == []


def test_customer_scope(pg):
    acme = operator(NW, roles=("viewer",), customers=["acme"])
    dep = pg.deployment_information(acme, "application:payments-platform")
    assert {d["customer"] for d in dep["deployments"]} == {"acme"}
    with pytest.raises(NotFoundError):
        pg.get_entity_details(acme, "customer:globex")
    related = pg.get_related(acme, "application:ledger", depth=6)
    assert not any(c in r["entity"]["key"] for r in related["related"] for c in ("globex", "initech"))
    assert pg.graph_stats(acme)["entities"]["customer"] == 1


def test_api_keys_and_concurrent_rest(pg):
    nw_key, _ = pg.auth.create_api_key(NW, "pg-viewer", ["viewer"])
    co_key, meta = pg.auth.create_api_key(CO, "pg-viewer", ["viewer"])
    client = TestClient(create_app(settings=pg.settings, service=pg))

    def call(i):
        key, expect = (nw_key, {"acme", "globex"}) if i % 2 == 0 else (co_key, {"fabrikam"})
        app = "application:ledger" if i % 2 == 0 else "application:payments-platform"
        r = client.get("/api/v1/deployments", params={"application": app, "environment": "prod"}, headers=auth_header(key))
        return r.status_code, {d["customer"] for d in r.json()["deployments"]} == expect

    with ThreadPoolExecutor(max_workers=8) as pool:
        assert list(pool.map(call, range(24))) == [(200, True)] * 24
    pg.auth.revoke_api_key(CO, meta["id"])
    assert client.get("/api/v1/graph/stats", headers=auth_header(co_key)).status_code == 401


def test_ingestion_lock_and_incremental_resync(pg):
    admin_p = operator(NW)
    cid = pg.connector_ids["northwind/fixtures"]
    run_id = pg.start_sync(admin_p, cid, "incremental")
    with pytest.raises(ConflictError):
        pg.start_sync(admin_p, cid, "full")
    result = asyncio.run(pg.execute_sync(admin_p, cid, run_id, "incremental"))
    assert result["status"] == "succeeded"
    assert pg.get_entity_details(operator(NW, roles=("viewer",)), "service:ledger-service")["entity"]["status"] == "active"

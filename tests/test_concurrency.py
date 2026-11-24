"""Concurrency tests on a file-based SQLite database (in-memory SQLite shares one connection and
is not thread-safe): parallel REST requests, parallel MCP tool calls and ingestion run locking."""

from __future__ import annotations

import asyncio
import json
import threading
from concurrent.futures import ThreadPoolExecutor

import pytest
from fastapi.testclient import TestClient
from mcp.client import Client

from conftest import SAMPLE, auth_header, make_settings, new_service, operator, seed
from ontology_platform import admin
from ontology_platform.api.app import create_app
from ontology_platform.errors import ConflictError
from ontology_platform.mcp_server.server import build_mcp_server

WORKERS = 12


@pytest.fixture(scope="module")
def platform(tmp_path_factory):
    db_file = tmp_path_factory.mktemp("concurrency") / "ontology.db"
    svc = new_service(make_settings(database_url=f"sqlite:///{db_file.as_posix()}"))
    assert svc.db.supports_concurrency
    svc.connector_ids = seed(svc)  # type: ignore[attr-defined]
    keys = {
        "nw": svc.auth.create_api_key("northwind", "nw", ["viewer"])[0],
        "acme": svc.auth.create_api_key("northwind", "acme", ["viewer"], ["acme"])[0],
        "co": svc.auth.create_api_key("contoso", "co", ["viewer"])[0],
    }
    yield svc, keys
    svc.db.engine.dispose()


# Each case: (key label, method, url, params/body, check(response json) -> bool)
def _cases():
    return [
        ("nw", "get", "/api/v1/deployments", {"application": "application:ledger"},
         lambda b: {d["customer"] for d in b["deployments"]} == {"acme", "globex"}),
        ("acme", "get", "/api/v1/deployments", {"application": "application:ledger"},
         lambda b: {d["customer"] for d in b["deployments"]} == {"acme"}),
        ("co", "get", "/api/v1/deployments", {"application": "application:payments-platform"},
         lambda b: [d["customer"] for d in b["deployments"]] == ["fabrikam"]),
        ("co", "get", "/api/v1/entities", {"q": "ledger"}, lambda b: b["results"] == []),
        ("nw", "get", "/api/v1/entities", {"q": "ledger"}, lambda b: any(r["name"] == "ledger-service" for r in b["results"])),
        ("acme", "get", "/api/v1/entities/customer:globex", None, None),  # expects 404
        ("nw", "get", "/api/v1/entities/application:ledger/related", {"relation": "contains", "direction": "out"},
         lambda b: {r["entity"]["name"] for r in b["related"]} == {"ledger-service", "ledger-db"}),
        ("co", "get", "/api/v1/documents/search", {"q": "failover"}, lambda b: b["results"] == []),
        ("nw", "post", "/api/v1/query", {"question": "Which APIs are exposed by claims-portal application?"},
         lambda b: {x["entity"]["name"] for x in b["results"]} == {"claims-api"}),
        ("acme", "get", "/api/v1/graph/stats", None, lambda b: b["entities"]["customer"] == 1),
        ("co", "get", "/api/v1/graph/stats", None, lambda b: b["entities"]["customer"] == 1),
        ("bad", "get", "/api/v1/graph/stats", None, None),  # expects 401
    ]


def test_concurrent_rest_requests_are_correct_and_isolated(platform):
    svc, keys = platform
    client = TestClient(create_app(settings=svc.settings, service=svc))
    cases = _cases() * 4  # 48 requests
    barrier = threading.Barrier(WORKERS)

    def run(i_case):
        i, (label, method, url, payload, check) = i_case
        if i < WORKERS:
            barrier.wait(timeout=10)  # release the first wave together
        headers = auth_header(keys.get(label, "op_bad_key"))
        if method == "post":
            r = client.post(url, json=payload, headers=headers)
        else:
            r = client.get(url, params=payload, headers=headers)
        return label, url, r.status_code, r.json(), check

    with ThreadPoolExecutor(max_workers=WORKERS) as pool:
        results = list(pool.map(run, enumerate(cases)))

    assert len(results) == len(cases)
    for label, url, status, body, check in results:
        if label == "bad":
            assert status == 401, body
        elif check is None:
            assert status == 404, (label, url, body)
        else:
            assert status == 200, (label, url, body)
            assert check(body), (label, url, body)


async def test_concurrent_mcp_tool_calls(platform):
    svc, _ = platform
    server = build_mcp_server(svc, default_principal=operator("northwind", roles=("viewer",), customers=["acme"]))
    calls = [
        ("get_deployment_information", {"application": "application:payments-platform"},
         lambda d: {x["customer"] for x in d["deployments"]} == {"acme"}),
        ("search_entities", {"query": "fraud", "types": ["service"]}, lambda d: d["results"][0]["name"] == "fraud-detector"),
        ("get_related_entities", {"entity": "product:payhub", "relation_types": ["contains"], "direction": "out"},
         lambda d: {r["entity"]["name"] for r in d["related"]} == {"payments-platform", "ledger"}),
        ("get_entity_details", {"entity": "customer:globex"}, None),  # hidden by customer scope
        ("search_documentation", {"query": "promote read replica"}, lambda d: d["results"][0]["title"] == "Payments Runbook"),
        ("trace_application_dependencies", {"entity": "service:auth-service", "direction": "upstream"},
         lambda d: d["impact"]["affected_customers"] == ["acme"]),
    ] * 4  # 24 calls
    async with Client(server) as c:
        results = await asyncio.gather(*(c.call_tool(name, args) for name, args, _ in calls))
    for (name, args, check), res in zip(calls, results):
        if check is None:
            assert res.is_error and "not_found" in res.content[0].text, (name, args)
        else:
            assert not res.is_error, (name, args, res.content)
            assert check(res.structured_content), (name, args)


@pytest.fixture
def ingest_tenant(platform, request):
    svc, _ = platform
    tenant = "ccy-" + request.node.name.removeprefix("test_concurrent_ingestion_")[:40].replace("_", "-")
    admin.create_tenant(svc.db, tenant, "Concurrency")
    files = ["gcp.yaml", "aws.yaml", "azure.yaml"]
    connectors = [
        admin.register_connector(svc.db, tenant, f"c{i}", "fixture", {"path": str(SAMPLE / "northwind" / "fixtures" / f)})["id"]
        for i, f in enumerate(files)
    ]
    return svc, tenant, connectors


def test_concurrent_ingestion_refused_for_same_connector(ingest_tenant):
    svc, tenant, (cid, *_) = ingest_tenant
    p = operator(tenant)
    first = svc.start_sync(p, cid, "full")

    def attempt(_):
        try:
            svc.start_sync(p, cid, "full")
            return "started"
        except ConflictError:
            return "conflict"

    with ThreadPoolExecutor(max_workers=8) as pool:
        outcomes = list(pool.map(attempt, range(8)))
    assert outcomes == ["conflict"] * 8
    result = asyncio.run(svc.execute_sync(p, cid, first, "full"))
    assert result["status"] == "succeeded"
    # once the run finished, the connector can be synced again
    again = svc.start_sync(p, cid, "full")
    assert asyncio.run(svc.execute_sync(p, cid, again, "full"))["status"] == "succeeded"


def test_concurrent_ingestion_allowed_for_different_connectors(ingest_tenant):
    svc, tenant, connectors = ingest_tenant
    p = operator(tenant)
    barrier = threading.Barrier(len(connectors))

    def ingest(cid):
        barrier.wait(timeout=10)
        run_id = svc.start_sync(p, cid, "full")
        return asyncio.run(svc.execute_sync(p, cid, run_id, "full"))

    with ThreadPoolExecutor(max_workers=len(connectors)) as pool:
        results = list(pool.map(ingest, connectors))
    assert [r["status"] for r in results] == ["succeeded"] * len(connectors), results
    runs = admin.list_runs(svc.db, tenant, None, 50)
    assert {r["connector_id"] for r in runs} == set(connectors)
    assert all(r["status"] == "succeeded" for r in runs)


def test_concurrent_workflows_complete_with_correct_answers(platform):
    svc, _ = platform
    questions = [
        (operator("northwind", roles=("viewer",)), "Which APIs are exposed by claims-portal application?", "claims-api"),
        (operator("northwind", roles=("viewer",)), "Which APIs are exposed by claims-portal application?", "claims-api"),
        (operator("northwind", roles=("viewer",), customers=["acme"]), "Which APIs are exposed by claims-portal application?", "claims-api"),
        (operator("contoso", roles=("viewer",)), "Which APIs are exposed by claims-portal application?", None),
        (operator("northwind", roles=("viewer",)), "Which APIs are exposed by claims-portal application?", "claims-api"),
    ]

    async def run_all():
        return await asyncio.gather(*(svc.run_workflow(p, q) for p, q, _ in questions))

    runs = asyncio.run(run_all())
    assert len({r["id"] for r in runs}) == len(questions)
    for (p, _, expected), run in zip(questions, runs):
        assert run["status"] == "completed", run["status"]
        assert run["tenant_id"] == p.tenant_id
        answer = json.dumps(run["answer"])
        if expected:
            assert expected in answer
        else:
            assert "claims-api" not in answer  # contoso has no claims-portal
        assert svc.get_workflow(p, run["id"])["status"] == "completed"



def test_simultaneous_start_of_idle_connector_admits_exactly_one(ingest_tenant):
    svc, tenant, (*_, cid) = ingest_tenant
    p = operator(tenant)
    barrier = threading.Barrier(8)

    def attempt(_):
        barrier.wait(timeout=10)
        try:
            svc.start_sync(p, cid, "full")
            return "started"
        except ConflictError:
            return "conflict"

    with ThreadPoolExecutor(max_workers=8) as pool:
        outcomes = list(pool.map(attempt, range(8)))
    assert outcomes.count("started") == 1, outcomes

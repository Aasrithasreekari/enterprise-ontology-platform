"""Agent workflow over REST: run, list, fetch, events, validation, auth and tenant isolation."""

import pytest
from fastapi.testclient import TestClient

from conftest import new_service, seed
from ontology_platform.api.app import create_app


@pytest.fixture(scope="module")
def api():
    svc = new_service()
    seed(svc)
    keys = {
        "viewer": svc.auth.create_api_key("northwind", "viewer", ["viewer"])[0],
        "contoso": svc.auth.create_api_key("contoso", "viewer", ["viewer"])[0],
    }
    return TestClient(create_app(settings=svc.settings, service=svc)), keys


def h(key):
    return {"Authorization": f"Bearer {key}"}


def test_agent_query_returns_full_run(api):
    client, keys = api
    r = client.post("/api/v1/agent/query", json={"question": "What is impacted if auth-service fails?"}, headers=h(keys["viewer"]))
    assert r.status_code == 200
    run = r.json()
    assert run["status"] == "completed" and "impact" in run["intents"]
    assert run["validation"]["status"] == "validated" and run["validation"]["confirmed"] >= 1
    assert "claims-portal" in run["answer"]["summary"]
    assert run["answer"]["sources"] and all(f["evidence"] for f in run["facts"] if f["status"] == "confirmed")
    steps = {s["id"]: s for s in run["plan"]}
    assert steps["validate"]["depends_on"] and all(s["status"] == "completed" for s in steps.values())
    assert run["events"][0]["type"] == "workflow_started"


def test_parallel_plan_for_multi_intent_question(api):
    client, keys = api
    q = "What is impacted if payments-platform fails, where is it deployed in prod, and which documentation describes it?"
    run = client.post("/api/v1/agent/query", json={"question": q}, headers=h(keys["viewer"])).json()
    assert {"impact", "deployments", "documentation"} <= set(run["intents"])
    roots = [s for s in run["plan"] if not s["depends_on"]]
    assert len(roots) >= 2  # independent steps are scheduled in parallel


def test_runs_list_get_and_events(api):
    client, keys = api
    v = h(keys["viewer"])
    created = client.post("/api/v1/agent/query", json={"question": "Which APIs are exposed by the claims-portal application?"}, headers=v).json()
    listed = client.get("/api/v1/agent/runs", params={"limit": 100}, headers=v).json()
    assert listed["total"] >= 1
    row = next(x for x in listed["runs"] if x["id"] == created["id"])
    assert row["query"] == created["query"] and row["validation_status"] == created["validation"]["status"]
    fetched = client.get(f"/api/v1/agent/runs/{created['id']}", headers=v).json()
    assert fetched["answer"]["summary"] == created["answer"]["summary"]
    events = client.get(f"/api/v1/agent/runs/{created['id']}/events", headers=v).json()["events"]
    assert [e["type"] for e in events] == [e["type"] for e in created["events"]]
    page = client.get("/api/v1/agent/runs", params={"limit": 1, "offset": 0}, headers=v).json()
    assert len(page["runs"]) == 1


def test_validation_and_auth(api):
    client, keys = api
    v = h(keys["viewer"])
    assert client.post("/api/v1/agent/query", json={"question": "x"}, headers=v).status_code == 422
    assert client.post("/api/v1/agent/query", json={"question": "q" * 1001}, headers=v).status_code == 422
    assert client.get("/api/v1/agent/runs?limit=0", headers=v).status_code == 422
    assert client.get("/api/v1/agent/runs?limit=101", headers=v).status_code == 422
    assert client.get("/api/v1/agent/runs?offset=-1", headers=v).status_code == 422
    assert client.post("/api/v1/agent/query", json={"question": "Which APIs exist?"}).status_code == 401
    assert client.get("/api/v1/agent/runs").status_code == 401
    assert client.get("/api/v1/agent/runs/unknown-run", headers=v).status_code == 404


def test_runs_are_tenant_isolated(api):
    client, keys = api
    run = client.post("/api/v1/agent/query", json={"question": "Which repositories belong to Acme Bank's products?"},
                      headers=h(keys["viewer"])).json()
    other = h(keys["contoso"])
    assert client.get(f"/api/v1/agent/runs/{run['id']}", headers=other).status_code == 404
    assert client.get(f"/api/v1/agent/runs/{run['id']}/events", headers=other).status_code == 404
    assert all(x["id"] != run["id"] for x in client.get("/api/v1/agent/runs", headers=other).json()["runs"])

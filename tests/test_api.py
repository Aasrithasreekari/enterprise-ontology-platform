import pytest
from fastapi.testclient import TestClient

from conftest import SAMPLE, new_service, seed
from ontology_platform.api.app import create_app


@pytest.fixture(scope="module")
def api():
    svc = new_service()
    seed(svc)
    admin_key, _ = svc.auth.create_api_key("northwind", "admin", ["admin"])
    viewer_key, _ = svc.auth.create_api_key("northwind", "viewer", ["viewer"])
    acme_key, _ = svc.auth.create_api_key("northwind", "acme", ["viewer"], ["acme"])
    client = TestClient(create_app(settings=svc.settings, service=svc))
    return client, {"admin": admin_key, "viewer": viewer_key, "acme": acme_key}


def h(key):
    return {"Authorization": f"Bearer {key}"}


def test_ops_endpoints_unauthenticated(api):
    client, _ = api
    assert client.get("/health").json() == {"status": "ok"}
    assert client.get("/ready").json() == {"status": "ready"}
    assert "op_ingestion_runs_total" in client.get("/metrics").text
    assert client.get("/openapi.json").status_code == 200


def test_auth_required(api):
    client, _ = api
    r = client.get("/api/v1/entities?q=ledger")
    assert r.status_code == 401 and r.json()["error"] == "unauthenticated"
    assert r.headers["www-authenticate"] == "Bearer"
    assert client.get("/api/v1/entities", headers=h("op_bad_key")).status_code == 401


def test_x_api_key_header_and_request_id(api):
    client, keys = api
    r = client.get("/api/v1/entities?q=ledger", headers={"X-API-Key": keys["viewer"], "X-Request-ID": "req-123"})
    assert r.status_code == 200 and r.headers["x-request-id"] == "req-123"


def test_query_endpoint(api):
    client, keys = api
    r = client.post("/api/v1/query", json={"question": "Which APIs are exposed by claims-portal application?"}, headers=h(keys["viewer"]))
    assert r.status_code == 200
    body = r.json()
    assert body["intent"] == "apis_exposed" and {x["entity"]["name"] for x in body["results"]} == {"claims-api"}


def test_request_validation(api):
    client, keys = api
    assert client.post("/api/v1/query", json={"question": "x"}, headers=h(keys["viewer"])).status_code == 422
    assert client.get("/api/v1/entities?type=spaceship", headers=h(keys["viewer"])).status_code == 422
    assert client.get("/api/v1/entities/application:claims-portal/related?depth=99", headers=h(keys["viewer"])).status_code == 422


def test_entity_routes(api):
    client, keys = api
    v = h(keys["viewer"])
    d = client.get("/api/v1/entities/service:ledger-service?history=true", headers=v).json()
    assert d["entity"]["name"] == "ledger-service" and d["history"]
    rel = client.get("/api/v1/entities/application:ledger/related?relation=contains&direction=out", headers=v).json()
    assert {r["entity"]["name"] for r in rel["related"]} == {"ledger-service", "ledger-db"}
    assert client.get("/api/v1/entities/nothing-here", headers=v).status_code == 404
    paths = client.get("/api/v1/paths", params={"source": "customer:initech", "target": "service:ocr-worker"}, headers=v).json()
    assert paths["connected"]
    deps = client.get("/api/v1/dependencies/service:auth-service?direction=upstream", headers=v).json()
    assert "acme" in deps["impact"]["affected_customers"]
    dep = client.get("/api/v1/deployments", params={"application": "application:ledger", "environment": "prod"}, headers=v).json()
    assert {d["customer"] for d in dep["deployments"]} == {"acme", "globex"}
    docs = client.get("/api/v1/documents/search", params={"q": "OCR throughput"}, headers=v).json()
    assert docs["results"][0]["title"] == "Document Intake OCR Tuning"
    schema = client.get("/api/v1/ontology/schema", headers=v).json()
    assert len(schema["entity_types"]) == 13


def test_customer_scoped_key_over_rest(api):
    client, keys = api
    dep = client.get("/api/v1/deployments", params={"application": "application:ledger"}, headers=h(keys["acme"])).json()
    assert {d["customer"] for d in dep["deployments"]} == {"acme"}
    assert client.get("/api/v1/entities/customer:globex", headers=h(keys["acme"])).status_code == 404


def test_viewer_cannot_manage(api):
    client, keys = api
    v = h(keys["viewer"])
    assert client.get("/api/v1/connectors", headers=v).status_code == 403
    assert client.post("/api/v1/admin/api-keys", json={"name": "x", "roles": ["admin"]}, headers=v).status_code == 403


def test_connector_registration_sync_and_runs(api, tmp_path):
    client, keys = api
    a = h(keys["admin"])
    bad = client.post("/api/v1/connectors", json={"name": "gh", "type": "github", "config": {"org": "x"}, "secret_ref": "ghp_rawtoken"}, headers=a)
    assert bad.status_code == 422 and "secret_ref" in bad.json()["message"]
    created = client.post("/api/v1/connectors", json={"name": "gcp-fixture", "type": "fixture", "config": {"path": str(SAMPLE / "northwind" / "fixtures" / "gcp.yaml")}}, headers=a)
    assert created.status_code == 201
    cid = created.json()["id"]
    assert client.post("/api/v1/connectors", json={"name": "gcp-fixture", "type": "fixture", "config": {"path": "."}}, headers=a).status_code == 409
    r = client.post(f"/api/v1/connectors/{cid}/sync", json={"mode": "full"}, headers=a)
    assert r.status_code == 202
    run = client.get(f"/api/v1/ingestion/runs/{r.json()['run_id']}", headers=a).json()  # TestClient runs background tasks before returning
    assert run["status"] == "succeeded" and run["stats"]["records_seen"] == 2
    assert any(x["id"] == cid for x in client.get("/api/v1/connectors", headers=a).json()["connectors"])
    assert "fixture" in client.get("/api/v1/connector-types", headers=a).json()["types"]


def test_push_records(api):
    client, keys = api
    a = h(keys["admin"])
    cid = client.post("/api/v1/connectors", json={"name": "cmdb-hook", "type": "push"}, headers=a).json()["id"]
    record = {"kind": "catalog", "external_id": "catalog:cmdb-1", "source_system": "cmdb",
              "payload": {"entities": [{"type": "customer", "name": "umbrella", "display_name": "Umbrella Corp"}],
                          "relationships": [{"type": "owns", "source": {"type": "customer", "name": "umbrella"}, "target": {"type": "product", "name": "claimspro"}}]}}
    r = client.post(f"/api/v1/connectors/{cid}/records", json={"records": [record]}, headers=a)
    assert r.status_code == 200 and r.json()["status"] == "succeeded"
    found = client.get("/api/v1/entities", params={"q": "Umbrella Corp"}, headers=a).json()["results"]
    assert found[0]["name"] == "Umbrella Corp"
    assert client.post(f"/api/v1/connectors/{cid}/records", json={"records": [{"kind": "nope"}]}, headers=a).status_code == 422


def test_api_key_admin_and_audit(api):
    client, keys = api
    a = h(keys["admin"])
    created = client.post("/api/v1/admin/api-keys", json={"name": "temp", "roles": ["viewer"]}, headers=a).json()
    assert client.get("/api/v1/graph/stats", headers=h(created["api_key"])).status_code == 200
    assert client.delete(f"/api/v1/admin/api-keys/{created['id']}", headers=a).status_code == 204
    assert client.get("/api/v1/graph/stats", headers=h(created["api_key"])).status_code == 401
    audit = client.get("/api/v1/admin/audit", headers=a).json()["entries"]
    assert any(e["action"] == "revoke_api_key" for e in audit)

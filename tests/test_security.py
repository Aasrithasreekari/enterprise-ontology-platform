"""Security tests: authentication edge cases, authorization, tenant isolation, customer-scope ABAC
and secret hygiene, over the service facade, REST and MCP (stdio-style and streamable HTTP)."""

from __future__ import annotations

import base64
import json
import time

import jwt
import pytest
from fastapi.testclient import TestClient
from mcp.client import Client

from conftest import auth_header, make_settings, mcp_http, new_service, operator, seed
from ontology_platform.api.app import create_app
from ontology_platform.auth.principal import Principal
from ontology_platform.auth.service import REDACTED, redact
from ontology_platform.errors import AuthenticationError, AuthorizationError, NotFoundError
from ontology_platform.mcp_server.server import build_mcp_server

JWT_KEY = "security-test-signing-key-0123456789abcdef"
AUDIENCE = "ontology-security-tests"
OTHER_CUSTOMERS = ("globex", "initech")


# ----------------------------------------------------------------------------- fixtures
@pytest.fixture(scope="module")
def env():
    """Seeded platform with JWT enabled, a REST client and API keys for several roles/tenants."""
    svc = new_service(make_settings(jwt_key=JWT_KEY, jwt_audience=AUDIENCE))
    svc.connector_ids = seed(svc)  # type: ignore[attr-defined]
    keys = {}
    for label, tenant, roles, customers in [
        ("nw_admin", "northwind", ["admin"], None),
        ("nw_viewer", "northwind", ["viewer"], None),
        ("nw_ingestor", "northwind", ["ingestor"], None),
        ("nw_acme", "northwind", ["viewer"], ["acme"]),
        ("co_viewer", "contoso", ["viewer"], None),
        ("co_admin", "contoso", ["admin"], None),
        ("revoked", "northwind", ["admin"], None),
    ]:
        keys[label], meta = svc.auth.create_api_key(tenant, label, roles, customers)
        keys[f"{label}_id"] = meta["id"]
    svc.auth.revoke_api_key("northwind", keys["revoked_id"])
    nw = operator("northwind", roles=("viewer",))
    ids = {
        ref: svc.get_entity_details(nw, ref)["entity"]["id"]
        for ref in ["service:ledger-service", "application:ledger", "application:claims-portal", "customer:acme",
                    "customer:globex", "deployment:ledger/globex/prod", "api:payments-api"]
    }
    client = TestClient(create_app(settings=svc.settings, service=svc))
    return svc, client, keys, ids


def _token(**claims) -> str:
    base = {"sub": "alice", "tenant": "northwind", "roles": ["viewer"], "aud": AUDIENCE, "exp": int(time.time()) + 300}
    base.update(claims)
    return jwt.encode({k: v for k, v in base.items() if v is not None}, JWT_KEY, algorithm="HS256")


def _keys_of(result: dict) -> set[str]:
    return {r["entity"]["key"] for r in result["related"]}


def _mentions_other_customer(keys) -> bool:
    return any(c in k.lower() for k in keys for c in OTHER_CUSTOMERS)


# ----------------------------------------------------------------------------- authentication (REST)
def _tampered_key(key: str) -> str:
    return key[:-1] + ("A" if key[-1] != "A" else "B")


@pytest.mark.parametrize(
    "make_token",
    [
        lambda k: "op_nope_secret",
        lambda k: "op_only",
        lambda k: "op__",
        lambda k: "garbage",
        lambda k: "a.b.c",
        lambda k: _tampered_key(k["nw_admin"]),
        lambda k: k["nw_admin"] + "x",
        lambda k: k["nw_admin"].replace(k["nw_admin_id"], k["co_admin_id"]),  # valid id, other key's secret
        lambda k: k["revoked"],
        lambda k: "op_" + "'; DROP TABLE api_keys; --_x",
    ],
    ids=["unknown-id", "two-parts", "empty-parts", "garbage", "fake-jwt", "tampered", "suffixed", "swapped-id", "revoked", "sql-meta"],
)
def test_invalid_api_keys_are_401_without_echoing_the_credential(env, make_token):
    _, client, keys, _ = env
    token = make_token(keys)
    for headers in (auth_header(token), {"X-API-Key": token}):
        r = client.get("/api/v1/graph/stats", headers=headers)
        assert r.status_code == 401, (token, r.text)
        assert r.json()["error"] == "unauthenticated"
        assert token not in r.text
        assert keys["nw_admin"] not in r.text


def test_missing_and_non_bearer_schemes_rejected(env):
    _, client, keys, _ = env
    assert client.get("/api/v1/graph/stats").status_code == 401
    assert client.get("/api/v1/graph/stats", headers={"Authorization": "Bearer "}).status_code == 401
    basic = base64.b64encode(b"admin:admin").decode()
    assert client.get("/api/v1/graph/stats", headers={"Authorization": f"Basic {basic}"}).status_code == 401


def _forge(claims: dict, key: str = JWT_KEY, alg: str = "HS256") -> str:
    return jwt.encode(claims, key, algorithm=alg)


def _tamper_payload(token: str, **changes) -> str:
    header, payload, sig = token.split(".")
    data = json.loads(base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4)))
    data.update(changes)
    new_payload = base64.urlsafe_b64encode(json.dumps(data).encode()).rstrip(b"=").decode()
    return f"{header}.{new_payload}.{sig}"


def _unsigned(claims: dict) -> str:
    enc = lambda d: base64.urlsafe_b64encode(json.dumps(d).encode()).rstrip(b"=").decode()  # noqa: E731
    return f"{enc({'alg': 'none', 'typ': 'JWT'})}.{enc(claims)}."


@pytest.mark.parametrize(
    "make_token",
    [
        lambda: _tamper_payload(_token(), roles=["admin"]),
        lambda: _tamper_payload(_token(), tenant="contoso"),
        lambda: _tamper_payload(_token(customers=["acme"]), customers=None),
        lambda: _token(exp=int(time.time()) - 30),
        lambda: _token(aud="some-other-service"),
        lambda: _token(aud=None),
        lambda: _token(exp=None),
        lambda: _token(sub=None),
        lambda: _forge({"sub": "x", "tenant": "northwind", "roles": ["admin"], "aud": AUDIENCE, "exp": int(time.time()) + 60},
                       key="attacker-chosen-key-0123456789abcdef"),
        lambda: _forge({"sub": "x", "tenant": "northwind", "roles": ["admin"], "aud": AUDIENCE, "exp": int(time.time()) + 60},
                       key=JWT_KEY * 2, alg="HS512"),
        lambda: _unsigned({"sub": "x", "tenant": "northwind", "roles": ["admin"], "aud": AUDIENCE, "exp": int(time.time()) + 60}),
        lambda: _token(tenant="nonexistent"),
        lambda: _token(tenant=["northwind"]),
        lambda: _token(roles=["root"]),
        lambda: _token(roles=[]),
        lambda: _token(customers="acme"),  # malformed scope must fail closed, not widen to all customers
        lambda: _token(customers=[1, 2]),
    ],
    ids=["tampered-roles", "tampered-tenant", "stripped-customers", "expired", "wrong-aud", "no-aud", "no-exp", "no-sub",
         "wrong-key", "wrong-alg", "alg-none", "unknown-tenant", "tenant-list", "unknown-role", "no-roles",
         "customers-string", "customers-non-str"],
)
def test_bad_jwts_rejected(env, make_token):
    svc, client, _, _ = env
    token = make_token()
    with pytest.raises(AuthenticationError):
        svc.auth.authenticate(token)
    r = client.get("/api/v1/graph/stats", headers=auth_header(token))
    assert r.status_code == 401 and token not in r.text


def test_valid_jwt_and_customer_claim_restricts(env, ids):
    _, client, _, _ = env
    assert client.get("/api/v1/graph/stats", headers=auth_header(_token())).status_code == 200
    restricted = auth_header(_token(customers=["Acme"]))
    assert client.get("/api/v1/entities/customer:globex", headers=restricted).status_code == 404
    dep = client.get("/api/v1/deployments", params={"application": "application:ledger"}, headers=restricted).json()
    assert {d["customer"] for d in dep["deployments"]} == {"acme"}
    # an empty list means "no customers", not "all customers"
    none = auth_header(_token(customers=[]))
    assert client.get("/api/v1/entities/customer:acme", headers=none).status_code == 404


@pytest.fixture(scope="module")
def ids(env):
    return env[3]


# ----------------------------------------------------------------------------- authorization (REST)
def test_viewer_is_forbidden_on_admin_and_ingest_endpoints(env):
    _, client, keys, _ = env
    connector_id = env[0].connector_ids["northwind/fixtures"]
    calls = [
        ("get", "/api/v1/connectors", None),
        ("get", "/api/v1/connector-types", None),
        ("post", "/api/v1/connectors", {"name": "x", "type": "fixture", "config": {"path": "."}}),
        ("post", f"/api/v1/connectors/{connector_id}/sync", {"mode": "full"}),
        ("post", f"/api/v1/connectors/{connector_id}/records", {"records": [{"kind": "catalog", "external_id": "x", "source_system": "s", "payload": {}}]}),
        ("get", "/api/v1/ingestion/runs", None),
        ("get", "/api/v1/ingestion/runs/some-run", None),
        ("get", "/api/v1/admin/api-keys", None),
        ("post", "/api/v1/admin/api-keys", {"name": "x", "roles": ["admin"]}),
        ("delete", f"/api/v1/admin/api-keys/{keys['nw_admin_id']}", None),
        ("get", "/api/v1/admin/audit", None),
    ]
    for method, url, body in calls:
        kwargs = {"headers": auth_header(keys["nw_viewer"])}
        if body is not None:
            kwargs["json"] = body
        r = getattr(client, method)(url, **kwargs)
        assert r.status_code == 403, (method, url, r.status_code, r.text)
        assert r.json()["error"] == "forbidden"
    # the delete attempt did not revoke anything
    assert client.get("/api/v1/graph/stats", headers=auth_header(keys["nw_admin"])).status_code == 200


def test_ingestor_cannot_manage_keys_or_register_connectors(env):
    _, client, keys, _ = env
    h = auth_header(keys["nw_ingestor"])
    assert client.get("/api/v1/connectors", headers=h).status_code == 200
    assert client.post("/api/v1/connectors", json={"name": "x", "type": "fixture"}, headers=h).status_code == 403
    assert client.post("/api/v1/admin/api-keys", json={"name": "x", "roles": ["admin"]}, headers=h).status_code == 403
    assert client.get("/api/v1/admin/audit", headers=h).status_code == 403


def test_principal_without_graph_read_is_refused(env):
    svc = env[0]
    nobody = Principal(subject="test:nobody", tenant_id="northwind", roles=frozenset({"unknown-role"}))
    for call in [
        lambda: svc.search_entities(nobody, "ledger"),
        lambda: svc.get_entity_details(nobody, "service:ledger-service"),
        lambda: svc.get_related(nobody, "service:ledger-service"),
        lambda: svc.find_relationships(nobody, "customer:acme", "service:ledger-service"),
        lambda: svc.ask(nobody, "What does ledger depend on?"),
        lambda: svc.search_documentation(nobody, "failover"),
        lambda: svc.graph_stats(nobody),
    ]:
        with pytest.raises(AuthorizationError):
            call()


# ----------------------------------------------------------------------------- tenant isolation (REST)
def test_cross_tenant_lookup_by_id_and_key_is_not_found(env, ids):
    _, client, keys, _ = env
    co = auth_header(keys["co_viewer"])
    for ref in [ids["service:ledger-service"], ids["application:ledger"], ids["customer:acme"], "service:ledger-service", "customer:acme"]:
        assert client.get(f"/api/v1/entities/{ref}", headers=co).status_code == 404, ref
        assert client.get(f"/api/v1/entities/{ref}/related", params={"depth": 3}, headers=co).status_code == 404, ref
        # 422 is acceptable for refs whose type has no dependencies (e.g. customers); never 200
        assert client.get(f"/api/v1/dependencies/{ref}", params={"direction": "upstream"}, headers=co).status_code in (404, 422), ref


def test_cross_tenant_find_relationships_and_deployments(env, ids):
    _, client, keys, _ = env
    co = auth_header(keys["co_viewer"])
    for source, target in [
        (ids["customer:acme"], ids["service:ledger-service"]),
        ("customer:acme", "service:ledger-service"),
        ("customer:fabrikam", ids["application:ledger"]),  # own entity to foreign id
    ]:
        r = client.get("/api/v1/paths", params={"source": source, "target": target}, headers=co)
        assert r.status_code == 404 or (r.status_code == 200 and not r.json()["connected"] and not r.json()["paths"]), r.text
    r = client.get("/api/v1/deployments", params={"application": ids["application:ledger"]}, headers=co)
    assert r.status_code == 404


def test_cross_tenant_search_and_documentation(env, ids):
    _, client, keys, _ = env
    co = auth_header(keys["co_viewer"])
    assert client.get("/api/v1/entities", params={"q": "ledger"}, headers=co).json()["results"] == []
    assert client.get("/api/v1/entities", params={"q": ids["service:ledger-service"]}, headers=co).json()["results"] == []
    docs = client.get("/api/v1/documents/search", params={"q": "failover"}, headers=co)
    assert docs.status_code == 200 and docs.json()["results"] == []
    scoped = client.get("/api/v1/documents/search", params={"q": "failover", "entity": ids["application:ledger"]}, headers=co)
    assert scoped.status_code == 404 or scoped.json()["results"] == []
    # sanity: northwind itself finds the document
    assert client.get("/api/v1/documents/search", params={"q": "failover"}, headers=auth_header(keys["nw_viewer"])).json()["results"]
    a = client.post("/api/v1/query", json={"question": "What depends on ledger-service?"}, headers=co).json()
    assert a["linked_entities"] == [] and a["results"] == []


def test_cross_tenant_admin_operations(env):
    svc, client, keys, _ = env
    co_admin = auth_header(keys["co_admin"])
    # revoking another tenant's key is not found and has no effect
    assert client.delete(f"/api/v1/admin/api-keys/{keys['nw_viewer_id']}", headers=co_admin).status_code == 404
    assert client.get("/api/v1/graph/stats", headers=auth_header(keys["nw_viewer"])).status_code == 200
    nw_cid = svc.connector_ids["northwind/fixtures"]
    assert client.post(f"/api/v1/connectors/{nw_cid}/sync", json={"mode": "full"}, headers=co_admin).status_code == 404
    listed = client.get("/api/v1/admin/api-keys", headers=co_admin).json()["api_keys"]
    assert {k["name"] for k in listed} <= {"co_viewer", "co_admin"}
    audit = client.get("/api/v1/admin/audit", headers=co_admin).json()["entries"]
    assert all(not e["principal"].startswith(("api_key:" + keys["nw_admin_id"], "test:northwind")) for e in audit)


# ----------------------------------------------------------------------------- customer-scope ABAC through traversal
def test_customer_restricted_key_cannot_reach_other_customers_via_multi_hop(env, ids):
    _, client, keys, _ = env
    acme = auth_header(keys["nw_acme"])
    for root in ["application:ledger", "application:payments-platform", "customer:acme", "product:payhub", "service:auth-service"]:
        r = client.get(f"/api/v1/entities/{root}/related", params={"depth": 12}, headers=acme)
        assert r.status_code == 200
        reached = _keys_of(r.json())
        assert reached and not _mentions_other_customer(reached), (root, sorted(k for k in reached if _mentions_other_customer([k])))
        for item in r.json()["related"]:  # no hidden node on any returned path either
            assert not _mentions_other_customer(json.dumps(item["path"]).lower().split('"')), item
    # direct and path access to another customer's deployment
    assert client.get(f"/api/v1/entities/{ids['deployment:ledger/globex/prod']}", headers=acme).status_code == 404
    r = client.get("/api/v1/paths", params={"source": "customer:acme", "target": ids["deployment:ledger/globex/prod"], "max_depth": 12}, headers=acme)
    assert r.status_code == 404
    r = client.get("/api/v1/dependencies/service:ledger-service", params={"direction": "upstream", "depth": 12}, headers=acme).json()
    assert r["impact"]["affected_customers"] == ["acme"]
    deployments = r["impact"].get("affected_deployments") or []
    assert not _mentions_other_customer([json.dumps(d) for d in deployments])
    a = client.post("/api/v1/query", json={"question": "Which Kubernetes clusters host Globex's applications?"}, headers=acme).json()
    assert a["results"] == []


def test_customer_restricted_trace_evidence_does_not_name_other_customers(env):
    svc = env[0]
    acme = operator("northwind", roles=("viewer",), customers=["acme"])
    trace = svc.trace_dependencies(acme, "application:ledger", "upstream", 6)
    assert not _mentions_other_customer([json.dumps(trace).lower()])


def test_customer_restricted_admin_cannot_widen_scope_with_new_key(env):
    svc = env[0]
    acme_admin = operator("northwind", roles=("admin",), customers=["acme"])
    try:
        created = svc.create_api_key(acme_admin, "widen", ["viewer"], None)
    except AuthorizationError:
        return
    p = svc.auth.authenticate(created["api_key"])
    assert p.allowed_customers is not None and p.allowed_customers <= frozenset({"acme"})


# ----------------------------------------------------------------------------- secret hygiene
def test_secrets_never_reach_error_bodies_or_audit(env):
    svc, client, keys, _ = env
    admin_h = auth_header(keys["nw_admin"])
    raw_token = "ghp_" + "S3cr3tT0kenValue" * 2
    # a raw secret pasted into secret_ref / config is refused and not echoed or persisted
    r = client.post("/api/v1/connectors", json={"name": "leaky", "type": "github", "config": {"org": "x", "token": raw_token},
                                                 "secret_ref": raw_token}, headers=admin_h)
    assert r.status_code == 422 and raw_token not in r.text
    # API keys pasted into free-text parameters
    client.post("/api/v1/query", json={"question": f"who owns {keys['nw_viewer']} ?"}, headers=admin_h)
    client.get("/api/v1/entities", params={"q": keys["co_admin"]}, headers=admin_h)
    # denied and failed requests carrying credentials
    client.get("/api/v1/admin/audit", headers=auth_header(keys["nw_viewer"]))
    client.get("/api/v1/graph/stats", headers=auth_header(_tampered_key(keys["nw_admin"])))
    created = client.post("/api/v1/admin/api-keys", json={"name": "fresh", "roles": ["viewer"]}, headers=admin_h).json()
    assert created["api_key"].startswith("op_")

    audit = client.get("/api/v1/admin/audit", params={"limit": 1000}, headers=admin_h)
    body = audit.text
    for secret in [raw_token, created["api_key"], *(keys[k] for k in keys if not k.endswith("_id"))]:
        assert secret not in body
        assert secret.rsplit("_", 1)[-1] not in body  # not even the secret half of a key
    leaky = [e for e in audit.json()["entries"] if e["action"] == "register_connector" and e["params"].get("name") == "leaky"]
    assert leaky and leaky[0]["params"]["secret_ref"] == REDACTED and leaky[0]["params"]["config"]["token"] == REDACTED
    # key listings never expose hashes or plaintext
    listed = client.get("/api/v1/admin/api-keys", headers=admin_h).text
    assert "key_hash" not in listed and created["api_key"] not in listed
    for row in svc.auth.list_api_keys("northwind"):
        assert set(row) == {"id", "name", "roles", "allowed_customers", "revoked", "created_at", "last_used_at"}


def test_redact_keeps_references_and_non_secrets():
    assert redact({"secret_ref": "env:GITHUB_TOKEN", "config": {"org": "acme", "page_size": 50}}) == {
        "secret_ref": "env:GITHUB_TOKEN", "config": {"org": "acme", "page_size": 50}}
    assert redact({"headers": {"Authorization": "Bearer abc"}, "password": "p"}) == {"headers": {"Authorization": REDACTED}, "password": REDACTED}
    jwt_like = _token()
    assert jwt_like not in json.dumps(redact({"q": f"token is {jwt_like}"}))


# ----------------------------------------------------------------------------- MCP (in-process, stdio-style principal)
async def test_mcp_cross_tenant_rules(env, ids):
    svc = env[0]
    server = build_mcp_server(svc, default_principal=operator("contoso", roles=("viewer",)))
    async with Client(server) as c:
        for tool, args in [
            ("get_entity_details", {"entity": ids["service:ledger-service"]}),
            ("get_entity_details", {"entity": "customer:acme"}),
            ("get_related_entities", {"entity": ids["application:ledger"], "depth": 3}),
            ("trace_application_dependencies", {"entity": ids["service:ledger-service"], "direction": "upstream"}),
            ("get_deployment_information", {"application": ids["application:ledger"]}),
        ]:
            res = await c.call_tool(tool, args)
            assert res.is_error and "not_found" in res.content[0].text, (tool, args)
        rel = await c.call_tool("find_entity_relationships", {"source": ids["customer:acme"], "target": ids["service:ledger-service"]})
        assert rel.is_error or not rel.structured_content["connected"]
        found = (await c.call_tool("search_entities", {"query": "ledger"})).structured_content
        assert found["results"] == []
        docs = (await c.call_tool("search_documentation", {"query": "failover"})).structured_content
        assert docs["results"] == []
        q = (await c.call_tool("query_knowledge_graph", {"question": "What depends on ledger-service?"})).structured_content
        assert q["results"] == []


async def test_mcp_customer_scope_and_permissions(env, ids):
    svc = env[0]
    acme = build_mcp_server(svc, default_principal=operator("northwind", roles=("viewer",), customers=["acme"]))
    async with Client(acme) as c:
        related = (await c.call_tool("get_related_entities", {"entity": "application:ledger", "depth": 6})).structured_content
        assert related["related"] and not _mentions_other_customer(_keys_of(related))
        hidden = await c.call_tool("get_entity_details", {"entity": ids["deployment:ledger/globex/prod"]})
        assert hidden.is_error and "not_found" in hidden.content[0].text
    nobody = build_mcp_server(svc, default_principal=Principal(subject="test:nobody", tenant_id="northwind", roles=frozenset({"ghost"})))
    async with Client(nobody) as c:
        res = await c.call_tool("search_entities", {"query": "ledger"})
        assert res.is_error and "forbidden" in res.content[0].text


# ----------------------------------------------------------------------------- MCP over streamable HTTP (real header auth)
async def test_mcp_http_authentication(env, ids):
    svc, _, keys, _ = env
    bad_tokens = [
        None,
        "op_nope_secret",
        _tampered_key(keys["nw_viewer"]),
        keys["revoked"],
        _token(exp=int(time.time()) - 30),
        _token(aud="other"),
        _tamper_payload(_token(), tenant="contoso"),
    ]
    async with mcp_http(svc) as connect:
        for token in bad_tokens:
            async with connect(token) as c:
                res = await c.call_tool("search_entities", {"query": "ledger"})
            assert res.is_error and "unauthenticated" in res.content[0].text, token
            if token:
                assert token not in res.content[0].text
        async with connect(keys["co_viewer"]) as c:
            res = await c.call_tool("get_entity_details", {"entity": ids["service:ledger-service"]})
            assert res.is_error and "not_found" in res.content[0].text
            own = (await c.call_tool("search_entities", {"query": "payments"})).structured_content
            assert own["results"] and all(r["id"] != ids.get("application:ledger") for r in own["results"])
        async with connect(_token(customers=["acme"])) as c:
            related = (await c.call_tool("get_related_entities", {"entity": "application:ledger", "depth": 6})).structured_content
            assert related["related"] and not _mentions_other_customer(_keys_of(related))
        async with connect(keys["nw_viewer"]) as c:
            ok = (await c.call_tool("get_entity_details", {"entity": ids["service:ledger-service"]})).structured_content
            assert ok["entity"]["name"] == "ledger-service"


# ----------------------------------------------------------------------------- agent workflows
async def test_workflow_runs_are_tenant_isolated(env):
    svc = env[0]
    nw, co = operator("northwind", roles=("viewer",)), operator("contoso", roles=("viewer",))
    nw_run = await svc.run_workflow(nw, "Which APIs are exposed by claims-portal application?")
    co_run = await svc.run_workflow(co, "Where is payments-platform deployed?")
    assert nw_run["tenant_id"] == "northwind" and co_run["tenant_id"] == "contoso"
    with pytest.raises(NotFoundError):
        svc.get_workflow(co, nw_run["id"])
    assert svc.get_workflow(nw, nw_run["id"])["id"] == nw_run["id"]
    co_listed = svc.list_workflows(co, limit=100)["runs"]
    assert co_run["id"] in {r["id"] for r in co_listed} and nw_run["id"] not in {r["id"] for r in co_listed}
    nw_listed = svc.list_workflows(nw, limit=100)["runs"]
    assert co_run["id"] not in {r["id"] for r in nw_listed}
    # the contoso answer never draws on northwind facts
    assert "northwind" not in json.dumps(co_run["answer"]).lower()


async def test_workflow_requires_graph_read(env):
    svc = env[0]
    nobody = Principal(subject="test:nobody", tenant_id="northwind", roles=frozenset({"ghost"}))
    with pytest.raises(AuthorizationError):
        await svc.run_workflow(nobody, "Which APIs are exposed by claims-portal application?")
    with pytest.raises(AuthorizationError):
        svc.list_workflows(nobody)
    with pytest.raises(AuthorizationError):
        svc.get_workflow(nobody, "any-run")


async def test_workflow_respects_customer_scope(env):
    svc = env[0]
    acme = operator("northwind", roles=("viewer",), customers=["acme"])
    run = await svc.run_workflow(acme, "Where is ledger deployed in prod?")
    text = json.dumps({k: run[k] for k in ("facts", "answer", "linked_entities")}).lower()
    assert "deployment:ledger/globex" not in text and "customer:globex" not in text


# ----------------------------------------------------------------------------- customer-restricted principals: runs, keys, audit
def test_customer_restricted_principal_sees_only_own_workflow_runs(env):
    import asyncio

    svc = env[0]
    unrestricted = operator("northwind", roles=("viewer",))
    acme = Principal(subject="test:acme-only", tenant_id="northwind", roles=frozenset({"viewer"}), allowed_customers=frozenset({"acme"}))
    other = asyncio.run(svc.run_workflow(unrestricted, "Which Kubernetes clusters host Customer B's applications?"))
    own = asyncio.run(svc.run_workflow(acme, "Which APIs are exposed by payments-platform?"))
    with pytest.raises(NotFoundError):
        svc.get_workflow(acme, other["id"])
    assert svc.get_workflow(acme, own["id"])["id"] == own["id"]
    listed = svc.list_workflows(acme, limit=100)
    assert {r["id"] for r in listed["runs"]} == {own["id"]} and listed["total"] == 1
    assert svc.get_workflow(unrestricted, own["id"])["id"] == own["id"]  # unrestricted users still see all runs


def test_customer_restricted_admin_manages_only_keys_within_scope(env):
    svc = env[0]
    tenant_admin = operator("northwind", roles=("admin",))
    acme_admin = Principal(subject="test:acme-admin", tenant_id="northwind", roles=frozenset({"admin"}), allowed_customers=frozenset({"acme"}))
    broad = svc.create_api_key(tenant_admin, "broad", ["viewer"], None)
    narrow = svc.create_api_key(tenant_admin, "narrow", ["viewer"], ["acme"])
    visible = {k["id"] for k in svc.list_api_keys(acme_admin)}
    assert narrow["id"] in visible and broad["id"] not in visible
    with pytest.raises(AuthorizationError):
        svc.revoke_api_key(acme_admin, broad["id"])
    svc.revoke_api_key(acme_admin, narrow["id"])
    with pytest.raises(AuthorizationError):
        svc.audit_log(acme_admin)
    assert svc.audit_log(tenant_admin, limit=5)

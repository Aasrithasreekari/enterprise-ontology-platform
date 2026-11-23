"""Authentication, authorization, audit, tenant isolation and customer-scope ABAC."""

import time

import jwt
import pytest

from conftest import make_settings
from ontology_platform.auth.service import AuthService
from ontology_platform.errors import AuthenticationError, AuthorizationError, NotFoundError, ValidationError


# ----------------------------------------------------------------------------- API keys
def test_api_key_lifecycle(fresh):
    auth = fresh.auth
    key, meta = auth.create_api_key("t1", "ci", ["viewer"])
    assert key.startswith("op_") and meta["roles"] == ["viewer"]
    p = auth.authenticate(f"Bearer {key}")
    assert p.tenant_id == "t1" and p.roles == frozenset({"viewer"})
    listed = auth.list_api_keys("t1")
    assert "key_hash" not in listed[0] and listed[0]["last_used_at"] is not None
    auth.revoke_api_key("t1", meta["id"])
    with pytest.raises(AuthenticationError):
        auth.authenticate(key)


def test_tampered_and_malformed_keys_rejected(fresh):
    key, _ = fresh.auth.create_api_key("t1", "k", ["viewer"])
    for bad in [key[:-2] + "xx", "op_nope_secret", "op_only", "garbage", "", None]:
        with pytest.raises(AuthenticationError):
            fresh.auth.authenticate(bad)


def test_invalid_roles_and_unknown_tenant(fresh):
    with pytest.raises(ValidationError):
        fresh.auth.create_api_key("t1", "k", ["superuser"])
    with pytest.raises(NotFoundError):
        fresh.auth.create_api_key("nope", "k", ["viewer"])


# ----------------------------------------------------------------------------- JWT
def _jwt_auth(fresh):
    return AuthService(fresh.db, make_settings(jwt_key="test-signing-key-0123456789abcdef", jwt_audience="ontology"))


def _token(**claims):
    base = {"sub": "alice", "tenant": "t1", "roles": ["viewer"], "aud": "ontology", "exp": int(time.time()) + 60}
    base.update(claims)
    return jwt.encode({k: v for k, v in base.items() if v is not None}, "test-signing-key-0123456789abcdef", algorithm="HS256")


def test_jwt_valid(fresh):
    p = _jwt_auth(fresh).authenticate(_token(customers=["Acme"]))
    assert p.subject == "jwt:alice" and p.allowed_customers == frozenset({"acme"}) and p.auth_method == "jwt"


@pytest.mark.parametrize(
    "claims",
    [{"exp": int(time.time()) - 10}, {"aud": "other"}, {"tenant": "unknown"}, {"tenant": None}, {"roles": ["root"]}, {"roles": "viewer"}],
)
def test_jwt_rejections(fresh, claims):
    with pytest.raises(AuthenticationError):
        _jwt_auth(fresh).authenticate(_token(**claims))


def test_jwt_wrong_signature_and_disabled(fresh):
    forged = jwt.encode({"sub": "x", "tenant": "t1", "roles": ["admin"], "aud": "ontology", "exp": int(time.time()) + 60}, "another-key-0123456789abcdef0000", algorithm="HS256")
    with pytest.raises(AuthenticationError):
        _jwt_auth(fresh).authenticate(forged)
    with pytest.raises(AuthenticationError, match="not configured"):
        fresh.auth.authenticate(_token())


# ----------------------------------------------------------------------------- authorization & audit
def test_role_permissions(seeded, nw_viewer):
    with pytest.raises(AuthorizationError):
        seeded.register_connector(nw_viewer, "x", "fixture", {"path": "."}, None)
    with pytest.raises(AuthorizationError):
        seeded.start_sync(nw_viewer, "any", "full")
    with pytest.raises(AuthorizationError):
        seeded.create_api_key(nw_viewer, "k", ["admin"], None)
    with pytest.raises(AuthorizationError):
        seeded.audit_log(nw_viewer)


def test_audit_log_records_success_and_denial(seeded, nw_viewer, nw_admin):
    seeded.search_entities(nw_viewer, "ledger", channel="mcp")
    with pytest.raises(AuthorizationError):
        seeded.list_connectors(nw_viewer)
    entries = seeded.audit_log(nw_admin, limit=10)
    assert entries[0]["action"] == "list_connectors" and entries[0]["outcome"] == "denied"
    assert any(e["action"] == "search_entities" and e["channel"] == "mcp" and e["outcome"] == "ok" for e in entries)


# ----------------------------------------------------------------------------- tenant isolation
def test_tenants_have_independent_graphs(seeded, contoso_viewer, nw_viewer):
    contoso_app = seeded.get_entity_details(contoso_viewer, "application:payments-platform")
    northwind_app = seeded.get_entity_details(nw_viewer, "application:payments-platform")
    assert contoso_app["entity"]["id"] != northwind_app["entity"]["id"]
    # Contoso's copy only knows its own deployment
    deps = seeded.deployment_information(contoso_viewer, "application:payments-platform")["deployments"]
    assert [d["customer"] for d in deps] == ["fabrikam"]


def test_cross_tenant_access_by_id_is_not_found(seeded, contoso_viewer, nw_viewer):
    nw_id = seeded.get_entity_details(nw_viewer, "service:ledger-service")["entity"]["id"]
    with pytest.raises(NotFoundError):
        seeded.get_entity_details(contoso_viewer, nw_id)
    assert seeded.search_entities(contoso_viewer, "ledger") == []
    assert seeded.search_documentation(contoso_viewer, "failover")["results"] == []
    a = seeded.ask(contoso_viewer, "Which clusters host Acme Bank's applications?")
    assert a["linked_entities"] == [] and a["results"] == []


def test_cross_tenant_connector_and_run_access(seeded):
    from conftest import operator

    nw_connector = seeded.connector_ids["northwind/fixtures"]
    contoso_admin = operator("contoso")
    with pytest.raises(NotFoundError):
        seeded.start_sync(contoso_admin, nw_connector, "full")
    assert all(c["id"] != nw_connector for c in seeded.list_connectors(contoso_admin))


# ----------------------------------------------------------------------------- customer scope (ABAC)
def test_customer_restricted_principal_sees_only_its_customer(seeded, acme_viewer):
    info = seeded.deployment_information(acme_viewer, "application:payments-platform")
    assert {d["customer"] for d in info["deployments"]} == {"acme"}
    with pytest.raises(NotFoundError):
        seeded.get_entity_details(acme_viewer, "customer:globex")
    # shared entities (applications, services) remain visible
    assert seeded.get_entity_details(acme_viewer, "service:auth-service")["entity"]["name"] == "auth-service"


def test_customer_restriction_applies_to_traversals_and_qa(seeded, acme_viewer):
    a = seeded.ask(acme_viewer, "Which Kubernetes clusters host Customer B's applications?")
    assert a["results"] == []
    impact = seeded.ask(acme_viewer, "What applications are affected if auth-service goes down?")["impact"]
    assert impact["affected_customers"] == ["acme"]
    related = seeded.get_related(acme_viewer, "application:claims-portal", depth=2)
    assert not any("globex" in r["entity"]["name"].lower() or "initech" in r["entity"]["name"].lower() for r in related["related"])
    stats = seeded.graph_stats(acme_viewer)
    assert stats["entities"]["customer"] == 1


def test_customer_restricted_search_hides_other_customer_docs_and_resources(seeded, acme_viewer):
    hits = seeded.search_entities(acme_viewer, "globex")
    assert all("globex" not in (h["name"] or "").lower() for h in hits)
    acme_doc = seeded.search_documentation(acme_viewer, "Acme Bank production change windows")
    assert acme_doc["results"]  # documentation is shared unless customer-tagged


def test_blank_jwt_settings_mean_disabled():
    s = make_settings(jwt_key="  ", jwt_audience="", mcp_api_key="")
    assert s.jwt_key is None and s.jwt_audience is None and s.mcp_api_key is None

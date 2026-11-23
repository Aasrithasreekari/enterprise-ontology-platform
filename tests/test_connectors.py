"""Connector tests against API-shaped mock responses (httpx.MockTransport) and stub SDK clients.

These verify request construction, pagination, auth headers, retry handling and normalization.
They do NOT prove compatibility with live tenants (no credentials in this environment).
"""

import asyncio
import json

import httpx
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa

from conftest import make_settings
from ontology_platform.connectors.base import connector_class, resolve_secret
from ontology_platform.connectors.cloud import AwsConnector
from ontology_platform.connectors.http import RetryingHttpClient, parse_link_next
from ontology_platform.errors import ConnectorError, TransientConnectorError, ValidationError

SETTINGS = make_settings()


def collect(connector, since=None):
    async def run():
        return [r async for r in connector.fetch(since=since)]

    return asyncio.run(run())


def make(ctype, config, secret, handler):
    return connector_class(ctype)(config, secret, SETTINGS, transport=httpx.MockTransport(handler))


# ----------------------------------------------------------------------------- GitHub
def test_github_pagination_files_and_auth():
    seen_auth = set()

    def handler(req: httpx.Request) -> httpx.Response:
        seen_auth.add(req.headers.get("authorization"))
        if req.url.path == "/orgs/acme/repos" and req.url.params.get("page") is None:
            return httpx.Response(200, json=[{"name": "a", "full_name": "acme/a", "html_url": "https://github.com/acme/a", "default_branch": "main", "pushed_at": "2026-02-01T00:00:00Z", "topics": ["x"]}],
                                  headers={"Link": '<https://api.github.com/orgs/acme/repos?page=2>; rel="next"'})
        if req.url.path == "/orgs/acme/repos":
            return httpx.Response(200, json=[{"name": "old", "full_name": "acme/old", "archived": True, "pushed_at": "2026-01-15T00:00:00Z"},
                                             {"name": "b", "full_name": "acme/b", "pushed_at": "2026-01-10T00:00:00Z"}])
        if req.url.path == "/repos/acme/a/contents/README.md":
            assert req.headers["accept"] == "application/vnd.github.raw"
            return httpx.Response(200, text="# A")
        return httpx.Response(404)

    c = make("github", {"org": "acme", "files": ["README.md", "catalog-info.yaml"]}, "ghp_test", handler)
    records = collect(c)
    assert [r.external_id for r in records] == ["github:acme/a", "github:acme/b"]  # archived skipped
    assert records[0].payload["files"] == {"README.md": "# A"} and records[0].payload["topics"] == ["x"]
    assert seen_auth == {"Bearer ghp_test"}
    assert c.next_cursor() == "2026-02-01T00:00:00Z"


def test_github_incremental_stops_at_cursor():
    def handler(req):
        if req.url.path == "/orgs/acme/repos":
            return httpx.Response(200, json=[{"name": "new", "full_name": "acme/new", "pushed_at": "2026-03-01T00:00:00Z"},
                                             {"name": "old", "full_name": "acme/old", "pushed_at": "2026-01-01T00:00:00Z"}])
        return httpx.Response(404)

    records = collect(make("github", {"org": "acme", "files": []}, "t", handler), since="2026-02-01T00:00:00Z")
    assert [r.external_id for r in records] == ["github:acme/new"]


# ----------------------------------------------------------------------------- GitLab / Bitbucket
def test_gitlab_projects_and_raw_files():
    def handler(req):
        assert req.headers["private-token"] == "glpat"
        if req.url.path == "/api/v4/groups/acme%2Fplatform/projects" or req.url.raw_path.startswith(b"/api/v4/groups/acme%2Fplatform/projects"):
            if req.url.params["page"] == "1":
                return httpx.Response(200, json=[{"id": 7, "path": "ledger", "path_with_namespace": "acme/platform/ledger", "web_url": "https://gitlab.com/acme/platform/ledger", "default_branch": "main", "last_activity_at": "2026-01-01T00:00:00Z"}],
                                      headers={"X-Next-Page": "2"})
            return httpx.Response(200, json=[])
        if b"/projects/7/repository/files/README.md/raw" in req.url.raw_path:
            assert req.url.params["ref"] == "main"
            return httpx.Response(200, text="ledger readme")
        return httpx.Response(404)

    records = collect(make("gitlab", {"group": "acme/platform", "files": ["README.md"]}, "glpat", handler))
    assert len(records) == 1
    assert records[0].payload["path"] == "acme/platform/ledger" and records[0].payload["files"]["README.md"] == "ledger readme"


def test_bitbucket_basic_auth_and_next_links():
    def handler(req):
        assert req.headers["authorization"].startswith("Basic ")
        if req.url.path == "/2.0/repositories/ws" and "page" not in req.url.params:
            return httpx.Response(200, json={"values": [{"full_name": "ws/claims", "slug": "claims", "name": "Claims", "mainbranch": {"name": "develop"}, "links": {"html": {"href": "https://bitbucket.org/ws/claims"}}}],
                                             "next": "https://api.bitbucket.org/2.0/repositories/ws?page=2"})
        if req.url.path == "/2.0/repositories/ws":
            return httpx.Response(200, json={"values": []})
        if req.url.path == "/2.0/repositories/ws/claims/src/develop/catalog-info.yaml":
            return httpx.Response(200, text="kind: System\nmetadata: {name: claims}\n")
        return httpx.Response(404)

    records = collect(make("bitbucket", {"workspace": "ws", "files": ["catalog-info.yaml"]}, "user:app-pass", handler))
    assert records[0].payload["default_branch"] == "develop"
    assert "catalog-info.yaml" in records[0].payload["files"]


# ----------------------------------------------------------------------------- Confluence
def test_confluence_search_pagination_and_html_to_text():
    def handler(req):
        assert req.url.path == "/wiki/rest/api/content/search"
        if "start" not in str(req.url):
            assert 'space="ENG"' in req.url.params["cql"]
            return httpx.Response(200, json={
                "results": [{"id": "11", "title": "Runbook", "body": {"storage": {"value": "<h1>Ops</h1><p>Restart &amp; check <b>ledger</b></p>"}},
                             "version": {"when": "2026-01-02T00:00:00Z"}, "metadata": {"labels": {"results": [{"name": "app-ledger"}]}},
                             "_links": {"webui": "/spaces/ENG/pages/11"}}],
                "_links": {"base": "https://acme.atlassian.net/wiki", "next": "/rest/api/content/search?cql=x&start=1"}})
        return httpx.Response(200, json={"results": [], "_links": {}})

    records = collect(make("confluence", {"base_url": "https://acme.atlassian.net", "spaces": ["ENG"]}, "me@acme.com:token", handler))
    p = records[0].payload
    assert p["body"] == "Ops\nRestart & check ledger" and p["labels"] == ["app-ledger"]
    assert p["url"] == "https://acme.atlassian.net/wiki/spaces/ENG/pages/11"


# ----------------------------------------------------------------------------- Kubernetes
def test_kubernetes_continue_token_and_normalization():
    calls = []

    def handler(req):
        calls.append(str(req.url))
        if req.url.path == "/apis/apps/v1/deployments":
            if "continue" not in req.url.params:
                return httpx.Response(200, json={"items": [{"metadata": {"name": "pg", "namespace": "acme-prod", "labels": {"app": "pg"}, "annotations": {"kubectl.kubernetes.io/last-applied-configuration": "{}", "ontology.io/uses": "arn:x"}},
                                                            "spec": {"replicas": 3, "template": {"spec": {"containers": [{"image": "pg:1"}]}}}}],
                                                 "metadata": {"continue": "tok"}})
            return httpx.Response(200, json={"items": [], "metadata": {}})
        return httpx.Response(200, json={"items": []})

    records = collect(make("kubernetes", {"cluster_name": "prod", "api_server": "https://k8s.local"}, "sa-token", handler))
    assert len(records) == 1
    p = records[0].payload
    assert p["replicas"] == 3 and p["images"] == ["pg:1"] and p["kind"] == "Deployment"
    assert "kubectl.kubernetes.io/last-applied-configuration" not in p["annotations"]
    assert any("continue=tok" in c for c in calls)


# ----------------------------------------------------------------------------- Azure / GCP / AWS
def test_azure_service_principal_token_and_skip_token():
    def handler(req):
        if req.url.host == "login.microsoftonline.com":
            body = req.content.decode()
            assert "grant_type=client_credentials" in body and "client_id=cid" in body
            return httpx.Response(200, json={"access_token": "az-token"})
        assert req.headers["authorization"] == "Bearer az-token"
        payload = json.loads(req.content)
        if "$skipToken" not in payload["options"]:
            return httpx.Response(200, json={"data": [{"id": "/subscriptions/s/resourceGroups/g/providers/Microsoft.Sql/servers/x/databases/db1", "name": "db1", "type": "Microsoft.Sql/servers/databases", "location": "westeurope", "subscriptionId": "s", "tags": {"customer": "globex"}}], "$skipToken": "next"})
        return httpx.Response(200, json={"data": []})

    secret = json.dumps({"tenant_id": "tid", "client_id": "cid", "client_secret": "cs"})
    records = collect(make("azure", {"subscriptions": ["s"]}, secret, handler))
    assert records[0].payload["resource_type"] == "microsoft.sql/servers/databases"
    assert records[0].payload["tags"] == {"customer": "globex"}


def test_gcp_service_account_jwt_flow():
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    pem = key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()).decode()

    def handler(req):
        if req.url.host == "oauth2.googleapis.com":
            assert "jwt-bearer" in req.content.decode()
            return httpx.Response(200, json={"access_token": "gcp-token"})
        assert req.headers["authorization"] == "Bearer gcp-token"
        assert req.url.path == "/v1/projects/p1/assets"
        return httpx.Response(200, json={"assets": [{"name": "//container.googleapis.com/projects/p1/locations/x/clusters/gke1", "assetType": "container.googleapis.com/Cluster",
                                                     "resource": {"location": "europe-west1", "parent": "//cloudresourcemanager.googleapis.com/projects/123", "data": {"name": "gke1", "resourceLabels": {}, "labels": {"cluster": "gke1"}}}}]})

    secret = json.dumps({"client_email": "sa@p1.iam.gserviceaccount.com", "private_key": pem})
    records = collect(make("gcp", {"scope": "projects/p1"}, secret, handler))
    assert records[0].payload["name"] == "gke1" and records[0].payload["account"] == "123"


def test_aws_tagging_api_via_injected_client():
    class Paginator:
        def paginate(self, **kwargs):
            assert kwargs["ResourceTypeFilters"] == ["rds:db"]
            yield {"ResourceTagMappingList": [{"ResourceARN": "arn:aws:rds:us-east-1:111:db:pay-db", "Tags": [{"Key": "customer", "Value": "acme"}]}]}

    class Client:
        def get_paginator(self, name):
            assert name == "get_resources"
            return Paginator()

    c = AwsConnector({"regions": ["us-east-1"], "resource_type_filters": ["rds:db"]}, None, SETTINGS)
    c.client_factory = lambda region: Client()
    records = collect(c)
    p = records[0].payload
    assert p["resource_type"] == "aws:rds:db" and p["account"] == "111" and p["tags"] == {"customer": "acme"} and p["name"] == "pay-db"


# ----------------------------------------------------------------------------- HTTP behaviour
def test_retry_on_503_then_success():
    attempts = {"n": 0}

    def handler(req):
        attempts["n"] += 1
        return httpx.Response(503) if attempts["n"] < 3 else httpx.Response(200, json={"ok": True})

    client = RetryingHttpClient("https://x", SETTINGS, transport=httpx.MockTransport(handler))
    assert asyncio.run(client.get_json("/a")) == {"ok": True}
    assert attempts["n"] == 3


def test_retry_exhaustion_raises_transient_error():
    client = RetryingHttpClient("https://x", SETTINGS, transport=httpx.MockTransport(lambda r: httpx.Response(429, headers={"Retry-After": "0"})))
    with pytest.raises(TransientConnectorError):
        asyncio.run(client.get_json("/a"))


def test_client_errors_are_not_retried():
    attempts = {"n": 0}

    def handler(req):
        attempts["n"] += 1
        return httpx.Response(401, text="bad token")

    client = RetryingHttpClient("https://x", SETTINGS, transport=httpx.MockTransport(handler))
    with pytest.raises(ConnectorError, match="401"):
        asyncio.run(client.get_json("/a"))
    assert attempts["n"] == 1


def test_link_header_parsing():
    assert parse_link_next('<https://a?page=2>; rel="next", <https://a?page=9>; rel="last"') == "https://a?page=2"
    assert parse_link_next('<https://a?page=9>; rel="last"') is None


# ----------------------------------------------------------------------------- config & secrets
def test_secret_resolution(monkeypatch, tmp_path):
    monkeypatch.setenv("OP_TEST_SECRET", "s3cr3t")
    assert resolve_secret("env:OP_TEST_SECRET") == "s3cr3t"
    f = tmp_path / "tok"
    f.write_text("filetoken\n")
    assert resolve_secret(f"file:{f}") == "filetoken"
    with pytest.raises(ConnectorError):
        resolve_secret("env:OP_MISSING_SECRET_X")
    with pytest.raises(ValidationError):
        resolve_secret("vault:foo")


def test_invalid_config_and_missing_secret_rejected():
    with pytest.raises(ValidationError):
        connector_class("github")({}, "t", SETTINGS)
    with pytest.raises(ConnectorError):
        connector_class("github")({"org": "x"}, None, SETTINGS)
    with pytest.raises(ValidationError):
        connector_class("nope")

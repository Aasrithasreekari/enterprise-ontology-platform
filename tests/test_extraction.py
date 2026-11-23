import pytest

from ontology_platform.connectors.base import SourceRecord
from ontology_platform.errors import ValidationError
from ontology_platform.extraction.structured import extract
from ontology_platform.extraction.text import MentionExtractor, chunk_text


def rels(result):
    return {(r.type.value, r.source.type.value, r.source.identity.get("name") or r.source.key, r.target.type.value, r.target.identity.get("name") or r.target.key) for r in result.relationships}


CATALOG = """
apiVersion: backstage.io/v1alpha1
kind: System
metadata: {name: payments-platform, description: Payments}
spec: {owner: team, domain: payhub}
---
kind: Component
metadata: {name: payment-gateway}
spec:
  type: service
  system: payments-platform
  dependsOn: [component:payment-processor, resource:payments-db]
  providesApis: [payments-api]
  consumesApis: [auth-api]
"""

OPENAPI = """
openapi: 3.0.3
info: {title: Payments API, version: 2.3.0, x-api-name: payments-api}
paths:
  /v2/payments: {get: {}, post: {}}
"""


def repo_record(files):
    return SourceRecord(
        kind="repository", external_id="github:acme/pg", source_system="github",
        payload={"provider": "github", "path": "acme/pg", "name": "pg", "url": "https://github.com/acme/pg", "files": files},
    )


def test_repository_with_catalog_openapi_and_readme():
    result = extract(repo_record({"catalog-info.yaml": CATALOG, "openapi.yaml": OPENAPI, "README.md": "# pg\nhello"}))
    types = {(e.type.value, e.display_name) for e in result.entities}
    assert ("repository", "acme/pg") in types
    assert ("application", "payments-platform") in types
    assert ("service", "payment-gateway") in types
    assert ("documentation", "pg README") in types
    r = rels(result)
    assert ("contains", "product", "payhub", "application", "payments-platform") in r
    assert ("contains", "application", "payments-platform", "service", "payment-gateway") in r
    assert ("has", "application", "payments-platform", "repository", "repository:github/acme/pg") in r
    assert ("depends_on", "service", "payment-gateway", "service", "payment-processor") in r
    assert ("depends_on", "service", "payment-gateway", "database", "payments-db") in r
    assert ("exposes", "application", "payments-platform", "api", "payments-api") in r
    assert ("depends_on", "service", "payment-gateway", "api", "auth-api") in r
    api = next(e for e in result.entities if e.type.value == "api")
    assert api.properties["endpoints"] == ["GET /v2/payments", "POST /v2/payments"]
    assert len(result.documents) == 1 and result.warnings == []


def test_invalid_catalog_yaml_is_a_warning_not_a_failure():
    result = extract(repo_record({"catalog-info.yaml": "kind: [unclosed", "openapi.yaml": "not: openapi"}))
    assert any("catalog-info.yaml skipped" in w for w in result.warnings)
    assert any("openapi.yaml skipped" in w for w in result.warnings)
    assert [e.type.value for e in result.entities] == ["repository"]


def test_invalid_payload_raises_validation_error():
    with pytest.raises(ValidationError):
        extract(SourceRecord(kind="repository", external_id="x", source_system="github", payload={"name": "missing provider"}))


def test_document_labels_create_describes():
    rec = SourceRecord(kind="document", external_id="confluence:1", source_system="confluence", payload={
        "source": "confluence", "external_id": "1", "title": "Runbook", "labels": ["app-payments-platform", "svc-ledger", "random"], "body": "text"})
    r = rels(extract(rec))
    assert ("describes", "documentation", "documentation:confluence/1", "application", "payments-platform") in r
    assert ("describes", "documentation", "documentation:confluence/1", "service", "ledger") in r
    assert len(r) == 2


def k8s(ns, labels, annotations=None):
    return SourceRecord(kind="k8s_workload", external_id=f"k8s:{ns}", source_system="kubernetes", payload={
        "cluster": "c1", "namespace": ns, "name": "pg", "labels": labels, "annotations": annotations or {}, "images": ["img:1"]})


def test_k8s_workload_infers_customer_and_tier_from_namespace():
    result = extract(k8s("acme-prod", {"app.kubernetes.io/name": "payment-gateway", "app.kubernetes.io/part-of": "payments-platform"},
                         {"ontology.io/uses": "arn:aws:rds:us-east-1:1:db:x"}))
    keys = {e.key for e in result.entities}
    assert "deployment:payments-platform/acme/prod" in keys
    assert "environment:acme/prod" in keys
    r = rels(result)
    assert ("belongs_to", "environment", "environment:acme/prod", "customer", "acme") in r
    assert ("deploys", "deployment", "deployment:payments-platform/acme/prod", "application", "payments-platform") in r
    assert ("runs_on", "deployment", "deployment:payments-platform/acme/prod", "kubernetes_cluster", "c1") in r
    assert any(t == "uses" for t, *_ in r)


def test_k8s_labels_override_namespace_and_shared_namespace_has_no_deployment():
    labelled = extract(k8s("shared", {"app": "billing", "ontology.io/customer": "globex", "ontology.io/environment": "UAT"}))
    assert "deployment:billing/globex/uat" in {e.key for e in labelled.entities}
    shared = extract(k8s("platform", {"app": "auth-service"}))
    assert not any(e.type.value == "deployment" for e in shared.entities)


def cloud(rtype, tags, rid="arn:aws:eks:us-east-1:1:cluster/prod-eks"):
    return SourceRecord(kind="cloud_resource", external_id=rid, source_system="aws", payload={
        "provider": "aws", "resource_id": rid, "resource_type": rtype, "name": rid.rsplit("/", 1)[-1], "tags": tags})


def test_cloud_cluster_and_database_classification():
    cl = extract(cloud("aws:eks:cluster", {}))
    assert ("hosted_on", "kubernetes_cluster", "prod-eks", "cloud_resource", "repository") not in rels(cl)
    assert {"kubernetes_cluster", "cloud_resource"} <= {e.type.value for e in cl.entities}
    db = extract(cloud("aws:rds:db", {"customer": "acme", "environment": "production", "application": "pay", "database": "payments-db"}, rid="arn:aws:rds:x:1:db:p"))
    r = rels(db)
    assert ("hosted_on", "database", "payments-db", "cloud_resource", "cloud_resource:aws/arn:aws:rds:x:1:db:p") in r
    assert ("uses", "deployment", "deployment:pay/acme/prod", "cloud_resource", "cloud_resource:aws/arn:aws:rds:x:1:db:p") in r
    res = next(e for e in db.entities if e.type.value == "cloud_resource")
    assert res.customer_scope == "acme"


def test_catalog_validation_warnings():
    rec = SourceRecord(kind="catalog", external_id="catalog:x", source_system="catalog", payload={
        "entities": [{"type": "customer", "name": "acme"}, {"type": "unknown", "name": "x"}, {"type": "product"}],
        "relationships": [{"type": "owns", "source": {"type": "customer", "name": "acme"}, "target": {"type": "product", "name": "p"}},
                          {"type": "bogus", "source": {}, "target": {}}]})
    result = extract(rec)
    assert len(result.entities) == 1 and len(result.relationships) == 1
    assert len(result.warnings) == 3


def test_chunk_text_respects_limit():
    text = "\n\n".join(f"para {i} " + "x" * 300 for i in range(10))
    chunks = chunk_text(text, max_chars=700)
    assert all(len(c) <= 700 for c in chunks)
    assert "".join(chunks).count("para") == 10
    assert chunk_text("y" * 2000, max_chars=900)[0] == "y" * 900


def test_mention_extractor_links_cross_type_and_skips_ambiguous():
    aliases = [("s1", "service", "payment-processor"), ("r1", "repository", "payment-processor"),
               ("a1", "application", "ledger"), ("a2", "application", "Ledger"), ("x", "service", "api")]
    # a1/a2 share 'ledger' within one type → ambiguous; 'api' generic → ignored
    found = MentionExtractor(aliases).find("The payment processor posts to the ledger via the api.", title="")
    assert {m.entity_id for m in found} == {"s1", "r1"}
    assert all(not m.in_title for m in found)

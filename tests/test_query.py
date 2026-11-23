"""Query engine validated against the sample enterprise and the example questions of the brief."""

import pytest

from ontology_platform.errors import NotFoundError, ValidationError


def names(items, key="entity"):
    return {i[key]["name"] for i in items}


# ----------------------------------------------------------------------------- the brief's questions
def test_q1_repositories_of_customer_product(seeded, nw_viewer):
    a = seeded.ask(nw_viewer, "Which repositories belong to Customer A's Product X?")
    assert a["intent"] == "repositories"
    assert {l["name"] for l in a["linked_entities"]} == {"Acme Bank", "PayHub"}
    assert names(a["results"]) == {"northwind/payments-gateway", "northwind/payment-processor", "northwind/fraud-detector", "northwind/ledger/ledger-service"}
    path = a["results"][0]["evidence_paths"][0]
    assert [step["type"] for step in path] == ["owns", "contains", "has"]
    assert path[0]["evidence"][0]["source_system"] == "catalog"


def test_q1_customer_not_owning_product(seeded, nw_viewer):
    a = seeded.ask(nw_viewer, "Which repositories belong to Initech Retail's PayHub?")
    assert a["results"] == [] and "does not own" in a["answer"]


def test_q2_apis_exposed_by_application(seeded, nw_viewer):
    a = seeded.ask(nw_viewer, "Which APIs are exposed by payments-platform?")
    assert a["intent"] == "apis_exposed"
    assert names(a["results"]) == {"payments-api", "webhooks-api"}
    payments = next(r for r in a["results"] if r["entity"]["name"] == "payments-api")
    assert "POST /v2/payments" in payments["endpoints"]


def test_q3_deployments_per_environment(seeded, nw_viewer):
    a = seeded.ask(nw_viewer, "Where is payments-platform deployed in SIT, UAT, and Production?")
    assert a["intent"] == "deployments"
    got = {(d["customer"], d["environment_tier"]) for d in a["results"]}
    assert got == {("acme", "sit"), ("acme", "uat"), ("acme", "prod"), ("globex", "uat"), ("globex", "prod")}
    only_prod = seeded.ask(nw_viewer, "Where is payments-platform deployed in production for Acme Bank?")
    assert [(d["customer"], d["environment_tier"]) for d in only_prod["results"]] == [("acme", "prod")]
    assert only_prod["results"][0]["clusters"][0]["name"] == "nw-prod-eks-use1"


def test_q4_dependencies(seeded, nw_viewer):
    a = seeded.ask(nw_viewer, "Which services does payments-platform depend on?")
    assert a["intent"] == "dependencies"
    deps = names(a["results"])
    assert {"auth-service", "ledger-service", "payments-db"} <= deps
    assert {c["name"] for c in a["components"]} == {"payment-gateway", "payment-processor", "fraud-detector"}


def test_q5_clusters_hosting_customer(seeded, nw_viewer):
    a = seeded.ask(nw_viewer, "Which Kubernetes clusters host Customer B's applications?")
    assert a["intent"] == "clusters_hosting"
    assert names(a["results"]) == {"nw-prod-eks-use1", "nw-prod-aks-weu", "nw-nonprod-eks-use1"}
    c = seeded.ask(nw_viewer, "Which clusters host Initech Retail's applications?")
    assert names(c["results"]) == {"nw-prod-aks-weu", "nw-nonprod-gke-euw1"}


def test_q6_documentation_for_repository(seeded, nw_viewer):
    a = seeded.ask(nw_viewer, "Which documentation describes the northwind/payment-processor repository?")
    assert a["intent"] == "documentation_for_entity"
    docs = names(a["results"])
    assert "payment-processor README" in docs
    top = a["results"][0]
    assert top["entity"]["name"] == "payment-processor README" and top["confidence"] == 1.0


def test_q7_impact_analysis(seeded, nw_viewer):
    a = seeded.ask(nw_viewer, "What applications are affected if auth-service goes down?")
    assert a["intent"] == "impact_analysis"
    impact = a["impact"]
    assert {x["name"] for x in impact["affected_applications"]} == {"payments-platform", "claims-portal", "identity-platform"}
    assert impact["affected_customers"] == ["acme", "globex", "initech"]
    assert {"payment-gateway", "claims-service", "claims-web"} <= {x["name"] for x in impact["affected_services"]}


def test_impact_includes_failing_services_own_application(seeded, nw_viewer):
    a = seeded.ask(nw_viewer, "What applications are affected if ledger-service goes down?")
    assert {x["name"] for x in a["impact"]["affected_applications"]} == {"ledger", "payments-platform"}


def test_impact_of_cluster_failure(seeded, nw_viewer):
    a = seeded.ask(nw_viewer, "What is impacted if cluster nw-nonprod-gke-euw1 fails?")
    assert a["intent"] == "impact_analysis"
    deployments = {r["entity"]["name"] for r in a["results"] if r["entity"]["type"] == "deployment"}
    assert deployments == {"claims-portal @ initech UAT"}


def test_entity_names_do_not_trigger_intents(seeded, nw_viewer):
    # "document-intake" contains "doc"; must still be a deployment question.
    a = seeded.ask(nw_viewer, "Where is document-intake deployed?")
    assert a["intent"] == "deployments"
    assert {(d["customer"], d["environment_tier"]) for d in a["results"]} == {("globex", "prod"), ("initech", "prod")}


def test_fallback_hybrid_search(seeded, nw_viewer):
    a = seeded.ask(nw_viewer, "Tell me about the ledger")
    assert a["intent"] == "hybrid_search"
    assert a["entity_hits"] and a["results"]


def test_empty_question_rejected(seeded, nw_viewer):
    with pytest.raises(ValidationError):
        seeded.ask(nw_viewer, "   ")


# ----------------------------------------------------------------------------- primitives
def test_search_entities_exact_first(seeded, nw_viewer):
    res = seeded.search_entities(nw_viewer, "Globex Insurance")
    assert res[0]["name"] == "Globex Insurance" and res[0]["match"] == "exact"
    typed = seeded.search_entities(nw_viewer, "payments", types=["api"])
    assert typed and all(r["type"] == "api" for r in typed)


def test_get_entity_details_with_history(seeded, nw_viewer):
    d = seeded.get_entity_details(nw_viewer, "service:payment-processor", include_history=True)
    assert d["entity"]["type"] == "service"
    assert {r["type"] for r in d["relationships"]} >= {"depends_on", "contains", "runs_on", "implements"}
    assert d["provenance"] and d["history"][0]["change"] == "created"


def test_resolve_ambiguity_and_not_found(seeded, nw_viewer):
    with pytest.raises(ValidationError, match="ambiguous"):
        seeded.get_entity_details(nw_viewer, "claims-portal")  # application and repository share the alias
    with pytest.raises(NotFoundError):
        seeded.get_entity_details(nw_viewer, "does-not-exist")


def test_get_related_with_filters(seeded, nw_viewer):
    r = seeded.get_related(nw_viewer, "application:claims-portal", relation_types=["contains"], direction="out")
    assert names(r["related"]) == {"claims-web", "claims-service", "claims-db"}
    # Generic BFS is broader than the customer-scoped QA pattern: via product -> application -> cluster
    # it also reaches clusters where *other* customers run the same application.
    deep = seeded.get_related(nw_viewer, "customer:globex", depth=3, target_types=["kubernetes_cluster"])
    assert {"nw-prod-eks-use1", "nw-prod-aks-weu", "nw-nonprod-eks-use1"} <= names(deep["related"])
    assert all(r["depth"] <= 3 and len(r["path"]) == r["depth"] for r in deep["related"])


def test_find_relationships_paths_with_evidence(seeded, nw_viewer):
    r = seeded.find_relationships(nw_viewer, "customer:acme", "repository:github/northwind/fraud-detector")
    assert r["connected"]
    path = r["paths"][0]
    assert path[0]["source"] == "customer:Acme Bank" and path[-1]["target"] == "repository:northwind/fraud-detector"
    assert all(step["evidence"] for step in path)


def test_trace_downstream_and_upstream(seeded, nw_viewer):
    down = seeded.trace_dependencies(nw_viewer, "service:payment-processor")
    assert {i["entity"]["name"] for i in down["items"]} == {"ledger-service", "fraud-detector", "payments-db", "ledger-db"}
    hops = {i["entity"]["name"]: i["hops"] for i in down["items"]}
    assert hops["ledger-db"] == 2
    up = seeded.trace_dependencies(nw_viewer, "database:ledger-db", direction="upstream")
    assert {"ledger-service", "payment-processor", "payment-gateway"} <= {i["entity"]["name"] for i in up["items"]}


def test_deployment_information_filters(seeded, nw_viewer):
    info = seeded.deployment_information(nw_viewer, "application:payments-platform", customer="Acme Bank", environment="production")
    (d,) = info["deployments"]
    assert d["namespace"] == "acme-prod"
    assert d["cloud_resources"][0]["name"] == "payments-db-acme-prod"
    assert set(d["workloads"]) == {"nw-prod-eks-use1/payment-gateway", "nw-prod-eks-use1/payment-processor", "nw-prod-eks-use1/fraud-detector"}
    with pytest.raises(ValidationError):
        seeded.deployment_information(nw_viewer, "application:payments-platform", environment="moon")


def test_search_documentation(seeded, nw_viewer):
    r = seeded.search_documentation(nw_viewer, "failover replica")
    assert r["results"][0]["title"] == "Payments Runbook"
    scoped = seeded.search_documentation(nw_viewer, "token introspection", entity="service:auth-service")
    assert scoped["results"] and all(x["title"] for x in scoped["results"])


def test_graph_stats(seeded, nw_viewer):
    stats = seeded.graph_stats(nw_viewer)
    assert stats["entities"]["deployment"] == 13 and stats["entities"]["customer"] == 3

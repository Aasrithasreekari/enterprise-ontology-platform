"""Agent workflow: intent classification, planning, evidence validation, synthesis, orchestration
behaviour (parallel/sequential, timeouts, retries, failures) and end-to-end scenarios."""

from __future__ import annotations

import asyncio
import time

import pytest
from sqlalchemy.exc import OperationalError

from conftest import operator
from ontology_platform.agents import specialists
from ontology_platform.agents.evidence import validate
from ontology_platform.agents.intents import classify
from ontology_platform.agents.models import AgentName, AgentOutput, Evidence, Fact, FactStatus, Intent, PlanStep, StepStatus
from ontology_platform.agents.planner import build_plan
from ontology_platform.agents.synthesis import synthesize
from ontology_platform.auth.principal import Principal
from ontology_platform.errors import AuthorizationError, NotFoundError, ValidationError
from ontology_platform.query.engine import QueryEngine
from ontology_platform.storage.repository import Scope


def run(svc, question, p=None):
    return asyncio.run(svc.run_workflow(p or operator("northwind", roles=("viewer",)), question))


@pytest.fixture
def engine(seeded):
    with seeded.db.session() as s:
        yield QueryEngine(s, Scope("northwind"), seeded.settings, seeded.cache)


# ----------------------------------------------------------------------------- intents & planning
@pytest.mark.parametrize(
    "question,expected",
    [
        ("What happens if Payment Service goes down?", [Intent.IMPACT]),
        ("Which applications depend on Payment Service?", [Intent.DEPENDENTS]),
        ("What does payments-platform depend on?", [Intent.DEPENDENCIES]),
        ("Which applications depend on Payment Service, and where are they deployed?", [Intent.DEPENDENTS, Intent.DEPLOYMENTS]),
        ("Where is document-intake deployed?", [Intent.DEPLOYMENTS]),  # "document" in the name is not a doc intent
        ("Which Kubernetes clusters host Customer B's applications?", [Intent.CLUSTERS]),
        ("Tell me about ledger", [Intent.ENTITY_LOOKUP]),
    ],
)
def test_intent_classification(engine, question, expected):
    assert classify(engine, question).intents == expected


def test_plan_runs_dependent_steps_sequentially(engine):
    plan = build_plan(classify(engine, "Which applications depend on Payment Service, and where are they deployed?"))
    s1, s2 = plan.steps[0], plan.steps[1]
    assert (s1.agent, s2.agent) == (AgentName.DEPENDENCY, AgentName.DEPLOYMENT)
    assert s2.depends_on == [s1.id] and s2.inputs["applications_from"] == s1.id
    assert [s.id for s in plan.steps[-2:]] == ["validate", "synthesize"]


def test_plan_runs_independent_steps_in_parallel(engine):
    plan = build_plan(classify(engine, "Where is claims-portal deployed and what documentation describes it?"))
    retrieval = [s for s in plan.steps if s.agent not in (AgentName.EVIDENCE, AgentName.SYNTHESIS)]
    assert {s.agent for s in retrieval} == {AgentName.DEPLOYMENT, AgentName.DOCUMENTATION}
    assert all(not s.depends_on for s in retrieval)


def test_plan_reports_missing_target_and_is_bounded(engine):
    plan = build_plan(classify(engine, "What breaks if the mainframe goes down?"))
    assert any("Impact analysis needs" in m for m in plan.missing)
    many = build_plan(classify(engine, "Repositories, APIs, clusters and documentation of payments-platform and claims-portal"), max_steps=3)
    assert len(many.steps) == 3 and any("limited" in m for m in many.missing)


# ----------------------------------------------------------------------------- evidence validation
def _fact(evidence, path=None, entity_ids=(), kind="dependency"):
    return Fact(id="f", kind=kind, statement="x", evidence=evidence, path=path or [], entity_ids=list(entity_ids), produced_by=AgentName.GRAPH)


def test_evidence_rules(engine):
    svc_id = engine.resolve("service:auth-service").id
    strong = Evidence(source_system="github", extractor="structured:repository", confidence=1.0, relationship_id="r1")
    weak = Evidence(source_system="confluence", extractor="text_mention", confidence=0.6, relationship_id="r1")
    edge = [{"relationship_id": "r1", "type": "depends_on", "source": "a", "target": "b"}]
    confirmed, inferred, unsupported, no_edge_prov = _fact([strong], edge, [svc_id]), _fact([weak], edge, [svc_id]), _fact([]), _fact([strong], edge + [{"relationship_id": "r2", "type": "has"}])
    invisible = _fact([strong], edge, ["does-not-exist"])
    report = validate(engine, [confirmed, inferred, unsupported, no_edge_prov, invisible], [], {})
    assert [f.status for f in (confirmed, inferred, unsupported, no_edge_prov, invisible)] == [
        FactStatus.CONFIRMED, FactStatus.INFERRED, FactStatus.UNSUPPORTED, FactStatus.UNSUPPORTED, FactStatus.UNSUPPORTED]
    assert report.status == "partially_validated" and report.unsupported == 3
    assert sum(i.code == "UNSUPPORTED_FACT" for i in report.issues) == 3


def test_stub_entities_are_inferred(fresh):
    """An entity only referenced by another source (a placeholder) can't make a fact confirmed."""
    from ontology_platform import admin
    from ontology_platform.connectors.base import SourceRecord

    c = admin.register_connector(fresh.db, "t1", "hook", "push", {})
    record = SourceRecord.model_validate({
        "kind": "catalog", "external_id": "catalog:x", "source_system": "cmdb",
        "payload": {"entities": [{"type": "customer", "name": "acme"}],
                    "relationships": [{"type": "owns", "source": {"type": "customer", "name": "acme"}, "target": {"type": "product", "name": "undefined-product"}}]},
    })
    asyncio.run(fresh.push_records(operator("t1"), c["id"], [record]))
    with fresh.db.session() as s:
        eng = QueryEngine(s, Scope("t1"), fresh.settings, fresh.cache)
        stub = eng.resolve("product:undefined-product")
        assert stub.status == "stub"
        f = _fact([Evidence(source_system="cmdb", extractor="structured:catalog", confidence=1.0)], entity_ids=[stub.id], kind="entity")
        validate(eng, [f], [], {})
    assert f.status == FactStatus.INFERRED and "not defined by its own" in f.validation_notes[0]


def test_synthesis_excludes_unsupported_and_never_invents():
    step = PlanStep(id="s1", agent=AgentName.GRAPH, action="repositories", purpose="p", status=StepStatus.COMPLETED)
    ok = Fact(id="f1", kind="repository", statement="repo A", produced_by=AgentName.GRAPH, status=FactStatus.CONFIRMED,
              object={"id": "1", "type": "repository", "name": "A"}, evidence=[Evidence(source_system="github", source_url="https://x/a")])
    bad = Fact(id="f2", kind="repository", statement="repo B", produced_by=AgentName.GRAPH, status=FactStatus.UNSUPPORTED,
               object={"id": "2", "type": "repository", "name": "B"})
    from ontology_platform.agents.models import ValidationReport

    ans = synthesize("q", [step], {"s1": AgentOutput(facts=[ok, bad])}, [ok, bad], ValidationReport(status="partially_validated", confirmed=1, unsupported=1), [])
    text = ans.summary + " ".join(i for s in ans.sections for i in s.items)
    assert "A" in ans.summary and "repo B" not in text
    assert ans.confirmed_facts == ["repo A"] and [s.source_url for s in ans.sources] == ["https://x/a"]
    assert any("excluded" in l for l in ans.limitations)


# ----------------------------------------------------------------------------- orchestration behaviour
def test_parallel_batch_and_trace(seeded):
    r = run(seeded, "Where is claims-portal deployed and what documentation describes it?")
    assert r["status"] == "completed"
    batch = [e for e in r["events"] if e["type"] == "parallel_batch"]
    assert batch and len(batch[0]["data"]["steps"]) == 2
    assert all(s["status"] == "completed" and s["duration_ms"] is not None for s in r["plan"])
    types = [e["type"] for e in r["events"]]
    assert types[0] == "workflow_started" and types[-1] == "workflow_finished"
    assert {"intent_classified", "plan_created", "step_started", "step_completed"} <= set(types)


def test_step_timeout_fails_and_skips_dependents(seeded, monkeypatch):
    class Slow:
        name = AgentName.DEPENDENCY

        def run(self, *a, **k):
            time.sleep(0.5)
            return AgentOutput()

    monkeypatch.setitem(specialists.AGENTS, AgentName.DEPENDENCY, Slow())
    monkeypatch.setattr(seeded.orchestrator.settings, "agent_step_timeout_seconds", 0.1)
    monkeypatch.setattr(seeded.orchestrator.settings, "agent_max_retries", 0)
    r = run(seeded, "Which applications depend on Payment Service, and where are they deployed?")
    steps = {s["id"]: s for s in r["plan"]}
    assert steps["s1"]["status"] == "failed" and "timed out" in steps["s1"]["error"]
    assert steps["s2"]["status"] == "skipped"
    assert r["status"] == "failed" and r["validation"]["status"] == "failed"
    assert r["answer"]["confirmed_facts"] == [] and "not available" in r["answer"]["summary"]


def test_transient_error_is_retried_once(seeded, monkeypatch):
    real = specialists.AGENTS[AgentName.GRAPH]
    calls = {"n": 0}

    class Flaky:
        name = AgentName.GRAPH

        def run(self, *a, **k):
            calls["n"] += 1
            if calls["n"] == 1:
                raise OperationalError("select", {}, Exception("connection reset"))
            return real.run(*a, **k)

    monkeypatch.setitem(specialists.AGENTS, AgentName.GRAPH, Flaky())
    r = run(seeded, "Which repositories belong to Customer A's Product X?")
    step = r["plan"][0]
    assert step["status"] == "completed" and step["attempts"] == 2
    assert any(e["type"] == "step_retry" for e in r["events"])


def test_deterministic_error_is_not_retried(seeded, monkeypatch):
    class Broken:
        name = AgentName.GRAPH

        def run(self, *a, **k):
            raise KeyError("boom")

    monkeypatch.setitem(specialists.AGENTS, AgentName.GRAPH, Broken())
    r = run(seeded, "Which repositories belong to Customer A's Product X?")
    assert r["plan"][0]["attempts"] == 1 and r["plan"][0]["status"] == "failed"
    assert any(i["code"] == "STEP_FAILED" for i in r["validation"]["issues"])


def test_permissions_and_input_validation(seeded):
    nobody = Principal(subject="x", tenant_id="northwind", roles=frozenset())
    with pytest.raises(AuthorizationError):
        run(seeded, "Which repositories belong to Customer A's Product X?", nobody)
    with pytest.raises(ValidationError):
        run(seeded, "  ")


def test_runs_are_persisted_and_tenant_scoped(seeded):
    r = run(seeded, "Which APIs are exposed by payments-platform?")
    viewer = operator("northwind", roles=("viewer",))
    assert seeded.get_workflow(viewer, r["id"])["answer"]["summary"] == r["answer"]["summary"]
    listed = seeded.list_workflows(viewer, limit=100)
    assert any(x["id"] == r["id"] for x in listed["runs"]) and listed["total"] >= 1
    with pytest.raises(NotFoundError):
        seeded.get_workflow(operator("contoso", roles=("viewer",)), r["id"])
    assert all(x["id"] != r["id"] for x in seeded.list_workflows(operator("contoso", roles=("viewer",)))["runs"])


def test_customer_scope_applies_inside_agents(seeded):
    acme = operator("northwind", roles=("viewer",), customers=["acme"])
    r = run(seeded, "Which applications depend on Payment Service, and where are they deployed?", acme)
    assert r["answer"]["confirmed_facts"]
    assert all("globex" not in f.lower() for f in r["answer"]["confirmed_facts"] + r["answer"]["inferred_facts"])


# ----------------------------------------------------------------------------- end-to-end scenarios
def facts_text(r):
    return " ".join(r["answer"]["confirmed_facts"] + r["answer"]["inferred_facts"])


def test_e2e_1_repositories_of_customer_product(seeded):
    r = run(seeded, "Which repositories belong to Customer A's Product X?")
    assert r["validation"]["status"] == "validated"
    for repo in ("northwind/payments-gateway", "northwind/payment-processor", "northwind/fraud-detector", "northwind/ledger/ledger-service"):
        assert repo in r["answer"]["summary"]


def test_e2e_2_application_deployments_by_environment(seeded):
    r = run(seeded, "Where is Application Y deployed across SIT, UAT and Production?")
    text = facts_text(r)
    for expected in ("acme in SIT on nw-nonprod-eks-use1", "acme in UAT on nw-nonprod-eks-use1", "acme in PROD on nw-prod-eks-use1",
                     "globex in PROD on nw-prod-eks-use1"):
        assert f"payments-platform is deployed for {expected}" in text
    assert r["validation"]["status"] == "validated"


def test_missing_environment_is_reported(seeded):
    r = run(seeded, "Where is claims-portal deployed in SIT and UAT?")
    assert "No SIT deployment recorded for claims-portal." in r["answer"]["missing_information"]
    assert "globex in UAT" in facts_text(r)


def test_e2e_3_clusters_hosting_customer(seeded):
    r = run(seeded, "Which Kubernetes clusters host Customer B's applications?")
    assert all(c in r["answer"]["summary"] for c in ("nw-prod-eks-use1", "nw-prod-aks-weu", "nw-nonprod-eks-use1"))
    assert "nw-nonprod-gke-euw1" not in r["answer"]["summary"]


def test_e2e_4_impact_if_payment_service_goes_down(seeded):
    r = run(seeded, "What happens if Payment Service goes down?")
    assert "payments-platform" in r["answer"]["summary"] and "acme" in r["answer"]["summary"]
    assert any("not observed failures" in l for l in r["answer"]["limitations"])


def test_e2e_5_dependents_of_payment_service(seeded):
    r = run(seeded, "Which applications depend on Payment Service?")
    assert "payment-gateway depends on payment-processor" in facts_text(r)


def test_e2e_6_documentation_on_authentication(seeded):
    r = run(seeded, "What documentation describes Payment Service authentication?")
    assert r["intents"] == ["documentation"]
    titles = " ".join(r["answer"]["confirmed_facts"] + r["answer"]["inferred_facts"])
    assert "Auth Service Integration Guide" in titles or "payment-processor README" in titles
    assert r["answer"]["sources"]


def test_e2e_7_dependents_and_their_deployments(seeded):
    r = run(seeded, "Which applications depend on Payment Service, and where are they deployed?")
    plan = {s["id"]: s for s in r["plan"]}
    assert plan["s2"]["depends_on"] == ["s1"] and plan["s2"]["status"] == "completed"
    assert "payments-platform is deployed for acme in PROD on nw-prod-eks-use1" in facts_text(r)
    assert r["validation"]["status"] == "validated"


def test_e2e_8_relationship_chain(seeded):
    r = run(seeded, "What is the relationship between Customer A, Product X, Application Y and its repository?")
    assert set(r["intents"]) == {"relationship", "repositories"}
    text = facts_text(r)
    assert "Acme Bank -[owns]-> PayHub" in text and "PayHub -[contains]-> payments-platform" in text
    assert "payments-platform -[has]-> northwind/payments-gateway" in text

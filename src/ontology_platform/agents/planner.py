"""Turns classified intents and linked entities into an execution plan.

Steps without dependencies run in parallel; a step that needs another step's output declares it in
`depends_on` (e.g. deployments of the applications found by an impact analysis). The plan always
ends with evidence validation and response synthesis. Plans are bounded by `max_steps`.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from ontology_platform.agents.intents import Classification
from ontology_platform.agents.models import AgentName, Intent, PlanStep
from ontology_platform.ontology.schema import EntityType as E

IMPACT_TARGETS = (E.SERVICE, E.APPLICATION, E.DATABASE, E.API, E.CLOUD_RESOURCE)
APP_TARGETS = (E.APPLICATION, E.SERVICE)


@dataclass
class Plan:
    steps: list[PlanStep]
    missing: list[str] = field(default_factory=list)


def _of(cls: Classification, types: tuple[E, ...]) -> list[str]:
    wanted = {t.value for t in types}
    return [l.entity.id for l in cls.linked if l.entity.type in wanted]


def build_plan(cls: Classification, max_steps: int = 12) -> Plan:
    steps: list[PlanStep] = []
    missing: list[str] = []
    intents = cls.intents

    def add(agent: AgentName, action: str, purpose: str, inputs: dict, depends_on: list[str] | None = None) -> PlanStep:
        step = PlanStep(id=f"s{len(steps) + 1}", agent=agent, action=action, purpose=purpose, inputs=inputs, depends_on=depends_on or [])
        steps.append(step)
        return step

    upstream_step: PlanStep | None = None
    if Intent.IMPACT in intents or Intent.DEPENDENTS in intents:
        targets = _of(cls, IMPACT_TARGETS)
        if not targets:
            missing.append("Impact analysis needs a service, application, database or API; none was recognised in the question.")
        for target in targets[:2]:
            upstream_step = add(AgentName.DEPENDENCY, "impact", "Find components and applications that depend on the target", {"entity_id": target})

    if Intent.DEPENDENCIES in intents:
        targets = _of(cls, APP_TARGETS)
        if not targets:
            missing.append("Dependency tracing needs an application or service; none was recognised in the question.")
        for target in targets[:2]:
            add(AgentName.DEPENDENCY, "dependencies", "Trace what the target depends on", {"entity_id": target})

    if Intent.DEPLOYMENTS in intents:
        customers = [l.entity.identity.get("name", l.entity.name) for l in cls.linked if l.entity.type == E.CUSTOMER.value]
        if upstream_step is not None:
            # "...and where are they deployed?": deployments of the applications found upstream.
            add(AgentName.DEPLOYMENT, "deployments", "Locate deployments of the affected applications",
                {"applications_from": upstream_step.id, "customers": customers}, [upstream_step.id])
        else:
            apps = _of(cls, APP_TARGETS)
            if apps:
                add(AgentName.DEPLOYMENT, "deployments", "Locate deployments per customer and environment", {"entity_ids": apps[:5], "customers": customers})
            elif Intent.CLUSTERS not in intents:
                missing.append("Deployment lookup needs an application or service; none was recognised in the question.")

    graph_ids = [l.entity.id for l in cls.linked]
    app_ids = _of(cls, (E.APPLICATION,))
    for intent, action, purpose in (
        (Intent.REPOSITORIES, "repositories", "Find repositories through ownership and composition relationships"),
        (Intent.APIS, "apis", "Find APIs exposed by the application or its services"),
        (Intent.CLUSTERS, "clusters", "Find Kubernetes clusters hosting the deployments"),
    ):
        if intent in intents:
            # In a relationship question ("... Application Y and its repository") the repositories asked
            # for are the application's own, not every repository of the product.
            ids = app_ids if (Intent.RELATIONSHIP in intents and action == "repositories" and app_ids) else graph_ids
            if ids:
                add(AgentName.GRAPH, action, purpose, {"entity_ids": ids})
            else:
                missing.append(f"{action.capitalize()} lookup needs a customer, product, application or service; none was recognised.")

    if Intent.RELATIONSHIP in intents:
        if len(graph_ids) >= 2:
            add(AgentName.GRAPH, "relationship_paths", "Explain how the entities are connected", {"entity_ids": graph_ids[:4]})
        elif graph_ids:
            add(AgentName.GRAPH, "entity_details", "Show the entity and its direct relationships", {"entity_ids": graph_ids[:1]})
        else:
            missing.append("Relationship discovery needs at least two recognised entities.")

    if Intent.ENTITY_LOOKUP in intents and graph_ids:
        add(AgentName.GRAPH, "entity_details", "Show the entity and its direct relationships", {"entity_ids": graph_ids[:3]})

    if Intent.DOCUMENTATION in intents:
        docs_for = [l.entity.id for l in cls.linked if l.entity.type != E.DOCUMENTATION.value][:3]
        add(AgentName.DOCUMENTATION, "documentation", "Retrieve documentation and relevant passages",
            {"entity_ids": docs_for, "query": cls.masked_question.strip() or None})

    retrieval = steps[: max(max_steps - 2, 1)]
    if len(steps) > len(retrieval):
        missing.append(f"The plan was limited to {len(retrieval)} retrieval steps.")
    ids = [s.id for s in retrieval]
    validate = PlanStep(id="validate", agent=AgentName.EVIDENCE, action="validate", purpose="Check every fact against its provenance", depends_on=ids)
    synth = PlanStep(id="synthesize", agent=AgentName.SYNTHESIS, action="synthesize", purpose="Compose the grounded answer", depends_on=["validate"])
    return Plan(steps=retrieval + [validate, synth], missing=missing)

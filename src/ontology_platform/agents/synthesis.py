"""Response synthesis agent: composes the final answer from validated facts only.

It never adds information: every sentence is a fact statement, a count over facts, or a fixed
limitation statement. Unsupported facts are excluded and reported through validation issues.
"""

from __future__ import annotations

from ontology_platform.agents.models import (
    AgentName,
    AgentOutput,
    AnswerSection,
    Evidence,
    Fact,
    FactStatus,
    FinalAnswer,
    PlanStep,
    StepStatus,
    ValidationReport,
)

SECTION_TITLES = {
    "impact": "Potentially affected applications",
    "dependencies": "Dependencies",
    "deployments": "Deployments",
    "repositories": "Repositories",
    "apis": "APIs",
    "clusters": "Kubernetes clusters",
    "relationship_paths": "How the entities are connected",
    "entity_details": "Entity details",
    "documentation": "Documentation",
}

LIMITATIONS = {
    "impact": "Impact is derived from declared dependencies (catalog dependsOn/consumesApis and deployment annotations); "
    "it shows components that could be affected, not observed failures.",
    "documentation": "Documentation passages are ranked by keyword relevance (BM25), not semantic similarity.",
    "deployments": "Deployment data reflects the last successful ingestion of the Kubernetes and cloud sources.",
}


def _tag(f: Fact) -> str:
    return f.statement if f.status == FactStatus.CONFIRMED else f"{f.statement} [inferred: {'; '.join(f.validation_notes) or 'weak evidence'}]"


def synthesize(question: str, steps: list[PlanStep], outputs: dict[str, AgentOutput], facts: list[Fact],
               report: ValidationReport, plan_missing: list[str]) -> FinalAnswer:
    usable = [f for f in facts if f.status in (FactStatus.CONFIRMED, FactStatus.INFERRED)]
    by_step: dict[str, list[Fact]] = {}
    for step in steps:
        out = outputs.get(step.id)
        if out:
            ids = {f.id for f in out.facts}
            by_step[step.id] = [f for f in usable if f.id in ids]

    summary: list[str] = []
    sections: list[AnswerSection] = []
    for step in steps:
        if step.agent in (AgentName.EVIDENCE, AgentName.SYNTHESIS):
            continue
        out, step_facts = outputs.get(step.id), by_step.get(step.id, [])
        if step.status != StepStatus.COMPLETED or out is None:
            summary.append(f"{SECTION_TITLES.get(step.action, step.action)}: not available ({step.status.value}).")
            continue
        if step.action == "impact":
            root = out.data["root"]["name"]
            apps = out.data.get("affected_application_names", [])
            custs = out.data.get("affected_customers", [])
            summary.append(
                f"If {root} becomes unavailable, {len(apps)} application(s) are potentially affected"
                + (f": {', '.join(apps)}" if apps else "") + "."
                + (f" Customers with deployments of these applications: {', '.join(custs)}." if custs else "")
            )
        elif step.action == "dependencies":
            names = [f.object.name for f in step_facts if f.object]
            summary.append(f"{out.data['root']['name']} depends on {len(names)} component(s)" + (f": {', '.join(names)}." if names else "."))
        elif step.action == "deployments":
            summary.append(f"{len([f for f in step_facts])} deployment(s) found across "
                           f"{len(out.data.get('deployments', {}))} application(s).")
        elif step.action in ("repositories", "apis", "clusters"):
            names = sorted({f.object.name for f in step_facts if f.object})
            summary.append(f"{SECTION_TITLES[step.action]}: {', '.join(names) if names else 'none found'}.")
        elif step.action == "relationship_paths":
            summary.append(f"{len(step_facts)} connecting path(s) found." if step_facts else "No connecting path found.")
        elif step.action == "documentation":
            docs = [f for f in step_facts if f.kind == "document"]
            passages = [f for f in step_facts if f.kind == "passage"]
            summary.append(f"{len(docs)} linked document(s) and {len(passages)} relevant passage(s).")
        elif step.action == "entity_details":
            summary.extend(f.statement + "." for f in step_facts if f.kind == "entity")
        items = [_tag(f) for f in step_facts] + out.notes[:10]
        if items:
            sections.append(AnswerSection(title=SECTION_TITLES.get(step.action, step.action), items=items, fact_ids=[f.id for f in step_facts]))

    missing = list(dict.fromkeys(plan_missing + [m for o in outputs.values() for m in o.missing]))
    limitations = [LIMITATIONS[s.action] for s in steps if s.action in LIMITATIONS and s.status == StepStatus.COMPLETED]
    failed = [s for s in steps if s.status in (StepStatus.FAILED, StepStatus.SKIPPED) and s.agent not in (AgentName.SYNTHESIS,)]
    limitations += [f"{s.agent.value} ({s.action}) {s.status.value}: {s.error or s.skip_reason}" for s in failed]
    if report.unsupported:
        limitations.append(f"{report.unsupported} statement(s) lacked supporting evidence and were excluded.")

    sources: list[Evidence] = []
    seen: set[tuple[str, str | None]] = set()
    for f in usable:
        for ev in f.evidence:
            key = (ev.source_system, ev.source_url)
            if key not in seen:
                seen.add(key)
                sources.append(Evidence(source_system=ev.source_system, source_url=ev.source_url, extractor=ev.extractor))
    if not summary:
        summary.append("No information could be retrieved for this question.")
    return FinalAnswer(
        summary=" ".join(list(dict.fromkeys(summary))),
        sections=sections,
        confirmed_facts=[f.statement for f in usable if f.status == FactStatus.CONFIRMED],
        inferred_facts=[_tag(f) for f in usable if f.status == FactStatus.INFERRED],
        missing_information=missing,
        limitations=list(dict.fromkeys(limitations)),
        sources=sources[:25],
    )

"""Evidence and validation agent: grades every fact against stored provenance.

Rules (deterministic):
* UNSUPPORTED — a fact without any provenance, an edge on its path without provenance, or a fact
  relying on an entity that is stale or no longer visible. Unsupported facts never appear as
  established facts in the answer.
* INFERRED — every edge has provenance, but at least one rests only on a text mention, a reference
  (an entity named by another source but not defined by its own), confidence below 0.8, or a
  placeholder (stub) entity.
* CONFIRMED — everything else: each edge is backed by a structured source at confidence ≥ 0.8.
"""

from __future__ import annotations

from ontology_platform.agents.models import AgentOutput, Fact, FactStatus, PlanStep, StepStatus, ValidationIssue, ValidationReport
from ontology_platform.query.engine import QueryEngine

WEAK_EXTRACTORS = {"text_mention", "reference", "bm25_passage"}
MIN_CONFIDENCE = 0.8


def _strong(ev) -> bool:
    return (ev.extractor or "") not in WEAK_EXTRACTORS and (ev.confidence is None or ev.confidence >= MIN_CONFIDENCE)


def validate(engine: QueryEngine, facts: list[Fact], steps: list[PlanStep], outputs: dict[str, AgentOutput]) -> ValidationReport:
    issues: list[ValidationIssue] = []
    entity_ids = sorted({i for f in facts for i in f.entity_ids})
    visible = {e.id: e for e in engine.repo.find_entities(ids=entity_ids, include_stale=True)} if entity_ids else {}

    for fact in facts:
        notes: list[str] = []
        status = FactStatus.CONFIRMED
        if not fact.evidence:
            status = FactStatus.UNSUPPORTED
            notes.append("no provenance recorded")
        for eid in fact.entity_ids:
            ent = visible.get(eid)
            if ent is None:
                status = FactStatus.UNSUPPORTED
                notes.append(f"entity {eid} is not visible to the caller")
            elif ent.status == "stale":
                status = FactStatus.UNSUPPORTED
                notes.append(f"{ent.name} is stale (no longer observed by any source)")
            elif ent.status == "stub" and status == FactStatus.CONFIRMED:
                status = FactStatus.INFERRED
                notes.append(f"{ent.name} is referenced by another source but not defined by its own")
        for edge in fact.path:
            edge_ev = [e for e in fact.evidence if e.relationship_id == edge.get("relationship_id")]
            if not edge_ev:
                status = FactStatus.UNSUPPORTED
                notes.append(f"edge {edge.get('type')} {edge.get('source')}→{edge.get('target')} has no provenance")
            elif not any(_strong(e) for e in edge_ev) and status == FactStatus.CONFIRMED:
                status = FactStatus.INFERRED
                kinds = ", ".join(sorted({e.extractor or '?' for e in edge_ev}))
                notes.append(f"edge {edge.get('type')} {edge.get('source')}→{edge.get('target')} is only supported by {kinds}")
        if not fact.path and fact.evidence and status == FactStatus.CONFIRMED and not any(_strong(e) for e in fact.evidence):
            if fact.kind != "passage":  # a passage is evidence of the document's content, not a graph claim
                status = FactStatus.INFERRED
                notes.append("supported only by weak evidence")
        fact.status = status
        fact.validation_notes = notes
        if status == FactStatus.UNSUPPORTED:
            issues.append(ValidationIssue(severity="error", code="UNSUPPORTED_FACT", message=f"{fact.statement} — {'; '.join(notes)}", fact_id=fact.id))

    for step in steps:
        if step.status == StepStatus.FAILED:
            issues.append(ValidationIssue(severity="error", code="STEP_FAILED", message=f"{step.agent.value} ({step.action}) failed: {step.error}"))
        elif step.status == StepStatus.SKIPPED:
            issues.append(ValidationIssue(severity="warning", code="STEP_SKIPPED", message=f"{step.agent.value} ({step.action}) skipped: {step.skip_reason}"))
    for out in outputs.values():
        for cycle_member in out.data.get("cycles", []):
            issues.append(ValidationIssue(severity="warning", code="CIRCULAR_DEPENDENCY",
                                          message=f"{cycle_member} is both upstream and downstream of {out.data['root']['name']}; impact may be overstated"))
        for m in out.missing:
            issues.append(ValidationIssue(severity="info", code="MISSING_INFORMATION", message=m))
    # Contradiction check: the same claim must not be both supported and unsupported.
    by_statement: dict[str, set[FactStatus]] = {}
    for f in facts:
        by_statement.setdefault(f.statement, set()).add(f.status)  # type: ignore[arg-type]
    for statement, statuses in by_statement.items():
        if FactStatus.UNSUPPORTED in statuses and len(statuses) > 1:
            issues.append(ValidationIssue(severity="warning", code="CONFLICTING_EVIDENCE", message=f"Conflicting evidence for: {statement}"))

    counts = {s: sum(1 for f in facts if f.status == s) for s in FactStatus}
    failed_steps = [s for s in steps if s.status == StepStatus.FAILED]
    if not facts:
        overall = "failed" if failed_steps else "insufficient_evidence"
    elif counts[FactStatus.UNSUPPORTED] == 0 and counts[FactStatus.INFERRED] == 0 and not failed_steps:
        overall = "validated"
    else:
        overall = "partially_validated"
    return ValidationReport(
        status=overall,
        confirmed=counts[FactStatus.CONFIRMED],
        inferred=counts[FactStatus.INFERRED],
        unsupported=counts[FactStatus.UNSUPPORTED],
        issues=issues,
    )

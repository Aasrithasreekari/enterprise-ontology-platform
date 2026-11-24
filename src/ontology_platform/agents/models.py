"""Data contracts of the agent workflow: intents, plans, facts, evidence, validation and runs.

Agents exchange these structured objects only; no free-form text is passed between agents, which
keeps every claim in the final answer traceable to a fact and every fact to its evidence.
"""

from __future__ import annotations

from datetime import UTC, datetime
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, Field


def now() -> datetime:
    return datetime.now(UTC)


class Intent(StrEnum):
    REPOSITORIES = "repositories"
    APIS = "apis"
    DEPLOYMENTS = "deployments"
    DEPENDENCIES = "dependencies"  # what X depends on (downstream)
    DEPENDENTS = "dependents"  # what depends on X (upstream)
    IMPACT = "impact"  # what breaks if X fails (upstream + roll-up)
    CLUSTERS = "clusters"
    DOCUMENTATION = "documentation"
    RELATIONSHIP = "relationship"  # how entities are connected
    ENTITY_LOOKUP = "entity_lookup"


class AgentName(StrEnum):
    GRAPH = "graph_intelligence"
    DOCUMENTATION = "documentation_intelligence"
    DEPENDENCY = "dependency_impact"
    DEPLOYMENT = "deployment_intelligence"
    EVIDENCE = "evidence_validation"
    SYNTHESIS = "response_synthesis"


class StepStatus(StrEnum):
    PENDING = "pending"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    SKIPPED = "skipped"


class RunStatus(StrEnum):
    RUNNING = "running"
    COMPLETED = "completed"
    COMPLETED_WITH_ERRORS = "completed_with_errors"
    FAILED = "failed"
    CANCELLED = "cancelled"


class FactStatus(StrEnum):
    CONFIRMED = "confirmed"  # every edge backed by a structured source
    INFERRED = "inferred"  # relies on text mentions, references or low-confidence edges
    UNSUPPORTED = "unsupported"  # no provenance, or stale data


class EntityRefOut(BaseModel):
    id: str
    type: str
    name: str
    key: str | None = None
    status: str | None = None


class Evidence(BaseModel):
    """One piece of provenance as stored by ingestion; never synthesized by agents."""

    source_system: str
    source_url: str | None = None
    extractor: str | None = None
    confidence: float | None = None
    excerpt: str | None = None
    relationship_id: str | None = None


class Fact(BaseModel):
    id: str
    kind: str  # e.g. repository, api, dependency, impact, deployment, document, relationship, entity
    statement: str
    subject: EntityRefOut | None = None
    object: EntityRefOut | None = None
    path: list[dict[str, Any]] = Field(default_factory=list, description="Graph edges supporting the fact")
    evidence: list[Evidence] = Field(default_factory=list)
    entity_ids: list[str] = Field(default_factory=list, description="All entities the fact relies on")
    produced_by: AgentName
    status: FactStatus | None = None  # set by the evidence agent
    validation_notes: list[str] = Field(default_factory=list)


class AgentOutput(BaseModel):
    facts: list[Fact] = Field(default_factory=list)
    data: dict[str, Any] = Field(default_factory=dict, description="Structured results passed to dependent steps")
    missing: list[str] = Field(default_factory=list)
    notes: list[str] = Field(default_factory=list)
    tools: list[str] = Field(default_factory=list, description="Platform operations invoked")


class PlanStep(BaseModel):
    id: str
    agent: AgentName
    action: str
    purpose: str
    inputs: dict[str, Any] = Field(default_factory=dict)
    depends_on: list[str] = Field(default_factory=list)
    status: StepStatus = StepStatus.PENDING
    attempts: int = 0
    started_at: datetime | None = None
    finished_at: datetime | None = None
    duration_ms: float | None = None
    tools: list[str] = Field(default_factory=list)
    error: str | None = None
    skip_reason: str | None = None
    summary: str | None = None


class WorkflowEvent(BaseModel):
    at: datetime = Field(default_factory=now)
    type: str
    step_id: str | None = None
    agent: AgentName | None = None
    message: str
    data: dict[str, Any] = Field(default_factory=dict)


class ValidationIssue(BaseModel):
    severity: str  # error | warning | info
    code: str
    message: str
    fact_id: str | None = None


class ValidationReport(BaseModel):
    status: str  # validated | partially_validated | insufficient_evidence | failed
    confirmed: int = 0
    inferred: int = 0
    unsupported: int = 0
    issues: list[ValidationIssue] = Field(default_factory=list)


class AnswerSection(BaseModel):
    title: str
    items: list[str]
    fact_ids: list[str] = Field(default_factory=list)


class FinalAnswer(BaseModel):
    summary: str
    sections: list[AnswerSection] = Field(default_factory=list)
    confirmed_facts: list[str] = Field(default_factory=list)
    inferred_facts: list[str] = Field(default_factory=list)
    missing_information: list[str] = Field(default_factory=list)
    limitations: list[str] = Field(default_factory=list)
    sources: list[Evidence] = Field(default_factory=list)


class WorkflowRun(BaseModel):
    id: str
    tenant_id: str
    principal: str
    correlation_id: str | None = None
    query: str
    status: RunStatus = RunStatus.RUNNING
    intents: list[Intent] = Field(default_factory=list)
    linked_entities: list[EntityRefOut] = Field(default_factory=list)
    plan: list[PlanStep] = Field(default_factory=list)
    events: list[WorkflowEvent] = Field(default_factory=list)
    facts: list[Fact] = Field(default_factory=list)
    validation: ValidationReport | None = None
    answer: FinalAnswer | None = None
    started_at: datetime = Field(default_factory=now)
    finished_at: datetime | None = None
    duration_ms: float | None = None

    def step(self, step_id: str) -> PlanStep:
        return next(s for s in self.plan if s.id == step_id)

    def event(self, type_: str, message: str, step: PlanStep | None = None, **data: Any) -> None:
        self.events.append(
            WorkflowEvent(type=type_, message=message, step_id=step.id if step else None, agent=step.agent if step else None, data=data)
        )

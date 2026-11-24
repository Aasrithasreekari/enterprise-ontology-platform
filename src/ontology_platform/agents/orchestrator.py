"""Agent orchestrator: classify → plan → execute (parallel/sequential) → validate → synthesize.

Execution model
* Steps whose dependencies are complete run concurrently, bounded by a semaphore
  (`agent_max_parallel`; 1 when the database cannot be used concurrently, e.g. in-memory SQLite).
* Each step runs in a worker thread with its own database session and a QueryEngine bound to the
  caller's scope, under a timeout. Only transient failures (database operational errors, transient
  connector errors, timeouts) are retried, at most `agent_max_retries` times.
* A step whose dependency failed is skipped, never run on missing input. Validation and synthesis
  always run, so failures are reported rather than hidden.
* Plans are bounded (`agent_max_steps`); there are no loops, so execution always terminates.
A timed-out step is reported as failed; Python cannot interrupt its worker thread, which finishes in
the background and whose result is discarded.
"""

from __future__ import annotations

import asyncio
import time
import uuid
from typing import Any

from sqlalchemy.exc import OperationalError

from ontology_platform.agents.evidence import validate
from ontology_platform.agents.intents import classify
from ontology_platform.agents.models import (
    AgentName,
    AgentOutput,
    EntityRefOut,
    PlanStep,
    RunStatus,
    StepStatus,
    WorkflowRun,
    now,
)
from ontology_platform.agents.planner import build_plan
from ontology_platform.agents.specialists import AGENTS
from ontology_platform.agents.store import WorkflowStore
from ontology_platform.agents.synthesis import synthesize
from ontology_platform.auth.principal import Permission, Principal
from ontology_platform.config import Settings
from ontology_platform.errors import TransientConnectorError, ValidationError
from ontology_platform.observability import AGENT_LATENCY, AGENT_STEPS, WORKFLOW_LATENCY, WORKFLOW_RUNS, correlation_id, get_logger
from ontology_platform.query.engine import QueryEngine
from ontology_platform.search.bm25 import IndexCache
from ontology_platform.storage.db import Database

log = get_logger(__name__)
TRANSIENT = (OperationalError, TransientConnectorError, TimeoutError)


class AgentOrchestrator:
    def __init__(self, db: Database, settings: Settings, cache: IndexCache, store: WorkflowStore) -> None:
        self.db = db
        self.settings = settings
        self.cache = cache
        self.store = store
        self.max_parallel = settings.agent_max_parallel if db.supports_concurrency else 1

    def _engine_call(self, principal: Principal, fn):
        """Run fn(engine) in a fresh session bound to the principal's tenant/customer scope."""
        principal.require(Permission.GRAPH_READ)  # checked before every tool execution
        with self.db.session() as s:
            return fn(QueryEngine(s, principal.scope, self.settings, self.cache))

    async def run(self, principal: Principal, question: str) -> WorkflowRun:
        principal.require(Permission.GRAPH_READ)
        question = (question or "").strip()
        if len(question) < 3:
            raise ValidationError("question must be at least 3 characters")
        run = WorkflowRun(id=uuid.uuid4().hex, tenant_id=principal.tenant_id, principal=principal.subject, query=question)
        token = correlation_id.set(correlation_id.get() or f"wf-{run.id}")
        run.correlation_id = correlation_id.get()
        started = time.perf_counter()
        run.event("workflow_started", "Workflow started")
        await asyncio.to_thread(self.store.save, run)
        try:
            cls, plan = await asyncio.to_thread(
                self._engine_call, principal, lambda eng: (lambda c: (c, build_plan(c, self.settings.agent_max_steps)))(classify(eng, question))
            )
            run.intents = cls.intents
            run.linked_entities = [EntityRefOut(id=l.entity.id, type=l.entity.type, name=l.entity.name, key=l.entity.canonical_key, status=l.entity.status) for l in cls.linked]
            run.plan = plan.steps
            run.event("intent_classified", f"Intents: {', '.join(i.value for i in cls.intents)}",
                      linked=[e.name for e in run.linked_entities], notes=cls.notes)
            run.event("plan_created", f"{len(plan.steps)} steps planned", steps=[{"id": s.id, "agent": s.agent.value, "action": s.action, "depends_on": s.depends_on} for s in plan.steps])

            outputs: dict[str, AgentOutput] = {}
            await self._execute(run, principal, question, outputs)

            # Evidence validation and synthesis operate on the collected facts.
            facts = [f for s in run.plan if s.id in outputs for f in outputs[s.id].facts]
            for i, f in enumerate(facts, 1):
                f.id = f"f{i}"
            retrieval = [s for s in run.plan if s.agent not in (AgentName.EVIDENCE, AgentName.SYNTHESIS)]
            vstep, sstep = run.step("validate"), run.step("synthesize")
            self._start(run, vstep)
            report = await asyncio.to_thread(self._engine_call, principal, lambda eng: validate(eng, facts, retrieval, outputs))
            self._finish(run, vstep, StepStatus.COMPLETED, summary=f"{report.status}: {report.confirmed} confirmed, {report.inferred} inferred, {report.unsupported} unsupported")
            vstep.tools = ["provenance_check", "entity_status_check"]
            run.validation, run.facts = report, facts
            self._start(run, sstep)
            run.answer = synthesize(question, retrieval, outputs, facts, report, plan.missing)
            self._finish(run, sstep, StepStatus.COMPLETED, summary=f"{len(run.answer.sections)} section(s), {len(run.answer.sources)} source(s)")

            failed = [s for s in retrieval if s.status == StepStatus.FAILED]
            if retrieval and not any(s.status == StepStatus.COMPLETED for s in retrieval):
                run.status = RunStatus.FAILED
            elif failed or any(s.status == StepStatus.SKIPPED for s in retrieval):
                run.status = RunStatus.COMPLETED_WITH_ERRORS
            else:
                run.status = RunStatus.COMPLETED
        except asyncio.CancelledError:
            run.status = RunStatus.CANCELLED
            run.event("workflow_cancelled", "Workflow cancelled by the caller")
            raise
        except Exception as exc:
            run.status = RunStatus.FAILED
            run.event("workflow_failed", f"{type(exc).__name__}: {exc}")
            log.exception("workflow failed", extra={"run_id": run.id})
            raise
        finally:
            run.finished_at = now()
            run.duration_ms = round((time.perf_counter() - started) * 1000, 1)
            run.event("workflow_finished", f"Workflow {run.status.value} in {run.duration_ms} ms")
            WORKFLOW_RUNS.labels(status=run.status.value).inc()
            WORKFLOW_LATENCY.observe(run.duration_ms / 1000)
            try:
                await asyncio.shield(asyncio.to_thread(self.store.save, run))
            finally:
                correlation_id.reset(token)
        return run

    # ------------------------------------------------------------------ execution
    async def _execute(self, run: WorkflowRun, principal: Principal, question: str, outputs: dict[str, AgentOutput]) -> None:
        sem = asyncio.Semaphore(self.max_parallel)
        pending = [s for s in run.plan if s.agent not in (AgentName.EVIDENCE, AgentName.SYNTHESIS)]
        done: set[str] = set()
        while pending:
            ready, blocked = [], []
            for step in pending:
                deps = [run.step(d) for d in step.depends_on]
                if any(d.status in (StepStatus.FAILED, StepStatus.SKIPPED) for d in deps):
                    step.status, step.skip_reason = StepStatus.SKIPPED, "a step it depends on did not complete"
                    run.event("step_skipped", step.skip_reason, step)
                    AGENT_STEPS.labels(agent=step.agent.value, status="skipped").inc()
                    done.add(step.id)
                elif all(d in done for d in step.depends_on):
                    ready.append(step)
                else:
                    blocked.append(step)
            if not ready:
                for step in blocked:  # unreachable for planner output; guards against bad plans
                    step.status, step.skip_reason = StepStatus.SKIPPED, "unresolvable dependency"
                    run.event("step_skipped", step.skip_reason, step)
                break
            if len(ready) > 1:
                run.event("parallel_batch", f"Running {len(ready)} steps concurrently", steps=[s.id for s in ready])

            async def guarded(step: PlanStep) -> None:
                async with sem:
                    await self._run_step(run, principal, question, step, outputs)

            await asyncio.gather(*(guarded(s) for s in ready))
            done.update(s.id for s in ready)
            pending = blocked

    async def _run_step(self, run: WorkflowRun, principal: Principal, question: str, step: PlanStep, outputs: dict[str, AgentOutput]) -> None:
        agent = AGENTS[step.agent]
        self._start(run, step)
        attempts = 1 + self.settings.agent_max_retries
        for attempt in range(1, attempts + 1):
            step.attempts = attempt
            began = time.perf_counter()
            try:
                out = await asyncio.wait_for(
                    asyncio.to_thread(self._engine_call, principal, lambda eng: agent.run(eng, step, question, dict(outputs))),
                    timeout=self.settings.agent_step_timeout_seconds,
                )
            except TRANSIENT as exc:
                err = "timed out" if isinstance(exc, TimeoutError) else f"{type(exc).__name__}: {exc}"
                AGENT_LATENCY.labels(agent=step.agent.value).observe(time.perf_counter() - began)
                if attempt < attempts:
                    run.event("step_retry", f"Attempt {attempt} failed ({err}); retrying", step)
                    continue
                self._finish(run, step, StepStatus.FAILED, error=err)
                return
            except Exception as exc:  # deterministic failure (not found, validation, bug): do not retry
                AGENT_LATENCY.labels(agent=step.agent.value).observe(time.perf_counter() - began)
                self._finish(run, step, StepStatus.FAILED, error=f"{type(exc).__name__}: {exc}"[:500])
                return
            AGENT_LATENCY.labels(agent=step.agent.value).observe(time.perf_counter() - began)
            outputs[step.id] = out
            step.tools = out.tools
            self._finish(run, step, StepStatus.COMPLETED, summary=f"{len(out.facts)} fact(s), {len(out.missing)} missing item(s)")
            return

    def _start(self, run: WorkflowRun, step: PlanStep) -> None:
        step.status, step.started_at = StepStatus.RUNNING, now()
        run.event("step_started", step.purpose, step, action=step.action)

    def _finish(self, run: WorkflowRun, step: PlanStep, status: StepStatus, summary: str | None = None, error: str | None = None) -> None:
        step.status, step.finished_at = status, now()
        step.duration_ms = round((step.finished_at - step.started_at).total_seconds() * 1000, 1) if step.started_at else None
        step.summary, step.error = summary, error
        AGENT_STEPS.labels(agent=step.agent.value, status=status.value).inc()
        run.event("step_completed" if status == StepStatus.COMPLETED else "step_failed", summary or error or status.value, step,
                  duration_ms=step.duration_ms, attempts=step.attempts, tools=step.tools)


def run_summary(run: WorkflowRun) -> dict[str, Any]:
    return run.model_dump(mode="json")

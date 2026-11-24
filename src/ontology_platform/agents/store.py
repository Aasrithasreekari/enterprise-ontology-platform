"""Tenant-scoped persistence of workflow runs."""

from __future__ import annotations

from typing import Any

from sqlalchemy import delete, func, select

from ontology_platform.agents.models import WorkflowRun
from ontology_platform.errors import NotFoundError
from ontology_platform.storage.db import Database
from ontology_platform.storage.orm import WorkflowRunRecord


class WorkflowStore:
    def __init__(self, db: Database, retained_per_tenant: int = 1000) -> None:
        self.db = db
        self.retained = retained_per_tenant

    def save(self, run: WorkflowRun) -> None:
        doc = run.model_dump(mode="json")
        with self.db.session() as s:
            rec = s.get(WorkflowRunRecord, run.id)
            if rec is None:
                rec = WorkflowRunRecord(id=run.id, tenant_id=run.tenant_id, principal=run.principal, query=run.query,
                                        started_at=run.started_at, document=doc, status=run.status.value)
                s.add(rec)
            rec.status = run.status.value
            rec.validation_status = run.validation.status if run.validation else None
            rec.intents = [i.value for i in run.intents]
            rec.document = doc
            rec.duration_ms = run.duration_ms
        if run.finished_at is not None:
            self._prune(run.tenant_id)

    def get(self, tenant_id: str, run_id: str, principal: str | None = None) -> dict[str, Any]:
        """``principal`` restricts the lookup to runs created by that principal."""
        with self.db.session() as s:
            rec = s.get(WorkflowRunRecord, run_id)
            # Runs of other tenants (or other principals, when restricted) are indistinguishable from missing ones.
            if rec is None or rec.tenant_id != tenant_id or (principal is not None and rec.principal != principal):
                raise NotFoundError(f"workflow run '{run_id}' not found")
            return dict(rec.document)

    def list(self, tenant_id: str, limit: int = 20, offset: int = 0, principal: str | None = None) -> dict[str, Any]:
        cond = [WorkflowRunRecord.tenant_id == tenant_id]
        if principal is not None:
            cond.append(WorkflowRunRecord.principal == principal)
        with self.db.session() as s:
            total = s.scalar(select(func.count()).select_from(WorkflowRunRecord).where(*cond)) or 0
            rows = s.scalars(
                select(WorkflowRunRecord)
                .where(*cond)
                .order_by(WorkflowRunRecord.started_at.desc(), WorkflowRunRecord.id)
                .offset(offset)
                .limit(limit)
            )
            runs = [
                {
                    "id": r.id, "query": r.query, "status": r.status, "intents": r.intents, "principal": r.principal,
                    "started_at": r.started_at.isoformat() if r.started_at else None, "duration_ms": r.duration_ms,
                    "validation_status": r.validation_status,
                }
                for r in rows
            ]
        return {"runs": runs, "total": total, "limit": limit, "offset": offset}

    def _prune(self, tenant_id: str) -> None:
        with self.db.session() as s:
            keep = select(WorkflowRunRecord.id).where(WorkflowRunRecord.tenant_id == tenant_id).order_by(
                WorkflowRunRecord.started_at.desc()).limit(self.retained)
            s.execute(delete(WorkflowRunRecord).where(WorkflowRunRecord.tenant_id == tenant_id, WorkflowRunRecord.id.not_in(keep)))

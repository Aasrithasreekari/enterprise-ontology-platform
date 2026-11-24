"""Tenant-scoped graph persistence.

Every query issued here is filtered by ``scope.tenant_id``; reads are additionally filtered by
the principal's customer visibility. Nothing outside this module builds entity/relationship
queries, which keeps tenant isolation enforceable in one place.
"""

from __future__ import annotations

import hashlib
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Literal

from sqlalchemy import Select, and_, delete, func, or_, select, update
from sqlalchemy.orm import Session

from ontology_platform.errors import NotFoundError, ValidationError
from ontology_platform.observability import GRAPH_WRITES
from ontology_platform.ontology.keys import normalize_name
from ontology_platform.ontology.schema import EntityStatus
from ontology_platform.storage.orm import (
    Document,
    DocumentChunk,
    Entity,
    EntityAlias,
    GraphChange,
    GraphRevision,
    Provenance,
    Relationship,
    utcnow,
)

Direction = Literal["out", "in", "both"]


@dataclass(frozen=True)
class Scope:
    """What the caller may see: one tenant, optionally restricted to a set of customers."""

    tenant_id: str
    allowed_customers: frozenset[str] | None = None  # None → all customers of the tenant

    def can_see_customer(self, customer_scope: str | None) -> bool:
        return customer_scope is None or self.allowed_customers is None or customer_scope in self.allowed_customers


@dataclass
class ProvenanceInfo:
    connector_id: str
    source_system: str
    source_record_key: str
    extractor: str
    source_url: str | None = None
    confidence: float = 1.0
    evidence: str | None = None


@dataclass(frozen=True)
class RedactedProvenance:
    """Detached, read-only view of a provenance row whose details belong to another customer.

    Returned instead of the ORM object so the stored row is never modified."""

    subject_kind: str
    subject_id: str
    connector_id: str
    source_system: str
    extractor: str
    confidence: float
    observed_at: datetime | None
    first_observed_at: datetime | None
    source_record_key: str = "restricted"
    source_url: str | None = None
    evidence: str | None = "[restricted: observed in a source record of another customer]"
    last_run_id: str | None = None

    @classmethod
    def of(cls, p: Provenance) -> "RedactedProvenance":
        return cls(p.subject_kind, p.subject_id, p.connector_id, p.source_system, p.extractor, p.confidence,
                   p.observed_at, p.first_observed_at)


@dataclass
class Neighbor:
    relationship: Relationship
    entity: Entity
    direction: Literal["out", "in"]


@dataclass
class StaleReport:
    entities: list[str] = field(default_factory=list)
    relationships: list[str] = field(default_factory=list)


def entity_snapshot(e: Entity) -> dict[str, Any]:
    return {
        "type": e.type,
        "canonical_key": e.canonical_key,
        "name": e.name,
        "description": e.description,
        "properties": e.properties,
        "status": e.status,
        "customer_scope": e.customer_scope,
    }


def relationship_snapshot(r: Relationship) -> dict[str, Any]:
    return {
        "type": r.type,
        "source_id": r.source_id,
        "target_id": r.target_id,
        "properties": r.properties,
        "confidence": r.confidence,
        "status": r.status,
    }


class GraphRepository:
    def __init__(self, session: Session, scope: Scope) -> None:
        self.session = session
        self.scope = scope
        self.tenant_id = scope.tenant_id

    # ------------------------------------------------------------------ visibility helpers
    def _visible(self, stmt: Select, include_stale: bool = False, alias: type[Entity] = Entity) -> Select:
        stmt = stmt.where(alias.tenant_id == self.tenant_id)
        if self.scope.allowed_customers is not None:
            stmt = stmt.where(
                or_(alias.customer_scope.is_(None), alias.customer_scope.in_(sorted(self.scope.allowed_customers)))
            )
        if not include_stale:
            stmt = stmt.where(alias.status != EntityStatus.STALE.value)
        return stmt

    # ------------------------------------------------------------------ entity reads
    def get_entity(self, entity_id: str, include_stale: bool = True) -> Entity:
        stmt = self._visible(select(Entity).where(Entity.id == entity_id), include_stale=include_stale)
        entity = self.session.scalars(stmt).first()
        if entity is None:
            raise NotFoundError(f"entity '{entity_id}' not found")
        return entity

    def find_by_key(self, canonical_key: str, include_stale: bool = True) -> Entity | None:
        stmt = self._visible(select(Entity).where(Entity.canonical_key == canonical_key), include_stale=include_stale)
        return self.session.scalars(stmt).first()

    def find_by_alias(self, entity_type: str, alias_norm: str) -> list[Entity]:
        stmt = self._visible(
            select(Entity)
            .join(EntityAlias, EntityAlias.entity_id == Entity.id)
            .where(EntityAlias.tenant_id == self.tenant_id)
            .where(EntityAlias.entity_type == entity_type, EntityAlias.alias_norm == alias_norm),
            include_stale=True,
        )
        return list(self.session.scalars(stmt).unique())

    def find_by_alias_types(self, entity_types: Sequence[str] | None, alias_norm: str) -> list[Entity]:
        """Entities of any of ``entity_types`` (all types when None) having this alias, in one query."""
        stmt = (
            select(Entity)
            .join(EntityAlias, EntityAlias.entity_id == Entity.id)
            .where(EntityAlias.tenant_id == self.tenant_id, EntityAlias.alias_norm == alias_norm)
        )
        if entity_types:
            stmt = stmt.where(EntityAlias.entity_type.in_(list(entity_types)))
        return list(self.session.scalars(self._visible(stmt, include_stale=True)).unique())

    def fuzzy_candidates(self, entity_type: str, name_norm: str, limit: int = 200) -> list[Entity]:
        first = name_norm.split("-", 1)[0]
        stmt = self._visible(
            select(Entity).where(Entity.type == entity_type, Entity.name_norm.like(f"{first}%")), include_stale=True
        ).limit(limit)
        return list(self.session.scalars(stmt))

    def find_entities(
        self,
        types: Sequence[str] | None = None,
        name_norms: Sequence[str] | None = None,
        ids: Iterable[str] | None = None,
        statuses: Sequence[str] | None = None,
        property_filters: dict[str, Any] | None = None,
        include_stale: bool = False,
        limit: int | None = None,
    ) -> list[Entity]:
        stmt = self._visible(select(Entity), include_stale=include_stale)
        if types:
            stmt = stmt.where(Entity.type.in_(list(types)))
        if name_norms:
            stmt = stmt.where(Entity.name_norm.in_(list(name_norms)))
        if ids is not None:
            stmt = stmt.where(Entity.id.in_(list(ids)))
        if statuses:
            stmt = stmt.where(Entity.status.in_(list(statuses)))
        stmt = stmt.order_by(Entity.type, Entity.name)
        rows = list(self.session.scalars(stmt))
        if property_filters:
            rows = [e for e in rows if _match_properties(e.properties or {}, property_filters)]
        return rows[:limit] if limit else rows

    def aliases_for(self, entity_ids: Iterable[str]) -> dict[str, list[str]]:
        ids = list(entity_ids)
        out: dict[str, list[str]] = {i: [] for i in ids}
        if not ids:
            return out
        stmt = select(EntityAlias).where(EntityAlias.tenant_id == self.tenant_id, EntityAlias.entity_id.in_(ids))
        for a in self.session.scalars(stmt):
            out[a.entity_id].append(a.alias)
        return out

    def all_aliases(self) -> list[tuple[str, str, str]]:
        """(entity_id, entity_type, alias) for visible, non-stale entities."""
        stmt = self._visible(
            select(EntityAlias.entity_id, EntityAlias.entity_type, EntityAlias.alias).join(
                Entity, Entity.id == EntityAlias.entity_id
            )
        ).where(EntityAlias.tenant_id == self.tenant_id)
        return [tuple(r) for r in self.session.execute(stmt)]

    # ------------------------------------------------------------------ entity writes
    def create_entity(
        self,
        *,
        entity_type: str,
        canonical_key: str,
        name: str,
        identity: dict[str, str],
        description: str | None,
        properties: dict[str, Any],
        customer_scope: str | None,
        status: EntityStatus,
        run_id: str | None,
    ) -> Entity:
        entity = Entity(
            tenant_id=self.tenant_id,
            type=entity_type,
            canonical_key=canonical_key,
            name=name,
            name_norm=normalize_name(name),
            identity=identity,
            description=description,
            properties=dict(properties),
            customer_scope=customer_scope,
            status=status.value,
            version=1,
        )
        self.session.add(entity)
        self.session.flush()
        self._record_change("entity", entity.id, 1, "created", entity_snapshot(entity), run_id)
        self.add_alias(entity, name)
        return entity

    def update_entity(
        self,
        entity: Entity,
        *,
        name: str | None,
        description: str | None,
        properties: dict[str, Any],
        customer_scope: str | None,
        promote: bool,
        run_id: str | None,
    ) -> bool:
        """Merge new observations into an entity. Returns True when a new version was written."""
        changed = False
        change_type = "updated"
        if promote and entity.status != EntityStatus.ACTIVE.value:
            change_type = "reactivated" if entity.status == EntityStatus.STALE.value else "updated"
            # A stub's placeholder name is replaced by the first defining source's name.
            if entity.status == EntityStatus.STUB.value and name and name != entity.name:
                entity.name, entity.name_norm = name, normalize_name(name)
            entity.status = EntityStatus.ACTIVE.value
            changed = True
        if name and name != entity.name:
            self.add_alias(entity, name)  # first defining source keeps the display name
        if description and description != entity.description:
            entity.description = description
            changed = True
        merged = {**(entity.properties or {}), **properties}
        if merged != (entity.properties or {}):
            entity.properties = merged
            changed = True
        if customer_scope and entity.customer_scope is None:
            entity.customer_scope = customer_scope
            changed = True
        if changed:
            entity.version += 1
            entity.updated_at = utcnow()
            self.session.flush()
            self._record_change("entity", entity.id, entity.version, change_type, entity_snapshot(entity), run_id)
        return changed

    def add_alias(self, entity: Entity, alias: str) -> None:
        alias_norm = normalize_name(alias)
        if not alias_norm:
            return
        exists = self.session.scalar(
            select(func.count())
            .select_from(EntityAlias)
            .where(
                EntityAlias.tenant_id == self.tenant_id,
                EntityAlias.entity_id == entity.id,
                EntityAlias.alias_norm == alias_norm,
            )
        )
        if not exists:
            self.session.add(
                EntityAlias(
                    tenant_id=self.tenant_id,
                    entity_id=entity.id,
                    entity_type=entity.type,
                    alias=alias,
                    alias_norm=alias_norm,
                )
            )
            self.session.flush()

    # ------------------------------------------------------------------ relationships
    def get_relationship(self, source_id: str, rel_type: str, target_id: str) -> Relationship | None:
        stmt = select(Relationship).where(
            Relationship.tenant_id == self.tenant_id,
            Relationship.type == rel_type,
            Relationship.source_id == source_id,
            Relationship.target_id == target_id,
        )
        return self.session.scalars(stmt).first()

    def upsert_relationship(
        self,
        *,
        rel_type: str,
        source_id: str,
        target_id: str,
        properties: dict[str, Any],
        confidence: float,
        run_id: str | None,
    ) -> tuple[Relationship, str | None]:
        self._require_endpoints(source_id, target_id)
        rel = self.get_relationship(source_id, rel_type, target_id)
        if rel is None:
            rel = Relationship(
                tenant_id=self.tenant_id,
                type=rel_type,
                source_id=source_id,
                target_id=target_id,
                properties=dict(properties),
                confidence=confidence,
                status=EntityStatus.ACTIVE.value,
                version=1,
            )
            self.session.add(rel)
            self.session.flush()
            self._record_change("relationship", rel.id, 1, "created", relationship_snapshot(rel), run_id)
            return rel, "created"
        changed = False
        change_type = "updated"
        if rel.status != EntityStatus.ACTIVE.value:
            rel.status = EntityStatus.ACTIVE.value
            change_type = "reactivated"
            changed = True
        merged = {**(rel.properties or {}), **properties}
        if merged != (rel.properties or {}):
            rel.properties = merged
            changed = True
        if confidence > rel.confidence:
            rel.confidence = confidence
            changed = True
        if changed:
            rel.version += 1
            rel.updated_at = utcnow()
            self.session.flush()
            self._record_change("relationship", rel.id, rel.version, change_type, relationship_snapshot(rel), run_id)
            return rel, change_type
        return rel, None

    def _require_endpoints(self, *entity_ids: str) -> None:
        """Referential integrity: relationship endpoints must be entities of this tenant."""
        for entity_id in entity_ids:
            entity = self.session.get(Entity, entity_id)
            if entity is None or entity.tenant_id != self.tenant_id:
                raise ValidationError(f"relationship endpoint '{entity_id}' does not exist in this tenant")

    def neighbors(
        self,
        entity_ids: Iterable[str],
        relation_types: Sequence[str] | None = None,
        direction: Direction = "both",
        target_types: Sequence[str] | None = None,
        include_stale: bool = False,
        min_confidence: float = 0.0,
    ) -> list[Neighbor]:
        ids = list(set(entity_ids))
        if not ids:
            return []
        out: list[Neighbor] = []
        legs: list[tuple[Literal["out", "in"], Any, Any]] = []
        if direction in ("out", "both"):
            legs.append(("out", Relationship.source_id, Relationship.target_id))
        if direction in ("in", "both"):
            legs.append(("in", Relationship.target_id, Relationship.source_id))
        for leg, anchor_col, other_col in legs:
            stmt = select(Relationship, Entity).join(Entity, Entity.id == other_col)
            stmt = self._visible(stmt, include_stale=include_stale)
            stmt = stmt.where(Relationship.tenant_id == self.tenant_id, anchor_col.in_(ids))
            if relation_types:
                stmt = stmt.where(Relationship.type.in_(list(relation_types)))
            if target_types:
                stmt = stmt.where(Entity.type.in_(list(target_types)))
            if not include_stale:
                stmt = stmt.where(Relationship.status != EntityStatus.STALE.value)
            if min_confidence > 0:
                stmt = stmt.where(Relationship.confidence >= min_confidence)
            for rel, ent in self.session.execute(stmt):
                out.append(Neighbor(relationship=rel, entity=ent, direction=leg))
        return out

    def list_relationships(
        self,
        source_id: str | None = None,
        target_id: str | None = None,
        rel_type: str | None = None,
        include_stale: bool = False,
        limit: int = 500,
    ) -> list[Relationship]:
        src = Entity.__table__.alias("src")
        tgt = Entity.__table__.alias("tgt")
        stmt = (
            select(Relationship)
            .join(src, src.c.id == Relationship.source_id)
            .join(tgt, tgt.c.id == Relationship.target_id)
            .where(Relationship.tenant_id == self.tenant_id)
        )
        if self.scope.allowed_customers is not None:
            allowed = sorted(self.scope.allowed_customers)
            stmt = stmt.where(
                and_(
                    or_(src.c.customer_scope.is_(None), src.c.customer_scope.in_(allowed)),
                    or_(tgt.c.customer_scope.is_(None), tgt.c.customer_scope.in_(allowed)),
                )
            )
        if source_id:
            stmt = stmt.where(Relationship.source_id == source_id)
        if target_id:
            stmt = stmt.where(Relationship.target_id == target_id)
        if rel_type:
            stmt = stmt.where(Relationship.type == rel_type)
        if not include_stale:
            stmt = stmt.where(Relationship.status != EntityStatus.STALE.value)
        return list(self.session.scalars(stmt.limit(limit)))

    # ------------------------------------------------------------------ provenance
    def record_provenance(self, subject_kind: str, subject_id: str, info: ProvenanceInfo, run_id: str | None) -> None:
        stmt = select(Provenance).where(
            Provenance.tenant_id == self.tenant_id,
            Provenance.subject_kind == subject_kind,
            Provenance.subject_id == subject_id,
            Provenance.connector_id == info.connector_id,
            Provenance.source_record_key == info.source_record_key,
            Provenance.extractor == info.extractor,
        )
        row = self.session.scalars(stmt).first()
        now = utcnow()
        if row is None:
            self.session.add(
                Provenance(
                    tenant_id=self.tenant_id,
                    subject_kind=subject_kind,
                    subject_id=subject_id,
                    connector_id=info.connector_id,
                    source_system=info.source_system,
                    source_record_key=info.source_record_key,
                    source_url=info.source_url,
                    extractor=info.extractor,
                    confidence=info.confidence,
                    evidence=_truncate(info.evidence),
                    last_run_id=run_id,
                    first_observed_at=now,
                    observed_at=now,
                )
            )
        else:
            row.last_run_id = run_id
            row.observed_at = now
            row.confidence = info.confidence
            row.source_url = info.source_url or row.source_url
            row.evidence = _truncate(info.evidence) or row.evidence
        self.session.flush()

    def touch_record_provenance(self, connector_id: str, record_key: str, run_id: str) -> int:
        result = self.session.execute(
            update(Provenance)
            .where(
                Provenance.tenant_id == self.tenant_id,
                Provenance.connector_id == connector_id,
                Provenance.source_record_key == record_key,
            )
            .values(last_run_id=run_id, observed_at=utcnow())
        )
        return result.rowcount or 0

    def retract_record_provenance(self, connector_id: str, record_key: str, run_id: str) -> StaleReport:
        """Drop provenance of a re-extracted record that was not re-confirmed in this run.

        Facts that lose their last source become stale.
        """
        rows = list(
            self.session.scalars(
                select(Provenance).where(
                    Provenance.tenant_id == self.tenant_id,
                    Provenance.connector_id == connector_id,
                    Provenance.source_record_key == record_key,
                    or_(Provenance.last_run_id.is_(None), Provenance.last_run_id != run_id),
                )
            )
        )
        return self._drop_provenance(rows, run_id)

    def provenance_for(self, subject_kind: str, subject_ids: Iterable[str]) -> dict[str, list[Provenance]]:
        ids = list(subject_ids)
        out: dict[str, list[Provenance]] = {i: [] for i in ids}
        if not ids:
            return out
        stmt = (
            select(Provenance)
            .where(
                Provenance.tenant_id == self.tenant_id,
                Provenance.subject_kind == subject_kind,
                Provenance.subject_id.in_(ids),
            )
            .order_by(Provenance.confidence.desc())
        )
        rows = list(self.session.scalars(stmt))
        restricted = self._foreign_customer_records(rows)
        for p in rows:
            out[p.subject_id].append(RedactedProvenance.of(p) if (p.connector_id, p.source_record_key) in restricted else p)
        return out

    def _foreign_customer_records(self, rows: list[Provenance]) -> set[tuple[str, str]]:
        """Source records that also produced entities of customers the caller may not see.

        A shared fact (e.g. a service running on a cluster) can be observed in another customer's
        workload; its evidence text and URL would reveal that customer's namespace or resources.
        """
        if self.scope.allowed_customers is None or not rows:
            return set()
        keys = {p.source_record_key for p in rows}
        stmt = (
            select(Provenance.connector_id, Provenance.source_record_key, Entity.customer_scope)
            .join(Entity, Entity.id == Provenance.subject_id)
            .where(
                Provenance.tenant_id == self.tenant_id,
                Provenance.subject_kind == "entity",
                Provenance.source_record_key.in_(keys),
                Entity.customer_scope.is_not(None),
            )
            .distinct()
        )
        return {(c, k) for c, k, scope in self.session.execute(stmt) if scope not in self.scope.allowed_customers}

    def mark_unobserved_stale(self, connector_id: str, run_id: str) -> StaleReport:
        """Full-sync reconciliation: forget this connector's facts not seen in ``run_id``."""
        rows = list(
            self.session.scalars(
                select(Provenance).where(
                    Provenance.tenant_id == self.tenant_id,
                    Provenance.connector_id == connector_id,
                    or_(Provenance.last_run_id.is_(None), Provenance.last_run_id != run_id),
                )
            )
        )
        return self._drop_provenance(rows, run_id)

    def _drop_provenance(self, rows: list[Provenance], run_id: str) -> StaleReport:
        subjects = {(p.subject_kind, p.subject_id) for p in rows}
        for p in rows:
            self.session.delete(p)
        self.session.flush()
        report = StaleReport()
        for kind, sid in sorted(subjects):
            remaining = self.session.scalar(
                select(func.count())
                .select_from(Provenance)
                .where(Provenance.tenant_id == self.tenant_id, Provenance.subject_kind == kind, Provenance.subject_id == sid)
            )
            if remaining:
                continue
            model = Entity if kind == "entity" else Relationship
            obj = self.session.get(model, sid)
            if obj is None or obj.tenant_id != self.tenant_id or obj.status == EntityStatus.STALE.value:
                continue
            obj.status = EntityStatus.STALE.value
            obj.version += 1
            obj.updated_at = utcnow()
            snap = entity_snapshot(obj) if kind == "entity" else relationship_snapshot(obj)
            self._record_change(kind, sid, obj.version, "stale", snap, run_id)
            (report.entities if kind == "entity" else report.relationships).append(sid)
        # Relationships touching stale entities are stale as well.
        if report.entities:
            rels = self.session.scalars(
                select(Relationship).where(
                    Relationship.tenant_id == self.tenant_id,
                    Relationship.status != EntityStatus.STALE.value,
                    or_(Relationship.source_id.in_(report.entities), Relationship.target_id.in_(report.entities)),
                )
            )
            for rel in rels:
                rel.status = EntityStatus.STALE.value
                rel.version += 1
                rel.updated_at = utcnow()
                self._record_change("relationship", rel.id, rel.version, "stale", relationship_snapshot(rel), run_id)
                report.relationships.append(rel.id)
        self.session.flush()
        return report

    # ------------------------------------------------------------------ history
    def _record_change(
        self, kind: str, subject_id: str, version: int, change_type: str, snapshot: dict[str, Any], run_id: str | None
    ) -> None:
        self.session.add(
            GraphChange(
                tenant_id=self.tenant_id,
                subject_kind=kind,
                subject_id=subject_id,
                version=version,
                change_type=change_type,
                snapshot=snapshot,
                run_id=run_id,
            )
        )
        GRAPH_WRITES.labels(subject=kind, change=change_type).inc()

    def history(self, subject_kind: str, subject_id: str) -> list[GraphChange]:
        stmt = (
            select(GraphChange)
            .where(
                GraphChange.tenant_id == self.tenant_id,
                GraphChange.subject_kind == subject_kind,
                GraphChange.subject_id == subject_id,
            )
            .order_by(GraphChange.version)
        )
        return list(self.session.scalars(stmt))

    # ------------------------------------------------------------------ documents
    def upsert_document(self, entity_id: str, title: str, url: str | None, chunks: list[str]) -> bool:
        content_hash = hashlib.sha256("\n".join(chunks).encode()).hexdigest()
        doc = self.session.scalars(
            select(Document).where(Document.tenant_id == self.tenant_id, Document.entity_id == entity_id)
        ).first()
        if doc is not None and doc.content_hash == content_hash and doc.title == title:
            return False
        if doc is None:
            doc = Document(tenant_id=self.tenant_id, entity_id=entity_id, title=title, url=url, content_hash=content_hash)
            self.session.add(doc)
            self.session.flush()
        else:
            doc.title, doc.url, doc.content_hash, doc.updated_at = title, url, content_hash, utcnow()
            self.session.execute(delete(DocumentChunk).where(DocumentChunk.document_id == doc.id))
        for i, text in enumerate(chunks):
            self.session.add(DocumentChunk(tenant_id=self.tenant_id, document_id=doc.id, ordinal=i, text=text))
        self.session.flush()
        return True

    def visible_document_chunks(self) -> list[tuple[DocumentChunk, Document, Entity]]:
        stmt = (
            select(DocumentChunk, Document, Entity)
            .join(Document, Document.id == DocumentChunk.document_id)
            .join(Entity, Entity.id == Document.entity_id)
            .where(DocumentChunk.tenant_id == self.tenant_id)
        )
        stmt = self._visible(stmt)
        return [tuple(r) for r in self.session.execute(stmt)]  # type: ignore[misc]

    # ------------------------------------------------------------------ revisions
    def bump_revision(self) -> int:
        rev = self.session.get(GraphRevision, self.tenant_id)
        if rev is None:
            rev = GraphRevision(tenant_id=self.tenant_id, revision=1)
            self.session.add(rev)
        else:
            rev.revision += 1
        self.session.flush()
        return rev.revision

    def revision(self) -> int:
        rev = self.session.get(GraphRevision, self.tenant_id)
        return rev.revision if rev else 0

    def stats(self) -> dict[str, Any]:
        """Counts per type, computed in SQL and honouring the caller's visibility."""
        ent = self.session.execute(self._visible(select(Entity.type, func.count()).group_by(Entity.type))).all()
        src = Entity.__table__.alias("src")
        tgt = Entity.__table__.alias("tgt")
        stmt = (
            select(Relationship.type, func.count())
            .join(src, src.c.id == Relationship.source_id)
            .join(tgt, tgt.c.id == Relationship.target_id)
            .where(Relationship.tenant_id == self.tenant_id, Relationship.status != EntityStatus.STALE.value)
            .group_by(Relationship.type)
        )
        if self.scope.allowed_customers is not None:
            allowed = sorted(self.scope.allowed_customers)
            stmt = stmt.where(
                or_(src.c.customer_scope.is_(None), src.c.customer_scope.in_(allowed)),
                or_(tgt.c.customer_scope.is_(None), tgt.c.customer_scope.in_(allowed)),
            )
        rels = self.session.execute(stmt).all()
        return {"entities": {t: c for t, c in ent}, "relationships": {t: c for t, c in rels}, "revision": self.revision()}


def _truncate(text: str | None, limit: int = 2000) -> str | None:
    if text is None:
        return None
    return text if len(text) <= limit else text[: limit - 1] + "…"


def _match_properties(props: dict[str, Any], filters: dict[str, Any]) -> bool:
    for key, expected in filters.items():
        actual = props.get(key)
        if isinstance(actual, list):
            if expected not in actual:
                return False
        elif str(actual).lower() != str(expected).lower():
            return False
    return True

"""Ingestion pipeline: connector → records → extraction → resolution → graph."""

from __future__ import annotations

import traceback
from collections.abc import AsyncIterable, Callable
from dataclasses import dataclass, field
from datetime import timedelta
from typing import Any

import httpx
from sqlalchemy import delete, select

from ontology_platform.config import Settings
from ontology_platform.connectors.base import Connector, RecordKind, SourceRecord, connector_class, resolve_secret
from ontology_platform.errors import ConflictError, NotFoundError, PlatformError, ValidationError
from ontology_platform.extraction.structured import extract
from ontology_platform.extraction.text import MentionExtractor, chunk_text
from ontology_platform.observability import INGESTED_RECORDS, INGESTION_RUNS, correlation_id, get_logger
from ontology_platform.ontology.models import ExtractionResult
from ontology_platform.ontology.schema import EntityType, RelationType, is_relation_allowed
from ontology_platform.resolution.resolver import EntityResolver
from ontology_platform.storage.db import Database
from ontology_platform.storage.orm import ConnectorConfig, IngestionRun, SourceRecordState, utcnow
from ontology_platform.storage.repository import GraphRepository, ProvenanceInfo, Scope

log = get_logger(__name__)

STALE_RUN_AFTER = timedelta(hours=6)  # a "running" run older than this is considered abandoned


@dataclass
class RunStats:
    records_seen: int = 0
    records_unchanged: int = 0
    records_processed: int = 0
    records_failed: int = 0
    entities_created: int = 0
    entities_updated: int = 0
    relationships_created: int = 0
    relationships_updated: int = 0
    relationships_rejected: int = 0
    mentions_linked: int = 0
    stale_entities: int = 0
    stale_relationships: int = 0
    warnings: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        d = dict(self.__dict__)
        d["warnings"] = self.warnings[:50]
        d["warning_count"] = len(self.warnings)
        return d


@dataclass
class _ChangedDoc:
    entity_id: str
    title: str
    text: str
    record_key: str
    source_system: str
    source_url: str | None


TransportFactory = Callable[[str], httpx.AsyncBaseTransport | None]


class IngestionPipeline:
    def __init__(self, db: Database, settings: Settings, transport_factory: TransportFactory | None = None) -> None:
        self.db = db
        self.settings = settings
        self.transport_factory = transport_factory

    # ------------------------------------------------------------------ public API
    def start_run(self, tenant_id: str, connector_id: str, mode: str, triggered_by: str | None) -> str:
        """Create a run row (refusing concurrent runs of the same connector). Returns run id."""
        if mode not in ("full", "incremental", "push"):
            raise ValidationError(f"invalid mode '{mode}'")
        with self.db.session() as s:
            cfg = self._load_connector(s, tenant_id, connector_id)
            if not cfg.enabled:
                raise ConflictError(f"connector '{cfg.name}' is disabled")
            running = s.scalars(
                select(IngestionRun).where(
                    IngestionRun.tenant_id == tenant_id,
                    IngestionRun.connector_id == connector_id,
                    IngestionRun.status == "running",
                )
            ).all()
            for r in running:
                started = r.started_at if r.started_at.tzinfo else r.started_at.replace(tzinfo=utcnow().tzinfo)
                if utcnow() - started < STALE_RUN_AFTER:
                    raise ConflictError(f"connector '{cfg.name}' already has a running ingestion ({r.id})")
                r.status, r.error, r.finished_at = "failed", "abandoned (exceeded max run time)", utcnow()
            run = IngestionRun(tenant_id=tenant_id, connector_id=connector_id, mode=mode, status="running", triggered_by=triggered_by)
            s.add(run)
            s.flush()
            return run.id

    async def run(
        self,
        tenant_id: str,
        connector_id: str,
        mode: str = "full",
        triggered_by: str | None = None,
        run_id: str | None = None,
        records: AsyncIterable[SourceRecord] | None = None,
    ) -> dict[str, Any]:
        """Execute a sync. ``records`` overrides the connector fetch (push ingestion)."""
        run_id = run_id or self.start_run(tenant_id, connector_id, mode, triggered_by)
        token = correlation_id.set(f"run-{run_id}")
        stats = RunStats()
        status, error = "succeeded", None
        connector: Connector | None = None
        connector_type = "push"
        try:
            with self.db.session() as s:
                cfg = self._load_connector(s, tenant_id, connector_id)
                cfg_type, cfg_config, cfg_secret_ref, cursor = cfg.type, dict(cfg.config), cfg.secret_ref, cfg.cursor
            connector_type = cfg_type
            log.info("ingestion started", extra={"tenant": tenant_id, "connector": connector_id, "mode": mode})
            if records is None:
                cls = connector_class(cfg_type)
                transport = self.transport_factory(cfg_type) if self.transport_factory else None
                connector = cls(cfg_config, resolve_secret(cfg_secret_ref), self.settings, transport=transport)
                since = cursor if (mode == "incremental" and cls.supports_incremental) else None
                records = connector.fetch(since=since)
            changed_docs: list[_ChangedDoc] = []
            changed_keys: list[str] = []
            async for record in records:
                stats.records_seen += 1
                self._process_record(tenant_id, connector_id, cfg_type, record, run_id, stats, changed_docs, changed_keys)
            self._link_mentions(tenant_id, connector_id, run_id, changed_docs, stats)
            self._retract_unconfirmed(tenant_id, connector_id, run_id, changed_keys, stats)
            if mode == "full":
                self._reconcile_full_sync(tenant_id, connector_id, run_id, stats)
            if stats.records_failed:
                status = "partial"
            with self.db.session() as s:
                if connector is not None and connector.supports_incremental and connector.next_cursor():
                    cfg = self._load_connector(s, tenant_id, connector_id)
                    cfg.cursor = connector.next_cursor()
                GraphRepository(s, Scope(tenant_id)).bump_revision()
        except Exception as exc:  # the run must always be finalized
            status = "failed"
            error = f"{type(exc).__name__}: {exc}"
            log.error("ingestion failed", extra={"run_id": run_id, "error": error, "trace": traceback.format_exc(limit=5)})
            with self.db.session() as s:  # facts written before the failure are visible
                GraphRepository(s, Scope(tenant_id)).bump_revision()
        finally:
            if connector is not None:
                await connector.aclose()
            with self.db.session() as s:
                run = s.get(IngestionRun, run_id)
                if run is not None:
                    run.status, run.error, run.stats, run.finished_at = status, error, stats.as_dict(), utcnow()
            INGESTION_RUNS.labels(connector_type=connector_type, status=status).inc()
            log.info("ingestion finished", extra={"run_id": run_id, "status": status, "stats": stats.as_dict()})
            correlation_id.reset(token)
        return {"run_id": run_id, "status": status, "error": error, "stats": stats.as_dict()}

    # ------------------------------------------------------------------ steps
    @staticmethod
    def _load_connector(session, tenant_id: str, connector_id: str) -> ConnectorConfig:
        cfg = session.get(ConnectorConfig, connector_id)
        if cfg is None or cfg.tenant_id != tenant_id:
            raise NotFoundError(f"connector '{connector_id}' not found")
        return cfg

    def _process_record(
        self,
        tenant_id: str,
        connector_id: str,
        connector_type: str,
        record: SourceRecord,
        run_id: str,
        stats: RunStats,
        changed_docs: list[_ChangedDoc],
        changed_keys: list[str],
    ) -> None:
        key = record.record_key
        digest = record.content_hash()
        try:
            with self.db.session() as s:
                repo = GraphRepository(s, Scope(tenant_id))
                state = s.scalars(
                    select(SourceRecordState).where(
                        SourceRecordState.tenant_id == tenant_id,
                        SourceRecordState.connector_id == connector_id,
                        SourceRecordState.record_key == key,
                    )
                ).first()
                if state is not None and state.content_hash == digest:
                    repo.touch_record_provenance(connector_id, key, run_id)
                    state.last_run_id = run_id
                    stats.records_unchanged += 1
                    INGESTED_RECORDS.labels(connector_type=connector_type, outcome="unchanged").inc()
                    return
                result = extract(record)
                resolver = EntityResolver(repo, self.settings.fuzzy_match_threshold)
                source_system = record.source_system if connector_type != "fixture" else f"{record.source_system} (fixture)"
                docs = self._apply(repo, resolver, result, record, connector_id, source_system, run_id, stats)
                for entity_id, doc in docs:
                    changed_docs.append(_ChangedDoc(entity_id, doc.title, doc.text, key, source_system, record.source_url))
                if state is None:
                    s.add(SourceRecordState(tenant_id=tenant_id, connector_id=connector_id, record_key=key, content_hash=digest, last_run_id=run_id))
                else:
                    state.content_hash, state.last_run_id, state.updated_at = digest, run_id, utcnow()
                changed_keys.append(key)
                stats.records_processed += 1
                stats.warnings.extend(result.warnings)
                INGESTED_RECORDS.labels(connector_type=connector_type, outcome="processed").inc()
        except (PlatformError, ValueError, KeyError, TypeError) as exc:
            stats.records_failed += 1
            stats.warnings.append(f"{key}: {type(exc).__name__}: {exc}")
            INGESTED_RECORDS.labels(connector_type=connector_type, outcome="failed").inc()
            log.warning("record failed", extra={"record": key, "error": str(exc)})
            # Keep previously known facts of this record alive: a parse error is not a deletion.
            with self.db.session() as s:
                GraphRepository(s, Scope(tenant_id)).touch_record_provenance(connector_id, key, run_id)
                state = s.scalars(
                    select(SourceRecordState).where(
                        SourceRecordState.tenant_id == tenant_id,
                        SourceRecordState.connector_id == connector_id,
                        SourceRecordState.record_key == key,
                    )
                ).first()
                if state is not None:
                    state.last_run_id = run_id

    def _apply(
        self,
        repo: GraphRepository,
        resolver: EntityResolver,
        result: ExtractionResult,
        record: SourceRecord,
        connector_id: str,
        source_system: str,
        run_id: str,
        stats: RunStats,
    ) -> list[tuple[str, Any]]:
        def prov(extractor: str, confidence: float = 1.0, evidence: str | None = None) -> ProvenanceInfo:
            return ProvenanceInfo(
                connector_id=connector_id,
                source_system=source_system,
                source_record_key=record.record_key,
                extractor=extractor,
                source_url=record.source_url,
                confidence=confidence,
                evidence=evidence,
            )

        extractor = f"structured:{record.kind.value}"
        for cand in result.entities:
            missing = cand.missing_identity_fields()
            if missing:
                stats.warnings.append(f"{record.record_key}: {cand.type.value} missing identity {missing}")
                continue
            res = resolver.resolve(cand, run_id)
            if res.created:
                stats.entities_created += 1
            elif res.changed:
                stats.entities_updated += 1
            repo.record_provenance("entity", res.entity.id, prov(extractor, cand.confidence, cand.evidence), run_id)

        for rel in result.relationships:
            if not is_relation_allowed(rel.type, rel.source.type, rel.target.type):
                stats.relationships_rejected += 1
                stats.warnings.append(
                    f"{record.record_key}: relationship {rel.source.type.value} -{rel.type.value}-> {rel.target.type.value} violates ontology"
                )
                continue
            try:
                src = resolver.resolve_ref(rel.source, run_id)
                tgt = resolver.resolve_ref(rel.target, run_id)
            except ValidationError as exc:
                stats.warnings.append(f"{record.record_key}: unresolvable endpoint: {exc}")
                continue
            for endpoint in (src, tgt):
                if endpoint.created:
                    stats.entities_created += 1
                repo.record_provenance("entity", endpoint.entity.id, prov("reference", 0.5), run_id)
            if src.entity.id == tgt.entity.id:
                continue
            r, change = repo.upsert_relationship(
                rel_type=rel.type.value,
                source_id=src.entity.id,
                target_id=tgt.entity.id,
                properties=rel.properties,
                confidence=rel.confidence,
                run_id=run_id,
            )
            if change == "created":
                stats.relationships_created += 1
            elif change:
                stats.relationships_updated += 1
            repo.record_provenance("relationship", r.id, prov(extractor, rel.confidence, rel.evidence), run_id)

        docs: list[tuple[str, Any]] = []
        for doc in result.documents:
            entity = resolver.resolve_ref(doc.entity, run_id).entity
            repo.upsert_document(entity.id, doc.title, doc.url, chunk_text(doc.text))
            docs.append((entity.id, doc))
        return docs

    def _link_mentions(self, tenant_id: str, connector_id: str, run_id: str, docs: list[_ChangedDoc], stats: RunStats) -> None:
        if not docs:
            return
        with self.db.session() as s:
            repo = GraphRepository(s, Scope(tenant_id))
            extractor = MentionExtractor(repo.all_aliases())
            for doc in docs:
                for m in extractor.find(doc.text, doc.title, exclude_ids={doc.entity_id}):
                    if not is_relation_allowed(RelationType.DESCRIBES, EntityType.DOCUMENTATION, EntityType(m.entity_type)):
                        continue
                    confidence = 0.85 if m.in_title else 0.6
                    rel, change = repo.upsert_relationship(
                        rel_type=RelationType.DESCRIBES.value,
                        source_id=doc.entity_id,
                        target_id=m.entity_id,
                        properties={},
                        confidence=confidence,
                        run_id=run_id,
                    )
                    if change == "created":
                        stats.mentions_linked += 1
                    repo.record_provenance(
                        "relationship",
                        rel.id,
                        ProvenanceInfo(
                            connector_id=connector_id,
                            source_system=doc.source_system,
                            source_record_key=doc.record_key,
                            extractor="text_mention",
                            source_url=doc.source_url,
                            confidence=confidence,
                            evidence=m.snippet,
                        ),
                        run_id,
                    )

    def _retract_unconfirmed(self, tenant_id: str, connector_id: str, run_id: str, keys: list[str], stats: RunStats) -> None:
        """For re-extracted records, drop facts the new version of the record no longer states."""
        if not keys:
            return
        with self.db.session() as s:
            repo = GraphRepository(s, Scope(tenant_id))
            for key in keys:
                report = repo.retract_record_provenance(connector_id, key, run_id)
                stats.stale_entities += len(report.entities)
                stats.stale_relationships += len(report.relationships)

    def _reconcile_full_sync(self, tenant_id: str, connector_id: str, run_id: str, stats: RunStats) -> None:
        with self.db.session() as s:
            report = GraphRepository(s, Scope(tenant_id)).mark_unobserved_stale(connector_id, run_id)
            stats.stale_entities += len(report.entities)
            stats.stale_relationships += len(report.relationships)
            s.execute(
                delete(SourceRecordState).where(
                    SourceRecordState.tenant_id == tenant_id,
                    SourceRecordState.connector_id == connector_id,
                    SourceRecordState.last_run_id != run_id,
                )
            )


async def iterate(records: list[SourceRecord]):
    """Adapt a list to the async iterable expected by ``IngestionPipeline.run``."""
    for r in records:
        yield r


__all__ = ["IngestionPipeline", "RunStats", "iterate", "RecordKind"]

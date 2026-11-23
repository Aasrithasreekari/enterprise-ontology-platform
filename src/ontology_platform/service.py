"""Application service facade shared by the REST API, MCP server and CLI.

Every public method takes the authenticated ``Principal``, enforces permissions, runs the
operation within the principal's tenant/customer scope, records metrics and writes an audit entry.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any, Literal, TypeVar

from sqlalchemy import select

from ontology_platform import admin
from ontology_platform.auth.principal import Permission, Principal
from ontology_platform.auth.service import AuthService, audit
from ontology_platform.config import Settings
from ontology_platform.connectors.base import SourceRecord, available_connector_types
from ontology_platform.errors import PlatformError
from ontology_platform.ingestion.pipeline import IngestionPipeline, iterate
from ontology_platform.observability import QUERY_LATENCY, get_logger
from ontology_platform.ontology.schema import schema_as_dict
from ontology_platform.query.engine import QueryEngine
from ontology_platform.query.qa import QuestionAnswerer
from ontology_platform.search.bm25 import IndexCache
from ontology_platform.storage.db import Database
from ontology_platform.storage.orm import AuditLog
from ontology_platform.storage.repository import GraphRepository

log = get_logger(__name__)
T = TypeVar("T")
Channel = Literal["rest", "mcp", "cli"]


class PlatformService:
    def __init__(self, db: Database, settings: Settings, pipeline: IngestionPipeline | None = None) -> None:
        self.db = db
        self.settings = settings
        self.auth = AuthService(db, settings)
        self.pipeline = pipeline or IngestionPipeline(db, settings)
        self.cache = IndexCache()

    # ------------------------------------------------------------------ plumbing
    def _run(
        self,
        principal: Principal,
        permission: Permission,
        action: str,
        params: dict[str, Any],
        channel: Channel,
        fn: Callable[[], T],
    ) -> T:
        try:
            principal.require(permission)
            with QUERY_LATENCY.labels(operation=action).time():
                result = fn()
        except PlatformError as exc:
            audit(self.db, principal, channel, action, params, "denied" if exc.status_code in (401, 403) else "error", str(exc))
            raise
        audit(self.db, principal, channel, action, params, "ok")
        return result

    def _query(self, principal: Principal, action: str, params: dict[str, Any], channel: Channel, fn: Callable[[QueryEngine], T]) -> T:
        def op() -> T:
            with self.db.session() as s:
                return fn(QueryEngine(s, principal.scope, self.settings, self.cache))

        return self._run(principal, Permission.GRAPH_READ, action, params, channel, op)

    # ------------------------------------------------------------------ graph queries
    def search_entities(self, p: Principal, query: str, types: list[str] | None = None, limit: int = 10,
                        property_filters: dict[str, Any] | None = None, channel: Channel = "rest") -> list[dict[str, Any]]:
        params = {"query": query, "types": types, "limit": limit, "property_filters": property_filters}
        return self._query(p, "search_entities", params, channel, lambda e: e.search_entities(query, types, limit, property_filters))

    def get_entity_details(self, p: Principal, ref: str, include_provenance: bool = True, include_history: bool = False,
                           channel: Channel = "rest") -> dict[str, Any]:
        return self._query(p, "get_entity_details", {"ref": ref}, channel,
                           lambda e: e.get_entity_details(ref, include_provenance, include_history))

    def get_related(self, p: Principal, ref: str, relation_types: list[str] | None = None, direction: str = "both",
                    target_types: list[str] | None = None, depth: int = 1, channel: Channel = "rest") -> dict[str, Any]:
        params = {"ref": ref, "relation_types": relation_types, "direction": direction, "target_types": target_types, "depth": depth}
        return self._query(p, "get_related_entities", params, channel,
                           lambda e: e.get_related(ref, relation_types, direction, target_types, depth))  # type: ignore[arg-type]

    def find_relationships(self, p: Principal, source: str, target: str, max_depth: int = 4, channel: Channel = "rest") -> dict[str, Any]:
        return self._query(p, "find_entity_relationships", {"source": source, "target": target, "max_depth": max_depth}, channel,
                           lambda e: e.find_relationships(source, target, max_depth))

    def ask(self, p: Principal, question: str, limit: int = 50, channel: Channel = "rest") -> dict[str, Any]:
        return self._query(p, "query_knowledge_graph", {"question": question}, channel, lambda e: QuestionAnswerer(e).ask(question, limit))

    def search_documentation(self, p: Principal, query: str, entity: str | None = None, limit: int = 5, channel: Channel = "rest") -> dict[str, Any]:
        return self._query(p, "search_documentation", {"query": query, "entity": entity, "limit": limit}, channel,
                           lambda e: e.search_documentation(query, entity, limit))

    def trace_dependencies(self, p: Principal, ref: str, direction: str = "downstream", depth: int = 4, channel: Channel = "rest") -> dict[str, Any]:
        return self._query(p, "trace_application_dependencies", {"ref": ref, "direction": direction, "depth": depth}, channel,
                           lambda e: e.trace_dependencies(ref, direction, depth))  # type: ignore[arg-type]

    def deployment_information(self, p: Principal, application: str, customer: str | None = None, environment: str | None = None,
                               channel: Channel = "rest") -> dict[str, Any]:
        params = {"application": application, "customer": customer, "environment": environment}
        return self._query(p, "get_deployment_information", params, channel,
                           lambda e: e.get_deployment_information(application, customer, environment))

    def graph_stats(self, p: Principal, channel: Channel = "rest") -> dict[str, Any]:
        return self._query(p, "graph_stats", {}, channel, lambda e: GraphRepository(e.repo.session, p.scope).stats())

    def ontology_schema(self, p: Principal, channel: Channel = "rest") -> dict[str, Any]:
        return self._run(p, Permission.GRAPH_READ, "get_ontology_schema", {}, channel, schema_as_dict)

    # ------------------------------------------------------------------ connectors & ingestion
    def list_connectors(self, p: Principal, channel: Channel = "rest") -> list[dict[str, Any]]:
        return self._run(p, Permission.CONNECTORS_READ, "list_connectors", {}, channel, lambda: admin.list_connectors(self.db, p.tenant_id))

    def connector_types(self, p: Principal) -> list[str]:
        p.require(Permission.CONNECTORS_READ)
        return available_connector_types()

    def register_connector(self, p: Principal, name: str, connector_type: str, config: dict[str, Any], secret_ref: str | None,
                           enabled: bool = True, channel: Channel = "rest") -> dict[str, Any]:
        params = {"name": name, "type": connector_type, "config": config, "secret_ref": secret_ref}
        return self._run(p, Permission.CONNECTORS_WRITE, "register_connector", params, channel,
                         lambda: admin.register_connector(self.db, p.tenant_id, name, connector_type, config, secret_ref, enabled))

    def start_sync(self, p: Principal, connector_id: str, mode: str = "full", channel: Channel = "rest") -> str:
        return self._run(p, Permission.INGEST_RUN, "start_sync", {"connector_id": connector_id, "mode": mode}, channel,
                         lambda: self.pipeline.start_run(p.tenant_id, connector_id, mode, p.subject))

    async def execute_sync(self, p: Principal, connector_id: str, run_id: str, mode: str) -> dict[str, Any]:
        p.require(Permission.INGEST_RUN)
        result = await self.pipeline.run(p.tenant_id, connector_id, mode, p.subject, run_id=run_id)
        self.cache.clear()
        return result

    async def push_records(self, p: Principal, connector_id: str, records: list[SourceRecord], channel: Channel = "rest") -> dict[str, Any]:
        run_id = self.start_sync(p, connector_id, "push", channel)
        result = await self.pipeline.run(p.tenant_id, connector_id, "push", p.subject, run_id=run_id, records=iterate(records))
        self.cache.clear()
        return result

    def list_runs(self, p: Principal, connector_id: str | None = None, limit: int = 50) -> list[dict[str, Any]]:
        p.require(Permission.CONNECTORS_READ)
        return admin.list_runs(self.db, p.tenant_id, connector_id, limit)

    def get_run(self, p: Principal, run_id: str) -> dict[str, Any]:
        p.require(Permission.CONNECTORS_READ)
        return admin.get_run(self.db, p.tenant_id, run_id)

    # ------------------------------------------------------------------ keys & audit
    def create_api_key(self, p: Principal, name: str, roles: list[str], allowed_customers: list[str] | None, channel: Channel = "rest") -> dict[str, Any]:
        def op() -> dict[str, Any]:
            plaintext, meta = self.auth.create_api_key(p.tenant_id, name, roles, allowed_customers)
            return {**meta, "api_key": plaintext}

        return self._run(p, Permission.KEYS_MANAGE, "create_api_key", {"name": name, "roles": roles, "allowed_customers": allowed_customers}, channel, op)

    def list_api_keys(self, p: Principal) -> list[dict[str, Any]]:
        p.require(Permission.KEYS_MANAGE)
        return self.auth.list_api_keys(p.tenant_id)

    def revoke_api_key(self, p: Principal, key_id: str, channel: Channel = "rest") -> None:
        self._run(p, Permission.KEYS_MANAGE, "revoke_api_key", {"key_id": key_id}, channel, lambda: self.auth.revoke_api_key(p.tenant_id, key_id))

    def audit_log(self, p: Principal, limit: int = 100) -> list[dict[str, Any]]:
        p.require(Permission.AUDIT_READ)
        with self.db.session() as s:
            rows = s.scalars(select(AuditLog).where(AuditLog.tenant_id == p.tenant_id).order_by(AuditLog.id.desc()).limit(min(limit, 1000)))
            return [
                {"at": r.at.isoformat(), "principal": r.principal, "channel": r.channel, "action": r.action, "params": r.params, "outcome": r.outcome, "detail": r.detail}
                for r in rows
            ]

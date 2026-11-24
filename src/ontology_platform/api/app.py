"""FastAPI application exposing the platform over REST (OpenAPI docs at /docs)."""

from __future__ import annotations

import time
import uuid
from contextlib import asynccontextmanager
from typing import Any, Literal

from fastapi import BackgroundTasks, Depends, FastAPI, Header, Query, Request
from fastapi.responses import JSONResponse, PlainTextResponse
from prometheus_client import CONTENT_TYPE_LATEST, generate_latest
from pydantic import BaseModel, Field
from sqlalchemy import text

from ontology_platform.auth.principal import Principal
from ontology_platform.config import Settings, get_settings
from ontology_platform.connectors.base import SourceRecord
from ontology_platform.errors import AuthenticationError, PlatformError
from ontology_platform.observability import HTTP_REQUESTS, REGISTRY, configure_logging, correlation_id, get_logger
from ontology_platform.ontology.schema import EntityType, RelationType
from ontology_platform.api.ui import mount_ui
from ontology_platform.service import PlatformService
from ontology_platform.storage.db import Database

log = get_logger(__name__)

EntityTypeName = Literal[tuple(t.value for t in EntityType)]  # type: ignore[valid-type]
RelationTypeName = Literal[tuple(t.value for t in RelationType)]  # type: ignore[valid-type]


# ----------------------------------------------------------------------------- request models
class AskRequest(BaseModel):
    question: str = Field(min_length=3, max_length=1000)
    limit: int = Field(default=50, ge=1, le=500)


class AgentQueryRequest(BaseModel):
    question: str = Field(min_length=3, max_length=1000, description="Natural-language enterprise question")


class ConnectorCreate(BaseModel):
    name: str = Field(min_length=1, max_length=255, pattern=r"^[A-Za-z0-9][A-Za-z0-9 _.-]*$")
    type: str
    config: dict[str, Any] = Field(default_factory=dict)
    secret_ref: str | None = Field(default=None, description="env:VAR or file:/path — never the secret itself")
    enabled: bool = True


class SyncRequest(BaseModel):
    mode: Literal["full", "incremental"] = "full"


class PushRequest(BaseModel):
    records: list[SourceRecord] = Field(min_length=1, max_length=1000)


class ApiKeyCreate(BaseModel):
    name: str = Field(min_length=1, max_length=255)
    roles: list[Literal["viewer", "ingestor", "admin"]] = Field(min_length=1)
    allowed_customers: list[str] | None = None


# ----------------------------------------------------------------------------- app factory
def create_app(settings: Settings | None = None, service: PlatformService | None = None) -> FastAPI:
    settings = settings or get_settings()
    if service is None:
        configure_logging(settings.log_level, settings.log_json)
        service = PlatformService(Database.from_settings(settings), settings)

    @asynccontextmanager
    async def lifespan(_: FastAPI):
        yield
        service.db.engine.dispose()

    app = FastAPI(
        title="Enterprise Ontology Platform",
        version="0.1.0",
        description="Unified enterprise knowledge graph over code, docs, cloud and Kubernetes. "
        "Authenticate with `Authorization: Bearer <api key or JWT>`.",
        lifespan=lifespan,
    )
    app.state.service = service

    @app.middleware("http")
    async def request_context(request: Request, call_next):
        rid = request.headers.get("X-Request-ID") or uuid.uuid4().hex
        token = correlation_id.set(rid)
        start = time.perf_counter()
        try:
            response = await call_next(request)
        finally:
            correlation_id.reset(token)
        route = request.scope.get("route")
        HTTP_REQUESTS.labels(request.method, getattr(route, "path", "unmatched"), str(response.status_code)).inc()
        response.headers["X-Request-ID"] = rid
        log.info(
            "http request",
            extra={"method": request.method, "path": request.url.path, "status": response.status_code,
                   "ms": round((time.perf_counter() - start) * 1000, 1), "correlation_id": rid},
        )
        return response

    @app.exception_handler(PlatformError)
    async def platform_error(_: Request, exc: PlatformError):
        headers = {"WWW-Authenticate": "Bearer"} if isinstance(exc, AuthenticationError) else None
        return JSONResponse(status_code=exc.status_code, content={"error": exc.code, "message": str(exc)}, headers=headers)

    def svc() -> PlatformService:
        return app.state.service

    def principal(authorization: str | None = Header(default=None), x_api_key: str | None = Header(default=None)) -> Principal:
        return svc().auth.authenticate(authorization or x_api_key)

    P = Depends(principal)

    # ------------------------------------------------------------------ operational
    @app.get("/health", tags=["ops"])
    def health() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/ready", tags=["ops"])
    def ready() -> dict[str, str]:
        with svc().db.engine.connect() as conn:
            conn.execute(text("SELECT 1"))
        return {"status": "ready"}

    @app.get("/metrics", tags=["ops"], response_class=PlainTextResponse)
    def metrics() -> PlainTextResponse:
        return PlainTextResponse(generate_latest(REGISTRY).decode(), media_type=CONTENT_TYPE_LATEST)

    # ------------------------------------------------------------------ graph
    v1 = "/api/v1"

    @app.get(f"{v1}/ontology/schema", tags=["ontology"])
    def ontology_schema(p: Principal = P):
        return svc().ontology_schema(p)

    @app.get(f"{v1}/graph/stats", tags=["graph"])
    def graph_stats(p: Principal = P):
        return svc().graph_stats(p)

    @app.get(f"{v1}/entities", tags=["graph"])
    def search_entities(
        q: str = Query(default="", max_length=500),
        type: list[EntityTypeName] | None = Query(default=None),  # type: ignore[valid-type]
        limit: int = Query(default=20, ge=1, le=500),
        p: Principal = P,
    ):
        return {"results": svc().search_entities(p, q, list(type) if type else None, limit)}

    @app.get(f"{v1}/entities/{{ref}}", tags=["graph"])
    def get_entity(ref: str, history: bool = False, p: Principal = P):
        return svc().get_entity_details(p, ref, include_history=history)

    @app.get(f"{v1}/entities/{{ref}}/related", tags=["graph"])
    def related(
        ref: str,
        relation: list[RelationTypeName] | None = Query(default=None),  # type: ignore[valid-type]
        direction: Literal["out", "in", "both"] = "both",
        target_type: list[EntityTypeName] | None = Query(default=None),  # type: ignore[valid-type]
        depth: int = Query(default=1, ge=1, le=12),
        p: Principal = P,
    ):
        return svc().get_related(p, ref, list(relation) if relation else None, direction, list(target_type) if target_type else None, depth)

    @app.get(f"{v1}/paths", tags=["graph"])
    def paths(source: str, target: str, max_depth: int = Query(default=4, ge=1, le=12), p: Principal = P):
        return svc().find_relationships(p, source, target, max_depth)

    @app.get(f"{v1}/dependencies/{{ref}}", tags=["query"])
    def dependencies(ref: str, direction: Literal["downstream", "upstream"] = "downstream",
                     depth: int = Query(default=4, ge=1, le=12), p: Principal = P):
        return svc().trace_dependencies(p, ref, direction, depth)

    @app.get(f"{v1}/deployments", tags=["query"])
    def deployments(application: str, customer: str | None = None, environment: str | None = None, p: Principal = P):
        return svc().deployment_information(p, application, customer, environment)

    @app.get(f"{v1}/documents/search", tags=["query"])
    def search_docs(q: str = Query(min_length=1, max_length=500), entity: str | None = None,
                    limit: int = Query(default=5, ge=1, le=50), p: Principal = P):
        return svc().search_documentation(p, q, entity, limit)

    @app.post(f"{v1}/query", tags=["query"])
    def ask(body: AskRequest, p: Principal = P):
        return svc().ask(p, body.question, body.limit)

    # ------------------------------------------------------------------ multi-agent workflow
    @app.post(f"{v1}/agent/query", tags=["agent"])
    async def agent_query(body: AgentQueryRequest, p: Principal = P):
        """Run the evidence-validated multi-agent workflow; returns the full run (plan, events, facts, answer)."""
        return await svc().run_workflow(p, body.question, channel="rest")

    @app.get(f"{v1}/agent/runs", tags=["agent"])
    def agent_runs(limit: int = Query(default=20, ge=1, le=100), offset: int = Query(default=0, ge=0), p: Principal = P):
        return svc().list_workflows(p, limit=limit, offset=offset)

    @app.get(f"{v1}/agent/runs/{{run_id}}", tags=["agent"])
    def agent_run(run_id: str, p: Principal = P):
        return svc().get_workflow(p, run_id)

    @app.get(f"{v1}/agent/runs/{{run_id}}/events", tags=["agent"])
    def agent_run_events(run_id: str, p: Principal = P):
        return {"events": svc().get_workflow(p, run_id).get("events", [])}

    # ------------------------------------------------------------------ connectors & ingestion
    @app.get(f"{v1}/connector-types", tags=["ingestion"])
    def connector_types(p: Principal = P):
        return {"types": svc().connector_types(p)}

    @app.get(f"{v1}/connectors", tags=["ingestion"])
    def list_connectors(p: Principal = P):
        return {"connectors": svc().list_connectors(p)}

    @app.post(f"{v1}/connectors", tags=["ingestion"], status_code=201)
    def create_connector(body: ConnectorCreate, p: Principal = P):
        return svc().register_connector(p, body.name, body.type, body.config, body.secret_ref, body.enabled)

    @app.post(f"{v1}/connectors/{{connector_id}}/sync", tags=["ingestion"], status_code=202)
    def sync(connector_id: str, body: SyncRequest, background: BackgroundTasks, p: Principal = P):
        run_id = svc().start_sync(p, connector_id, body.mode)
        background.add_task(svc().execute_sync, p, connector_id, run_id, body.mode)
        return {"run_id": run_id, "status": "running"}

    @app.post(f"{v1}/connectors/{{connector_id}}/records", tags=["ingestion"])
    async def push(connector_id: str, body: PushRequest, p: Principal = P):
        return await svc().push_records(p, connector_id, body.records)

    @app.get(f"{v1}/ingestion/runs", tags=["ingestion"])
    def runs(connector_id: str | None = None, limit: int = Query(default=50, ge=1, le=500), p: Principal = P):
        return {"runs": svc().list_runs(p, connector_id, limit)}

    @app.get(f"{v1}/ingestion/runs/{{run_id}}", tags=["ingestion"])
    def run(run_id: str, p: Principal = P):
        return svc().get_run(p, run_id)

    # ------------------------------------------------------------------ admin
    @app.get(f"{v1}/admin/api-keys", tags=["admin"])
    def list_keys(p: Principal = P):
        return {"api_keys": svc().list_api_keys(p)}

    @app.post(f"{v1}/admin/api-keys", tags=["admin"], status_code=201)
    def create_key(body: ApiKeyCreate, p: Principal = P):
        return svc().create_api_key(p, body.name, list(body.roles), body.allowed_customers)

    @app.delete(f"{v1}/admin/api-keys/{{key_id}}", tags=["admin"], status_code=204)
    def revoke_key(key_id: str, p: Principal = P):
        svc().revoke_api_key(p, key_id)

    @app.get(f"{v1}/admin/audit", tags=["admin"])
    def audit_log(limit: int = Query(default=100, ge=1, le=1000), p: Principal = P):
        return {"entries": svc().audit_log(p, limit)}

    # ------------------------------------------------------------------ operator console
    mount_ui(app)

    return app

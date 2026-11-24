"""MCP server exposing the enterprise knowledge graph to AI agents.

Transports:
* stdio — for local agents; authenticates as the owner of ``OP_MCP_API_KEY``.
* streamable HTTP — every request must carry ``Authorization: Bearer <api key | JWT>``;
  there is no fallback identity.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Annotated, Any, Literal, TypeVar

from mcp.server.mcpserver import Context, MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp_types import ToolAnnotations
from pydantic import Field

from ontology_platform.auth.principal import Principal
from ontology_platform.errors import AuthenticationError, PlatformError
from ontology_platform.observability import TOOL_CALLS, get_logger
from ontology_platform.ontology.schema import EntityType, RelationType
from ontology_platform.service import PlatformService

log = get_logger(__name__)
T = TypeVar("T")

EntityTypeName = Literal[tuple(t.value for t in EntityType)]  # type: ignore[valid-type]
RelationTypeName = Literal[tuple(t.value for t in RelationType)]  # type: ignore[valid-type]
EntityRefArg = Annotated[
    str,
    Field(min_length=1, max_length=512, description="Entity id, canonical key (e.g. 'application:payments-platform') or name/alias"),
]

READ_ONLY = ToolAnnotations(read_only_hint=True, destructive_hint=False, idempotent_hint=True, open_world_hint=False)

INSTRUCTIONS = """\
Enterprise Ontology Platform: a knowledge graph of customers, products, applications, services,
APIs, repositories, databases, cloud resources, Kubernetes clusters, deployments, environments and
documentation, built from GitHub/GitLab/Bitbucket, Confluence, Kubernetes and AWS/Azure/GCP.

Choosing a tool:
* Broad or multi-part questions ("what breaks if X fails and where is it deployed?") ->
  `run_enterprise_intelligence_workflow`: plans the work across specialist agents, validates every
  fact against evidence and returns a structured answer with confirmed vs inferred facts.
* Single, well-scoped questions -> `query_knowledge_graph` (one intent, fast, raw results).
* Unknown names -> `search_entities` first, then pass the returned `key` to the other tools.
* One entity -> `get_entity_details`; its neighbourhood -> `get_related_entities`;
  how two entities connect -> `find_entity_relationships`.
* Impact analysis -> `trace_application_dependencies` with direction="upstream";
  what something needs -> direction="downstream".
* Where something runs -> `get_deployment_information`; runbooks/READMEs -> `search_documentation`;
  valid entity/relationship type names -> `get_ontology_schema`.

Every fact carries provenance (source system, URL, extractor, confidence); cite it in answers and
distinguish confirmed from inferred facts. Results are limited to your tenant and the customers
your credentials allow.
"""


def build_mcp_server(service: PlatformService, default_principal: Principal | None = None) -> MCPServer:
    """Create the MCP server. ``default_principal`` is used only when no Authorization header is
    present (stdio transport); never pass one for network transports."""
    mcp = MCPServer(name="enterprise-ontology", title="Enterprise Ontology Platform", instructions=INSTRUCTIONS, version="0.1.0")

    def principal_for(ctx: Context | None) -> Principal:
        headers = None
        if ctx is not None:
            try:
                headers = ctx.headers
            except (ValueError, AttributeError):
                headers = None
        auth = None
        if headers:
            auth = headers.get("authorization") or headers.get("Authorization")
        if auth:
            return service.auth.authenticate(auth)
        if default_principal is not None:
            return default_principal
        raise AuthenticationError("missing credentials: send 'Authorization: Bearer <api key>'")

    def call(tool: str, ctx: Context | None, fn: Callable[[Principal], T]) -> T:
        try:
            result = fn(principal_for(ctx))
        except PlatformError as exc:
            TOOL_CALLS.labels(tool=tool, outcome=exc.code).inc()
            raise ToolError(f"{exc.code}: {exc}") from exc
        TOOL_CALLS.labels(tool=tool, outcome="ok").inc()
        return result

    async def acall(tool: str, ctx: Context | None, fn: Callable[[Principal], Awaitable[T]]) -> T:
        try:
            result = await fn(principal_for(ctx))
        except PlatformError as exc:
            TOOL_CALLS.labels(tool=tool, outcome=exc.code).inc()
            raise ToolError(f"{exc.code}: {exc}") from exc
        TOOL_CALLS.labels(tool=tool, outcome="ok").inc()
        return result

    @mcp.tool(annotations=READ_ONLY)
    async def run_enterprise_intelligence_workflow(
        question: Annotated[
            str,
            Field(min_length=3, max_length=1000, description="Natural-language enterprise question; may combine several asks"),
        ],
        ctx: Context | None = None,
    ) -> dict[str, Any]:
        """Answer a complex enterprise question with the evidence-validated multi-agent workflow.

        Use when the question spans several concerns (impact + deployments + documentation, ownership
        chains, cross-customer comparisons) or when the answer must separate confirmed from inferred
        facts. For a single lookup prefer `query_knowledge_graph` or a specific graph tool.

        Returns the workflow run: `answer` (summary, sections, confirmed_facts, inferred_facts,
        missing_information, limitations, sources), `validation` (status and counts), `facts` with
        evidence, the executed `plan` (agents, status, timings) and `events`. Cite `answer.sources`.

        Example: question="What is impacted if auth-service fails, and in which production
        environments is it deployed?"
        """
        return await acall(
            "run_enterprise_intelligence_workflow", ctx, lambda p: service.run_workflow(p, question, channel="mcp")
        )

    @mcp.tool(annotations=READ_ONLY)
    def search_entities(
        query: Annotated[str, Field(max_length=500, description="Free text: names, aliases, keywords")],
        types: Annotated[list[EntityTypeName] | None, Field(description="Restrict to these entity types")] = None,  # type: ignore[valid-type]
        limit: Annotated[int, Field(ge=1, le=100)] = 10,
        ctx: Context | None = None,
    ) -> dict[str, Any]:
        """Find entities by name, alias or keywords; use this to turn a name into a canonical entity key.

        Exact alias matches rank first, then keyword (BM25) matches. Returns `results`: id, type, name,
        key, status, description, score and match kind. Pass a result's `key` to the other tools.

        Example: query="fraud", types=["service"] -> the fraud-detector service.
        """
        return call("search_entities", ctx, lambda p: {"results": service.search_entities(p, query, list(types) if types else None, limit, channel="mcp")})

    @mcp.tool(annotations=READ_ONLY)
    def get_entity_details(
        entity: EntityRefArg,
        include_history: Annotated[bool, Field(description="Include version history snapshots")] = False,
        ctx: Context | None = None,
    ) -> dict[str, Any]:
        """Everything known about one entity: properties, aliases, direct relationships (with direction and
        confidence), provenance of each source, and optionally version history.

        Use after `search_entities` once you know which entity is meant. Fails with not_found when the
        reference does not resolve.

        Example: entity="api:auth-api" -> its endpoints, owning application and source repositories.
        """
        return call("get_entity_details", ctx, lambda p: service.get_entity_details(p, entity, include_history=include_history, channel="mcp"))

    @mcp.tool(annotations=READ_ONLY)
    def get_related_entities(
        entity: EntityRefArg,
        relation_types: Annotated[list[RelationTypeName] | None, Field(description="Only follow these relationship types")] = None,  # type: ignore[valid-type]
        direction: Annotated[Literal["out", "in", "both"], Field(description="Edge direction relative to the entity")] = "both",
        target_types: Annotated[list[EntityTypeName] | None, Field(description="Only return entities of these types")] = None,  # type: ignore[valid-type]
        depth: Annotated[int, Field(ge=1, le=6, description="Traversal depth")] = 1,
        ctx: Context | None = None,
    ) -> dict[str, Any]:
        """Neighbourhood of an entity: everything reachable breadth-first within `depth` hops, optionally
        filtered by relationship type, direction and target entity type.

        Returns `root`, `count` and `related` (entity, depth and the edge path that reaches it).
        Use for "what does X contain / expose / run on"; use `trace_application_dependencies` for
        transitive depends_on chains instead.

        Example: entity="product:payhub", relation_types=["contains"], direction="out" -> its applications.
        """
        return call(
            "get_related_entities",
            ctx,
            lambda p: service.get_related(p, entity, list(relation_types) if relation_types else None, direction,
                                          list(target_types) if target_types else None, depth, channel="mcp"),
        )

    @mcp.tool(annotations=READ_ONLY)
    def find_entity_relationships(
        source: EntityRefArg,
        target: EntityRefArg,
        max_depth: Annotated[int, Field(ge=1, le=6)] = 4,
        ctx: Context | None = None,
    ) -> dict[str, Any]:
        """How two entities are connected: the shortest paths between them, with evidence for every edge.

        Returns `connected` (bool) and `paths` (lists of edges: type, source, target, confidence,
        evidence). Use to justify a relationship claim or explain an indirect link.

        Example: source="customer:acme", target="api:payments-api" -> customer owns product, which
        contains the application that exposes the API.
        """
        return call("find_entity_relationships", ctx, lambda p: service.find_relationships(p, source, target, max_depth, channel="mcp"))

    @mcp.tool(annotations=READ_ONLY)
    def query_knowledge_graph(
        question: Annotated[str, Field(min_length=3, max_length=1000, description="Natural-language question about the enterprise")],
        limit: Annotated[int, Field(ge=1, le=200)] = 50,
        ctx: Context | None = None,
    ) -> dict[str, Any]:
        """Answer one well-scoped enterprise question with a single graph query.

        Recognised question kinds: repositories of a customer's product, APIs exposed by an application,
        deployments per environment, dependencies, clusters hosting a customer, documentation and the
        impact of an outage. Returns `intent`, linked entities, structured `results` and evidence paths.
        For multi-part questions or validated confirmed/inferred answers use
        `run_enterprise_intelligence_workflow`.

        Example: question="Which Kubernetes clusters host Customer B's applications?"
        """
        return call("query_knowledge_graph", ctx, lambda p: service.ask(p, question, limit, channel="mcp"))

    @mcp.tool(annotations=READ_ONLY)
    def search_documentation(
        query: Annotated[str, Field(min_length=1, max_length=500)],
        entity: Annotated[str | None, Field(max_length=512, description="Only documents describing this entity")] = None,
        limit: Annotated[int, Field(ge=1, le=20)] = 5,
        ctx: Context | None = None,
    ) -> dict[str, Any]:
        """Full-text search over Confluence pages and repository READMEs (runbooks, design notes, tuning guides).

        Returns `results`: passage text, document title, URL and score; optionally restricted to
        documents describing one entity. Quote the URL when citing.

        Example: query="promote read replica" -> the Payments Runbook passage.
        """
        return call("search_documentation", ctx, lambda p: service.search_documentation(p, query, entity, limit, channel="mcp"))

    @mcp.tool(annotations=READ_ONLY)
    def trace_application_dependencies(
        entity: EntityRefArg,
        direction: Annotated[
            Literal["downstream", "upstream"],
            Field(description="downstream = what it depends on; upstream = what depends on it (impact analysis)"),
        ] = "downstream",
        depth: Annotated[int, Field(ge=1, le=6)] = 4,
        ctx: Context | None = None,
    ) -> dict[str, Any]:
        """Transitive depends_on tracing for an application, service, database or API.

        direction="downstream" lists what the entity needs (with hop counts and evidence);
        direction="upstream" lists what depends on it and adds an `impact` summary of affected
        services, applications, deployments and customers - use it for outage/blast-radius questions.

        Example: entity="service:notification-service", direction="upstream" -> affected applications.
        """
        return call("trace_application_dependencies", ctx, lambda p: service.trace_dependencies(p, entity, direction, depth, channel="mcp"))

    @mcp.tool(annotations=READ_ONLY)
    def get_deployment_information(
        application: EntityRefArg,
        customer: Annotated[str | None, Field(max_length=255, description="Customer name/alias filter")] = None,
        environment: Annotated[str | None, Field(max_length=32, description="sit | uat | staging | prod | dev (aliases accepted)")] = None,
        ctx: Context | None = None,
    ) -> dict[str, Any]:
        """Where an application is deployed: per customer and environment tier, the clusters, namespace,
        workloads (images, replicas) and cloud resources, with source evidence.

        `application` must be an entity reference (key, id or alias), not a sentence; resolve names with
        `search_entities` first.

        Example: application="application:claims-portal", environment="uat".
        """
        return call("get_deployment_information", ctx, lambda p: service.deployment_information(p, application, customer, environment, channel="mcp"))

    @mcp.tool(annotations=READ_ONLY)
    def get_ontology_schema(ctx: Context | None = None) -> dict[str, Any]:
        """The ontology itself: entity types (with identity fields), relationship types with their allowed
        source/target types, and environment tiers.

        Use to find valid values for `types`, `relation_types` and `target_types` filters of other tools.
        """
        return call("get_ontology_schema", ctx, lambda p: service.ontology_schema(p, channel="mcp"))

    return mcp

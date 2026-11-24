# MCP Integration

The platform exposes the knowledge graph to AI assistants through a Model Context Protocol server
named `enterprise-ontology` (`src/ontology_platform/mcp_server/server.py`). It is built on the
official `mcp` Python SDK (v2, `MCPServer`) and calls the same `PlatformService` facade as the REST
API, so authorization, tenant and customer scoping, audit and metrics are identical on both
interfaces.

## 1. Transports

| | stdio | Streamable HTTP |
|---|---|---|
| Start | `ontology-platform mcp --transport stdio` (the default) | `ontology-platform mcp --transport http --host 127.0.0.1 --port 8001` |
| Endpoint | the client launches the process and talks over stdin/stdout | `http://<host>:<port>/mcp` |
| Identity | the owner of `OP_MCP_API_KEY` | `Authorization: Bearer <api key or JWT>` on every request |
| Missing credential | the process exits with `OP_MCP_API_KEY must be set for the stdio transport` | every tool call fails with `unauthenticated` |
| Typical use | a local assistant on the same machine, often with the SQLite database | the `mcp` container in Podman Compose (`127.0.0.1:8001`) |

Details:

* **stdio.** The key in `OP_MCP_API_KEY` is authenticated once at startup; an invalid key stops the
  process. Every tool call then acts as that principal. Logs go to stderr so stdout carries only
  protocol messages.
* **HTTP.** The server runs in stateless mode (`stateless_http=True`) and has no default identity.
  The credential is verified on every tool call, so a revoked key stops working immediately.
* **Host-header allow-list (DNS-rebinding protection).** `OP_MCP_ALLOWED_HOSTS` is a comma-separated
  list such as `localhost:*,127.0.0.1:*,mcp:*`. When set, only those `Host` values (and the matching
  `http://` origins) are accepted; other requests get HTTP 421. When empty, the SDK applies its own
  localhost allow-list only if the server is bound to `127.0.0.1`, `localhost` or `::1`; bound to any
  other address (for example `0.0.0.0`) there is no host check. Compose sets
  `localhost:*,127.0.0.1:*,mcp:*` for the `mcp` service.

## 2. Tools

All tools are annotated read-only (`readOnlyHint: true`, `destructiveHint: false`,
`idempotentHint: true`, `openWorldHint: false`). Inputs are validated from the annotated type hints;
out-of-range values are rejected before the platform runs. Entity arguments accept an id, a
canonical key (`application:payments-platform`) or a name/alias, up to 512 characters.

| Tool | Purpose | Key parameters | REST equivalent |
|---|---|---|---|
| `run_enterprise_intelligence_workflow` | Multi-part questions answered by the evidence-validated multi-agent workflow ([AGENTIC_WORKFLOW.md](AGENTIC_WORKFLOW.md)) | `question` (3–1000 chars) | `POST /api/v1/agent/query` |
| `query_knowledge_graph` | One well-scoped natural-language question, one graph query | `question` (3–1000), `limit` (1–200, default 50) | `POST /api/v1/query` |
| `search_entities` | Turn a name, alias or keyword into entity keys | `query` (≤ 500), `types`, `limit` (1–100, default 10) | `GET /api/v1/entities` |
| `get_entity_details` | Properties, aliases, relationships, provenance, optional history | `entity`, `include_history` (default false) | `GET /api/v1/entities/{ref}` |
| `get_related_entities` | Neighbourhood within N hops | `entity`, `relation_types`, `direction` (`out`/`in`/`both`), `target_types`, `depth` (1–6, default 1) | `GET /api/v1/entities/{ref}/related` |
| `find_entity_relationships` | Shortest paths between two entities, with evidence per edge | `source`, `target`, `max_depth` (1–6, default 4) | `GET /api/v1/paths` |
| `search_documentation` | BM25 search over Confluence pages and READMEs | `query` (1–500), `entity`, `limit` (1–20, default 5) | `GET /api/v1/documents/search` |
| `trace_application_dependencies` | Transitive `depends_on`; `upstream` adds an impact summary | `entity`, `direction` (`downstream`/`upstream`), `depth` (1–6, default 4) | `GET /api/v1/dependencies/{ref}` |
| `get_deployment_information` | Deployments per customer and environment tier | `application`, `customer`, `environment` | `GET /api/v1/deployments` |
| `get_ontology_schema` | Entity and relationship types, for valid filter values | none | `GET /api/v1/ontology/schema` |

Depth parameters are additionally capped by `OP_MAX_TRAVERSAL_DEPTH` (default 6). Tool results are
structured JSON with the same shapes as the REST responses in
[API_DOCUMENTATION.md](API_DOCUMENTATION.md).

**Tool selection.** The server sends instructions to the client at initialization. In short: use
`run_enterprise_intelligence_workflow` for broad or multi-part questions and when confirmed and
inferred facts must be separated; `query_knowledge_graph` for a single question; `search_entities`
first when a name is unknown, then pass the returned `key` to the other tools; `direction="upstream"`
for impact analysis.

## 3. Authentication and authorization

```mermaid
flowchart LR
    C[Tool call] --> H{Authorization header?}
    H -->|yes| A[AuthService.authenticate<br/>API key or JWT]
    H -->|no, stdio| D[Startup principal<br/>from OP_MCP_API_KEY]
    H -->|no, HTTP| E[unauthenticated]
    A --> P[Principal: tenant, roles, allowed customers]
    D --> P
    P --> S[PlatformService: require graph:read,<br/>scope to tenant + customers, audit]
```

* Every tool needs the `graph:read` permission (all three roles have it).
* Results are limited to the principal's tenant and, for customer-restricted credentials, to the
  allowed customers. Entities outside the scope are reported as `not_found`.
* Each call is written to the audit log with channel `mcp` and counted in
  `op_mcp_tool_calls_total{tool, outcome}`.
* Platform errors are returned as MCP tool errors (`isError: true`) whose text is
  `<code>: <message>`, for example `unauthenticated: invalid api key` or
  `not_found: no entity matches 'mainframe'`. Credentials are never echoed in error text.

Roles, JWT claims and the audit model are described in [SECURITY.md](SECURITY.md).

## 4. Request flow (streamable HTTP)

```mermaid
sequenceDiagram
    autonumber
    participant C as MCP client (AI assistant)
    participant T as MCP HTTP transport (/mcp)
    participant M as MCPServer tool
    participant A as AuthService
    participant S as PlatformService
    participant Q as QueryEngine
    participant DB as PostgreSQL
    C->>T: POST /mcp tools/call (Authorization: Bearer key)
    T->>T: Host header in OP_MCP_ALLOWED_HOSTS?
    T->>M: validated arguments
    M->>A: authenticate(Authorization)
    A->>DB: look up key hash / verify JWT, check tenant
    A-->>M: Principal (tenant, roles, customers)
    M->>S: operation(principal, args, channel="mcp")
    S->>S: require graph:read
    S->>Q: run in a new session, scoped to tenant + customers
    Q->>DB: bounded traversal / search queries
    Q-->>S: result with provenance
    S->>DB: audit_log row (credentials redacted)
    S-->>M: result
    M-->>C: structured tool result (or isError with "code: message")
```

## 5. Client configuration

Many MCP clients read an `mcpServers` map (for example a project `.mcp.json`). Copy
[.mcp.json.example](.mcp.json.example) to `.mcp.json` (git-ignored) and insert a key:

```json
{
  "mcpServers": {
    "enterprise-ontology": {
      "type": "http",
      "url": "http://127.0.0.1:8001/mcp",
      "headers": { "Authorization": "Bearer REPLACE_WITH_YOUR_API_KEY" }
    }
  }
}
```

stdio, where the client starts the server itself:

```json
{
  "mcpServers": {
    "enterprise-ontology": {
      "command": "C:/path/to/project/.venv/Scripts/ontology-platform.exe",
      "args": ["mcp", "--transport", "stdio"],
      "env": {
        "OP_DATABASE_URL": "sqlite:///C:/path/to/project/ontology.db",
        "OP_MCP_API_KEY": "op_..."
      }
    }
  }
}
```

Give the assistant a key with only the `viewer` role, and restrict it to customers where needed:

```powershell
ontology-platform create-api-key --tenant northwind --name ai-assistant --roles viewer --customers acme
```

Step-by-step setup, including checks and troubleshooting, is in
[docs/LOCAL_WALKTHROUGH.md](docs/LOCAL_WALKTHROUGH.md) (step B6).

## 6. Concurrency

* The nine synchronous tools are run by the SDK in worker threads (`anyio.to_thread.run_sync`), so a
  slow query does not block the server's event loop and concurrent calls proceed in parallel.
* Each call opens its own database session. With PostgreSQL, concurrent database work is bounded by
  the connection pool (`OP_DB_POOL_SIZE` + `OP_DB_MAX_OVERFLOW`, waiting up to
  `OP_DB_POOL_TIMEOUT_SECONDS`).
* `run_enterprise_intelligence_workflow` is an async tool. The orchestrator runs each agent step in a
  worker thread with its own session; independent steps run concurrently up to
  `OP_AGENT_MAX_PARALLEL`, each under `OP_AGENT_STEP_TIMEOUT_SECONDS`.
* The BM25 search indexes and the entity-linking alias index are cached in process memory per tenant
  graph revision and customer scope; the `mcp` and `api` containers each keep their own cache.

Concurrent tool calls are exercised in `tests/test_concurrency.py` (24 parallel calls with a
customer-restricted principal) and HTTP authentication in `tests/test_security.py`.

## 7. Limitations

* The stdio principal is fixed at startup: revoking `OP_MCP_API_KEY` takes effect only after the
  stdio server process restarts.
* There is no rate limiting or per-client quota on `/mcp`.
* The HTTP transport has no TLS of its own; Compose binds it to `127.0.0.1`. Put a TLS-terminating
  proxy in front of it before exposing it beyond the host.

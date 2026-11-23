# Enterprise Ontology Platform

Connects to enterprise systems (GitHub, GitLab, Bitbucket, Confluence, Kubernetes, AWS, Azure,
GCP and catalog exports). It builds a **tenant-isolated knowledge graph** of customers, products,
applications, services, APIs, repositories, databases, clusters, deployments, environments and
documentation, with provenance and version history. AI agents query it through **MCP**, and other
clients through REST.

* Design: [ARCHITECTURE.md](ARCHITECTURE.md)
* Schema: [ONTOLOGY_SCHEMA.md](ONTOLOGY_SCHEMA.md)
* Progress and honest status: [IMPLEMENTATION_PLAN.md](IMPLEMENTATION_PLAN.md)
* **Run it locally (step by step, incl. connecting Claude):** [docs/LOCAL_WALKTHROUGH.md](docs/LOCAL_WALKTHROUGH.md)
* Containers: [docs/PODMAN.md](docs/PODMAN.md)

## Quick start (local Python, SQLite)

```powershell
python -m venv .venv
```

```powershell
.venv\Scripts\pip install -e ".[dev]"
```

```powershell
.venv\Scripts\ontology-platform seed-demo
```

`seed-demo` applies migrations, loads the sample enterprise (tenants `northwind` and `contoso`) and
prints API keys. Then:

```powershell
.venv\Scripts\ontology-platform ask --tenant northwind "Which Kubernetes clusters host Customer B's applications?"
```

```powershell
.venv\Scripts\ontology-platform serve
```

REST docs are at http://127.0.0.1:8000/docs. Send `Authorization: Bearer <key>` with each request.

## Quick start (Podman)

```powershell
Copy-Item .env.example .env
```

```powershell
podman compose up -d
```

```powershell
podman compose exec api ontology-platform seed-demo --data-dir /app/sample_data
```

See [docs/PODMAN.md](docs/PODMAN.md) for prerequisites, logs, rebuilds and troubleshooting.

## Connecting an AI agent (MCP)

**stdio** (e.g. Claude Code / Claude Desktop). Add to the client's MCP config:

```json
{
  "mcpServers": {
    "enterprise-ontology": {
      "command": "C:/path/to/.venv/Scripts/ontology-platform.exe",
      "args": ["mcp", "--transport", "stdio"],
      "env": { "OP_DATABASE_URL": "sqlite:///C:/path/to/ontology.db", "OP_MCP_API_KEY": "op_..." }
    }
  }
}
```

**HTTP**: `http://127.0.0.1:8001/mcp` with header `Authorization: Bearer <api key or JWT>`.

Tools:
* `query_knowledge_graph`
* `search_entities`
* `get_entity_details`
* `get_related_entities`
* `find_entity_relationships`
* `search_documentation`
* `trace_application_dependencies`
* `get_deployment_information`
* `get_ontology_schema`

## Adding a real source

```powershell
$env:GITHUB_TOKEN = "<token>"
```

```powershell
.venv\Scripts\ontology-platform add-connector --tenant northwind --name github-main --type github --config '{\"org\": \"my-org\"}' --secret-ref env:GITHUB_TOKEN
```

```powershell
.venv\Scripts\ontology-platform sync --tenant northwind --connector github-main
```

Secrets are referenced (`env:NAME` or `file:/path`), never stored. Each connector's config fields
are defined by its `*Config` model in `src/ontology_platform/connectors/`. The real connectors have
been tested only against mock API responses. See the status in IMPLEMENTATION_PLAN.md.

## Tests

```powershell
.venv\Scripts\python -m pytest -q
```

## Layout

```
src/ontology_platform/
  ontology/      types, rules, keys, candidate models
  storage/       ORM, sessions, tenant-scoped GraphRepository
  connectors/    framework + github/gitlab/bitbucket/confluence/kubernetes/aws/azure/gcp/catalog/fixture/push
  extraction/    structured extractors, text mentions, tagging conventions
  resolution/    entity resolver
  ingestion/     pipeline
  search/        BM25 + index cache
  query/         graph engine, question answering
  auth/          principals, API keys, JWT, audit
  api/           FastAPI app
  mcp_server/    MCP server
  migrations/    Alembic
sample_data/     generator + generated fixtures (fictional "Northwind" enterprise)
tests/
```

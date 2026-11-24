# Enterprise Ontology Platform

A unified knowledge graph of an enterprise's software landscape, exposed to AI assistants through
the **Model Context Protocol (MCP)**.

Enterprise knowledge is spread across many systems:
* source code in GitHub, GitLab and Bitbucket
* documentation in Confluence
* workloads in Kubernetes
* infrastructure in AWS, Azure and GCP

The platform connects to these systems and extracts the entities that matter: customers,
products, applications, services, APIs, repositories, databases, clusters, deployments,
environments and documentation. It links them into one graph, so questions like these can be
answered precisely, with the source of every fact:

* *Which repositories belong to Customer A's Product X?*
* *Where is Application Y deployed in SIT, UAT and Production?*
* *Which Kubernetes clusters host Customer B's applications?*
* *What is affected if Service X goes down?*

## Features

| Area | What it does |
|---|---|
| **Connectors** | GitHub, GitLab, Bitbucket, Confluence, Kubernetes, AWS, Azure, GCP, catalog files (CMDB/CRM exports), push API |
| **Ingestion** | Incremental and idempotent; extracts entities from Backstage `catalog-info.yaml`, OpenAPI specs, READMEs, Kubernetes labels, cloud tags and document text |
| **Knowledge graph** | 13 entity types, 14 relationship types, entity resolution and de-duplication, provenance and version history for every fact |
| **Query engine** | Graph traversal, dependency and impact analysis, deployment lookup, document search, and natural-language questions answered with evidence paths |
| **Interfaces** | MCP server (stdio and HTTP), REST API with OpenAPI docs, CLI |
| **Security** | API keys and JWT, roles, strict tenant isolation, per-customer access restriction, audit log |
| **Operations** | PostgreSQL with migrations, Podman containers, structured logs, Prometheus metrics |

## How it works

```
 Sources ──▶ Connectors ──▶ Extraction ──▶ Entity resolution ──▶ Knowledge graph (PostgreSQL)
 (code, docs, cloud, k8s)                                                  │
                                                                           ▼
                                AI assistants ◀── MCP server ◀── Query engine ──▶ REST API
```

Every relationship keeps a record of where it came from (source system, URL, extractor,
confidence), so answers can always be traced back to their source. See
[ARCHITECTURE.md](ARCHITECTURE.md) for the full design.

## Quick start

The fastest way to try it uses Python and a local SQLite database. It needs Python 3.11.

```powershell
python -m venv .venv
```

```powershell
.venv\Scripts\pip install -e ".[dev]"
```

```powershell
.venv\Scripts\ontology-platform seed-demo
```

`seed-demo` creates the database and loads a sample enterprise (a fictional company,
"Northwind", with three customers, two products and six applications). It also prints API keys
for trying the platform.

Ask a question:

```powershell
.venv\Scripts\ontology-platform ask --tenant northwind "Which Kubernetes clusters host Customer B's applications?"
```

Start the REST API, then open http://127.0.0.1:8000/docs:

```powershell
.venv\Scripts\ontology-platform serve
```

For the full setup — PostgreSQL in containers with Podman, and connecting an AI assistant over
MCP — follow **[docs/LOCAL_WALKTHROUGH.md](docs/LOCAL_WALKTHROUGH.md)**.

## Connecting an AI assistant

The MCP server exposes nine tools:

| Tool | Purpose |
|---|---|
| `query_knowledge_graph` | Answer a natural-language question with evidence |
| `search_entities` | Find entities by name, alias or keyword |
| `get_entity_details` | Properties, relationships, provenance and history of an entity |
| `get_related_entities` | Neighbours of an entity, up to N hops |
| `find_entity_relationships` | How two entities are connected |
| `search_documentation` | Search documentation passages |
| `trace_application_dependencies` | Dependencies, or impact analysis when run upstream |
| `get_deployment_information` | Deployments per customer and environment |
| `get_ontology_schema` | The entity and relationship types |

Any MCP-compatible assistant can connect:
* **HTTP:** `http://127.0.0.1:8001/mcp` with the header `Authorization: Bearer <api key>`. A
  template is in [.mcp.json.example](.mcp.json.example).
* **stdio:** see [docs/LOCAL_WALKTHROUGH.md](docs/LOCAL_WALKTHROUGH.md).

## Documentation

| Document | Contents |
|---|---|
| [docs/LOCAL_WALKTHROUGH.md](docs/LOCAL_WALKTHROUGH.md) | Step-by-step local setup, from zero to an AI assistant answering questions |
| [ARCHITECTURE.md](ARCHITECTURE.md) | Components, data flow, technology choices, security model |
| [ONTOLOGY_SCHEMA.md](ONTOLOGY_SCHEMA.md) | Entity and relationship types, identity rules, extraction conventions |
| [IMPLEMENTATION_PLAN.md](IMPLEMENTATION_PLAN.md) | Feature status, test coverage and roadmap |
| [docs/PODMAN.md](docs/PODMAN.md) | Container operations and troubleshooting |
| [poc/puppygraph/FINDINGS.md](poc/puppygraph/FINDINGS.md) | Evaluation of PuppyGraph as an alternative graph query engine |

## Project layout

```
src/ontology_platform/
  ontology/      entity and relationship types, identity rules
  connectors/    source system integrations
  extraction/    entity and relationship extraction
  resolution/    entity resolution and de-duplication
  ingestion/     ingestion pipeline
  storage/       database model and graph repository
  search/        text search index
  query/         graph query engine and question answering
  auth/          authentication, authorization, audit
  api/           REST API
  mcp_server/    MCP server
  migrations/    database migrations
sample_data/     sample enterprise dataset
tests/           automated tests
poc/puppygraph/  PuppyGraph evaluation
docs/            guides
```

## Development

Run the test suite (129 tests):

```powershell
.venv\Scripts\python -m pytest -q
```

Connectors for real systems take their credentials as references (`env:NAME` or `file:/path`); no
secret is ever stored in the database. To add a new source, implement a `Connector` subclass in
`src/ontology_platform/connectors/`. See ARCHITECTURE.md §11.

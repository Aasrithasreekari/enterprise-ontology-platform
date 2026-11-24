# Enterprise Ontology Platform

A unified, tenant-isolated **knowledge graph of an enterprise's software landscape**, built from its
code repositories, documentation, Kubernetes clusters and cloud accounts. AI assistants query it
through the **Model Context Protocol (MCP)**; people use a REST API and a web console.

Answers come from graph traversal with evidence: every fact links back to the source record it
was extracted from. A multi-agent workflow plans multi-part questions, runs specialist agents (in
parallel where possible), and checks every fact against its provenance before answering.

---

## Contents

1. [Why this exists](#1-why-this-exists)
2. [Capabilities](#2-capabilities)
3. [Screenshots](#3-screenshots)
4. [Architecture](#4-architecture)
5. [Agentic workflow](#5-agentic-workflow)
6. [Technology stack](#6-technology-stack)
7. [Getting started](#7-getting-started)
8. [Configuration](#8-configuration)
9. [REST API](#9-rest-api)
10. [MCP integration](#10-mcp-integration)
11. [Example questions](#11-example-questions)
12. [Security](#12-security)
13. [Testing](#13-testing)
14. [Project structure](#14-project-structure)
15. [Known limitations](#15-known-limitations)
16. [Further documentation](#16-further-documentation)

---

## 1. Why this exists

In a large enterprise, knowledge about one product is scattered across many systems:

* source code in thousands of GitHub, GitLab and Bitbucket repositories
* design documents and runbooks in Confluence
* workloads in Kubernetes clusters
* infrastructure in AWS, Azure and GCP

The same application is often deployed many times, for different customers and in SIT, UAT and
production.

An AI assistant asked *"Which clusters host Customer B's applications?"* or *"What breaks if the
payment service goes down?"* cannot answer reliably from documents alone:

| Document-based RAG | This platform |
|---|---|
| Retrieves text that is *similar* to the question | Traverses typed relationships: customer → product → application → deployment → cluster |
| Cannot tell which deployment belongs to which customer | Each (application, customer, environment) is a distinct deployment node |
| Multi-hop questions depend on luck in retrieval | Multi-hop traversal and dependency/impact analysis are explicit graph operations |
| Sources are passages | Every edge carries provenance: source system, URL, extractor, confidence |
| No notion of who may see what | Tenant isolation and per-customer access restrictions on every query |

Documents are still used: they are linked into the graph (`documentation —describes→ service`),
and their passages are searched alongside graph facts.

## 2. Capabilities

| Area | Implemented |
|---|---|
| **Connectors** | GitHub, GitLab, Bitbucket, Confluence, Kubernetes, AWS, Azure, GCP; catalog files (CMDB/CRM exports); push API; fixture adapter for the sample data |
| **Ingestion** | Incremental and idempotent (content hashes, cursors); per-record failure isolation; stale-fact detection; bounded parallel sync across connectors; run summaries |
| **Extraction** | Backstage `catalog-info.yaml`, OpenAPI specs, READMEs, Confluence pages and labels, Kubernetes labels and annotations, cloud tags, entity mentions in text |
| **Knowledge graph** | 13 entity types and 14 relationship types with domain/range rules; entity resolution (key → alias → fuzzy); provenance and version history for every fact |
| **Query engine** | Entity search, neighbourhood and path queries, dependency and impact analysis, deployment lookup, document search, natural-language questions with evidence paths |
| **Agentic workflow** | Orchestrator with intent classification, planning, parallel/sequential execution of specialist agents, evidence validation and response synthesis; persisted run history |
| **Interfaces** | MCP server (10 tools; stdio and streamable HTTP), REST API with OpenAPI docs, web console at `/ui`, CLI |
| **Security** | API keys and JWT, roles, tenant isolation, customer-scope restrictions with evidence redaction, audit log with credential redaction |
| **Operations** | PostgreSQL with Alembic migrations, Podman containers, structured JSON logs with correlation IDs, Prometheus metrics |

## 3. Screenshots

All screenshots show the web console against the bundled sample enterprise (PostgreSQL in Podman).
Every value comes from the knowledge graph; nothing is hardcoded.

### Agent workflow: impact analysis, then deployment lookup

![Agent workflow answer with validation and execution timeline](docs/images/console-ask-workflow.png)

*"Which applications depend on Payment Service, and where are they deployed?"* has two intents. The
**dependency & impact agent** finds that `payments-platform` contains `payment-gateway`, which
depends on `payment-processor` (alias "Payment Service"). The **deployment agent** then uses that
result to list the application's five deployments. The page shows:
* the answer
* confirmed and inferred facts, each with its graph path and provenance
* the validation verdict
* missing information and limitations
* sources
* the execution timeline: the two steps run in sequence because the second needs the first's output

### Agent workflow: independent steps run in parallel

![Workflow with two agents running concurrently](docs/images/console-ask-parallel.png)

*"Where is claims-portal deployed and what documentation describes it?"* plans a deployment step and
a documentation step with no dependency between them, so they run **concurrently** (same stage in
the timeline). A fact supported only by a text mention in a document is labelled **inferred**.

### Knowledge graph explorer

![Entity search, relationship graph and entity details](docs/images/console-explorer.png)

Search by name or alias and explore an entity's neighbourhood (colour = entity type; click a node
to expand it). The details panel lists relationships with confidence and the entity's provenance.
"Find connection paths" highlights the shortest paths between two entities.

### Impact analysis and deployments

![Upstream dependency trace and deployment lookup](docs/images/console-impact-deployments.png)

Upstream tracing shows what is affected if a component fails, downstream tracing what it depends
on. Deployment lookup lists each customer and environment with its cluster, namespace, workloads
and cloud resources.

### Workflow history

![Stored workflow runs](docs/images/console-history.png)

Every workflow run is stored with its plan, events, facts, validation and answer. Opening a run
shows it again, so answers remain auditable after the fact.

## 4. Architecture

### 4.1 High-level view

```mermaid
flowchart LR
    subgraph Sources
        SCM[GitHub / GitLab / Bitbucket]
        DOC[Confluence]
        K8S[Kubernetes]
        CLOUD[AWS / Azure / GCP]
        CAT[Catalog files / push API]
    end
    subgraph Ingestion
        CON[Connectors<br/>normalized records]
        EXT[Extraction<br/>entities + relationships]
        RES[Entity resolution<br/>ontology validation]
    end
    PG[(PostgreSQL<br/>knowledge graph<br/>provenance · history<br/>workflow runs)]
    subgraph Query
        QE[Query engine<br/>traversal · paths · impact<br/>BM25 search]
        AG[Agent orchestrator<br/>specialist agents<br/>evidence validation]
    end
    subgraph Interfaces
        MCP[MCP server<br/>stdio · HTTP]
        API[REST API<br/>OpenAPI]
        UI[Web console /ui]
    end
    SCM & DOC & K8S & CLOUD & CAT --> CON --> EXT --> RES --> PG
    PG --> QE --> AG
    QE --> MCP & API
    AG --> MCP & API
    API --> UI
    MCP --> ASSIST[AI assistants]
```

All interfaces share one service layer. It authenticates the caller, checks permissions, binds
every query to the caller's tenant and customer scope, records metrics and writes the audit log.

### 4.2 Ingestion and connectors

```mermaid
flowchart TD
    A[Connector.fetch since cursor] -->|SourceRecord kind=repository/document/k8s_workload/cloud_resource/catalog| B{Content hash changed?}
    B -- no --> C[Mark provenance as seen<br/>skip extraction]
    B -- yes --> D[Extractor for the record kind]
    D --> E[Resolve entities<br/>canonical key → alias → fuzzy]
    E --> F[Validate relationships<br/>against ontology rules]
    F --> G[Upsert entities and relationships<br/>new version only when something changed]
    G --> H[Record provenance per fact]
    H --> I[Text pass: link entity mentions<br/>in changed documents]
    I --> J[Full sync: facts no source still reports<br/>are marked stale, never deleted]
```

* **Connectors** turn each system into normalized records. The record *kind*, not the source system,
  picks the extractor, so GitHub, GitLab and Bitbucket share one repository extractor. Secrets are
  referenced (`env:NAME`, `file:/path`) and never stored. HTTP calls retry with backoff on 429/5xx.
* **Idempotency:** unchanged records are skipped, and only one ingestion per connector can run at a
  time; a partial unique index in the database enforces this.
* **Entity resolution** uses type-specific identities. A *deployment* is identified by
  (application, customer, tier), so the same application deployed for two customers gives two
  distinct nodes.
* **Ontology:** 13 entity types and 14 relationship types with allowed source/target pairs.
  Relationships that break the rules are rejected and counted. See
  [ONTOLOGY_SCHEMA.md](ONTOLOGY_SCHEMA.md).

### 4.3 Storage

PostgreSQL holds the graph as relational tables:
* `entities`, `entity_aliases` and `relationships`
* `provenance`: one row per fact and source record
* `graph_changes`: append-only version history
* `documents` and `document_chunks`
* `ingestion_runs`, `workflow_runs`, `api_keys` and `audit_log`

The required queries are bounded-depth traversals, which SQL handles well, and keeping one
transactional store makes provenance and versioning simple. All graph access goes through a
tenant-scoped repository. SQLite works for development and tests. A graph query engine
(PuppyGraph) over the same tables was evaluated: see
[poc/puppygraph/FINDINGS.md](poc/puppygraph/FINDINGS.md).

### 4.4 Query engine and hybrid retrieval

| Retrieval | How |
|---|---|
| Exact lookup | Canonical key (`application:payments-platform`) or entity id |
| Alias matching | Every name a source used for an entity (e.g. "Payment Service" → `payment-processor`) |
| Keyword search | BM25 over entity text and document chunks, with stemming and common abbreviations (`auth`, `k8s`, `db`, `prod`) |
| Graph traversal | Neighbours, multi-step patterns, shortest paths, transitive dependency tracing |
| Evidence | Provenance for every edge on every returned path |

Graph facts and document passages are combined in the same answer.

### 4.5 Security and observability

Covered in [§12](#12-security). Observability:
* JSON logs with a correlation ID per request or workflow run.
* Prometheus metrics at `/metrics`:
  * HTTP requests
  * query, workflow and agent-step latency
  * agent outcomes
  * database statement latency
  * ingestion records and runs
  * graph writes

## 5. Agentic workflow

There is **no language model inside the platform**. Classification and planning are deterministic
rules over the linked entities; agents call existing query-engine operations. The calling AI
assistant does the language reasoning, and the platform guarantees grounded, reproducible facts.

| Component | Responsibility |
|---|---|
| **Orchestrator** | Classify → plan → execute → validate → synthesize; bounded steps, timeouts, retries, run persistence |
| **Intent classifier** | Links entities in the question (names and aliases) and detects one or more intents: impact, dependents, dependencies, deployments, repositories, APIs, clusters, documentation, relationship, entity lookup |
| **Planner** | Creates steps with explicit dependencies; independent steps can run in parallel |
| **Graph Intelligence agent** | Repositories, APIs, clusters, relationship paths, entity details |
| **Documentation Intelligence agent** | Documents linked to entities plus BM25 passages |
| **Dependency & Impact agent** | Upstream and downstream tracing, affected applications and customers, circular-dependency detection |
| **Deployment Intelligence agent** | Deployments per customer and environment with clusters and namespaces; reports missing environments |
| **Evidence Validation agent** | Grades each fact: **confirmed** (structured sources, confidence ≥ 0.8), **inferred** (text mentions, references, placeholder entities) or **unsupported** (no provenance; excluded) |
| **Response Synthesis agent** | Composes the answer from validated facts only, with missing information, limitations and sources |

End-to-end example (*"Which applications depend on Payment Service, and where are they deployed?"*):

```mermaid
sequenceDiagram
    autonumber
    participant C as Client (UI / REST / MCP)
    participant S as Service layer
    participant O as Orchestrator
    participant I as Dependency & Impact agent
    participant D as Deployment agent
    participant V as Evidence agent
    participant Y as Synthesis agent
    participant Q as Query engine (tenant-scoped)
    C->>S: question + API key
    S->>S: authenticate, check graph:read
    S->>O: run(principal, question)
    O->>Q: link entities → payment-processor
    O->>O: intents = dependents, deployments<br/>plan s1 → s2 (s2 depends on s1)
    O->>I: s1 impact(payment-processor)
    I->>Q: trace_dependencies(upstream)
    I-->>O: facts + affected applications
    O->>D: s2 deployments(applications from s1)
    D->>Q: get_deployment_information
    D-->>O: deployment facts + missing environments
    O->>V: validate all facts against provenance
    V-->>O: confirmed / inferred / unsupported
    O->>Y: synthesize from validated facts
    Y-->>O: answer, sources, limitations
    O->>S: run (plan, events, facts, answer) persisted
    S-->>C: JSON result
```

**Execution and failure handling**
* Ready steps run concurrently under a semaphore (`OP_AGENT_MAX_PARALLEL`). Each runs in a worker
  thread with its own database session and a timeout (`OP_AGENT_STEP_TIMEOUT_SECONDS`).
* Only transient errors (database operational errors, timeouts) are retried, at most
  `OP_AGENT_MAX_RETRIES` times. A step whose dependency failed is **skipped**, never run on missing
  input.
* Validation and synthesis always run, so failures are reported in the answer, not hidden. Plans
  are bounded (`OP_AGENT_MAX_STEPS`) and acyclic, so there are no agent loops.
* **Observability:** each run records:
  * a timeline of events: classification, plan, parallel batches, step start/retry/completion/failure
  * per-step status, attempts, duration and tools
  * Prometheus metrics per agent

Details: [AGENTIC_WORKFLOW.md](AGENTIC_WORKFLOW.md).

## 6. Technology stack

Versions below are those installed and tested.

| Concern | Technology |
|---|---|
| Language | Python 3.11 |
| Web framework | FastAPI 0.165, Uvicorn 0.54, Pydantic 2.14, pydantic-settings 2.16 |
| Database | PostgreSQL 16 (production / containers), SQLite (development, tests) |
| Data access and migrations | SQLAlchemy 2.1, Alembic 1.20, psycopg 3.3 |
| Graph storage and querying | Relational property graph in PostgreSQL, traversed by the platform's query engine |
| Agent orchestration | Platform's own deterministic orchestrator (asyncio, bounded thread pool); no agent framework, no embedded LLM |
| MCP | Official `mcp` Python SDK 2.3 (`MCPServer`); stdio and streamable HTTP transports |
| Authentication | Hashed API keys; JWT via PyJWT 2.15 (HS256 / RS256) |
| Search | BM25 (implemented in the platform) over entities and document chunks |
| Connectors | httpx 0.28 (REST APIs), PyYAML 6.0, optional boto3 for AWS |
| Frontend | Static HTML, CSS and vanilla JavaScript served by FastAPI at `/ui` (no build step) |
| Containers | Podman 6 and podman-compose 1.6 (`Containerfile`, `compose.yaml`) |
| Observability | JSON logging, prometheus-client 0.26 |
| Testing | pytest 9.2, pytest-asyncio 1.5, FastAPI TestClient, in-process MCP client |

## 7. Getting started

### 7.1 Prerequisites

* Python 3.11
* For the full setup: Podman (with a Podman machine on Windows/macOS) and `podman-compose`

### 7.2 Quick start (Python + SQLite)

```powershell
git clone https://github.com/Aasrithasreekari/enterprise-ontology-platform.git
```

```powershell
cd enterprise-ontology-platform
```

```powershell
python -m venv .venv
```

```powershell
.venv\Scripts\pip install -e ".[dev,postgres]"
```

Create the database, load the sample enterprise and print demo API keys (shown once; save them):

```powershell
.venv\Scripts\ontology-platform seed-demo
```

The sample enterprise is a fictional software company, "Northwind": three customers (Acme Bank,
Globex Insurance, Initech Retail), three products (PayHub, ClaimsPro, Core Platform), five
applications, eight repositories on three
SCM platforms, six Confluence pages, four Kubernetes clusters and AWS/Azure/GCP resources. A second
tenant, "contoso", exists to demonstrate tenant isolation.

Ask a question from the command line:

```powershell
.venv\Scripts\ontology-platform ask --tenant northwind "Which Kubernetes clusters host Customer B's applications?"
```

Start the API:

```powershell
.venv\Scripts\ontology-platform serve
```

Then open the web console at http://127.0.0.1:8000/ui (paste a printed API key) or the API docs at
http://127.0.0.1:8000/docs. On Linux/macOS use `.venv/bin/` instead of `.venv\Scripts\`.

### 7.3 Full setup (PostgreSQL in Podman)

```powershell
Copy-Item .env.example .env
```

Set `POSTGRES_PASSWORD` in `.env`. Pin the Compose provider so Podman does not pick up another
compose tool (see [docs/PODMAN.md](docs/PODMAN.md) §1), then:

```powershell
podman compose build
```

```powershell
podman compose up -d db api mcp
```

```powershell
podman compose exec api ontology-platform seed-demo --data-dir /app/sample_data
```

| Service | Address | Notes |
|---|---|---|
| `db` | internal | PostgreSQL 16, named volume `pgdata` |
| `api` | http://127.0.0.1:8000 | Applies migrations on start (`init-db --wait`), then serves REST, `/ui`, `/docs`, `/metrics` |
| `mcp` | http://127.0.0.1:8001/mcp | MCP over streamable HTTP, Bearer authentication |

Health: `GET /health` (liveness), `GET /ready` (database reachable).

Migrations can also be applied manually with `ontology-platform init-db`. Step-by-step instructions,
including troubleshooting, are in [docs/LOCAL_WALKTHROUGH.md](docs/LOCAL_WALKTHROUGH.md).

### 7.4 MCP server

* **HTTP:** started by the `mcp` service above, or `ontology-platform mcp --transport http --port 8001`.
* **stdio:** `ontology-platform mcp --transport stdio` with `OP_MCP_API_KEY` set. The assistant
  usually launches this itself; see [§10](#10-mcp-integration).

### 7.5 CLI reference

| Command | Purpose |
|---|---|
| `init-db [--wait N]` | Apply database migrations |
| `seed-demo [--data-dir DIR]` | Load the sample tenants and print demo API keys |
| `create-tenant ID NAME` | Create a tenant |
| `create-api-key --tenant T --name N --roles viewer[,ingestor,admin] [--customers acme]` | Create an API key |
| `add-connector --tenant T --name N --type TYPE --config JSON [--secret-ref env:VAR]` | Register a connector |
| `list-connectors --tenant T` | List connectors |
| `sync --tenant T (--connector NAME \| --all) [--mode full\|incremental] [--parallel N]` | Run ingestion |
| `ask --tenant T "question"` | Answer a single question from the command line |
| `serve [--host H --port P]` | Run the REST API and web console |
| `mcp [--transport stdio\|http]` | Run the MCP server |

## 8. Configuration

Settings come from `OP_*` environment variables or a `.env` file. [`.env.example`](.env.example)
lists them with safe placeholder values; never commit a real `.env`.

| Variable | Default | Purpose |
|---|---|---|
| `OP_DATABASE_URL` | `sqlite:///./ontology.db` | Database (e.g. `postgresql+psycopg://user:pass@host/db`) |
| `OP_JWT_KEY`, `OP_JWT_ALGORITHM`, `OP_JWT_AUDIENCE`, `OP_JWT_ISSUER` | unset, `HS256` | Verify JWTs from an identity provider (HS256 keys must be ≥ 32 characters) |
| `OP_MCP_API_KEY` | unset | Identity of the MCP stdio server |
| `OP_MCP_ALLOWED_HOSTS` | unset | Host-header allow-list for MCP over HTTP |
| `OP_DB_POOL_SIZE`, `OP_DB_MAX_OVERFLOW`, `OP_DB_POOL_TIMEOUT_SECONDS` | 10, 10, 30 | PostgreSQL connection pool |
| `OP_AGENT_MAX_PARALLEL`, `OP_AGENT_STEP_TIMEOUT_SECONDS`, `OP_AGENT_MAX_RETRIES`, `OP_AGENT_MAX_STEPS` | 4, 20, 1, 12 | Agent workflow limits |
| `OP_LOG_LEVEL`, `OP_LOG_JSON` | `INFO`, `true` | Logging |

The full list is in [DEPLOYMENT.md](DEPLOYMENT.md).

**External systems** are registered as connectors with a non-secret config and a secret reference.
For example, a GitHub organization with its token in `GITHUB_TOKEN`:

```powershell
.venv\Scripts\ontology-platform add-connector --tenant northwind --name github-main --type github --config '{\"org\": \"my-org\"}' --secret-ref env:GITHUB_TOKEN
```

```powershell
.venv\Scripts\ontology-platform sync --tenant northwind --connector github-main
```

## 9. REST API

Base path `/api/v1`. Send `Authorization: Bearer <api key or JWT>` (or `X-API-Key`). Interactive
OpenAPI documentation is at `/docs`. Errors use `{"error": "<code>", "message": "..."}`, and every
response carries `X-Request-ID`.

| Method | Path | Purpose | Permission |
|---|---|---|---|
| POST | `/agent/query` | Run the agentic workflow | `graph:read` |
| GET | `/agent/runs`, `/agent/runs/{id}`, `/agent/runs/{id}/events` | Workflow history, one run, its events | `graph:read` |
| POST | `/query` | Answer a single question (one intent) | `graph:read` |
| GET | `/entities?q=&type=` | Search entities | `graph:read` |
| GET | `/entities/{ref}` | Entity details, relationships, provenance (`?history=true` for versions) | `graph:read` |
| GET | `/entities/{ref}/related` | Neighbours up to N hops, filtered by relation/type | `graph:read` |
| GET | `/paths?source=&target=` | Shortest paths with evidence | `graph:read` |
| GET | `/dependencies/{ref}?direction=upstream\|downstream` | Dependency tracing / impact analysis | `graph:read` |
| GET | `/deployments?application=&customer=&environment=` | Deployments | `graph:read` |
| GET | `/documents/search?q=&entity=` | Documentation passages | `graph:read` |
| GET | `/ontology/schema`, `/graph/stats` | Ontology and graph counts | `graph:read` |
| GET, POST | `/connectors`, `/connector-types` | List / register connectors | `connectors:read` / `connectors:write` |
| POST | `/connectors/{id}/sync`, `/connectors/{id}/records` | Start a sync / push records | `ingest:run` |
| GET | `/ingestion/runs`, `/ingestion/runs/{id}` | Ingestion runs | `connectors:read` |
| GET, POST, DELETE | `/admin/api-keys` | Manage API keys | `keys:manage` |
| GET | `/admin/audit` | Audit log | `audit:read` |

Plus `GET /health`, `GET /ready`, `GET /metrics` (no authentication).

Example: run the agent workflow.

```powershell
Invoke-RestMethod -Method Post -Uri http://127.0.0.1:8000/api/v1/agent/query -Headers @{Authorization="Bearer $env:OP_KEY"} -ContentType "application/json" -Body '{"question":"Which applications depend on Payment Service, and where are they deployed?"}'
```

Response (abridged):

```json
{
  "id": "f5dce00828b04dc88f6d576780eddc98",
  "status": "completed",
  "intents": ["dependents", "deployments"],
  "linked_entities": [{"type": "service", "name": "payment-processor", "key": "service:payment-processor"}],
  "plan": [
    {"id": "s1", "agent": "dependency_impact", "action": "impact", "depends_on": [], "status": "completed", "duration_ms": 120.0},
    {"id": "s2", "agent": "deployment_intelligence", "action": "deployments", "depends_on": ["s1"], "status": "completed", "duration_ms": 55.0},
    {"id": "validate", "agent": "evidence_validation", "status": "completed"},
    {"id": "synthesize", "agent": "response_synthesis", "status": "completed"}
  ],
  "validation": {"status": "validated", "confirmed": 7, "inferred": 0, "unsupported": 0, "issues": []},
  "answer": {
    "summary": "If payment-processor becomes unavailable, 1 application(s) are potentially affected: payments-platform. ...",
    "confirmed_facts": ["payment-gateway depends on payment-processor (1 hop(s)): payment-gateway -[depends_on]-> payment-processor", "..."],
    "missing_information": [],
    "limitations": ["Impact is derived from declared dependencies ...; it shows components that could be affected, not observed failures."],
    "sources": [{"source_system": "github (fixture)", "source_url": "https://github.com/northwind/payments-gateway", "extractor": "structured:repository"}]
  },
  "events": ["..."],
  "duration_ms": 166.1
}
```

Full reference with parameters and status codes: [API_DOCUMENTATION.md](API_DOCUMENTATION.md).

## 10. MCP integration

[MCP](https://modelcontextprotocol.io) is an open protocol through which AI assistants discover and
call tools. Exposing the graph as MCP tools lets any MCP-compatible assistant answer enterprise
questions from verified facts rather than guessing, under the same authentication and isolation
rules as the REST API.

| Tool | Use it for |
|---|---|
| `run_enterprise_intelligence_workflow` | Broad or multi-part questions: the full agentic workflow with validated facts, sources and trace |
| `query_knowledge_graph` | A single, well-scoped question |
| `search_entities` | Finding an entity by name, alias or keyword |
| `get_entity_details` | One entity: properties, relationships, provenance, history |
| `get_related_entities` | An entity's neighbourhood |
| `find_entity_relationships` | How two entities are connected |
| `trace_application_dependencies` | Dependencies (downstream) or impact analysis (upstream) |
| `get_deployment_information` | Where an application runs, per customer and environment |
| `search_documentation` | Runbooks, design pages, READMEs |
| `get_ontology_schema` | Valid entity and relationship types |

All tools are read-only. Inputs are validated against JSON schemas and failures come back as MCP
tool errors.

**HTTP transport.** Every request must carry `Authorization: Bearer <api key or JWT>`; there is no
default identity. Client configuration ([.mcp.json.example](.mcp.json.example)):

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

**stdio transport.** The assistant launches the server and it acts as the owner of `OP_MCP_API_KEY`:

```json
{
  "mcpServers": {
    "enterprise-ontology": {
      "command": "C:/path/to/enterprise-ontology-platform/.venv/Scripts/ontology-platform.exe",
      "args": ["mcp", "--transport", "stdio"],
      "env": { "OP_DATABASE_URL": "sqlite:///C:/path/to/enterprise-ontology-platform/ontology.db", "OP_MCP_API_KEY": "op_..." }
    }
  }
}
```

Create a read-only key for an assistant with
`ontology-platform create-api-key --tenant northwind --name assistant --roles viewer`. Details:
[MCP_INTEGRATION.md](MCP_INTEGRATION.md).

## 11. Example questions

These questions work against the sample data. The answers below are the actual summaries returned
by the agentic workflow.

| # | Question | Answer (summary) |
|---|---|---|
| 1 | Which repositories belong to Customer A's Product X? | Repositories: northwind/fraud-detector, northwind/ledger/ledger-service, northwind/payment-processor, northwind/payments-gateway. |
| 2 | Where is Application Y deployed? | payments-platform is deployed in 5 environment(s): acme PROD, acme SIT, acme UAT, globex PROD, globex UAT. |
| 3 | Which Kubernetes clusters host Customer B's applications? | Kubernetes clusters: nw-nonprod-eks-use1, nw-prod-aks-weu, nw-prod-eks-use1. |
| 4 | What happens if Payment Service goes down? | If payment-processor becomes unavailable, 1 application(s) are potentially affected: payments-platform. Customers with deployments that would be affected: acme, globex. |
| 5 | Which applications depend on Payment Service? | Same impact analysis; the facts show `payment-gateway -[depends_on]-> payment-processor` and that payments-platform contains payment-gateway. |
| 6 | Which documentation describes the authentication mechanism? | Passages from Auth Service Integration Guide, payments-gateway README, identity-platform README and PayHub Architecture Overview. |
| 7 | Which applications depend on Payment Service, and where are they deployed? | The impact analysis above, followed by the five deployments of payments-platform with clusters and namespaces. |

"Customer A", "Customer B", "Product X", "Application Y" and "Payment Service" are aliases in the
sample CRM catalog for Acme Bank, Globex Insurance, PayHub, payments-platform and
payment-processor. Answers come from graph traversal, not from fixed responses. More questions:
"What is the relationship between Customer A, Product X, Application Y and its repository?",
"Where is claims-portal deployed in SIT and UAT?" (reports that no SIT deployment exists), "What
does payments-platform depend on?".

## 12. Security

| Control | Implementation |
|---|---|
| Authentication | API keys (`op_<id>_<secret>`, stored as SHA-256 hashes, revocable); JWT from an identity provider (signature, expiry, audience, issuer checked; HS256 keys ≥ 32 characters) |
| Authorization | Roles → permissions: `viewer` (graph:read), `ingestor` (+ connectors:read, ingest:run), `admin` (+ connectors:write, keys:manage, audit:read); checked in the service layer for REST, MCP and before every agent step |
| Tenant isolation | The tenant comes only from the credential; every query filters by tenant; another tenant's ids look like "not found" |
| Customer restrictions | A key or token can be limited to customers. Other customers' entities, paths and stats are invisible, and evidence on shared facts that came from another customer's records is redacted. Restricted principals cannot create broader keys and see only their own workflow runs |
| Audit | Every read, write and denial is logged with principal, channel (REST/MCP/CLI), action and outcome; credentials in parameters are redacted |
| Secrets | Connector credentials are referenced (`env:`/`file:`), never stored; `.env` and MCP client configs are git-ignored |
| Provenance | Every entity and relationship records source system, source record, URL, extractor, confidence and observation time |
| MCP over HTTP | Bearer authentication on every call; Host-header allow-list against DNS rebinding |

Details and limitations: [SECURITY.md](SECURITY.md).

## 13. Testing

```powershell
.venv\Scripts\python -m pytest -q
```

Latest run: **243 passed, 5 skipped**. The 5 skipped tests are the PostgreSQL integration suite,
which runs when `OP_TEST_DATABASE_URL` is set and **passes 5/5 against PostgreSQL 16** in the Podman
stack.

| Area | Test files |
|---|---|
| Ontology, resolution, extraction | `test_ontology.py`, `test_resolution.py`, `test_extraction.py` |
| Ingestion and graph integrity | `test_ingestion.py`, `test_ingestion_parallel.py`, `test_graph_integrity.py` |
| Connectors (mock APIs) | `test_connectors.py` |
| Query engine | `test_query.py` |
| Agent workflow and end-to-end scenarios | `test_agents.py` (intents, planning, evidence rules, timeouts, retries, parallel execution, the example questions) |
| REST, MCP and UI | `test_api.py`, `test_api_workflow.py`, `test_mcp.py`, `test_mcp_workflow.py`, `test_ui.py` |
| Security | `test_auth_isolation.py`, `test_security.py` (invalid credentials, roles, cross-tenant access over REST and MCP, customer restrictions, secret redaction) |
| Concurrency | `test_concurrency.py` (parallel REST and MCP requests, concurrent workflows, ingestion locking) |
| PostgreSQL and migrations | `test_postgres.py`, `test_migrations.py` |

Tests use the deterministic sample dataset and mocked external APIs; no credentials are required.
See [TESTING.md](TESTING.md).

## 14. Project structure

```
src/ontology_platform/
  ontology/      entity and relationship types, identity rules, canonical keys
  connectors/    connector framework and the source system connectors
  extraction/    record extractors, text mention extraction, tagging conventions
  resolution/    entity resolution and de-duplication
  ingestion/     ingestion pipeline and parallel sync runner
  storage/       database model, sessions, tenant-scoped graph repository
  search/        BM25 index and cache
  query/         graph query engine and single-question answering
  agents/        orchestrator, intents, planner, specialist agents, evidence, synthesis, run store
  auth/          principals, API keys, JWT, audit
  api/           REST API and the web console (api/static/)
  mcp_server/    MCP server and tools
  migrations/    Alembic migrations (0001 schema, 0002 workflow runs, 0003 ingestion locking)
  service.py     service layer shared by REST, MCP and CLI
  cli.py         command-line interface
sample_data/     sample enterprise (generator + generated records)
tests/           automated tests
poc/puppygraph/  PuppyGraph evaluation and benchmark tooling
docs/            local walkthrough, Podman guide, screenshots
```

## 15. Known limitations

* **Connectors:**
  * The GitHub, GitLab, Bitbucket, Confluence, Kubernetes, AWS, Azure and GCP connectors are tested
    against mock API responses only, not against live accounts.
  * The sample data comes from the fixture adapter.
* **Language understanding:** intent detection is keyword-based. Unusual phrasings may fall back to
  entity lookup or documentation search.
* **Search:** document retrieval is lexical (BM25 with stemming); there is no vector/semantic search.
* **Impact analysis** follows declared dependencies (catalog `dependsOn`/`consumesApis`,
  deployment annotations): it shows what *could* be affected, not observed failures.
* **Conflicting property values** from different sources are not detected; the latest value wins and
  older ones remain in the version history.
* **Not built:** rate limiting and a built-in scheduler. Schedule syncs externally, e.g. cron or a
  Kubernetes CronJob running `ontology-platform sync`.
* **MCP:** the MCP container exposes no `/metrics`. Revoking the stdio server's key takes effect when
  it restarts.
* **Web console:** the relationship graph becomes crowded when highly connected nodes are expanded.

## 16. Further documentation

| Document | Contents |
|---|---|
| [docs/LOCAL_WALKTHROUGH.md](docs/LOCAL_WALKTHROUGH.md) | Step-by-step local setup, including connecting an AI assistant |
| [ARCHITECTURE.md](ARCHITECTURE.md) | Components, data flow, database design, technology choices |
| [AGENTIC_WORKFLOW.md](AGENTIC_WORKFLOW.md) | Agent workflow: intents, planning, agents, evidence model, reliability |
| [ONTOLOGY_SCHEMA.md](ONTOLOGY_SCHEMA.md) | Entity and relationship types, identity rules, extraction conventions |
| [API_DOCUMENTATION.md](API_DOCUMENTATION.md) | REST endpoints in detail |
| [MCP_INTEGRATION.md](MCP_INTEGRATION.md) | MCP transports, tools, authentication |
| [SECURITY.md](SECURITY.md) | Security model and limitations |
| [DEPLOYMENT.md](DEPLOYMENT.md) | Deployment, configuration reference, migrations, monitoring |
| [TESTING.md](TESTING.md) | Test suites and scenarios |
| [DESIGN_DECISIONS.md](DESIGN_DECISIONS.md) | Architecture decision records |
| [IMPLEMENTATION_PLAN.md](IMPLEMENTATION_PLAN.md) | Status and roadmap |
| [docs/PODMAN.md](docs/PODMAN.md) | Container operations and troubleshooting |
| [poc/puppygraph/FINDINGS.md](poc/puppygraph/FINDINGS.md) | PuppyGraph evaluation |

# Enterprise Ontology Platform — Architecture

## 1. Purpose

Enterprise knowledge (docs, code, cloud inventory, cluster workloads, customer/product catalogs)
lives in isolated systems. The platform continuously ingests those systems, extracts typed
entities and relationships, resolves duplicates into one **tenant-scoped knowledge graph** with
provenance and version history, and exposes it to AI agents through an **MCP server** (and a REST
API) so that questions such as *"which clusters host Customer B's applications?"* are answered by
graph traversal with evidence — not by semantic similarity alone.

## 2. High-level view

```
 ┌──────────────── Connector Layer ────────────────┐
 │ GitHub  GitLab  Bitbucket  Confluence  K8s       │   each connector: fetch() → normalized
 │ AWS  Azure  GCP  CatalogFile  Fixture(local)     │   SourceRecord(kind=repository|document|
 └───────────────┬─────────────────────────────────┘   k8s_workload|cloud_resource|catalog)
                 ▼
 ┌──────────── Ingestion Pipeline ─────────────────┐
 │ run bookkeeping · content-hash dedupe (idempotent)│
 │ retries · incremental cursors · stale detection   │
 └───────────────┬─────────────────────────────────┘
                 ▼
 ┌──── Extraction (per record kind, source-agnostic) ┐
 │ structured mappers (catalog-info.yaml, OpenAPI,    │
 │ K8s labels, cloud tags) + text mention extraction  │
 └───────────────┬────────────────────────────────────┘
                 ▼  EntityCandidate / RelationshipCandidate
 ┌──── Entity Resolution + Ontology validation ──────┐
 │ canonical identity keys · aliases · fuzzy match    │
 │ relationship rules (domain/range) enforcement      │
 └───────────────┬────────────────────────────────────┘
                 ▼
 ┌──── Knowledge Graph Store (SQL property graph) ───┐
 │ entities · relationships · aliases · provenance    │
 │ graph_changes (versions) · documents · audit_log   │
 └───────────────┬────────────────────────────────────┘
                 ▼
 ┌──── Query & Retrieval Engine ──────────────────────┐
 │ graph traversal · path finding · impact analysis    │
 │ metadata filters · BM25 lexical/semantic search     │
 │ intent-based question answering with evidence       │
 └──────┬──────────────────────────────┬──────────────┘
        ▼                              ▼
   MCP server (stdio / HTTP)      REST API (FastAPI)
        └────────── AuthN/AuthZ · tenant isolation · audit ──┘
```

## 3. Component responsibilities

| Module | Package | Responsibility |
|---|---|---|
| Config | `ontology_platform.config` | Environment-driven settings (`OP_*` env vars), no hardcoded secrets. |
| Logging / observability | `ontology_platform.observability` | JSON structured logs, request/run correlation IDs, Prometheus metrics. |
| Ontology | `ontology_platform.ontology` | Entity/relationship type registry, identity rules, domain/range rules, canonical keys. |
| Storage | `ontology_platform.storage` | SQLAlchemy ORM, sessions, `GraphRepository` (all reads/writes tenant-scoped). |
| Connectors | `ontology_platform.connectors` | Source adapters producing normalized `SourceRecord`s; secret resolution; HTTP retry. |
| Extraction | `ontology_platform.extraction` | Map records → entity/relationship candidates; text mention extraction. |
| Resolution | `ontology_platform.resolution` | Dedupe candidates onto existing entities (key → alias → fuzzy). |
| Ingestion | `ontology_platform.ingestion` | Orchestrates a sync run end-to-end: idempotency, provenance, versioning, staleness. |
| Search | `ontology_platform.search` | BM25 index over entities and document chunks (cached per tenant graph revision). |
| Query | `ontology_platform.query` | Graph operations + natural-language intent engine returning evidence. |
| Auth | `ontology_platform.auth` | API keys (hashed), JWT, roles → permissions, customer-scope ABAC, audit. |
| REST API | `ontology_platform.api` | FastAPI app, OpenAPI docs at `/docs`. |
| MCP server | `ontology_platform.mcp_server` | MCP tools over the query engine (stdio + streamable HTTP). |
| CLI | `ontology_platform.cli` | DB init, tenants, API keys, connector registration, sync, serve. |

## 4. Technology choices

| Choice | Why |
|---|---|
| **Python 3.11** | Ecosystem for MCP SDK, data processing, cloud SDKs. |
| **FastAPI + Pydantic v2** | Typed request/response validation, auto OpenAPI docs, async-capable. |
| **SQLAlchemy 2 + Alembic** | One code path for SQLite (dev/test, zero infra) and PostgreSQL (Podman Compose / prod); real migrations. |
| **Graph in relational tables** (not Neo4j) | Required queries are bounded-depth traversals (≤6 hops) that batched `IN` queries handle well; avoids a second datastore, keeps transactions/provenance/versioning in one ACID store. All graph access goes through `GraphRepository`, so a Neo4j/Apache AGE backend can be added if traversal depth/volume demands it. |
| **BM25 (pure Python)** for retrieval | Deterministic, no external model or GPU; good for identifier-heavy enterprise text. Dense embeddings can be added behind `search.SearchIndex` later — they are **not** implemented now. |
| **Official `mcp` SDK v2 (`MCPServer`)** | Standard protocol, validated JSON schemas from type hints, stdio + streamable HTTP transports. |
| **httpx** | Async HTTP for connectors; `MockTransport` lets connector parsing be tested against API-shaped responses. |
| **PyJWT** | Verify tokens from an external IdP (HS256 shared secret or RS256 public key). |
| **prometheus-client** | Standard metrics exposition at `/metrics`. |
| **Podman + Podman Compose** | Daemonless, rootless-capable containers; OCI `Containerfile` and a Compose-spec `compose.yaml` (db, api, mcp). No Docker dependency. See [docs/PODMAN.md](docs/PODMAN.md). |

Deliberately **not** included: message queue, separate vector DB, Neo4j, Celery. (PuppyGraph is
under evaluation as a query engine over PostgreSQL; see §4a.) Ingestion runs
in-process (API background task or CLI / Kubernetes CronJob). These can be introduced when scale
requires and the module boundaries allow it.

## 4a. Proposed target stack: PostgreSQL + PuppyGraph (under evaluation)

Proposed stack:
1. **PostgreSQL**: canonical store
2. **PuppyGraph**: graph query engine over PostgreSQL
3. **openCypher**: query language
4. **Vector index**: semantic document retrieval, where needed
5. **MCP**: agent interface
6. **Podman**: container runtime

A proof of concept was run before committing to it. Full evidence is in
[poc/puppygraph/FINDINGS.md](poc/puppygraph/FINDINGS.md).

| Layer | Decision | Status |
|---|---|---|
| Canonical storage | **PostgreSQL** holds the normalized `entities`, `relationships`, `provenance` and `graph_changes`. These tables are the single source of truth; nothing is copied into a graph store. | **Adopted** (running under Podman) |
| Graph contract | Read-only views `graph.entity_v` / `graph.relationship_v` flatten identity into scalar columns. These include `tenant_id`, `customer`, `environment_tier`, `canonical_key`, `source_systems`, `source_refs`, `evidence`, `confidence`, `version` and `status`. A least-privilege `puppygraph_ro` role can read only these views. | **Adopted** ([views.sql](poc/puppygraph/views.sql)) |
| Graph query engine | **PuppyGraph** maps the views to graph labels at query time (zero-ETL; not a graph database). It uses the **generic model**: one `Entity` node label with a `type` property, and one edge label per relationship type. That model is forced by PuppyGraph's unique-edge-label rule; per-type labels would need 34 composite edge labels. | **Candidate**: the PoC passed correctness and freshness. Performance, stability, RLS and licensing gates are open. |
| Query language | **openCypher** (PuppyGraph supports openCypher 9). All 7 enterprise questions plus tenant/customer/environment filters pass. Variable-length paths, `UNION`, parameters and typed `shortestPath` work. Untyped `shortestPath` crashed the engine (Java heap OOM) on an under-spec VM. | **Validated for curated queries** |
| Current traversal engine | The SQL traversal engine (`query/engine.py`) stays the production path until the gates pass. It answers Q1–Q7 in 42–70 ms. PuppyGraph would become a second implementation behind the same `QueryEngine` operations, using Cypher templates. | **In production** |
| Semantic retrieval | BM25 today. If it stops being enough, use **pgvector in the same PostgreSQL**; no separate vector database. | Planned, not implemented |
| MCP | A **custom enterprise MCP server is required**. PuppyGraph's official MCP server uses one static credential and returned data from both tenants in a single query. It has no principal scoping, audit or query bounding. Use it only for engineers' schema authoring with an admin credential. | **Decided** |
| Access control | The enterprise layer authenticates the principal and injects mandatory `tenant_id` / customer / environment predicates into every curated Cypher template (proven in the PoC). PuppyGraph RBAC/RLS with service-account impersonation is planned as defense in depth. | Predicates proven; RLS **not yet tested** |
| Containers | PuppyGraph 1.19.0 runs under **rootless Podman** via [compose.puppygraph.yaml](poc/puppygraph/compose.puppygraph.yaml), although the vendor documents Docker only. | **Verified** |

```
Sources ─▶ Connectors ─▶ Ingestion (extract, resolve, provenance, versioning)
                                   │ writes
                                   ▼
                     PostgreSQL (canonical tables) ──▶ graph.* views (read-only contract)
                                   │                          │ live JDBC, zero-ETL
                                   │                          ▼
                 SQL traversal engine (today)         PuppyGraph (openCypher)  [candidate]
                                   └──────────┬───────────────┘
                                              ▼
                       QueryEngine operations (curated, bounded, scoped)
                                              ▼
                 Enterprise MCP server / REST (authN, tenant+customer+env scoping, audit)
```

## 5. Data flow (one sync run)

1. `IngestionPipeline.run(tenant, connector_id, mode)` creates an `ingestion_runs` row (status
   `running`); a second concurrent run of the same connector is refused.
2. Connector `fetch(since=cursor)` yields `SourceRecord(kind, external_id, payload, source_url)`.
   HTTP calls retry with exponential backoff on 429/5xx/network errors.
3. For each record a SHA-256 content hash is compared with `source_records`. Unchanged → provenance
   is *touched* (marked seen in this run) and extraction is skipped (**idempotent**).
4. Changed/new records → kind-specific extractor → candidates.
5. Resolver maps each candidate to an existing entity (canonical key, alias, then fuzzy) or creates
   one; properties are merged; `version` increments and a `graph_changes` snapshot is written only
   when something actually changed.
6. Relationship candidates are validated against the ontology domain/range rules and upserted.
   Missing endpoints are created as `stub` entities (later promoted when a source defines them).
7. Text extraction pass: documents changed in this run are scanned for mentions of known entities →
   `describes` relationships with lower confidence + the snippet as evidence.
8. Full sync only: provenance rows of this connector not seen in this run are removed; entities /
   relationships with no remaining provenance become `stale` (never hard-deleted; versioned).
9. Run row is finalized with counters; incremental cursor is stored in `connector_state`.

## 6. Database design

| Table | Key columns |
|---|---|
| `tenants` | `id`, `name` |
| `entities` | `id`, `tenant_id`, `type`, `canonical_key` (unique per tenant), `name`, `description`, `properties` JSON, `customer_scope`, `status` (active/stub/stale), `version`, timestamps |
| `entity_aliases` | `tenant_id`, `entity_id`, `entity_type`, `alias_norm` |
| `relationships` | `id`, `tenant_id`, `type`, `source_id`, `target_id` (unique triple), `properties`, `confidence`, `status`, `version` |
| `provenance` | subject (entity/relationship), `connector_id`, `source_system`, `source_record_key`, `source_url`, `extractor`, `confidence`, `evidence`, `last_run_id`, `observed_at` |
| `graph_changes` | append-only version history: subject, version, change type, snapshot, run |
| `documents` / `document_chunks` | searchable source text linked to its Documentation entity |
| `source_records` | per connector/external id content hash (idempotency) |
| `connectors` | per tenant config (non-secret) + `secret_ref` (e.g. `env:GITHUB_TOKEN`) |
| `connector_state` | incremental cursor |
| `ingestion_runs` | status, mode, counters, error |
| `api_keys` | `prefix`, SHA-256 hash, roles, allowed customers, revoked |
| `audit_log` | principal, tenant, action, params, outcome |
| `graph_revisions` | per tenant monotonic counter (search cache invalidation) |

## 7. Ontology

See [ONTOLOGY_SCHEMA.md](ONTOLOGY_SCHEMA.md). Key point: identity is type-specific. A
`deployment` is identified by *(application, customer, environment tier)* and an `environment` by
*(customer, tier)*, so `payments` deployed for ACME-PROD and Globex-PROD are two distinct nodes even
though they share one `application` node.

## 8. Query engine

* **Graph primitives**: neighbours (filtered by relation/direction/type), multi-step pattern
  traversal, shortest paths (BFS), transitive dependency/impact tracing.
* **Hybrid retrieval**: BM25 over entity text and document chunks + metadata filters (type,
  status, customer scope) + graph expansion of hits.
* **Question answering** (`ask`): rule-based intent detection + entity linking (longest n-gram
  match on names/aliases with type hints) → compiled traversal pattern → results with **evidence**
  (graph paths and provenance: source system, URL, extractor, confidence). Unknown intents fall
  back to hybrid search plus the linked entities' neighbourhood. No LLM is embedded: the calling AI
  agent is the reasoning layer; the platform guarantees grounded, traceable facts.

## 9. MCP integration

`ontology_platform.mcp_server` builds an `MCPServer` with tools: `search_entities`,
`get_entity_details`, `get_related_entities`, `find_entity_relationships`, `query_knowledge_graph`,
`search_documentation`, `trace_application_dependencies`, `get_deployment_information`,
`get_ontology_schema`. Inputs are validated by Pydantic (from type hints with bounds); outputs are
structured JSON; errors are returned as MCP tool errors.

* **stdio** (local AI assistants that launch the server themselves): principal from `OP_MCP_API_KEY`.
* **streamable HTTP** (`/mcp`): every call must carry `Authorization: Bearer <api key or JWT>`.

Each tool call is authorized (`graph:read`) and audit-logged.

## 10. Security

* **Authentication**: API keys `op_<prefix>_<secret>` stored as SHA-256 hashes; or JWT
  (`sub`, `tenant`, `roles`, optional `customers`) verified with the configured key/algorithm.
* **Authorization**: roles → permissions (`viewer`: graph:read; `ingestor`: + connectors:read,
  ingest:run; `admin`: + connectors:write, keys:manage).
* **Tenant isolation**: tenant comes only from the authenticated principal; every repository
  query filters by `tenant_id`; tests assert cross-tenant invisibility.
* **Customer-scope ABAC**: principals may be restricted to a set of customers; customer-scoped
  entities (customer, environment, deployment, and anything tagged with a customer) of other
  customers are invisible, and traversals never pass through them.
* **Secrets**: connector credentials are never stored in the DB; `secret_ref` points to an env var
  or mounted file.
* **Traceability**: provenance on every fact, version history, audit log of reads and ingestion.

## 11. Extensibility

New source = subclass `Connector`, emit one of the normalized record kinds, register it with
`@register_connector("type")`. A new record kind = add an extractor to `extraction.registry`. New
entity/relationship types = add to `ontology.schema` (rules are data, not code).

## 12. Known limitations

* Real connectors (GitHub, GitLab, Bitbucket, Confluence, Kubernetes, Azure, GCP, AWS) are
  implemented against the documented APIs and tested with recorded-shape mock HTTP responses /
  stub clients; **none has been tested against a live tenant** (no credentials available).
* Semantic search is lexical BM25 with normalization, not dense embeddings.
* Push ingestion (`POST /api/v1/connectors/{id}/records`) accepts normalized records from webhooks/CI.
* Scheduling is external (cron / Kubernetes CronJob invoking `ontology-platform sync`).

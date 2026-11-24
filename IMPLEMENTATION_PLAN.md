# Implementation Plan and Status

This document tracks what is built, how it is verified, and what comes next. The design is in
[ARCHITECTURE.md](ARCHITECTURE.md).

**Status values**
* **Done:** implemented and covered by automated tests or verified end to end.
* **Partial:** implemented, but not yet verified against a live external system.
* **Planned:** not started.

## Status by milestone

### M1 — Ontology and graph store: Done

| Item | Status |
|---|---|
| Entity and relationship types with identity and domain/range rules (`ontology/schema.py`) | Done |
| Canonical keys, name and environment-tier normalization, customer scoping (`ontology/keys.py`) | Done |
| Graph tables: entities, aliases, relationships, provenance, version history, documents (`storage/orm.py`) | Done |
| Tenant-scoped repository with customer-visibility filtering on every read | Done |
| Database migrations (Alembic), tested to match the data model | Done |

### M2 — Connectors and ingestion: Done (live systems: Partial)

| Item | Status |
|---|---|
| Connector framework: registry, typed configuration, secret references, HTTP retries with backoff | Done |
| GitHub, GitLab, Bitbucket, Confluence, Kubernetes, AWS, Azure and GCP connectors | **Partial:** tested against API-shaped mock responses; not yet run against live accounts |
| Catalog-file connector (CMDB/CRM exports) and push connector (records via REST) | Done |
| Fixture connector used for the sample dataset | Done |
| Pipeline: idempotency, per-record failure isolation, incremental sync, retraction of removed facts, stale marking, run tracking | Done |

### M3 — Extraction and resolution: Done

| Item | Status |
|---|---|
| Extractors for Backstage catalogs, OpenAPI specs, READMEs, Confluence pages, Kubernetes workloads, cloud resources and catalog files | Done |
| Entity mentions in document text | Done |
| Entity resolution: canonical key, then alias, then fuzzy name match; placeholder entities; versioned merges | Done |

### M4 — Query and retrieval: Done

| Item | Status |
|---|---|
| Entity search (exact alias + BM25 ranking) and document search | Done |
| Entity details with provenance and history; related entities; paths between entities | Done |
| Dependency tracing and impact analysis (applications, deployments, customers) | Done |
| Deployment lookup per customer and environment | Done |
| Natural-language questions for all seven reference questions, with a search fallback | Done |

### M5 — Security: Done

| Item | Status |
|---|---|
| API keys (hashed, revocable) and JWT authentication | Done |
| Roles: viewer, ingestor, admin | Done |
| Tenant isolation and per-customer access restriction across all queries | Done |
| Audit log of reads, writes and denied requests | Done |

### M6 — Interfaces: Done

| Item | Status |
|---|---|
| REST API with OpenAPI docs, health, readiness and metrics endpoints | Done |
| MCP server with nine tools over stdio and HTTP (verified with real clients, including authentication); a tenth, workflow tool was added in M9 | Done |
| Command-line interface | Done |

### M7 — Containers: Done

| Item | Status |
|---|---|
| `Containerfile` and `compose.yaml` (PostgreSQL, API, MCP server) for Podman | Done |
| Stack verified end to end on PostgreSQL 16: migrations, data loading, REST, customer-restricted keys, MCP | Done |
| Automated tests against PostgreSQL | **Partial:** `tests/test_postgres.py` runs when `OP_TEST_DATABASE_URL` is set; the rest of the suite runs on SQLite, and there is no CI job yet |

### M8 — PuppyGraph evaluation: Proof of concept done; adoption pending

PuppyGraph was evaluated as an alternative graph query engine over the same PostgreSQL data.
Full results are in [poc/puppygraph/FINDINGS.md](poc/puppygraph/FINDINGS.md).

| Item | Status |
|---|---|
| Runs under rootless Podman, reads PostgreSQL through a read-only role and views | Done |
| Graph schema generated from the ontology | Done |
| All reference questions in openCypher, with results identical to the current engine | Done |
| Changes visible immediately after ingestion (no sync step) | Done |
| Benchmark and scale-test tooling | Done |
| Gate 1: performance at 100k+ entities on a host with at least 16 GB RAM | Planned |
| Gate 2: stability with 10 concurrent clients | Planned |
| Gate 3: PuppyGraph row-level security for tenants, customers and environments | Planned |
| Gate 4: licensing review | Planned |

Key findings so far:
* PuppyGraph's official MCP server is not tenant-aware, so the platform's own MCP server remains
  the agent interface.
* Unbounded shortest-path queries can exhaust PuppyGraph's memory on small hosts; queries must be
  bounded.
* The current engine stays the default until all four gates pass.

### M9 — Agentic workflow, UI and hardening: Done

| Item | Status |
|---|---|
| Orchestrator (classify, plan, execute in parallel or sequence, validate, synthesize) with six agents: graph, documentation, dependency and impact, deployment, evidence validation, response synthesis ([AGENTIC_WORKFLOW.md](AGENTIC_WORKFLOW.md)) | Done |
| REST endpoints `POST /api/v1/agent/query`, `GET /api/v1/agent/runs`, `/runs/{id}`, `/runs/{id}/events`, and MCP tool `run_enterprise_intelligence_workflow` | Done |
| Operator console at `/ui`: static HTML/CSS/JavaScript, strict security headers, no build step | Done |
| Workflow run persistence in `workflow_runs` (migration `0002`), tenant-scoped, with retention per tenant | Done |
| Security fixes: credential redaction in audit records, fail-closed JWT `customers` claim, minimum HS256 key length, redacted evidence for customer-restricted callers, restricted admins cannot create broader keys, relationship endpoints must belong to the tenant | Done |
| Parallel ingestion: `sync --parallel N` with failure isolation; one running ingestion per connector enforced by a partial unique index (migration `0003`) | Done |
| Performance fixes: batched provenance loading for paths and deployments, entity-linking alias index cached per graph revision, graph statistics computed in SQL, configurable connection pool, per-statement database latency metric | Done |
| PostgreSQL integration tests (opt-in via `OP_TEST_DATABASE_URL`) | Done |

## Test coverage

Automated tests (`pytest`). How to run them, the test data and the end-to-end scenarios are in
[TESTING.md](TESTING.md).

| Test file | Covers |
|---|---|
| `test_ontology.py` | Types, rules, keys, environment tiers, multi-customer deployment identity |
| `test_resolution.py` | Key, alias and fuzzy matching; placeholder entities; versioning |
| `test_extraction.py` | All extractors, invalid input handling, text mentions, chunking |
| `test_ingestion.py` | Idempotency, incremental sync, staleness, retraction, failure isolation, concurrency, push, provenance |
| `test_ingestion_parallel.py` | Parallel sync of several connectors: failure isolation, concurrency bound, overlapping runs |
| `test_graph_integrity.py` | No duplicates, stubs, invalid relationships, conflicting metadata, deep and circular traversals, tenant-bound endpoints |
| `test_connectors.py` | Every connector against mock APIs, retries, secrets, configuration validation |
| `test_query.py` | The seven reference questions and all query operations |
| `test_auth_isolation.py` | API keys, JWT, roles, audit, tenant isolation, customer restriction |
| `test_security.py` | Authentication edge cases, authorization, tenant isolation, customer-scope enforcement and evidence redaction, secret hygiene, over REST and MCP (including streamable HTTP) |
| `test_api.py` | REST API end to end |
| `test_api_workflow.py` | Agent workflow over REST: runs, pagination, events, validation, tenant isolation |
| `test_mcp.py` | MCP tools through the official MCP client |
| `test_mcp_workflow.py` | The workflow tool through the official MCP client |
| `test_agents.py` | Intents, planning, evidence rules, synthesis, orchestration (parallelism, timeouts, retries), eight end-to-end scenarios |
| `test_concurrency.py` | Parallel REST requests, MCP tool calls, workflows and ingestion starts on file-based SQLite |
| `test_ui.py` | Web console delivery and security headers |
| `test_migrations.py` | Migrations match the data model |
| `test_postgres.py` | Core queries, isolation, keys, concurrency and ingestion locking on PostgreSQL (opt-in) |

## Roadmap

1. Run the test suite against PostgreSQL in CI.
2. Verify each connector against a real sandbox account (GitHub organization, Confluence space, Kubernetes cluster, cloud accounts).
3. Complete PuppyGraph gates 1–4 and decide on adoption.
4. Refresh PostgreSQL statistics (`ANALYZE`) automatically after large ingestion runs.
5. Scheduled syncs (built-in scheduler or Kubernetes CronJob manifests).
6. Optional semantic search with pgvector in the same PostgreSQL database.
7. Alternative graph backend (Neo4j or Apache AGE) if traversal volume outgrows SQL.

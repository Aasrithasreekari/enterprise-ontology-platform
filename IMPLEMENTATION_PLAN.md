# Implementation Plan & Progress

Status legend: ✅ implemented and tested · 🟡 implemented, not verified against a live system · ⬜ not started / future

## Starting point

The repository was empty, so this is a greenfield implementation. Stack and rationale are in
[ARCHITECTURE.md](ARCHITECTURE.md) §4.

## Milestones

### M1 — Ontology & graph store ✅
- ✅ Entity/relationship type registry with identity and domain/range rules (`ontology/schema.py`)
- ✅ Canonical keys, name/tier normalization, customer scoping (`ontology/keys.py`)
- ✅ SQL property graph: entities, aliases, relationships, provenance, version history, documents (`storage/orm.py`)
- ✅ Tenant-scoped `GraphRepository` with customer-visibility filtering on every read
- ✅ Alembic migration `0001` (tested to match the ORM)

### M2 — Connectors & ingestion ✅ / 🟡
- ✅ Connector framework: registry, typed configs, `env:`/`file:` secret refs, retrying HTTP client (429/5xx/backoff/Retry-After)
- 🟡 GitHub, GitLab, Bitbucket, Confluence, Kubernetes, Azure (Resource Graph + SP token), GCP (Cloud Asset + SA JWT), AWS (Tagging API via boto3).
  These are tested against API-shaped mock responses and stub clients only. **Not tested against live tenants; no credentials were available.**
- ✅ `catalog_file` connector (CMDB/CRM export), `push` connector (records via REST)
- ✅ `fixture` local test adapter, used for the sample dataset
- ✅ Pipeline:
  - content-hash idempotency
  - per-record transactions and failure isolation
  - incremental cursors
  - retraction of facts a changed record no longer states
  - stale marking on full sync
  - refusal of concurrent runs
  - run bookkeeping

### M3 — Extraction & resolution ✅
- ✅ Structured extractors: Backstage catalog, OpenAPI, README, Confluence pages/labels, K8s workloads, cloud resources, catalog files
- ✅ Text mention extraction (n-gram alias matching; ambiguity and generic-term filtering)
- ✅ Resolver: key → alias → fuzzy (name-keyed types only); stub creation and promotion; property merge with versioning

### M4 — Query & retrieval ✅
- ✅ Search: exact alias + BM25 over entities and document chunks (cached per tenant revision)
- ✅ Entity details with provenance and history; related entities; shortest paths with evidence
- ✅ Dependency tracing downstream/upstream with impact roll-up (applications, deployments, customers)
- ✅ Deployment information per customer/tier
- ✅ Intent-based QA for all 7 example questions, plus a hybrid-search fallback

### M5 — Security ✅
- ✅ API keys (SHA-256 hashed, shown once, revocable); JWT (HS256/RS256, aud/iss/exp)
- ✅ Roles: viewer / ingestor / admin → permissions
- ✅ Tenant isolation (tested); customer-scope ABAC across search, traversal, QA and stats (tested)
- ✅ Audit log for reads, writes and denials (REST, MCP, CLI)

### M6 — Interfaces ✅
- ✅ REST API (FastAPI, OpenAPI at `/docs`), `/health`, `/ready`, `/metrics`
- ✅ MCP server with 9 tools. stdio transport verified with a real subprocess. Streamable HTTP
  transport verified with a real server and bearer auth, including rejection of requests without a
  token.
- ✅ CLI: `init-db`, tenants, keys, connectors, `sync`, `ask`, `seed-demo`, `serve`, `mcp`

### M7 — Containers (Podman) ✅
- ✅ `Containerfile`, `compose.yaml` (db/api/mcp, named volume, network), `.env.example`, `.containerignore`
- ✅ Podman 6.1.4 machine (rootless) initialized. Image built with `podman compose build` (~70 s);
  stack started with `podman compose up -d`; all services healthy.
- ✅ Migrations ran against **PostgreSQL 16** in the container.
- ✅ Verified in containers:
  - `seed-demo` loaded both tenants
  - REST query answered
  - the Acme-only key saw only Acme deployments
  - MCP over streamable HTTP answered with bearer auth
- ⚠️ `podman compose` picked **Docker Desktop's compose binary** as its provider on this machine.
  The provider is pinned to `podman-compose` via `PODMAN_COMPOSE_PROVIDER` (see docs/PODMAN.md).
- 🟡 The pytest suite itself still runs on SQLite; PostgreSQL is covered by the container run only.

### M8 — PuppyGraph proof of concept ✅ PoC complete / ⬜ adoption gated
Details: [poc/puppygraph/FINDINGS.md](poc/puppygraph/FINDINGS.md). Architecture: ARCHITECTURE.md §4a.

Done:
- ✅ Reviewed the official docs: schema v1 format, `whereClause`, Cypher support, MCP server, RBAC/RLS, editions
- ✅ PuppyGraph 1.19.0 runs under rootless Podman (compose overlay, healthcheck, persistent schema volume)
- ✅ PostgreSQL connectivity through a least-privilege read-only role over `graph.*` views
- ✅ Schema generated from the ontology. Found that **edge labels must be unique**, so the generic
  model was adopted (`Entity` + 14 edge labels); the typed model (34 composite labels) was also uploaded.
- ✅ openCypher: 11/11 core checks match the SQL engine:
  - Q1–Q7, including Q3b and Q7b
  - tenant predicate (S1) and customer predicate (S2)
  - parameters work; writes are rejected
- ✅ Zero-ETL freshness: a pushed change was visible to Cypher in 0.7 s, and retractions were too
- ✅ The official MCP server works but is **not tenant-aware** (cross-tenant read observed), so a
  custom enterprise MCP is required

Open:
- ❌ Untyped `shortestPath` crashes the engine (Java heap OOM). Tested on a 3.7 GB VM, below the
  8 GB minimum; the host has 7.7 GB in total.
- ✅ Gate harness built and validated locally (0 correctness mismatches at 7.3k entities):
  `scale_data.py`, `benchmark.py`, `run_gates.sh`, [HOST_RUNBOOK.md](poc/puppygraph/HOST_RUNBOOK.md).
  Waiting on a ≥16 GB host.
- ⬜ Gate 1: performance at realistic volume (≥100k entities) on a host with ≥8 GB (better, 16 GB)
  for PuppyGraph. At PoC scale it was 0.4–15 s versus 42–70 ms for the SQL engine.
- ⬜ Gate 2: stability with 10 concurrent agents running the curated query set, with timeouts
- ⬜ Gate 3: PuppyGraph RLS + service-account impersonation (tenant/customer/environment) in the licensed edition
- ⬜ Gate 4: licensing (MSA review, edition choice)
- ⬜ Only after the gates: a `PuppyGraphQueryBackend` implementing the existing `QueryEngine`
  operations as bounded, scoped Cypher templates, selectable by config. The SQL backend remains the
  default and the fallback.

## Test inventory (`pytest`: 129 passing)

| File | Covers |
|---|---|
| test_ontology.py | types, rules, keys, tiers, multi-customer deployment identity |
| test_resolution.py | key/alias/fuzzy matching, no-fuzzy for structured ids, stubs, versioning |
| test_extraction.py | all extractors, invalid input → warnings, mention extraction, chunking |
| test_ingestion.py | idempotency, incremental, staleness, retraction, failure isolation, failed fetch, concurrency, push, provenance |
| test_connectors.py | every connector (mock HTTP / stub client), retries, secrets, config validation |
| test_query.py | the 7 example questions + primitives (search, paths, traces, deployments, docs) |
| test_auth_isolation.py | API keys, JWT, roles, audit, tenant isolation, customer ABAC |
| test_api.py | REST end to end incl. auth, validation, sync, push, admin |
| test_mcp.py | MCP tools via official client: schemas, outputs, validation errors, auth |
| test_migrations.py | Alembic schema == ORM |

## Next steps
1. ⬜ Run the pytest suite against PostgreSQL (parametrize the DB URL) in CI.
1a. ⬜ PuppyGraph gates 1–4 (see M8); decide on adoption.
2. ⬜ Verify each SaaS connector against a real sandbox tenant (GitHub org, Confluence space, cluster, cloud account).
3. ⬜ Built-in scheduler, or Kubernetes CronJob manifests, for periodic syncs.
3a. ⬜ Run `ANALYZE` on the graph tables after large ingestion runs (stale statistics caused a 30× slowdown in harness validation).
4. ⬜ Optional semantic retrieval with pgvector in the same PostgreSQL (no separate vector DB).
5. ⬜ Graph backend adapter (Neo4j / Apache AGE) if traversal volume outgrows SQL.

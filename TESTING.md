# Testing

The test suite uses `pytest` with `pytest-asyncio` (`asyncio_mode = "auto"`, configured in
`pyproject.toml`). Tests live in `tests/`; shared fixtures and helpers are in `tests/conftest.py`.

## 1. Running the tests

Install the development extras once:

```powershell
.venv\Scripts\pip install -e ".[dev,postgres]"
```

Run everything:

```powershell
.venv\Scripts\python -m pytest -q
```

One file, or tests matching a name:

```powershell
.venv\Scripts\python -m pytest tests\test_agents.py -q
```

```powershell
.venv\Scripts\python -m pytest -q -k "e2e"
```

By default the suite needs no external services: it uses in-memory SQLite, and file-based SQLite
for the concurrency tests. Connectors are tested against mock HTTP transports and stub clients, never
live systems.

### PostgreSQL integration tests

`tests/test_postgres.py` is skipped unless `OP_TEST_DATABASE_URL` points at a PostgreSQL database:

```powershell
$env:OP_TEST_DATABASE_URL = "postgresql+psycopg://user:password@localhost:5432/ontology"
```

```powershell
.venv\Scripts\python -m pytest tests\test_postgres.py -q
```

The module applies the migrations itself (twice, to check they are idempotent), creates tenants with
the prefix `pgt<process id>-`, and deletes those rows afterwards, so it can run against a shared
development database. The default Compose stack does not publish the database port; the database
must be reachable from where `pytest` runs (the PuppyGraph overlay, for example, publishes it on
`127.0.0.1:5432`).

## 2. Test data

| Source | Content |
|---|---|
| `sample_data/generate.py` | Generates the committed sample files. Re-run only after editing it: `python sample_data/generate.py` |
| `sample_data/northwind/catalog/crm-export.yaml` | Catalog connector input (`catalog_file`): organization, customers Acme Bank, Globex Insurance and Initech Retail, products PayHub and ClaimsPro, plus business aliases such as "Customer A", "Customer B", "Product X", "Application Y" (payments-platform) and "Payment Service" (payment-processor) |
| `sample_data/northwind/fixtures/*.yaml` | Fixture connector input: normalized records shaped like GitHub, GitLab, Bitbucket, Confluence, Kubernetes, AWS, Azure and GCP data |
| `sample_data/contoso/fixtures/all.yaml` | A second tenant used to prove tenant isolation |

The **fixture connector** (`type: fixture`, `connectors/local.py`) replays the records under a
directory as if they came from the named source systems, with incremental support based on
`updated_at`. It is the "local test adapter" behind `seed-demo` and the tests.

Fixtures in `conftest.py`:

| Fixture / helper | Purpose |
|---|---|
| `seeded` | Session-wide, read-only platform with `northwind` and `contoso` loaded (catalog + fixtures) |
| `fresh` | Empty platform with one tenant `t1`, for tests that write |
| `operator(tenant, roles, customers)` | Builds a `Principal` directly (optionally customer-restricted) |
| `make_settings`, `new_service` | Settings and a `PlatformService` over in-memory SQLite (or a given URL) |
| `mcp_http(svc)` | Serves the MCP server over streamable HTTP in-process, with no default principal, and yields an authenticated client factory |
| `seed_tenants`, `purge_tenants` | Prefixed seeding and cleanup for shared databases (PostgreSQL tests) |

## 3. Test files

| File | Covers |
|---|---|
| `test_ontology.py` | Entity and relationship types, domain/range rules, canonical keys, environment tiers, multi-customer deployment identity |
| `test_resolution.py` | Key, alias and fuzzy matching; placeholder (stub) entities; versioning |
| `test_extraction.py` | All extractors, invalid input handling, text mentions, chunking |
| `test_ingestion.py` | Idempotency, incremental sync, staleness, retraction, failure isolation, concurrent-run refusal, push, provenance |
| `test_ingestion_parallel.py` | `sync_connectors` (CLI `sync --parallel`): a failing connector does not stop others, start failures are isolated, concurrency bound (1 on in-memory SQLite), runs overlap and all complete, record errors reported as `source_errors` |
| `test_graph_integrity.py` | No duplicates after repeated or overlapping ingestion, alias matches counted as resolved duplicates, stub creation and promotion, rejection of invalid relationships, merging and versioning of conflicting metadata, depth-3 traversal, termination on circular dependencies, relationship endpoints must belong to the tenant, tenant-scoped repository reads |
| `test_connectors.py` | Every connector against mock APIs, retries, secret references, configuration validation |
| `test_query.py` | The seven reference questions and all query operations |
| `test_auth_isolation.py` | API keys, JWT, roles, audit, tenant isolation, customer restriction |
| `test_security.py` | Authentication edge cases (malformed, tampered, revoked, expired and wrong-audience credentials; non-Bearer schemes), role enforcement, cross-tenant access by id, key and name over REST, multi-hop customer-scope enforcement, evidence redaction for restricted callers, restricted admins cannot widen scope, secrets kept out of errors and audit, `redact()`, the same rules over MCP in-process and over streamable HTTP, workflow tenant isolation, permission and customer scope |
| `test_api.py` | REST API end to end: unauthenticated ops endpoints, auth (including `X-API-Key` and `X-Request-ID`), request validation, queries, customer-scoped keys, connectors, sync, push, API keys, audit |
| `test_api_workflow.py` | Workflow over REST: full run returned, parallel plan, list/get/events with pagination, input validation and auth, tenant isolation of stored runs |
| `test_mcp.py` | MCP tools through the official MCP client: tool list and schemas (all ten tools), structured output, graph tools, validation errors, no credentials without a default principal, customer-scoped principal |
| `test_mcp_workflow.py` | `run_enterprise_intelligence_workflow` over MCP: listing and guidance, end-to-end run, validation and auth |
| `test_agents.py` | Intent classification, planning (sequential and parallel steps, bounded plans), evidence rules, stub entities as inferred, synthesis excludes unsupported facts, orchestration (parallel batch, timeout, retry of transient errors only, no retry of deterministic errors), permissions, persistence, customer scope, and the eight end-to-end scenarios |
| `test_concurrency.py` | On file-based SQLite: parallel REST requests across tenants and customer scopes, 24 parallel MCP tool calls, concurrent ingestion refused for one connector and allowed for different connectors, simultaneous start admits exactly one run, concurrent workflows return correct answers |
| `test_ui.py` | `/ui` redirect and page, static assets with security headers, no external resources or key-like strings or `localStorage` in the files, API contracts unchanged |
| `test_migrations.py` | Alembic migrations produce exactly the ORM tables and columns, and re-running them is harmless |
| `test_postgres.py` | On PostgreSQL (opt-in): core queries, tenant isolation, customer scope, API keys with concurrent REST requests, ingestion lock and incremental re-sync |

## 4. End-to-end workflow scenarios

The eight scenarios run the full multi-agent workflow (`PlatformService.run_workflow`) on the sample
dataset and assert on the validated answer. All are in `tests/test_agents.py`.

| # | Question | Test | Asserted |
|---|---|---|---|
| 1 | Which repositories belong to Customer A's Product X? | `test_e2e_1_repositories_of_customer_product` | payments-gateway, payment-processor, fraud-detector and ledger-service repositories in the summary; status `validated` |
| 2 | Where is Application Y deployed across SIT, UAT and Production? | `test_e2e_2_application_deployments_by_environment` | Acme SIT/UAT/PROD and Globex PROD with clusters; `validated` |
| 3 | Which Kubernetes clusters host Customer B's applications? | `test_e2e_3_clusters_hosting_customer` | Globex's three clusters, and not the GKE cluster |
| 4 | What happens if Payment Service goes down? | `test_e2e_4_impact_if_payment_service_goes_down` | payments-platform and Acme affected; limitation that impact is not observed failure |
| 5 | Which applications depend on Payment Service? | `test_e2e_5_dependents_of_payment_service` | payment-gateway depends on payment-processor |
| 6 | What documentation describes Payment Service authentication? | `test_e2e_6_documentation_on_authentication` | Intent `documentation`; matching document and sources |
| 7 | Which applications depend on Payment Service, and where are they deployed? | `test_e2e_7_dependents_and_their_deployments` | Step `s2` depends on `s1` and completes; deployment facts; `validated` |
| 8 | What is the relationship between Customer A, Product X, Application Y and its repository? | `test_e2e_8_relationship_chain` | Intents `relationship` + `repositories`; the owns → contains → has chain |

`test_missing_environment_is_reported` additionally checks that a tier with no deployment is listed
under `missing_information`. The same workflow is exercised over REST (`test_api_workflow.py`), over
MCP (`test_mcp_workflow.py`) and under concurrency (`test_concurrency.py`).

## 5. Security and concurrency suites

* **Security** (`test_security.py`, with `test_auth_isolation.py`): each rule in
  [SECURITY.md](SECURITY.md) is checked over the service facade, REST, MCP with a fixed principal and
  MCP over streamable HTTP with real `Authorization` headers.
* **Concurrency** (`test_concurrency.py`, `test_ingestion_parallel.py`, plus orchestration tests in
  `test_agents.py`): uses file-based SQLite because in-memory SQLite shares one connection and is
  not thread-safe. The PostgreSQL module repeats the concurrent REST and ingestion-lock checks on a
  real server.

## 6. What is not tested

* Connectors against live GitHub, GitLab, Bitbucket, Confluence, Kubernetes or cloud accounts (only
  API-shaped mock responses).
* The Podman images and Compose stack (verified manually; see
  [IMPLEMENTATION_PLAN.md](IMPLEMENTATION_PLAN.md)).
* The console's JavaScript behaviour in a browser (`test_ui.py` checks the served files and headers).
* PostgreSQL, unless `OP_TEST_DATABASE_URL` is set.

## Latest verified results

Run on 2026-11-24 (Windows 11, Python 3.11):

| Run | Result |
|---|---|
| `pytest -q` (SQLite) | 239 passed, 5 skipped (the PostgreSQL suite, which needs `OP_TEST_DATABASE_URL`) |
| `pytest tests/test_postgres.py` with `OP_TEST_DATABASE_URL` pointing at PostgreSQL 16 in the Podman stack | 5 passed |
| Podman stack (`db`, `api`, `mcp`) rebuilt from the `Containerfile`; migrations `0002` and `0003` applied to an existing database | REST `/api/v1/agent/query`, the MCP `run_enterprise_intelligence_workflow` tool and `/ui` verified end to end |

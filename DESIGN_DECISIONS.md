# Design Decisions

Architecture decision records for the main choices in the platform. Each entry gives the context,
the decision and its consequences. The overall design is in [ARCHITECTURE.md](ARCHITECTURE.md).

| ADR | Decision | Status |
|---|---|---|
| [001](#adr-001-graph-stored-in-relational-tables-in-postgresql) | Graph stored in relational tables in PostgreSQL | Accepted |
| [002](#adr-002-puppygraph-evaluated-but-gated) | PuppyGraph evaluated but gated | Evaluation; not adopted |
| [003](#adr-003-deterministic-agent-orchestration-without-an-embedded-llm) | Deterministic agent orchestration without an embedded LLM | Accepted |
| [004](#adr-004-agents-are-thin-adapters-over-the-query-engine) | Agents are thin adapters over the query engine | Accepted |
| [005](#adr-005-rule-based-evidence-grading) | Rule-based evidence grading | Accepted |
| [006](#adr-006-bm25-instead-of-vector-search) | BM25 instead of vector search | Accepted |
| [007](#adr-007-static-operator-console-without-a-build-step) | Static operator console without a build step | Accepted |
| [008](#adr-008-podman-for-containers) | Podman for containers | Accepted |
| [009](#adr-009-per-step-database-sessions-and-bounded-parallelism) | Per-step database sessions and bounded parallelism | Accepted |
| [010](#adr-010-persist-every-workflow-run) | Persist every workflow run | Accepted |
| [011](#adr-011-database-enforced-single-running-ingestion-per-connector) | Database-enforced single running ingestion per connector | Accepted |

---

## ADR-001: Graph stored in relational tables in PostgreSQL

**Context.** The required questions (ownership chains, deployments per customer and tier, clusters
hosting a customer, dependency and impact analysis) are bounded-depth traversals. Every fact also
needs provenance, version history, tenant and customer filtering, and transactional ingestion.

**Decision.** Store the graph as a property graph in relational tables (`entities`, `relationships`,
`provenance`, `graph_changes`, ...) in PostgreSQL, with SQLite for development and tests through
the same SQLAlchemy code. Traversals run as batched `IN` queries per hop. All graph access goes through
`GraphRepository`, which applies the tenant and customer filters.

**Consequences.**
* One ACID store holds the graph, provenance, history, connectors, runs, keys and the audit log; no
  synchronization between stores.
* Each hop is a query, so traversal depth is capped (`OP_MAX_TRAVERSAL_DEPTH`, default 6, at most 12).
* There is no graph query language; new query shapes are written as `QueryEngine` operations.
* Another backend (a graph database or a graph engine over the same tables) can be added behind
  `GraphRepository` / `QueryEngine` without changing the interfaces.

## ADR-002: PuppyGraph evaluated but gated

**Context.** A graph query engine with openCypher could simplify complex traversals. PuppyGraph
queries PostgreSQL in place (zero-ETL), which would keep PostgreSQL as the single source of truth.

**Decision.** Evaluate PuppyGraph in a proof of concept over read-only views (`graph.entity_v`,
`graph.relationship_v`) and a least-privilege role, run as an optional Compose overlay. Keep the
SQL traversal engine as the production path until four gates pass: performance at 100k+ entities,
stability with 10 concurrent clients, row-level security for tenants, customers and environments,
and a licensing review. Evidence: [poc/puppygraph/FINDINGS.md](poc/puppygraph/FINDINGS.md).

**Consequences.**
* The PoC matched the current engine's results and saw changes immediately; the gates remain open.
* PuppyGraph's own MCP server is not tenant-aware, so the platform's MCP server stays the agent
  interface regardless of the outcome.
* Queries must be bounded; an untyped `shortestPath` exhausted PuppyGraph's memory on a small host.
* The overlay is not part of the default stack and adds nothing to normal deployments.

## ADR-003: Deterministic agent orchestration without an embedded LLM

**Context.** Multi-part enterprise questions need several retrievals, combined into one answer
whose facts can be traced. The caller is already an AI assistant capable of language reasoning.

**Decision.** Implement the workflow as deterministic code (`agents/`): keyword-based intent
classification with entity linking, a planner producing a bounded, acyclic list of steps with
explicit `depends_on`, specialist agents, evidence validation and templated synthesis. No language
model runs inside the platform. Details: [AGENTIC_WORKFLOW.md](AGENTIC_WORKFLOW.md).

**Consequences.**
* Runs are reproducible and unit-testable; the same question over the same graph yields the same
  plan and answer.
* No model cost, latency or external data transfer, and no generated facts: every statement comes
  from the graph.
* Unusual phrasings may be misclassified and fall back to entity lookup or documentation search;
  answer text is templated, not fluent prose. The calling assistant rephrases it.

## ADR-004: Agents are thin adapters over the query engine

**Context.** Access control, traversal limits and provenance handling already live in
`QueryEngine`, `QuestionAnswerer` and `GraphRepository`. Duplicating them in agents would create a
second path that could diverge.

**Decision.** Each specialist agent receives a `QueryEngine` bound to the caller's tenant and
customer scope and only calls existing operations (`trace_dependencies`,
`get_deployment_information`, `find_relationships`, `search_documentation`, and the
`QuestionAnswerer` handlers made public for this purpose). Agents convert results into facts with
evidence; they contain no graph or search logic.

**Consequences.**
* Tenant isolation, customer scoping and evidence redaction apply to agents automatically.
* Improvements to the engine benefit REST, MCP and the workflow at once.
* An agent can answer only what the engine can compute; new capabilities start in the engine.

## ADR-005: Rule-based evidence grading

**Context.** Facts in an answer differ in strength: some come from structured sources (catalog
files, Kubernetes labels, OpenAPI specs), others from text mentions or references to entities that no
source defines. Users and assistants need to tell them apart.

**Decision.** Grade every fact in `agents/evidence.py`:
* **unsupported**: no provenance, an edge without provenance, or a stale or invisible entity. Such
  facts are excluded from the answer and reported as `UNSUPPORTED_FACT`;
* **inferred**: provenance exists, but some edge rests only on `text_mention`, `reference` or
  `bm25_passage` evidence or on confidence below 0.8, or an entity is a placeholder (`stub`);
* **confirmed**: everything else.

The run is `validated` only when all facts are confirmed and no step failed.

**Consequences.**
* Grading is deterministic and explainable (`validation_notes` on each fact).
* Grades depend on the confidence values assigned by extractors.
* Only contradictions between statements are detected (`CONFLICTING_EVIDENCE`); conflicting
  property values from different sources are not (the store keeps the latest value plus history).

## ADR-006: BM25 instead of vector search

**Context.** Enterprise text is identifier-heavy (service names, cluster names, API paths).
Retrieval must be deterministic and must respect tenant and customer visibility.

**Decision.** Use a pure-Python BM25 index (`search/bm25.py`) over entity text and document chunks,
built in process and cached per tenant, graph revision and customer scope (LRU of 32 indexes).
Exact name and alias matches rank above BM25 hits.

**Consequences.**
* No embedding model, GPU or extra datastore; results are reproducible.
* Paraphrases and synonyms beyond the built-in normalization are missed.
* Each process builds its own index after a graph change; memory and build time grow with the
  corpus.
* Planned, not implemented: pgvector in the same PostgreSQL if lexical search stops being enough.

## ADR-007: Static operator console without a build step

**Context.** Operators and reviewers need a browser view of questions, workflow traces, impact,
documents and ingestion, without adding a front-end toolchain to a Python project.

**Decision.** Serve `/ui` as static HTML, CSS and plain JavaScript (`api/static/`, no framework, no
bundler, no third-party assets) through FastAPI `StaticFiles`. The console only calls the public
REST API with an API key held in `sessionStorage`.

**Consequences.**
* Nothing to install or build; the console ships inside the Python package and image.
* A strict Content-Security-Policy (`'self'` only, no inline scripts) is possible.
* The console has no privileges beyond the key pasted into it, and no API changes were needed.
* UI code is hand-written; there are no component or browser tests (`tests/test_ui.py` checks the
  served files and headers).

## ADR-008: Podman for containers

**Context.** The stack (PostgreSQL, API, MCP server) needs a reproducible local and server runtime
without requiring a privileged daemon.

**Decision.** Use Podman and Podman Compose with an OCI `Containerfile` and a Compose-spec
`compose.yaml`: rootless-friendly, non-root application user (UID 10001), named volumes only,
fully-qualified image names, ports bound to `127.0.0.1`.

**Consequences.**
* No Docker dependency; the same image runs under any OCI runtime.
* Podman ignores `HEALTHCHECK` in OCI-format images, so health checks are defined in
  `compose.yaml`.
* On Windows, the Compose provider must be pinned to `podman-compose` when Docker Desktop is
  installed ([docs/PODMAN.md](docs/PODMAN.md)).

## ADR-009: Per-step database sessions and bounded parallelism

**Context.** Independent workflow steps (for example deployments and documentation) can run
concurrently. SQLAlchemy sessions are not thread-safe, the connection pool is finite, and a hung step
must not hang the request.

**Decision.** The orchestrator runs each ready step in a worker thread (`asyncio.to_thread`) with
its own session and scoped `QueryEngine`, limited by a semaphore (`OP_AGENT_MAX_PARALLEL`), a
per-step timeout (`OP_AGENT_STEP_TIMEOUT_SECONDS`) and retries for transient errors only
(`OP_AGENT_MAX_RETRIES`). When the database cannot be shared across threads (in-memory SQLite),
parallelism is forced to 1. The connection pool is configurable (`OP_DB_POOL_SIZE`,
`OP_DB_MAX_OVERFLOW`, `OP_DB_POOL_TIMEOUT_SECONDS`). The same pattern bounds parallel ingestion
(`sync --parallel`).

**Consequences.**
* Parallel steps do not share transaction state; each sees the database as of its own queries.
* Pool sizing must account for concurrent runs × `OP_AGENT_MAX_PARALLEL` ([DEPLOYMENT.md](DEPLOYMENT.md#5-scaling-and-capacity)).
* Python cannot interrupt a thread: a timed-out step is reported as failed, but its thread finishes in
  the background and its result is discarded.

## ADR-010: Persist every workflow run

**Context.** Workflow answers must be auditable after the fact: which plan ran, which agents
produced which facts, and how each fact was graded.

**Decision.** Store each run in `workflow_runs` (migration `0002`): indexed columns for tenant,
principal, query, status, validation status, intents and timing, plus the full run document (plan,
events, facts, validation, answer) as JSON. The run is saved when it starts and again when it
finishes. Reads are tenant-scoped (`GET /api/v1/agent/runs`, `/runs/{id}`, `/runs/{id}/events`).
After each run, only the newest `OP_WORKFLOW_RUNS_RETAINED` runs per tenant are kept.

**Consequences.**
* Past runs can be inspected in the console's History tab or over the API; there is no re-run
  endpoint (ask the question again to get a new run).
* The stored document follows the Pydantic model of its time; older runs keep their original shape.
* A process that dies mid-run leaves the run in status `running`.
* Stored runs are visible to every principal of the tenant, not filtered by customer
  ([SECURITY.md](SECURITY.md#10-known-limitations)).

## ADR-011: Database-enforced single running ingestion per connector

**Context.** Two concurrent syncs of one connector would race on the same provenance and staleness
bookkeeping. The original check-then-insert guard could admit two runs when starts happened at the
same moment, including from different processes.

**Decision.** Add a partial unique index `uq_run_one_running` on `ingestion_runs (tenant_id,
connector_id) WHERE status = 'running'` (migration `0003`, which first closes existing duplicates).
A losing insert raises an integrity error that is returned as `409 conflict`. The application-level
check remains for a clear message and to mark runs older than 6 hours as abandoned.

**Consequences.**
* Exactly one run is admitted per connector, across threads, processes and replicas
  (`test_simultaneous_start_of_idle_connector_admits_exactly_one`).
* Works on PostgreSQL and SQLite, which both support partial indexes.
* If a process dies during ingestion, that connector is blocked until the 6-hour abandonment window
  passes or the run row is corrected.

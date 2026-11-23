# PuppyGraph Proof of Concept — Findings

**Date:** 2026-11-23
**PuppyGraph:** 1.19.0 (Developer edition image `docker.io/puppygraph/puppygraph:1.19.0`)
**Runtime:** Podman 6.1.4, rootless, WSL machine on Windows 11
**Dataset:** the platform's sample enterprise, 95 entities / ~300 relationships across 2 tenants

**Status: the PoC succeeded on correctness, freshness and Podman compatibility. Performance and
access control are not proven yet. Do not switch the query layer before the gates in §8 pass.**

## 1. What was built

| File | Purpose |
|---|---|
| `views.sql` | `graph.entity_v` / `graph.relationship_v` views over the canonical tables, plus a read-only `puppygraph_ro` role |
| `generate_schema.py` | Generates the PuppyGraph v1 schema from `ontology/schema.py`, in two models (generic / typed) |
| `setup_poc.py` | Applies the views, generates and uploads the schema, waits for Cypher |
| `compose.puppygraph.yaml` | Podman Compose overlay that adds PuppyGraph to the stack |
| `run_queries.py` | Runs the enterprise use cases as openCypher and compares them with the platform engine's answers |
| `results.json` | Results of the final run |

Reproduce:

```powershell
podman compose -f compose.yaml -f poc/puppygraph/compose.puppygraph.yaml up -d db api puppygraph
```

```powershell
.venv\Scripts\python poc\puppygraph\setup_poc.py
```

```powershell
.venv\Scripts\python poc\puppygraph\run_queries.py
```

## 2. Podman compatibility

| Check | Result |
|---|---|
| `podman pull` of the image (5.74 GB) | ✅ ~4 min |
| Runs under rootless Podman (`podman run` and `podman compose` overlay) | ✅ ready (`/readyz` 200) in ~55 s |
| Reaches PostgreSQL by service name on the compose network | ✅ `jdbc:postgresql://db:5432/ontology` |
| Compose healthcheck (`curl /readyz`) | ✅ healthy |
| Schema survives container restart | ✅ (and across re-creation with the `STORAGE_PATH_ROOT` volume in the overlay) |
| Official Podman support statement | ❌ none found: docs describe Docker only. It works in practice, but there is no vendor guarantee. |

## 3. Graph schema & relational mapping

* PuppyGraph maps tables to nodes and edges **at query time**. It is not a graph database and keeps no copy by default.
* **Per-label row filters (`whereClause`) work**. Our generic `entities` / `relationships` tables
  are mapped without changing the canonical model, through views that flatten JSON identity into
  scalar columns (PuppyGraph supports only scalar types). Preserved on every node and edge:
  * `tenant_id`
  * `customer`
  * `environment_tier`
  * `canonical_key`
  * `source_systems`
  * `source_refs` / `source_urls`
  * `evidence`
  * `confidence`
  * `version`
  * `status`
* **Constraint found: edge labels must be unique.** One label cannot connect several node-label
  pairs; the upload was rejected with `Duplicate edge labels found: DEPENDS_ON, ...`. Two models
  were tested and both upload successfully:
  * **Generic (chosen):** one `Entity` label with a `type` property, and one edge label per
    relationship type (14 edges). Agents write `(a:Entity {type:'application'})-[:DEPENDS_ON*1..4]->(b)`.
  * **Typed:** 13 node labels and 34 edge labels named `DEPENDS_ON__SERVICE__DATABASE`. Stronger
    typing, but every multi-type traversal must list label alternatives. Impractical for agents.
* Stale facts are excluded in the mapping (`status <> 'stale'`), so retractions take effect immediately.
* PuppyGraph connects as the least-privilege `puppygraph_ro` role, which can read only the `graph` views.

## 4. openCypher against the enterprise use cases (`results.json`)

| # | Use case | Result | Latency* |
|---|---|---|---|
| Q1 | Repositories of Acme's PayHub (customer→product→app→repo) | ✅ matches engine | 1.6 s |
| Q2 | APIs exposed by payments-platform | ✅ | 0.4 s |
| Q3 | claims-portal deployments per customer/tier/cluster | ✅ | 2.5–9.4 s (first run slow) |
| Q3b | …filtered to UAT (environment-level filter) | ✅ | 0.7 s |
| Q4 | Transitive dependencies (`DEPENDS_ON*1..4`) | ✅ | 1.8 s |
| Q5 | Clusters hosting Globex (customer→env→deployment→cluster) | ✅ | 0.7 s |
| Q6 | Docs describing a repo, with confidence + source system | ✅ | 0.5 s |
| Q7 | Applications affected if auth-service fails (UNION of 3 patterns) | ✅ | 1.5 s |
| Q7b | Customers affected (variable-length `*0..4` to deployments) | ✅ | 1.2 s |
| S1 | Tenant predicate isolates tenants | ✅ | 0.3 s |
| S2 | Customer-scope predicate | ✅ | 0.3 s |
| X1 | `$parameters` over HTTP | ✅ supported (values come back as strings) | 0.2 s |
| X2 | `CREATE` rejected by the server | ✅ `Unsupported Cypher clause: Create` | |
| P2 | `shortestPath` over typed edges `[:OWNS\|CONTAINS\|HAS*..4]` | ✅ correct path + per-edge provenance | 15–127 s |
| P1 | `shortestPath` over **all** edge types `[*..4]` (undirected) | ❌ **engine crashed**: `java.lang.OutOfMemoryError: Java heap space`; restarted itself after ~3 s; in-flight queries failed | |

\* On a VM with ~3.7 GB RAM, **below PuppyGraph's documented 8 GB development minimum** (the host
has 7.7 GB in total). For reference, the platform's current SQL traversal engine answers Q1–Q7 in
**42–70 ms** end to end over REST, including auth, question parsing and audit. The PuppyGraph
latencies are therefore not a fair verdict. They show only that, at this scale and on this
hardware, PuppyGraph offers no performance advantage.

## 5. Incremental ingestion & freshness

A customer pushed through the platform's ingestion API was visible to PuppyGraph's Cypher
**0.7 s later, with no sync step**. A retraction disappeared just as quickly, because the
platform marks the edge stale and the mapping filters stale rows. PostgreSQL remains the only
source of truth, so all of the platform's ingestion still applies to the PuppyGraph view:
* content-hash idempotency
* incremental cursors
* provenance
* versioning

## 6. MCP: official server vs. enterprise server

Tested `@puppygraph/mcp-server@1.3.0` (Apache-2.0, Node 20+, stdio only) in `--read-only` mode:

* 21 tools: query, schema, status, catalog/table probing and schema-authoring helpers.
* Cypher executes; `DETACH DELETE` is refused.
* **It authenticates with one static PuppyGraph credential and has no notion of the calling
  principal.** A single query returned customers of **both tenants** (`fabrikam` from contoso
  alongside northwind's).
* It does not do any of the following:
  * audit log per end user
  * customer/environment scoping
  * bounding of query cost; an agent can send the P1 query and crash the engine
  * return of provenance unless the agent asks for it

**Conclusion: a custom enterprise MCP server is required** for agent-facing access. It must
provide:
* authentication of the agent's principal
* curated, bounded tools that compile to parameterized Cypher templates with mandatory
  `tenant_id` / customer / environment predicates
* audit
* provenance in every answer

The official server is useful only for **platform engineers** authoring and debugging the graph
schema, with an admin credential, and never exposed to tenant agents. The current platform MCP
server already has the right shape; only its backend would change.

## 7. Operational & licensing limitations

* **Resources:** 8 GB RAM minimum for development; production nodes 16 vCPU / 64 GB. The image is
  5.7 GB. The Gremlin/Cypher JVM can be exhausted by one unbounded query, so guardrails are
  needed: bounded hops, typed edges, timeouts and concurrency limits.
* **Startup:** ~55–75 s before Cypher answers; queries fail with "Cypher engine unavailable" before that.
* **Licensing (pricing page):** the Developer edition is free with no expiry. It allows a single
  node and up to 2 data sources (we need 1), with basic username/password auth. The Enterprise
  edition is priced per machine and adds clustering and SSO. Commercial-use terms (MSA) were
  **not reviewed**; legal review is needed before any production use.
* **Access control:**
  * PuppyGraph documents RBAC roles, service accounts, impersonation (`X-PuppyGraph-Assume-User`)
    and row-level security driven by entitlement tables.
  * **Not tested in this PoC.** Whether these features are available in the Developer edition
    is also not documented.
  * In the PoC, isolation is enforced by predicates injected by the enterprise layer (S1/S2).
* **Results:** scalar values are returned as strings over HTTP, so the client must convert types.

## 7a. Scale-harness validation (local, 7.3k entities / 19.6k relationships)

Tooling for gates 1 and 2:
* `scale_data.py`: synthetic enterprise at any size, using the platform's own keys and scoping rules
* `benchmark.py`: the same sampled questions through both backends, with a correctness check, latency and concurrency
* `run_gates.sh`: runs everything on a host
* [HOST_RUNBOOK.md](HOST_RUNBOOK.md): host requirements and steps

The local run validates the harness only, not the gates:
* **Correctness:** 0 mismatches between the SQL engine and PuppyGraph across 7 operations × 5
  sampled parameter sets.
* **Latency, p50 at concurrency 1:**

  | Backend | Fastest operation | Slowest operation |
  |---|---|---|
  | SQL engine | 6 ms (docs) | 205 ms (transitive dependencies) |
  | PuppyGraph | 0.2 s | 4 s (deployments of an application) |

* **Second PuppyGraph memory failure mode:** besides the JVM heap OOM, the data-access compute node
  has a ~1.4 GB cap on this VM. Join-heavy queries fail cleanly with `[DAE-05] Memory of process
  exceed limit`, and at **concurrency 2** every `deployments_of_application` call failed.
* **Operational lesson for the platform itself:** after a bulk load, stale PostgreSQL planner
  statistics turned a 0.2 s alias scan into 6 s, because the planner chose a nested loop over
  26.9M row pairs. Ingestion should `ANALYZE` the graph tables after large runs (added to the plan).
  The generator now runs `ANALYZE` in autocommit mode.

## 8. Recommendation & gates

Adopt **PostgreSQL as the canonical store with the `graph.*` view contract** now; this is proven
and costs nothing. Treat **PuppyGraph as a candidate query engine** behind the platform's existing
query interface, and switch only if these gates pass:

1. **Performance:** on a host meeting the 8 GB minimum (better, 16 GB), with a realistic volume
   (≥100k entities, ≥1M relationships), the Q1–Q7 p95 latency must beat or match the SQL engine,
   and 6+ hop traversals must be acceptable.
2. **Stability:** no engine crash under the curated query set at 10 concurrent agents; query
   timeouts are configured.
3. **Security:** RLS + service-account impersonation keyed on `tenant_id` / `customer` /
   `environment_tier` must work in the licensed edition, as defense in depth beneath the
   enterprise-layer predicates.
4. **Licensing:** MSA reviewed; edition chosen.

Vector retrieval: when lexical BM25 stops being enough, use **pgvector in the same PostgreSQL**
(no new datastore). The docs show PuppyGraph can read pgvector columns through a cast view. Not
implemented.

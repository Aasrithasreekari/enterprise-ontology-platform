# PuppyGraph gate run: host runbook

This runbook covers gates 1–2 (performance and stability) from [FINDINGS.md](FINDINGS.md) §8. It
also gives a checklist for gate 3 (PuppyGraph RLS).

## Host requirements

| Item | Minimum | Why |
|---|---|---|
| RAM | **16 GB** (8 GB is PuppyGraph's documented dev minimum, and PostgreSQL, the API and the benchmark also need room) | A ~4 GB test VM hit Java heap OOM and `DAE-05` memory caps |
| CPU | 8 vCPU, amd64 **with AVX2** (or arm64) | PuppyGraph requirement |
| Disk | 40 GB free | 5.7 GB image + PostgreSQL + results |
| OS | Linux (Fedora/RHEL/Ubuntu); Windows/macOS work through a Podman machine sized to ≥16 GB | |
| Software | Podman ≥ 4.9, `podman-compose` ≥ 1.1, Python 3.11, git, curl | No Docker |
| Limits | open-files hard limit ≥ 65536 (`ulimit -Hn`) | PuppyGraph requirement |

## Getting the code onto the host

Clone the repository on the host (or copy it over SSH), run the steps below, and keep the resulting
`poc/puppygraph/gate_results/<timestamp>/` directory: it holds everything needed to evaluate the
gates and update [FINDINGS.md](FINDINGS.md).

## Steps (on the host)

```bash
cp .env.example .env    # set POSTGRES_PASSWORD and PUPPYGRAPH_PASSWORD
```

```bash
pip install podman-compose
```

```bash
export PODMAN_COMPOSE_PROVIDER=$(command -v podman-compose)
```

```bash
bash poc/puppygraph/run_gates.sh
```

For a quick smoke run first (~10k entities, about 10 minutes):

```bash
SCALE_CUSTOMERS=50 ITERATIONS=5 bash poc/puppygraph/run_gates.sh
```

The script:
1. records the environment (CPU, RAM, AVX2, Podman version and provider)
2. builds and starts `db`, `api` and `puppygraph` with Podman Compose
3. seeds the sample tenants
4. generates the **scale tenant**: ~100k entities and ~300k relationships with provenance, using
   the same identity and scoping rules as ingestion, and runs `ANALYZE`
5. applies the `graph.*` views and uploads the generated PuppyGraph schema
6. runs the use-case suite (`run_queries.py`)
7. runs the benchmark (`benchmark.py`):
   * a correctness pass that requires identical answers from both backends
   * p50/p95/max and throughput at concurrency 1 and 10, for both the SQL engine and PuppyGraph
8. collects `podman stats` and counts PuppyGraph OOM and memory-limit errors

## Pass criteria

| Gate | Pass when |
|---|---|
| 1 Performance | 0 correctness mismatches, and PuppyGraph p95 ≤ SQL-engine p95 for the traversal-heavy operations (`dependencies_of_application`, `impact_of_service_outage`, `repos_of_customer_product`), with no operation above 2 s p95 |
| 2 Stability | 0 `engine_unavailable` errors, 0 `OutOfMemoryError`, and < 1 % errors at concurrency 10 |

## Gate 3 checklist: PuppyGraph RLS (manual, in the PuppyGraph UI)

1. Enable authentication (`RBAC_ENABLED=true`, and `BOLTSERVER_AUTHENTICATION_ENABLED` /
   `GREMLINSERVER_AUTHENTICATION_ENABLED`). Confirm whether the Developer edition allows this.
2. Create the entitlement table `graph.entitlements(username, resource_type, resource_value)`,
   with rows for tenants, customers and environment tiers.
3. Add a `rowLevelSecurity` block to the schema. Map `entity_v.tenant_id`, `entity_v.customer` and
   `entity_v.environment_tier` (and the same columns on `relationship_v`) to resource types.
4. Create a service account with impersonation. Run `run_queries.py` with
   `X-PuppyGraph-Assume-User` set to a northwind-only user, then an acme-only user, then a
   prod-excluded user.
5. Pass when no row from another tenant, customer or tier is returned, including on multi-hop
   paths and in `shortestPath`.

## Validation of the tooling (small VM, 7.3k entities)

A run on a ~4 GB VM confirmed the tooling works; its timings are not a gate verdict. The correctness pass had 0
mismatches across all 7 operations. Results are in `benchmark_validation_local.json`.

"""Gate 1 (performance) + gate 2 (stability): current SQL engine vs PuppyGraph on the same data.

For every operation, both backends answer the same randomly sampled questions:
* SQL engine through the platform's own code paths (QueryEngine / QuestionAnswerer handlers)
* PuppyGraph through equivalent parameterized openCypher

Then:
1. correctness pass: concurrency 1, answers must be identical (mismatches are reported)
2. load passes: concurrency levels (default 1 and 10), p50/p95/max latency, errors, engine crashes

    python poc/puppygraph/benchmark.py --db postgresql+psycopg://ontology:PW@127.0.0.1:5432/ontology \
        --tenant scale --iterations 20 --concurrency 1 10 --out poc/puppygraph/benchmark_results.json
"""

from __future__ import annotations

import argparse
import json
import random
import statistics
import sys
import time
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path

import httpx
from sqlalchemy import select

from ontology_platform.config import Settings
from ontology_platform.query.engine import QueryEngine, Step
from ontology_platform.query.qa import Linked, QuestionAnswerer
from ontology_platform.storage.db import Database
from ontology_platform.storage.orm import Entity, Relationship
from ontology_platform.storage.repository import Scope


@dataclass
class Op:
    name: str
    pool: str  # which parameter pool to sample
    sql: Callable[[QueryEngine, dict], set[str]]
    cypher: list[str]  # queries; result = union of first query minus union of the rest ("EXCEPT" semantics)


def _linked(engine: QueryEngine, key: str) -> Linked:
    e = engine.resolve(key)
    return Linked(e, e.name, True)


def sql_repos(eng: QueryEngine, p: dict) -> set[str]:
    r = QuestionAnswerer(eng)._repositories("", [_linked(eng, p["customer"]), _linked(eng, p["product"])], 10_000)
    return {x["entity"]["name"] for x in r["results"]}


def sql_apis(eng: QueryEngine, p: dict) -> set[str]:
    r = QuestionAnswerer(eng)._apis("", [_linked(eng, p["app"])], 10_000)
    return {x["entity"]["name"] for x in r["results"]}


def sql_deployments(eng: QueryEngine, p: dict) -> set[str]:
    info = eng.get_deployment_information(p["app"])
    return {f"{d['customer']}/{d['environment_tier']}/{c['name']}" for d in info["deployments"] for c in d["clusters"]}


def sql_dependencies(eng: QueryEngine, p: dict) -> set[str]:
    return {i["entity"]["name"] for i in eng.trace_dependencies(p["app"], "downstream", 4)["items"]}


def sql_clusters(eng: QueryEngine, p: dict) -> set[str]:
    r = QuestionAnswerer(eng)._clusters("", [_linked(eng, p["customer"])], 10_000)
    return {x["entity"]["name"] for x in r["results"]}


def sql_impact(eng: QueryEngine, p: dict) -> set[str]:
    return {a["name"] for a in eng.trace_dependencies(p["service"], "upstream", 4)["impact"]["affected_applications"]}


def sql_docs(eng: QueryEngine, p: dict) -> set[str]:
    paths = eng.traverse([eng.resolve(p["app"])], [Step(("describes",), "in", ("documentation",))])
    return {pa.end.name for pa in paths}


W = "a.tenant_id = $tenant"
OPS = [
    Op("repos_of_customer_product", "customer_product", sql_repos, [
        "MATCH (c:Entity)-[:OWNS]->(p:Entity)-[:CONTAINS]->(a:Entity)-[:HAS]->(r:Entity) "
        "WHERE c.tenant_id = $tenant AND c.canonical_key = $customer AND p.canonical_key = $product AND r.type = 'repository' "
        "RETURN DISTINCT r.name AS value"]),
    Op("apis_of_application", "app", sql_apis, [
        f"MATCH (a:Entity)-[:EXPOSES]->(x:Entity) WHERE {W} AND a.canonical_key = $app AND x.type = 'api' RETURN DISTINCT x.name AS value "
        f"UNION MATCH (a:Entity)-[:CONTAINS]->(:Entity)-[:EXPOSES]->(x:Entity) WHERE {W} AND a.canonical_key = $app AND x.type = 'api' RETURN DISTINCT x.name AS value"]),
    Op("deployments_of_application", "app", sql_deployments, [
        "MATCH (k:Entity)<-[:RUNS_ON]-(d:Entity)-[:DEPLOYS]->(a:Entity) "
        f"WHERE {W} AND a.canonical_key = $app AND d.type = 'deployment' "
        "RETURN DISTINCT d.customer + '/' + d.environment_tier + '/' + k.name AS value"]),
    Op("dependencies_of_application", "app", sql_dependencies, [
        f"MATCH (a:Entity)-[:CONTAINS]->(s:Entity)-[:DEPENDS_ON*1..4]->(d:Entity) WHERE {W} AND a.canonical_key = $app AND s.type = 'service' RETURN DISTINCT d.name AS value",
        # the engine reports the application's own components separately, not as dependencies
        f"MATCH (a:Entity)-[:CONTAINS]->(s:Entity) WHERE {W} AND a.canonical_key = $app AND s.type = 'service' RETURN DISTINCT s.name AS value"]),
    Op("clusters_hosting_customer", "customer", sql_clusters, [
        "MATCH (c:Entity)<-[:BELONGS_TO]-(e:Entity)<-[:DEPLOYED_IN]-(d:Entity)-[:RUNS_ON]->(k:Entity) "
        "WHERE c.tenant_id = $tenant AND c.canonical_key = $customer AND e.type = 'environment' AND d.type = 'deployment' AND k.type = 'kubernetes_cluster' "
        "RETURN DISTINCT k.name AS value"]),
    Op("impact_of_service_outage", "service", sql_impact, [
        "MATCH (s:Entity)<-[:DEPENDS_ON*1..4]-(x:Entity)<-[:CONTAINS]-(app:Entity) WHERE s.tenant_id = $tenant AND s.canonical_key = $service AND app.type = 'application' RETURN DISTINCT app.name AS value "
        "UNION MATCH (s:Entity)-[:EXPOSES]->(:Entity)<-[:DEPENDS_ON*1..4]-(x:Entity)<-[:CONTAINS]-(app:Entity) WHERE s.tenant_id = $tenant AND s.canonical_key = $service AND app.type = 'application' RETURN DISTINCT app.name AS value "
        "UNION MATCH (app:Entity)-[:CONTAINS]->(s:Entity) WHERE s.tenant_id = $tenant AND s.canonical_key = $service AND app.type = 'application' RETURN DISTINCT app.name AS value"]),
    Op("docs_describing_application", "app", sql_docs, [
        f"MATCH (doc:Entity)-[:DESCRIBES]->(a:Entity) WHERE {W} AND a.canonical_key = $app AND doc.type = 'documentation' RETURN DISTINCT doc.name AS value"]),
]


class Backends:
    def __init__(self, db_url: str, tenant: str, pg_url: str, pg_auth: tuple[str, str], timeout: float) -> None:
        self.db = Database(db_url)
        self.tenant = tenant
        self.settings = Settings(_env_file=None, database_url=db_url, max_results=5000)  # platform maximum
        self.pg_url, self.pg_auth = pg_url, pg_auth
        self.http = httpx.Client(timeout=timeout, auth=pg_auth, limits=httpx.Limits(max_connections=64))

    def sql(self, op: Op, params: dict) -> set[str]:
        with self.db.session() as s:
            return op.sql(QueryEngine(s, Scope(self.tenant), self.settings), params)

    def cypher(self, op: Op, params: dict) -> set[str]:
        sets = []
        for q in op.cypher:
            r = self.http.post(f"{self.pg_url}/submitCypher", json={"query": q, "parameters": {"tenant": self.tenant, **params}})
            data = r.json() if r.headers.get("content-type", "").startswith("application/json") else r.text
            if r.status_code != 200 or not isinstance(data, list):
                raise RuntimeError(f"HTTP {r.status_code}: {str(data)[:300]}")
            sets.append({str(dict(zip(row["Keys"], row["Values"]))["value"]) for row in data})
        out = sets[0]
        for extra in sets[1:]:
            out = out - extra
        return out

    def pools(self, sample: int, rng: random.Random) -> dict[str, list[dict]]:
        with self.db.session() as s:
            def keys(etype: str) -> list[str]:
                return list(s.scalars(select(Entity.canonical_key).where(Entity.tenant_id == self.tenant, Entity.type == etype, Entity.status == "active")))

            src = Entity.__table__.alias("src")
            tgt = Entity.__table__.alias("tgt")
            owned = s.execute(
                select(src.c.canonical_key, tgt.c.canonical_key)
                .select_from(Relationship.__table__.join(src, src.c.id == Relationship.source_id).join(tgt, tgt.c.id == Relationship.target_id))
                .where(Relationship.tenant_id == self.tenant, Relationship.type == "owns", src.c.type == "customer")
            ).all()
        pick = lambda xs: rng.sample(xs, k=min(sample, len(xs)))  # noqa: E731
        return {
            "customer_product": [{"customer": c, "product": p} for c, p in pick(owned)],
            "app": [{"app": k} for k in pick(keys("application"))],
            "service": [{"service": k} for k in pick(keys("service"))],
            "customer": [{"customer": k} for k in pick(keys("customer"))],
        }


def timed(fn: Callable[[], set[str]]) -> tuple[float, set[str] | None, str | None]:
    t = time.perf_counter()
    try:
        res = fn()
        return (time.perf_counter() - t) * 1000, res, None
    except Exception as exc:  # recorded, not raised: stability is what we measure
        return (time.perf_counter() - t) * 1000, None, f"{type(exc).__name__}: {exc}"[:300]


def pct(values: list[float], q: float) -> float | None:
    if not values:
        return None
    values = sorted(values)
    return round(values[min(len(values) - 1, int(round(q * (len(values) - 1))))], 1)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", required=True)
    ap.add_argument("--tenant", default="scale")
    ap.add_argument("--puppygraph-url", default="http://127.0.0.1:8081")
    ap.add_argument("--puppygraph-user", default="puppygraph")
    ap.add_argument("--puppygraph-password", default="poc-local-only-123")
    ap.add_argument("--iterations", type=int, default=20, help="calls per operation per backend per concurrency level")
    ap.add_argument("--concurrency", type=int, nargs="+", default=[1, 10])
    ap.add_argument("--samples", type=int, default=25, help="distinct parameter values per pool")
    ap.add_argument("--timeout", type=float, default=120)
    ap.add_argument("--backends", nargs="+", default=["sql", "puppygraph"], choices=["sql", "puppygraph"])
    ap.add_argument("--ops", nargs="*", help="subset of operation names")
    ap.add_argument("--seed", type=int, default=7)
    ap.add_argument("--out", default="poc/puppygraph/benchmark_results.json")
    a = ap.parse_args()

    rng = random.Random(a.seed)
    be = Backends(a.db, a.tenant, a.puppygraph_url, (a.puppygraph_user, a.puppygraph_password), a.timeout)
    pools = be.pools(a.samples, rng)
    ops = [o for o in OPS if not a.ops or o.name in a.ops]
    run_fn = {"sql": be.sql, "puppygraph": be.cypher}
    report: dict = {"tenant": a.tenant, "started": time.strftime("%Y-%m-%dT%H:%M:%S"), "correctness": {}, "load": []}

    # 1. correctness: identical answers on every sampled parameter set
    print("== correctness (concurrency 1)")
    for op in ops:
        mismatches, errors, checked = [], [], 0
        for params in pools[op.pool][: min(10, len(pools[op.pool]))]:
            results = {}
            for b in a.backends:
                _, res, err = timed(lambda b=b: run_fn[b](op, params))
                results[b] = res
                if err:
                    errors.append({"backend": b, "params": params, "error": err})
            if len(a.backends) == 2 and None not in results.values():
                checked += 1
                if results["sql"] != results["puppygraph"]:
                    mismatches.append({"params": params, "only_sql": sorted(results["sql"] - results["puppygraph"])[:10],
                                       "only_puppygraph": sorted(results["puppygraph"] - results["sql"])[:10]})
        report["correctness"][op.name] = {"checked": checked, "mismatches": mismatches, "errors": errors[:5]}
        print(f"  {op.name:<30} checked={checked} mismatches={len(mismatches)} errors={len(errors)}")

    # 2. load
    for conc in a.concurrency:
        for b in a.backends:
            print(f"== load: backend={b} concurrency={conc}")
            for op in ops:
                jobs = [rng.choice(pools[op.pool]) for _ in range(a.iterations)]
                t0 = time.perf_counter()
                with ThreadPoolExecutor(max_workers=conc) as ex:
                    outcomes = list(ex.map(lambda p, op=op, b=b: timed(lambda: run_fn[b](op, p)), jobs))
                wall = time.perf_counter() - t0
                lat = [ms for ms, _, err in outcomes if err is None]
                errs = [err for _, _, err in outcomes if err]
                row = {"backend": b, "concurrency": conc, "op": op.name, "n": len(outcomes), "errors": len(errs),
                       "engine_unavailable": sum("engine unavailable" in e.lower() for e in errs),
                       "p50_ms": pct(lat, 0.5), "p95_ms": pct(lat, 0.95), "max_ms": round(max(lat), 1) if lat else None,
                       "throughput_qps": round(len(lat) / wall, 2) if wall else None, "error_sample": errs[:2]}
                report["load"].append(row)
                print(f"  {op.name:<30} p50={row['p50_ms']}ms p95={row['p95_ms']}ms max={row['max_ms']}ms errors={row['errors']} qps={row['throughput_qps']}")

    # 3. NL question overhead of the SQL engine (entity linking scans all aliases of the tenant)
    if "sql" in a.backends:
        app = pools["app"][0]["app"].split(":", 1)[1]
        lat = []
        for _ in range(3):
            def ask() -> set[str]:
                with be.db.session() as s:
                    return set(QuestionAnswerer(QueryEngine(s, Scope(a.tenant), be.settings)).ask(f"Which APIs are exposed by {app}?")["intent"])
            ms, _, err = timed(ask)
            lat.append(ms if err is None else float("nan"))
        report["sql_natural_language_ask_ms"] = [round(x, 1) for x in lat]
        print(f"== SQL engine full NL ask() incl. entity linking: {report['sql_natural_language_ask_ms']} ms")

    report["finished"] = time.strftime("%Y-%m-%dT%H:%M:%S")
    Path(a.out).write_text(json.dumps(report, indent=2), encoding="utf-8")
    bad = sum(len(v["mismatches"]) for v in report["correctness"].values())
    crashes = sum(r["engine_unavailable"] for r in report["load"])
    print(f"\nwritten {a.out}; correctness mismatches={bad}; engine-unavailable errors={crashes}")
    sys.exit(1 if bad else 0)


if __name__ == "__main__":
    main()

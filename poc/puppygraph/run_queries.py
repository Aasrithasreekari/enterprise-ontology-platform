"""PuppyGraph PoC: run the enterprise use cases as openCypher and compare with the platform engine.

Expected answers are the ones asserted in tests/test_query.py against the same sample dataset.
Usage:  python poc/puppygraph/run_queries.py [--url http://127.0.0.1:8081] [--user puppygraph] [--password ...]
"""

from __future__ import annotations

import argparse
import json
import sys
import time

import httpx

T = "northwind"

CASES: list[dict] = [
    {
        "id": "Q1", "question": "Which repositories belong to Customer A's (Acme) Product X (PayHub)?",
        "cypher": f"""
MATCH (c:Entity)-[:OWNS]->(p:Entity)-[:CONTAINS]->(a:Entity)-[:HAS]->(r:Entity)
WHERE c.tenant_id = '{T}' AND c.canonical_key = 'customer:acme' AND p.canonical_key = 'product:payhub'
  AND r.type = 'repository'
RETURN DISTINCT r.name AS value""",
        "expected": {"northwind/payments-gateway", "northwind/payment-processor", "northwind/fraud-detector", "northwind/ledger/ledger-service"},
    },
    {
        "id": "Q2", "question": "Which APIs are exposed by payments-platform?",
        "cypher": f"""
MATCH (a:Entity)-[:EXPOSES]->(api:Entity)
WHERE a.tenant_id = '{T}' AND a.canonical_key = 'application:payments-platform'
RETURN DISTINCT api.name AS value""",
        "expected": {"payments-api", "webhooks-api"},
    },
    {
        "id": "Q3", "question": "Where is claims-portal deployed (customer / tier / cluster)?",
        "cypher": f"""
MATCH (d:Entity)-[:DEPLOYS]->(a:Entity), (d)-[:RUNS_ON]->(k:Entity)
WHERE a.tenant_id = '{T}' AND a.canonical_key = 'application:claims-portal' AND d.type = 'deployment'
RETURN DISTINCT d.customer + '/' + d.environment_tier + '/' + k.name AS value""",
        "expected": {"globex/prod/nw-prod-aks-weu", "globex/uat/nw-nonprod-eks-use1", "initech/prod/nw-prod-aks-weu", "initech/uat/nw-nonprod-gke-euw1"},
    },
    {
        "id": "Q3b", "question": "Same, filtered to UAT only (environment-level filter)",
        "cypher": f"""
MATCH (d:Entity)-[:DEPLOYS]->(a:Entity)
WHERE a.tenant_id = '{T}' AND a.canonical_key = 'application:claims-portal' AND d.environment_tier IN ['uat']
RETURN DISTINCT d.customer AS value""",
        "expected": {"globex", "initech"},
    },
    {
        "id": "Q4", "question": "What does payments-platform depend on (transitively, via its services)?",
        "cypher": f"""
MATCH (a:Entity)-[:CONTAINS]->(s:Entity)-[:DEPENDS_ON*1..4]->(d:Entity)
WHERE a.tenant_id = '{T}' AND a.canonical_key = 'application:payments-platform'
RETURN DISTINCT d.name AS value""",
        # includes internal components reached via depends_on; the engine reports those separately
        "expected": {"auth-service", "auth-api", "ledger-service", "payments-db", "ledger-db", "payment-processor", "fraud-detector"},
    },
    {
        "id": "Q5", "question": "Which Kubernetes clusters host Customer B's (Globex) applications?",
        "cypher": f"""
MATCH (c:Entity)<-[:BELONGS_TO]-(e:Entity)<-[:DEPLOYED_IN]-(d:Entity)-[:RUNS_ON]->(k:Entity)
WHERE c.tenant_id = '{T}' AND c.canonical_key = 'customer:globex' AND e.type = 'environment'
  AND d.type = 'deployment' AND k.type = 'kubernetes_cluster'
RETURN DISTINCT k.name AS value""",
        "expected": {"nw-prod-eks-use1", "nw-prod-aks-weu", "nw-nonprod-eks-use1"},
    },
    {
        "id": "Q6", "question": "Which documentation describes the payment-processor repository (with confidence + source)?",
        "cypher": f"""
MATCH (doc:Entity)-[r:DESCRIBES]->(x:Entity)
WHERE x.tenant_id = '{T}' AND x.canonical_key = 'repository:github/northwind/payment-processor'
RETURN doc.name AS value, r.confidence AS confidence, r.source_systems AS sources, doc.url AS url
ORDER BY confidence DESC""",
        "expected_contains": {"payment-processor README"},
    },
    {
        "id": "Q7", "question": "What applications are affected if auth-service goes down?",
        "cypher": f"""
MATCH (s:Entity)<-[:DEPENDS_ON*1..4]-(x:Entity)<-[:CONTAINS]-(app:Entity)
WHERE s.tenant_id = '{T}' AND s.canonical_key = 'service:auth-service' AND app.type = 'application'
RETURN DISTINCT app.name AS value
UNION
MATCH (s:Entity)-[:EXPOSES]->(:Entity)<-[:DEPENDS_ON]-(x:Entity)<-[:CONTAINS]-(app:Entity)
WHERE s.tenant_id = '{T}' AND s.canonical_key = 'service:auth-service' AND app.type = 'application'
RETURN DISTINCT app.name AS value
UNION
MATCH (app:Entity)-[:CONTAINS]->(s:Entity)
WHERE s.tenant_id = '{T}' AND s.canonical_key = 'service:auth-service' AND app.type = 'application'
RETURN DISTINCT app.name AS value""",
        "expected": {"payments-platform", "claims-portal", "identity-platform"},
    },
    {
        "id": "Q7b", "question": "Customers affected if auth-service goes down (via deployments)",
        "cypher": f"""
MATCH (s:Entity)<-[:DEPENDS_ON*0..4]-(x:Entity)<-[:CONTAINS]-(app:Entity)<-[:DEPLOYS]-(d:Entity)
WHERE s.tenant_id = '{T}' AND s.canonical_key = 'service:auth-service' AND d.type = 'deployment'
RETURN DISTINCT d.customer AS value""",
        "expected": {"acme", "globex", "initech"},
    },
    {
        "id": "S1", "question": "Tenant isolation: the same Q2 pattern for tenant contoso must not return northwind data",
        "cypher": """
MATCH (a:Entity)-[:EXPOSES]->(api:Entity)
WHERE a.tenant_id = 'contoso' AND a.canonical_key = 'application:payments-platform'
RETURN DISTINCT api.name AS value""",
        "expected": set(),
    },
    {
        "id": "S2", "question": "Customer scope filter: Acme-only principal asking for ledger deployments",
        "cypher": f"""
MATCH (d:Entity)-[:DEPLOYS]->(a:Entity)
WHERE a.tenant_id = '{T}' AND a.canonical_key = 'application:ledger' AND (d.customer = '' OR d.customer IN ['acme'])
RETURN DISTINCT d.customer AS value""",
        "expected": {"acme"},
    },
    {
        "id": "X1", "question": "Cypher parameters ($tenant) — supported over HTTP?",
        "cypher": "MATCH (a:Entity) WHERE a.tenant_id = $tenant AND a.type = 'application' RETURN count(a) AS value",
        "params": {"tenant": T},
        "expected": {5},
        "probe": True,
    },
    {
        "id": "X2", "question": "Write query rejected? (CREATE)",
        "cypher": "CREATE (n:Entity {id: 'x'}) RETURN n",
        "expect_error": True,
        "probe": True,
    },
    {
        "id": "P2", "question": "Shortest path restricted to OWNS|CONTAINS|HAS (typed, bounded)",
        "cypher": f"""
MATCH p = shortestPath((c:Entity)-[:OWNS|CONTAINS|HAS*..4]->(r:Entity))
WHERE c.tenant_id = '{T}' AND c.canonical_key = 'customer:acme' AND r.canonical_key = 'repository:github/northwind/fraud-detector'
RETURN length(p) AS value, [n IN nodes(p) | n.name] AS names, [e IN relationships(p) | e.source_systems] AS sources""",
        "expected": {3},
        "probe": True,
    },
    {
        "id": "P1", "question": "Shortest path Acme -> fraud-detector repository (bounded to 4 hops), with edge provenance",
        "cypher": f"""
MATCH p = shortestPath((c:Entity)-[*..4]-(r:Entity))
WHERE c.tenant_id = '{T}' AND c.canonical_key = 'customer:acme' AND r.canonical_key = 'repository:github/northwind/fraud-detector'
RETURN length(p) AS value, [n IN nodes(p) | n.name] AS names, [e IN relationships(p) | e.source_systems] AS sources""",
        "expected": {3},
        "probe": True,
    },
]


def _hashable(v):
    return json.dumps(v, sort_keys=True) if isinstance(v, (list, dict)) else v


def run(url: str, auth: tuple[str, str], case: dict) -> dict:
    body: dict = {"query": case["cypher"].strip()}
    if case.get("params"):
        body["parameters"] = case["params"]
    start = time.perf_counter()
    resp = httpx.post(f"{url}/submitCypher", json=body, auth=auth, timeout=120)
    ms = round((time.perf_counter() - start) * 1000)
    out = {"id": case["id"], "question": case["question"], "http": resp.status_code, "ms": ms}
    try:
        data = resp.json()
    except ValueError:
        data = resp.text
    if resp.status_code != 200 or (isinstance(data, dict) and data.get("error")):
        out["error"] = (json.dumps(data) if not isinstance(data, str) else data)[:400]
        out["pass"] = bool(case.get("expect_error"))
        return out
    # /submitCypher returns a list of {"Keys": [...], "Values": [...]} rows.
    rows = [dict(zip(r.get("Keys", []), r.get("Values", []))) for r in data] if isinstance(data, list) else []
    out["rows"] = rows
    values = {_hashable(r.get("value")) for r in rows}
    out["values"] = sorted(map(str, values))
    if case.get("expect_error"):
        out["pass"] = False
    elif "expected" in case:
        out["pass"] = {str(v) for v in values} == {str(v) for v in case["expected"]}
    else:
        out["pass"] = case["expected_contains"] <= values
    return out


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--url", default="http://127.0.0.1:8081")
    ap.add_argument("--user", default="puppygraph")
    ap.add_argument("--password", default="poc-local-only-123")
    ap.add_argument("--json", help="write full results to this file")
    a = ap.parse_args()
    results = [run(a.url, (a.user, a.password), c) for c in CASES]
    for r in results:
        status = "PASS" if r["pass"] else "FAIL"
        detail = r.get("error") or ", ".join(r.get("values", []))
        print(f"{status} {r['id']:<4} {r['ms']:>6} ms  {r['question']}\n      -> {detail[:300]}")
    if a.json:
        with open(a.json, "w", encoding="utf-8") as f:
            json.dump(results, f, indent=2, default=str)
    core = [r for r, c in zip(results, CASES) if not c.get("probe")]
    print(f"\ncore use cases passed: {sum(r['pass'] for r in core)}/{len(core)}")
    sys.exit(0 if all(r["pass"] for r in core) else 1)


if __name__ == "__main__":
    main()

"""Bulk-generate a realistic, large synthetic enterprise into PostgreSQL (tenant "scale") for gate 1/2 benchmarks.

Writes directly into the canonical tables (entities, aliases, relationships, provenance), because
the benchmark measures the query layer, not ingestion. Identity keys come from the platform's own
``canonical_key`` so the data is indistinguishable from ingested data. Deterministic for a seed.

Defaults give ~100k entities / ~300k relationships:
    python poc/puppygraph/scale_data.py --db postgresql+psycopg://ontology:PW@127.0.0.1:5432/ontology
    python poc/puppygraph/scale_data.py --db ... --customers 50   # ~10k entities, quick smoke run
"""

from __future__ import annotations

import argparse
import random
import time
import uuid
from datetime import UTC, datetime

from sqlalchemy import create_engine, delete, insert, select

from ontology_platform.ontology.keys import canonical_key, customer_scope_for, normalize_name
from ontology_platform.ontology.schema import EntityType as E
from ontology_platform.storage.orm import Base, Entity, EntityAlias, GraphChange, GraphRevision, Provenance, Relationship, Tenant

TIERS = ["sit", "uat", "prod"]
NOW = datetime.now(UTC)


class Builder:
    def __init__(self, tenant: str, seed: int) -> None:
        self.tenant = tenant
        self.rng = random.Random(seed)
        self.entities: list[dict] = []
        self.aliases: list[dict] = []
        self.rels: list[dict] = []
        self.prov: list[dict] = []
        self.keys: set[tuple] = set()

    def _id(self) -> str:
        return uuid.UUID(int=self.rng.getrandbits(128)).hex

    def entity(self, etype: E, identity: dict, name: str, customer: str | None = None, props: dict | None = None, source: str = "synthetic") -> str:
        eid = self._id()
        scope = customer_scope_for(etype, identity, customer)  # same rule as ingestion
        self.entities.append(
            dict(id=eid, tenant_id=self.tenant, type=etype.value, canonical_key=canonical_key(etype, identity), name=name,
                 name_norm=normalize_name(name), description=None, identity=identity, properties=props or {}, customer_scope=scope,
                 status="active", version=1, created_at=NOW, updated_at=NOW)
        )
        self.aliases.append(dict(tenant_id=self.tenant, entity_id=eid, entity_type=etype.value, alias=name, alias_norm=normalize_name(name)))
        self._prov("entity", eid, source, f"{etype.value}:{name}")
        return eid

    def rel(self, rtype: str, src: str, tgt: str, confidence: float = 1.0, source: str = "synthetic") -> None:
        if (rtype, src, tgt) in self.keys or src == tgt:
            return
        self.keys.add((rtype, src, tgt))
        rid = self._id()
        self.rels.append(dict(id=rid, tenant_id=self.tenant, type=rtype, source_id=src, target_id=tgt, properties={},
                              confidence=confidence, status="active", version=1, created_at=NOW, updated_at=NOW))
        self._prov("relationship", rid, source, f"{rtype}:{src}->{tgt}")

    def _prov(self, kind: str, sid: str, source: str, ref: str) -> None:
        self.prov.append(dict(tenant_id=self.tenant, subject_kind=kind, subject_id=sid, connector_id="scale-generator",
                              source_system=f"{source} (synthetic)", source_record_key=ref[:500], source_url=None,
                              extractor="synthetic", confidence=1.0, evidence=None, last_run_id="scale",
                              first_observed_at=NOW, observed_at=NOW))


def build(b: Builder, customers: int, products: int, apps_per_product: int, services_per_app: int, clusters: int, owned: int) -> None:
    rng = b.rng
    org = b.entity(E.ORGANIZATION, {"name": "scale-org"}, "Scale Org")
    cluster_ids = [b.entity(E.KUBERNETES_CLUSTER, {"name": f"k8s-{i:03d}"}, f"k8s-{i:03d}") for i in range(clusters)]
    product_ids: dict[int, str] = {}
    product_apps: dict[int, list[tuple[str, str]]] = {}
    all_services: list[str] = []
    for p in range(products):
        pid = b.entity(E.PRODUCT, {"name": f"product-{p:03d}"}, f"product-{p:03d}")
        b.rel("owns", org, pid)
        product_ids[p] = pid
        product_apps[p] = []
        for a in range(apps_per_product):
            an = f"app-{p:03d}-{a:02d}"
            aid = b.entity(E.APPLICATION, {"name": an}, an)
            product_apps[p].append((aid, an))
            b.rel("contains", pid, aid)
            api = b.entity(E.API, {"name": f"{an}-api"}, f"{an}-api", props={"endpoints": [f"GET /{an}/items", f"POST /{an}/items"]})
            b.rel("exposes", aid, api)
            db = b.entity(E.DATABASE, {"name": f"{an}-db"}, f"{an}-db")
            b.rel("contains", aid, db)
            for d in range(3):
                doc = b.entity(E.DOCUMENTATION, {"source": "confluence", "external_id": f"{an}-{d}"}, f"{an} doc {d}", source="confluence")
                b.rel("describes", doc, aid, confidence=0.9, source="confluence")
            for s in range(services_per_app):
                sn = f"{an}-svc{s}"
                sid = b.entity(E.SERVICE, {"name": sn}, sn)
                b.rel("contains", aid, sid)
                repo = b.entity(E.REPOSITORY, {"provider": "github", "path": f"scale/{sn}"}, f"scale/{sn}", source="github")
                b.rel("has", aid, repo, source="github")
                b.rel("implements", repo, sid, source="github")
                if s == 0:
                    b.rel("exposes", sid, api)
                b.rel("depends_on", sid, db)
                # dependencies on up to two earlier services (DAG; mostly same product, some shared)
                for target in rng.sample(all_services[-40:], k=min(2, len(all_services[-40:]))):
                    b.rel("depends_on", sid, target)
                all_services.append(sid)
    for c in range(customers):
        cn = f"cust-{c:04d}"
        cid = b.entity(E.CUSTOMER, {"name": cn}, cn)
        b.rel("has_customer", org, cid)
        envs = {}
        for t in TIERS:
            envs[t] = b.entity(E.ENVIRONMENT, {"customer": cn, "tier": t}, f"{cn} {t.upper()}")
            b.rel("belongs_to", envs[t], cid)
        for p in rng.sample(range(products), k=min(owned, products)):
            b.rel("owns", cid, product_ids[p])
            for aid, an in product_apps[p]:
                for t in TIERS:
                    did = b.entity(E.DEPLOYMENT, {"application": an, "customer": cn, "tier": t}, f"{an} @ {cn} {t.upper()}",
                                   props={"namespace": f"{cn}-{t}", "tier": t}, source="kubernetes")
                    b.rel("deploys", did, aid, source="kubernetes")
                    b.rel("deployed_in", did, envs[t], source="kubernetes")
                    b.rel("deployed_in", aid, envs[t], source="kubernetes")
                    # prod: one cluster per customer (spread); non-prod: shared pools
                    cl = cluster_ids[c % clusters] if t == "prod" else cluster_ids[(c * 7 + TIERS.index(t)) % clusters]
                    b.rel("runs_on", did, cl, source="kubernetes")
                    res = b.entity(E.CLOUD_RESOURCE, {"provider": "aws", "resource_id": f"arn:aws:rds:us-east-1:1:db:{an}-{cn}-{t}"},
                                   f"{an}-{cn}-{t}-db", customer=cn, props={"resource_type": "aws:rds:db"}, source="aws")
                    b.rel("uses", did, res, source="aws")
                    b.rel("belongs_to", res, envs[t], source="aws")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", required=True, help="SQLAlchemy URL of the platform PostgreSQL")
    ap.add_argument("--tenant", default="scale")
    ap.add_argument("--customers", type=int, default=500)
    ap.add_argument("--products", type=int, default=20)
    ap.add_argument("--apps-per-product", type=int, default=10)
    ap.add_argument("--services-per-app", type=int, default=6)
    ap.add_argument("--clusters", type=int, default=30)
    ap.add_argument("--owned-products", type=int, default=3)
    ap.add_argument("--seed", type=int, default=42)
    a = ap.parse_args()

    t0 = time.perf_counter()
    b = Builder(a.tenant, a.seed)
    build(b, a.customers, a.products, a.apps_per_product, a.services_per_app, a.clusters, a.owned_products)
    print(f"generated {len(b.entities):,} entities, {len(b.rels):,} relationships, {len(b.prov):,} provenance rows in {time.perf_counter()-t0:.1f}s")

    engine = create_engine(a.db)
    Base.metadata.create_all(engine)  # no-op on a migrated database
    t1 = time.perf_counter()
    with engine.begin() as conn:
        # explicit child-first reset (does not rely on ON DELETE CASCADE being enforced)
        for table in (Provenance, GraphChange, EntityAlias, Relationship, Entity, GraphRevision):
            conn.execute(delete(table).where(table.tenant_id == a.tenant))
        conn.execute(delete(Tenant).where(Tenant.id == a.tenant))
        conn.execute(insert(Tenant), [dict(id=a.tenant, name="Scale benchmark", created_at=NOW)])
        for table, rows in ((Entity, b.entities), (EntityAlias, b.aliases), (Relationship, b.rels), (Provenance, b.prov)):
            for i in range(0, len(rows), 5000):
                conn.execute(insert(table), rows[i : i + 5000])
        conn.execute(insert(GraphRevision), [dict(tenant_id=a.tenant, revision=1)])
    # ANALYZE must commit: without fresh statistics after a bulk load the planner picks nested loops
    # (measured: 6 s instead of 0.2 s for alias loading).
    with engine.connect().execution_options(isolation_level="AUTOCOMMIT") as conn:
        n = conn.execute(select(Entity.id).where(Entity.tenant_id == a.tenant).limit(1)).first()
        conn.exec_driver_sql("ANALYZE")
    print(f"loaded into {engine.dialect.name} in {time.perf_counter()-t1:.1f}s (tenant '{a.tenant}', sample id {n[0] if n else None})")
    engine.dispose()


if __name__ == "__main__":
    main()

import asyncio

import pytest
import yaml
from sqlalchemy import func, select

from conftest import operator, sync
from ontology_platform import admin
from ontology_platform.connectors.base import SourceRecord
from ontology_platform.errors import NotFoundError, ValidationError
from ontology_platform.query.engine import QueryEngine
from ontology_platform.storage.orm import Entity, Relationship
from ontology_platform.storage.repository import GraphRepository, Scope


def catalog(external_id, entities=(), relationships=(), source_system="cmdb"):
    return SourceRecord.model_validate({
        "kind": "catalog", "external_id": external_id, "source_system": source_system,
        "payload": {"entities": list(entities), "relationships": list(relationships)},
    })


def svc(name):
    return {"type": "service", "name": name}


def dep(a, b):
    return {"type": "depends_on", "source": svc(a), "target": svc(b)}


def push(platform, tenant, connector_id, *records):
    return asyncio.run(platform.push_records(operator(tenant), connector_id, list(records)))


def hook(platform, tenant="t1", name="hook"):
    return admin.register_connector(platform.db, tenant, name, "push", {})["id"]


def repo_record(name, deps=(), repo="r1"):
    doc = {"kind": "Component", "metadata": {"name": name}, "spec": {"type": "service", "system": "app1", "dependsOn": [f"component:{d}" for d in deps]}}
    return {
        "kind": "repository", "external_id": f"github:org/{repo}", "source_system": "github", "updated_at": "2026-01-01T00:00:00+00:00",
        "payload": {"provider": "github", "path": f"org/{repo}", "name": repo, "files": {"catalog-info.yaml": yaml.safe_dump(doc)}},
    }


def entity(platform, key, tenant="t1"):
    with platform.db.session() as s:
        return s.scalars(select(Entity).where(Entity.tenant_id == tenant, Entity.canonical_key == key)).first()


def duplicate_rows(platform):
    with platform.db.session() as s:
        ents = s.execute(select(Entity.tenant_id, Entity.canonical_key).group_by(Entity.tenant_id, Entity.canonical_key).having(func.count() > 1)).all()
        rels = s.execute(
            select(Relationship.source_id, Relationship.type, Relationship.target_id)
            .group_by(Relationship.source_id, Relationship.type, Relationship.target_id)
            .having(func.count() > 1)
        ).all()
    return ents, rels


def engine_call(platform, tenant, fn):
    with platform.db.session() as s:
        return fn(QueryEngine(s, Scope(tenant), platform.settings))


def test_repeated_and_overlapping_ingestion_creates_no_duplicates(fresh, tmp_path):
    first, second = tmp_path / "a.yaml", tmp_path / "b.yaml"
    first.write_text(yaml.safe_dump({"records": [repo_record("alpha", deps=["beta"]), repo_record("beta", repo="r2")]}), encoding="utf-8")
    second.write_text(yaml.safe_dump({"records": [repo_record("alpha", deps=["beta"], repo="r3")]}), encoding="utf-8")
    a = admin.register_connector(fresh.db, "t1", "fx-a", "fixture", {"path": str(first)})["id"]
    b = admin.register_connector(fresh.db, "t1", "fx-b", "fixture", {"path": str(second)})["id"]
    for cid in (a, b, a, b):
        assert sync(fresh, "t1", cid)["status"] == "succeeded"
    assert duplicate_rows(fresh) == ([], [])
    with fresh.db.session() as s:
        alpha = entity(fresh, "service:alpha")
        deps = GraphRepository(s, Scope("t1")).list_relationships(source_id=alpha.id, rel_type="depends_on")
        assert len(deps) == 1


def test_alias_match_is_counted_as_resolved_duplicate(fresh):
    cid = hook(fresh)
    push(fresh, "t1", cid, catalog("c1", entities=[{"type": "service", "name": "payments-gateway", "aliases": ["paygw"]}]))
    result = push(fresh, "t1", cid, catalog("c2", entities=[{"type": "service", "name": "paygw", "properties": {"tier": "1"}}]))
    assert result["stats"]["duplicates_resolved"] == 1
    assert result["stats"]["entities_created"] == 0
    assert entity(fresh, "service:paygw") is None
    assert entity(fresh, "service:payments-gateway").properties["tier"] == "1"


def test_missing_endpoint_becomes_stub_then_promoted(fresh):
    cid = hook(fresh)
    result = push(fresh, "t1", cid, catalog("c1", entities=[svc("orders")], relationships=[dep("orders", "inventory")]))
    assert result["stats"]["relationships_created"] == 1
    assert entity(fresh, "service:inventory").status == "stub"
    push(fresh, "t1", cid, catalog("c2", entities=[svc("inventory")]))
    assert entity(fresh, "service:inventory").status == "active"


def test_invalid_ontology_relationship_is_rejected_and_counted(fresh):
    cid = hook(fresh)
    bad = {"type": "owns", "source": {"type": "service", "name": "orders"}, "target": {"type": "customer", "name": "acme"}}
    result = push(fresh, "t1", cid, catalog("c1", entities=[svc("orders")], relationships=[bad, dep("orders", "db-proxy")]))
    stats = result["stats"]
    assert stats["relationships_rejected"] == 1 and stats["relationships_created"] == 1
    assert any("violates ontology" in w for w in stats["warnings"])
    with fresh.db.session() as s:
        assert s.scalar(select(func.count()).select_from(Relationship).where(Relationship.type == "owns")) == 0
    assert entity(fresh, "customer:acme") is None


def test_conflicting_metadata_from_two_sources_is_merged_and_versioned(fresh):
    cmdb, catalog_hook = hook(fresh, name="cmdb"), hook(fresh, name="catalog")
    push(fresh, "t1", cmdb, catalog("c1", entities=[{**svc("billing"), "properties": {"tier": "gold", "owner": "team-a"}}], source_system="cmdb"))
    push(fresh, "t1", catalog_hook, catalog("c1", entities=[{**svc("billing"), "properties": {"tier": "silver", "language": "go"}}], source_system="backstage"))
    billing = entity(fresh, "service:billing")
    assert billing.properties == {"tier": "silver", "owner": "team-a", "language": "go"}
    with fresh.db.session() as s:
        repo = GraphRepository(s, Scope("t1"))
        tiers = [c.snapshot["properties"].get("tier") for c in repo.history("entity", billing.id)]
        assert tiers == ["gold", "silver"]
        assert [c.version for c in repo.history("entity", billing.id)] == [1, 2]
        systems = {p.source_system for p in repo.provenance_for("entity", [billing.id])[billing.id]}
        assert systems == {"cmdb", "backstage"}


def test_multi_hop_traversal_depth_three(fresh):
    cid = hook(fresh)
    push(fresh, "t1", cid, catalog("c1", entities=[svc(n) for n in ("hop-a", "hop-b", "hop-c", "hop-d", "hop-e")],
                                   relationships=[dep("hop-a", "hop-b"), dep("hop-b", "hop-c"), dep("hop-c", "hop-d"), dep("hop-d", "hop-e")]))
    result = engine_call(fresh, "t1", lambda q: q.get_related("hop-a", relation_types=["depends_on"], direction="out", depth=3))
    depths = {item["entity"]["name"]: item["depth"] for item in result["related"]}
    assert depths == {"hop-b": 1, "hop-c": 2, "hop-d": 3}


def test_circular_dependencies_terminate_and_list_each_entity_once(fresh):
    cid = hook(fresh)
    push(fresh, "t1", cid, catalog("c1", entities=[svc("cyc-a"), svc("cyc-b"), svc("cyc-c")],
                                   relationships=[dep("cyc-a", "cyc-b"), dep("cyc-b", "cyc-a"), dep("cyc-b", "cyc-c"), dep("cyc-c", "cyc-a")]))
    for direction in ("downstream", "upstream"):
        result = engine_call(fresh, "t1", lambda q: q.trace_dependencies("cyc-a", direction=direction, depth=10))
        names = [item["entity"]["name"] for item in result["items"]]
        assert sorted(names) == ["cyc-b", "cyc-c"]
    related = engine_call(fresh, "t1", lambda q: q.get_related("cyc-a", relation_types=["depends_on"], depth=5))
    names = [item["entity"]["name"] for item in related["related"]]
    assert sorted(names) == ["cyc-b", "cyc-c"]


def test_relationship_endpoints_must_exist_in_tenant(fresh):
    admin.create_tenant(fresh.db, "t2", "Tenant Two")
    push(fresh, "t1", hook(fresh), catalog("c1", entities=[svc("local")]))
    push(fresh, "t2", hook(fresh, tenant="t2"), catalog("c1", entities=[svc("foreign")]))
    local, foreign = entity(fresh, "service:local"), entity(fresh, "service:foreign", tenant="t2")
    with fresh.db.session() as s:
        repo = GraphRepository(s, Scope("t1"))
        for source_id, target_id in [(local.id, foreign.id), (foreign.id, local.id), (local.id, "0" * 32)]:
            with pytest.raises(ValidationError):
                repo.upsert_relationship(rel_type="depends_on", source_id=source_id, target_id=target_id, properties={}, confidence=1.0, run_id=None)
    with fresh.db.session() as s:
        assert s.scalar(select(func.count()).select_from(Relationship)) == 0


def test_repository_reads_are_tenant_scoped(fresh):
    admin.create_tenant(fresh.db, "t2", "Tenant Two")
    push(fresh, "t1", hook(fresh), catalog("c1", entities=[svc("shared-name"), svc("only-t1")], relationships=[dep("shared-name", "only-t1")]))
    push(fresh, "t2", hook(fresh, tenant="t2"), catalog("c1", entities=[svc("shared-name")]))
    t1_entity = entity(fresh, "service:only-t1")
    with fresh.db.session() as s:
        repo = GraphRepository(s, Scope("t2"))
        assert repo.find_by_key("service:only-t1") is None
        with pytest.raises(NotFoundError):
            repo.get_entity(t1_entity.id)
        assert repo.find_by_key("service:shared-name").tenant_id == "t2"
        assert repo.list_relationships() == []
        assert all(e.tenant_id == "t2" for e in repo.find_by_alias("service", "shared-name"))
        assert repo.neighbors([t1_entity.id]) == []

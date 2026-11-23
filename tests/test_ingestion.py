import asyncio

import pytest
import yaml

from conftest import operator, sync
from ontology_platform import admin
from ontology_platform.connectors.base import SourceRecord
from ontology_platform.errors import ConflictError, ValidationError
from ontology_platform.storage.orm import Entity, Relationship
from ontology_platform.storage.repository import GraphRepository, Scope
from sqlalchemy import select


def svc_record(name, deps=(), ts="2026-01-01T00:00:00+00:00", repo="r1"):
    catalog = {"kind": "Component", "metadata": {"name": name}, "spec": {"type": "service", "system": "app1", "dependsOn": [f"component:{d}" for d in deps]}}
    return {
        "kind": "repository", "external_id": f"github:org/{repo}", "source_system": "github", "updated_at": ts,
        "payload": {"provider": "github", "path": f"org/{repo}", "name": repo, "files": {"catalog-info.yaml": yaml.safe_dump(catalog)}},
    }


def write_fixture(path, records):
    path.write_text(yaml.safe_dump({"records": records}), encoding="utf-8")


@pytest.fixture
def fixture_connector(fresh, tmp_path):
    f = tmp_path / "data.yaml"
    write_fixture(f, [svc_record("alpha", deps=["beta"]), svc_record("beta", repo="r2")])
    c = admin.register_connector(fresh.db, "t1", "fx", "fixture", {"path": str(f)})
    return fresh, c["id"], f


def entity(svc, key):
    with svc.db.session() as s:
        return s.scalars(select(Entity).where(Entity.tenant_id == "t1", Entity.canonical_key == key)).first()


def test_full_sync_and_idempotent_rerun(fixture_connector):
    svc, cid, _ = fixture_connector
    first = sync(svc, "t1", cid)
    assert first["status"] == "succeeded"
    assert first["stats"]["records_processed"] == 2 and first["stats"]["relationships_created"] > 0
    second = sync(svc, "t1", cid)
    assert second["stats"]["records_unchanged"] == 2
    assert second["stats"]["entities_created"] == second["stats"]["relationships_created"] == 0
    assert second["stats"]["stale_entities"] == 0
    assert entity(svc, "service:alpha").status == "active"


def test_removed_record_becomes_stale_on_full_sync(fixture_connector):
    svc, cid, f = fixture_connector
    sync(svc, "t1", cid)
    write_fixture(f, [svc_record("alpha", deps=["beta"])])  # r2 repository disappears
    result = sync(svc, "t1", cid)
    assert result["stats"]["stale_entities"] >= 1
    assert entity(svc, "repository:github/org/r2").status == "stale"
    # beta is still referenced by alpha's dependsOn → it stays alive (as a reference)
    assert entity(svc, "service:beta").status != "stale"
    history = GraphRepository(svc.db._factory(), Scope("t1")).history("entity", entity(svc, "repository:github/org/r2").id)
    assert history[-1].change_type == "stale"


def test_changed_record_retracts_facts_it_no_longer_states(fixture_connector):
    svc, cid, f = fixture_connector
    sync(svc, "t1", cid)
    a, b = entity(svc, "service:alpha"), entity(svc, "service:beta")
    write_fixture(f, [svc_record("alpha", deps=[]), svc_record("beta", repo="r2")])
    result = sync(svc, "t1", cid, mode="incremental")  # same timestamps → incremental filters nothing new
    assert result["stats"]["records_seen"] == 0
    result = sync(svc, "t1", cid, mode="full")
    assert result["stats"]["records_processed"] == 1
    with svc.db.session() as s:
        rel = s.scalars(select(Relationship).where(Relationship.source_id == a.id, Relationship.target_id == b.id, Relationship.type == "depends_on")).one()
        assert rel.status == "stale" and rel.version == 2


def test_incremental_sync_uses_cursor(fixture_connector):
    svc, cid, f = fixture_connector
    sync(svc, "t1", cid, mode="incremental")
    connector = admin.list_connectors(svc.db, "t1")[0]
    assert connector["cursor"].startswith("2026-01-01")
    write_fixture(f, [svc_record("alpha", deps=["beta"]), svc_record("beta", repo="r2"), svc_record("gamma", ts="2026-02-01T00:00:00+00:00", repo="r3")])
    result = sync(svc, "t1", cid, mode="incremental")
    assert result["stats"]["records_seen"] == 1
    assert entity(svc, "service:gamma") is not None
    # incremental runs never mark unseen facts stale
    assert entity(svc, "service:alpha").status == "active"


def test_bad_record_is_isolated_and_keeps_previous_facts(fixture_connector):
    svc, cid, f = fixture_connector
    sync(svc, "t1", cid)
    bad = svc_record("beta", repo="r2")
    bad["payload"].pop("provider")  # invalid payload for the existing r2 record
    write_fixture(f, [svc_record("alpha", deps=["beta"]), bad])
    result = sync(svc, "t1", cid)
    assert result["status"] == "partial"
    assert result["stats"]["records_failed"] == 1
    assert entity(svc, "repository:github/org/r2").status == "active"  # not wiped by a parse error


def test_connector_failure_marks_run_failed_without_staling(fixture_connector):
    svc, cid, f = fixture_connector
    sync(svc, "t1", cid)
    f.unlink()  # source becomes unreachable
    result = sync(svc, "t1", cid)
    assert result["status"] == "failed" and "does not exist" in result["error"]
    assert entity(svc, "service:alpha").status == "active"
    runs = admin.list_runs(svc.db, "t1")
    assert runs[0]["status"] == "failed"


def test_concurrent_runs_are_refused(fixture_connector):
    svc, cid, _ = fixture_connector
    p = operator("t1")
    svc.start_sync(p, cid, "full")
    with pytest.raises(ConflictError):
        svc.start_sync(p, cid, "full")


def test_invalid_mode_rejected(fixture_connector):
    svc, cid, _ = fixture_connector
    with pytest.raises(ValidationError):
        svc.start_sync(operator("t1"), cid, "sideways")


def test_push_ingestion_and_ontology_violation_rejection(fresh):
    c = admin.register_connector(fresh.db, "t1", "hook", "push", {})
    records = [SourceRecord.model_validate({
        "kind": "catalog", "external_id": "catalog:push", "source_system": "cmdb",
        "payload": {"entities": [{"type": "customer", "name": "acme"}],
                    "relationships": [{"type": "owns", "source": {"type": "customer", "name": "acme"}, "target": {"type": "product", "name": "p1"}},
                                      {"type": "owns", "source": {"type": "product", "name": "p1"}, "target": {"type": "customer", "name": "acme"}}]},
    })]
    result = asyncio.run(fresh.push_records(operator("t1"), c["id"], records))
    assert result["status"] == "succeeded"
    assert result["stats"]["relationships_created"] == 1
    assert result["stats"]["relationships_rejected"] == 1
    p1 = entity(fresh, "product:p1")
    assert p1.status == "stub"  # referenced but never defined by its own source
    # a push connector cannot be "synced" (that would wipe pushed facts)
    assert sync(fresh, "t1", c["id"])["status"] == "failed"
    assert entity(fresh, "customer:acme").status == "active"


def test_provenance_recorded_for_entities_and_relationships(seeded):
    with seeded.db.session() as s:
        repo = GraphRepository(s, Scope("northwind"))
        e = repo.find_by_key("service:payment-processor")
        prov = repo.provenance_for("entity", [e.id])[e.id]
        systems = {p.source_system for p in prov}
        assert "github (fixture)" in systems and "kubernetes (fixture)" in systems
        rel = repo.list_relationships(source_id=e.id, rel_type="depends_on")
        rprov = repo.provenance_for("relationship", [r.id for r in rel])
        assert all(rprov[r.id] for r in rel)
        assert all(p.evidence for r in rel for p in rprov[r.id])

import asyncio

from conftest import SAMPLE, new_service, operator
from ontology_platform import admin
from ontology_platform.connectors.base import SourceRecord
from ontology_platform.ingestion.runner import effective_concurrency, sync_connectors


def setup(svc, tmp_path):
    admin.create_tenant(svc.db, "northwind", "Northwind")
    ids = {}
    for name, ctype, path in [
        ("crm", "catalog_file", SAMPLE / "northwind" / "catalog"),
        ("fixtures", "fixture", SAMPLE / "northwind" / "fixtures"),
        ("broken", "fixture", tmp_path / "missing"),
    ]:
        ids[name] = admin.register_connector(svc.db, "northwind", name, ctype, {"path": str(path)})["id"]
    return ids


def test_failing_connector_does_not_stop_the_others(tmp_path):
    svc = new_service()
    ids = setup(svc, tmp_path)
    report = asyncio.run(sync_connectors(svc, operator("northwind"), list(ids.values()), "full", concurrency=3))
    assert report["concurrency"] == 1  # in-memory SQLite cannot run syncs concurrently
    by_name = {r["connector"]: r for r in report["results"]}
    assert by_name["broken"]["status"] == "failed" and "does not exist" in by_name["broken"]["error"]
    assert by_name["crm"]["status"] == by_name["fixtures"]["status"] == "succeeded"
    totals = report["totals"]
    assert (totals["connectors"], totals["succeeded"], totals["failed"]) == (3, 2, 1)
    ok = [by_name["crm"]["stats"], by_name["fixtures"]["stats"]]
    for key in ("records_discovered", "entities_created", "relationships_created", "duplicates_resolved"):
        assert totals[key] == sum(s[key] for s in ok)
    assert totals["records_discovered"] > 0 and totals["entities_created"] > 0
    for stats in ok:
        assert stats["records_discovered"] == stats["records_seen"]
        assert isinstance(stats["source_errors"], list) and stats["source_error_count"] == len(stats["source_errors"])


def test_start_failures_are_isolated(tmp_path):
    svc = new_service()
    ids = setup(svc, tmp_path)
    p = operator("northwind")
    svc.start_sync(p, ids["crm"], "full")  # leaves a running run → second start is refused
    report = asyncio.run(sync_connectors(svc, p, [ids["crm"], ids["fixtures"]], "full", concurrency=2))
    by_name = {r["connector"]: r for r in report["results"]}
    assert by_name["crm"]["status"] == "failed" and by_name["crm"]["run_id"] is None
    assert by_name["fixtures"]["status"] == "succeeded"


def test_concurrency_bound_when_database_supports_it(tmp_path):
    svc = new_service()
    assert effective_concurrency(svc.db, 4) == 1
    svc.db.supports_concurrency = True
    assert effective_concurrency(svc.db, 4) == 4
    assert effective_concurrency(svc.db, 0) == 1


def test_bounded_parallel_runs_overlap_and_all_complete(tmp_path):
    svc = new_service()
    ids = setup(svc, tmp_path)
    svc.db.supports_concurrency = True
    active, peak = 0, 0
    original = svc.execute_sync

    async def tracked(p, connector_id, run_id, mode):
        nonlocal active, peak
        active += 1
        peak = max(peak, active)
        await asyncio.sleep(0.01)
        try:
            return await original(p, connector_id, run_id, mode)
        finally:
            active -= 1

    svc.execute_sync = tracked
    report = asyncio.run(sync_connectors(svc, operator("northwind"), list(ids.values()), "full", concurrency=2))
    assert peak == 2
    assert report["totals"]["succeeded"] == 2 and report["totals"]["failed"] == 1


def test_records_with_errors_are_reported_as_source_errors(fresh):
    cid = admin.register_connector(fresh.db, "t1", "hook", "push", {})["id"]
    good = SourceRecord.model_validate({"kind": "catalog", "external_id": "ok", "source_system": "cmdb", "payload": {"entities": [{"type": "service", "name": "a"}]}})
    bad = SourceRecord.model_validate({"kind": "repository", "external_id": "bad", "source_system": "github", "payload": {"name": "x"}})
    result = asyncio.run(fresh.push_records(operator("t1"), cid, [good, bad]))
    stats = result["stats"]
    assert result["status"] == "partial"
    assert stats["records_discovered"] == 2 and stats["records_failed"] == 1
    assert stats["source_error_count"] == 1 and stats["source_errors"][0].startswith("repository:bad")

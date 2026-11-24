"""Controlled parallel ingestion: sync several connectors of one tenant with bounded concurrency."""

from __future__ import annotations

import asyncio
from collections.abc import Sequence
from typing import Any, Protocol

from ontology_platform import admin
from ontology_platform.auth.principal import Principal
from ontology_platform.errors import PlatformError
from ontology_platform.observability import get_logger
from ontology_platform.storage.db import Database

log = get_logger(__name__)


class SyncService(Protocol):
    db: Database

    def start_sync(self, p: Principal, connector_id: str, mode: str = ..., channel: Any = ...) -> str: ...

    async def execute_sync(self, p: Principal, connector_id: str, run_id: str, mode: str) -> dict[str, Any]: ...


def effective_concurrency(db: Database, requested: int) -> int:
    """In-memory SQLite shares one connection across threads, so its runs are serialized."""
    if not getattr(db, "supports_concurrency", True):
        return 1
    return max(1, requested)


async def sync_connectors(
    service: SyncService,
    principal: Principal,
    connector_ids: Sequence[str],
    mode: str = "full",
    concurrency: int = 1,
    channel: str = "cli",
) -> dict[str, Any]:
    """Run one sync per connector; a failing connector never stops the others."""
    limit = effective_concurrency(service.db, concurrency)
    names = {c["id"]: c["name"] for c in admin.list_connectors(service.db, principal.tenant_id)}
    semaphore = asyncio.Semaphore(limit)

    async def one(connector_id: str) -> dict[str, Any]:
        base = {"connector_id": connector_id, "connector": names.get(connector_id)}
        async with semaphore:
            try:
                run_id = service.start_sync(principal, connector_id, mode, channel=channel)
            except PlatformError as exc:
                log.warning("sync not started", extra={"connector": connector_id, "error": str(exc)})
                return {**base, "run_id": None, "status": "failed", "error": f"{type(exc).__name__}: {exc}", "stats": {}}
            try:
                result = await service.execute_sync(principal, connector_id, run_id, mode)
            except Exception as exc:  # isolate this connector from the rest of the batch
                log.error("sync crashed", extra={"connector": connector_id, "run_id": run_id, "error": str(exc)})
                return {**base, "run_id": run_id, "status": "failed", "error": f"{type(exc).__name__}: {exc}", "stats": {}}
            return {**base, **result}

    results = list(await asyncio.gather(*(one(cid) for cid in connector_ids)))
    return {"tenant": principal.tenant_id, "mode": mode, "concurrency": limit, "results": results, "totals": _totals(results)}


def _totals(results: list[dict[str, Any]]) -> dict[str, Any]:
    totals: dict[str, Any] = {"connectors": len(results), "succeeded": 0, "partial": 0, "failed": 0}
    for r in results:
        totals[r["status"]] = totals.get(r["status"], 0) + 1
        for key, value in (r.get("stats") or {}).items():
            if isinstance(value, int) and not isinstance(value, bool):
                totals[key] = totals.get(key, 0) + value
    return totals


__all__ = ["SyncService", "effective_concurrency", "sync_connectors"]

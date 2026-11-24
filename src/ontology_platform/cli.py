"""Command-line interface: ``ontology-platform <command>`` (or ``python -m ontology_platform``)."""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from pathlib import Path
from typing import Any

from ontology_platform import admin
from ontology_platform.auth.principal import Principal
from ontology_platform.config import get_settings
from ontology_platform.errors import ConflictError, PlatformError
from ontology_platform.ingestion.runner import sync_connectors
from ontology_platform.observability import configure_logging
from ontology_platform.service import PlatformService
from ontology_platform.storage.db import Database

MIGRATIONS_DIR = Path(__file__).parent / "migrations"


def _print(data: Any) -> None:
    print(json.dumps(data, indent=2, default=str))


def _service() -> PlatformService:
    settings = get_settings()
    return PlatformService(Database.from_settings(settings), settings)


def _operator(tenant: str) -> Principal:
    """The CLI runs with direct database access, i.e. as a trusted operator (admin) of a tenant."""
    return Principal(subject=f"cli:{os.environ.get('USERNAME') or os.environ.get('USER') or 'operator'}", tenant_id=tenant, roles=frozenset({"admin"}), auth_method="cli")


def migrate(database_url: str | None = None) -> None:
    from alembic import command
    from alembic.config import Config

    cfg = Config()
    cfg.set_main_option("script_location", str(MIGRATIONS_DIR))
    cfg.set_main_option("sqlalchemy.url", (database_url or get_settings().database_url).replace("%", "%%"))
    command.upgrade(cfg, "head")


def wait_for_database(url: str, timeout: float) -> None:
    """Block until the database accepts connections (containers start before Postgres is ready)."""
    import time

    from sqlalchemy import create_engine, text

    deadline = time.monotonic() + timeout
    engine = create_engine(url)
    try:
        while True:
            try:
                with engine.connect() as conn:
                    conn.execute(text("SELECT 1"))
                return
            except Exception as exc:  # driver-specific connection errors
                if time.monotonic() >= deadline:
                    raise SystemExit(f"database not reachable after {timeout:.0f}s: {exc}") from exc
                time.sleep(2)
    finally:
        engine.dispose()


def cmd_init_db(a: argparse.Namespace) -> None:
    if a.wait:
        wait_for_database(get_settings().database_url, a.wait)
    migrate()
    _print({"status": "migrated", "database": get_settings().database_url.split("@")[-1]})


def cmd_create_tenant(a: argparse.Namespace) -> None:
    _print(admin.create_tenant(_service().db, a.tenant, a.name))


def cmd_create_api_key(a: argparse.Namespace) -> None:
    svc = _service()
    customers = [c.strip() for c in a.customers.split(",")] if a.customers else None
    plaintext, meta = svc.auth.create_api_key(a.tenant, a.name, [r.strip() for r in a.roles.split(",")], customers)
    _print({**meta, "api_key": plaintext, "note": "store this key now; it cannot be shown again"})


def cmd_add_connector(a: argparse.Namespace) -> None:
    svc = _service()
    config = json.loads(a.config) if a.config else {}
    _print(svc.register_connector(_operator(a.tenant), a.name, a.type, config, a.secret_ref, channel="cli"))


def cmd_list_connectors(a: argparse.Namespace) -> None:
    _print(admin.list_connectors(_service().db, a.tenant))


async def _sync(svc: PlatformService, tenant: str, connector_name: str, mode: str) -> dict[str, Any]:
    p = _operator(tenant)
    connector = admin.get_connector_by_name(svc.db, tenant, connector_name)
    run_id = svc.start_sync(p, connector["id"], mode, channel="cli")
    return await svc.execute_sync(p, connector["id"], run_id, mode)


def cmd_sync(a: argparse.Namespace) -> None:
    svc = _service()
    names = [c["name"] for c in admin.list_connectors(svc.db, a.tenant) if c["enabled"] and c["type"] != "push"] if a.all else [a.connector]
    if a.parallel <= 1:
        results = [asyncio.run(_sync(svc, a.tenant, n, a.mode)) for n in names]
        _print(results)
    else:
        ids = [admin.get_connector_by_name(svc.db, a.tenant, n)["id"] for n in names]
        report = asyncio.run(sync_connectors(svc, _operator(a.tenant), ids, a.mode, a.parallel, channel="cli"))
        _print(report)
        results = report["results"]
    if any(r["status"] == "failed" for r in results):
        sys.exit(1)


def cmd_ask(a: argparse.Namespace) -> None:
    _print(_service().ask(_operator(a.tenant), a.question, channel="cli"))


def cmd_seed_demo(a: argparse.Namespace) -> None:
    """Load the bundled sample dataset into tenants 'northwind' and 'contoso' and create API keys."""
    migrate()
    svc = _service()
    root = Path(a.data_dir).resolve()
    plan = {
        "northwind": [("crm-catalog", "catalog_file", {"path": str(root / "northwind" / "catalog")}),
                      ("sample-fixtures", "fixture", {"path": str(root / "northwind" / "fixtures")})],
        "contoso": [("sample-fixtures", "fixture", {"path": str(root / "contoso" / "fixtures")})],
    }
    out: dict[str, Any] = {}
    for tenant, connectors in plan.items():
        try:
            admin.create_tenant(svc.db, tenant, tenant.title())
        except ConflictError:
            pass
        runs = []
        for name, ctype, config in connectors:
            try:
                svc.register_connector(_operator(tenant), name, ctype, config, None, channel="cli")
            except ConflictError:
                pass
            runs.append(asyncio.run(_sync(svc, tenant, name, "full")))
        key, _ = svc.auth.create_api_key(tenant, "demo-admin", ["admin"])
        vkey, _ = svc.auth.create_api_key(tenant, "demo-viewer", ["viewer"])
        out[tenant] = {"runs": [{"status": r["status"], "stats": {k: v for k, v in r["stats"].items() if k != "warnings"}} for r in runs],
                       "admin_api_key": key, "viewer_api_key": vkey}
    if "northwind" in out:
        rkey, _ = svc.auth.create_api_key("northwind", "acme-only-viewer", ["viewer"], ["acme"])
        out["northwind"]["acme_restricted_viewer_api_key"] = rkey
    _print(out)


def cmd_serve(a: argparse.Namespace) -> None:
    import uvicorn

    from ontology_platform.api.app import create_app

    uvicorn.run(create_app(), host=a.host, port=a.port, log_config=None)


def cmd_mcp(a: argparse.Namespace) -> None:
    from ontology_platform.mcp_server.server import build_mcp_server

    settings = get_settings()
    configure_logging(settings.log_level, settings.log_json)
    svc = PlatformService(Database.from_settings(settings), settings)
    if a.transport == "stdio":
        if settings.mcp_api_key is None:
            sys.exit("OP_MCP_API_KEY must be set for the stdio transport")
        principal = svc.auth.authenticate(settings.mcp_api_key.get_secret_value())
        build_mcp_server(svc, default_principal=principal).run("stdio")
    else:
        from mcp.server.transport_security import TransportSecuritySettings

        hosts = [h.strip() for h in settings.mcp_allowed_hosts.split(",") if h.strip()]
        security = TransportSecuritySettings(allowed_hosts=hosts, allowed_origins=[f"http://{h}" for h in hosts]) if hosts else None
        # No default principal: every HTTP request must authenticate itself.
        build_mcp_server(svc).run("streamable-http", host=a.host, port=a.port, stateless_http=True, transport_security=security)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="ontology-platform", description="Enterprise Ontology Platform")
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("init-db", help="Apply database migrations")
    p.add_argument("--wait", type=float, default=0, help="seconds to wait for the database to accept connections")
    p.set_defaults(fn=cmd_init_db)

    p = sub.add_parser("create-tenant")
    p.add_argument("tenant")
    p.add_argument("name")
    p.set_defaults(fn=cmd_create_tenant)

    p = sub.add_parser("create-api-key")
    p.add_argument("--tenant", required=True)
    p.add_argument("--name", required=True)
    p.add_argument("--roles", default="viewer", help="comma-separated: viewer,ingestor,admin")
    p.add_argument("--customers", help="comma-separated customer restriction (omit for all)")
    p.set_defaults(fn=cmd_create_api_key)

    p = sub.add_parser("add-connector")
    p.add_argument("--tenant", required=True)
    p.add_argument("--name", required=True)
    p.add_argument("--type", required=True)
    p.add_argument("--config", help="JSON config")
    p.add_argument("--secret-ref", help="env:VAR or file:/path")
    p.set_defaults(fn=cmd_add_connector)

    p = sub.add_parser("list-connectors")
    p.add_argument("--tenant", required=True)
    p.set_defaults(fn=cmd_list_connectors)

    p = sub.add_parser("sync", help="Run ingestion (use from cron / Kubernetes CronJob)")
    p.add_argument("--tenant", required=True)
    group = p.add_mutually_exclusive_group(required=True)
    group.add_argument("--connector")
    group.add_argument("--all", action="store_true")
    p.add_argument("--mode", choices=["full", "incremental"], default="full")
    p.add_argument("--parallel", type=int, default=1, help="max connectors synced concurrently (default 1: sequential)")
    p.set_defaults(fn=cmd_sync)

    p = sub.add_parser("ask")
    p.add_argument("--tenant", required=True)
    p.add_argument("question")
    p.set_defaults(fn=cmd_ask)

    p = sub.add_parser("seed-demo", help="Load sample dataset and print demo API keys")
    p.add_argument("--data-dir", default="sample_data")
    p.set_defaults(fn=cmd_seed_demo)

    p = sub.add_parser("serve", help="Run the REST API")
    p.add_argument("--host", default="127.0.0.1")
    p.add_argument("--port", type=int, default=8000)
    p.set_defaults(fn=cmd_serve)

    p = sub.add_parser("mcp", help="Run the MCP server")
    p.add_argument("--transport", choices=["stdio", "http"], default="stdio")
    p.add_argument("--host", default="127.0.0.1")
    p.add_argument("--port", type=int, default=8001)
    p.set_defaults(fn=cmd_mcp)
    return parser


def main(argv: list[str] | None = None) -> None:
    args = build_parser().parse_args(argv)
    settings = get_settings()
    if args.command not in ("mcp", "serve"):
        configure_logging("WARNING", settings.log_json)
    try:
        args.fn(args)
    except PlatformError as exc:
        print(json.dumps({"error": exc.code, "message": str(exc)}), file=sys.stderr)
        sys.exit(2)


if __name__ == "__main__":
    main()

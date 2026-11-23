"""Set up the PuppyGraph PoC against a running Podman Compose stack.

1. apply poc/puppygraph/views.sql to PostgreSQL (via `podman exec` into the db container)
2. generate the graph schema from the ontology (generic or typed model)
3. upload it to PuppyGraph (POST /schema) and wait until Cypher answers

    python poc/puppygraph/setup_poc.py [--model generic|typed] [--db-container ontology-platform_db_1]
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from pathlib import Path

import httpx

HERE = Path(__file__).parent
sys.path.insert(0, str(HERE))
from generate_schema import build  # noqa: E402


def psql(container: str, args: list[str], stdin: str | None = None) -> str:
    cmd = ["podman", "exec", "-i", container, "psql", "-v", "ON_ERROR_STOP=1", "-U", os.environ.get("POSTGRES_USER", "ontology"),
           "-d", os.environ.get("POSTGRES_DB", "ontology"), *args]
    return subprocess.run(cmd, input=stdin, capture_output=True, text=True, check=True).stdout


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--model", choices=["generic", "typed"], default="generic")
    ap.add_argument("--db-container", default="ontology-platform_db_1")
    ap.add_argument("--url", default="http://127.0.0.1:8081")
    ap.add_argument("--user", default=os.environ.get("PUPPYGRAPH_USERNAME", "puppygraph"))
    ap.add_argument("--password", default=os.environ.get("PUPPYGRAPH_PASSWORD"), help="defaults to $PUPPYGRAPH_PASSWORD")
    a = ap.parse_args()

    ro_password = os.environ.get("PUPPYGRAPH_PG_PASSWORD")
    if not ro_password or not a.password:
        sys.exit("set PUPPYGRAPH_PASSWORD and PUPPYGRAPH_PG_PASSWORD (see .env.example)")
    psql(a.db_container, ["-v", f"ro_password={ro_password}"], (HERE / "views.sql").read_text(encoding="utf-8"))
    print("views applied")
    rows = psql(a.db_container, ["-At", "-c", "select distinct type, source_type, target_type from graph.relationship_v"])
    pairs = {tuple(line.split("|")) for line in rows.splitlines() if line.strip()}
    schema = build(pairs, a.model)
    (HERE / f"schema_{a.model}.json").write_text(json.dumps(schema, indent=2), encoding="utf-8")

    auth = (a.user, a.password)
    for _ in range(60):
        try:
            if httpx.get(f"{a.url}/readyz", timeout=5).status_code == 200:
                break
        except httpx.HTTPError:
            pass
        time.sleep(5)
    resp = httpx.post(f"{a.url}/schema", params={"postUploadBehavior": "none"}, json=schema, auth=auth, timeout=300)
    body = resp.json()
    if resp.status_code != 200 or not body.get("ok", False):
        sys.exit(f"schema upload failed ({resp.status_code}): {json.dumps(body)[:800]}")
    print(f"schema uploaded: {len(schema['node'])} node mappings, {len(schema['edge'])} edge mappings ({a.model} model)")
    for _ in range(30):
        r = httpx.post(f"{a.url}/submitCypher", json={"query": "MATCH (n) RETURN count(n) AS c"}, auth=auth, timeout=120)
        if r.status_code == 200 and isinstance(r.json(), list):
            print("cypher ready:", r.json())
            return
        time.sleep(5)
    sys.exit("cypher engine did not become available")


if __name__ == "__main__":
    main()

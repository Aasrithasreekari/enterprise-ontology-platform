"""Generate the PuppyGraph v1 graph schema from the platform ontology.

Every node label maps to ``graph.entity_v`` filtered by ``type``; every edge maps to
``graph.relationship_v`` filtered by (type, source_type, target_type). Because PuppyGraph edges
have a fixed from/to label, one ontology relationship (e.g. depends_on) becomes several edge
mappings. Only combinations allowed by the ontology are emitted.

    python poc/puppygraph/generate_schema.py > poc/puppygraph/schema.json
Env: PG_JDBC_URI, PG_USER, PG_PASSWORD (defaults target the Podman compose network).
"""

from __future__ import annotations

import json
import os
import sys

from ontology_platform.ontology.schema import ENTITY_TYPES, RELATION_TYPES

CATALOG = "ontology_pg"

NODE_FIELDS = [
    ("id", "STRING"), ("tenant_id", "STRING"), ("canonical_key", "STRING"), ("name", "STRING"),
    ("description", "STRING"), ("status", "STRING"), ("version", "INT"), ("customer", "STRING"),
    ("environment_tier", "STRING"), ("application_name", "STRING"), ("url", "STRING"), ("namespace", "STRING"),
    ("resource_type", "STRING"), ("source_systems", "STRING"), ("source_refs", "STRING"),
]
EDGE_FIELDS = [
    ("id", "STRING"), ("source_id", "STRING"), ("target_id", "STRING"), ("tenant_id", "STRING"),
    ("confidence", "DOUBLE"), ("status", "STRING"), ("version", "INT"), ("customer", "STRING"),
    ("source_systems", "STRING"), ("source_urls", "STRING"), ("evidence", "STRING"),
]


def label(entity_type: str) -> str:
    return "".join(part.capitalize() for part in entity_type.split("_"))  # kubernetes_cluster → KubernetesCluster


def source(table: str, where: str, fields: list[tuple[str, str]]) -> dict:
    return {
        "externalDataSource": {
            "enabled": True,
            "catalog": CATALOG,
            "schema": "graph",
            "table": table,
            "whereClause": where,
            "mappedField": [{"sourceFieldName": f, "targetFieldName": f} for f, _ in fields],
        }
    }


def build_generic() -> tuple[list[dict], list[dict]]:
    """Model B: one ``Entity`` node label (type as a property) and one edge label per relationship type."""
    nodes = [
        {
            "label": "Entity",
            "dataSourceGroup": source("entity_v", "status <> 'stale'", NODE_FIELDS),
            "id": [{"name": "id", "type": "STRING"}],
            "attribute": [{"name": f, "type": ty} for f, ty in NODE_FIELDS if f != "id"] + [{"name": "type", "type": "STRING"}],
        }
    ]
    nodes[0]["dataSourceGroup"]["externalDataSource"]["mappedField"].append({"sourceFieldName": "type", "targetFieldName": "type"})
    edges = [
        {
            "label": rel.value.upper(),
            "fromNodeLabel": "Entity",
            "toNodeLabel": "Entity",
            "dataSourceGroup": source("relationship_v", f"type = '{rel.value}' AND status <> 'stale'", EDGE_FIELDS),
            "id": [{"name": "id", "type": "STRING"}],
            "fromKey": [{"name": "source_id", "type": "STRING"}],
            "toKey": [{"name": "target_id", "type": "STRING"}],
            "attribute": [{"name": f, "type": ty} for f, ty in EDGE_FIELDS if f != "id"],
        }
        for rel in RELATION_TYPES
    ]
    return nodes, edges


def build(pairs: set[tuple[str, str, str]] | None = None, mode: str = "generic") -> dict:
    if mode == "generic":
        nodes, edges = build_generic()
        return {"catalog": _catalog(), "node": nodes, "edge": edges}
    nodes = [
        {
            "label": label(t.value),
            "dataSourceGroup": source("entity_v", f"type = '{t.value}'", NODE_FIELDS),
            "id": [{"name": "id", "type": "STRING"}],
            "attribute": [{"name": f, "type": ty} for f, ty in NODE_FIELDS if f != "id"],
        }
        for t in ENTITY_TYPES
    ]
    edges = []
    for rel, spec in RELATION_TYPES.items():
        for src, tgt in sorted(spec.allowed):
            if pairs is not None and (rel.value, src.value, tgt.value) not in pairs:
                continue
            where = f"type = '{rel.value}' AND source_type = '{src.value}' AND target_type = '{tgt.value}'"
            edges.append(
                {
                    # Model A: PuppyGraph requires unique edge labels, so the label encodes the endpoint pair.
                    "label": f"{rel.value.upper()}__{src.value.upper()}__{tgt.value.upper()}",
                    "fromNodeLabel": label(src.value),
                    "toNodeLabel": label(tgt.value),
                    "dataSourceGroup": source("relationship_v", where, EDGE_FIELDS),
                    "id": [{"name": "id", "type": "STRING"}],
                    "fromKey": [{"name": "source_id", "type": "STRING"}],
                    "toKey": [{"name": "target_id", "type": "STRING"}],
                    "attribute": [{"name": f, "type": ty} for f, ty in EDGE_FIELDS if f not in ("id",)],
                }
            )
    return {"catalog": _catalog(), "node": nodes, "edge": edges}


def _catalog() -> list[dict]:
    return [
            {
                "name": CATALOG,
                "type": "postgresql",
                "jdbc": {
                    "username": os.environ.get("PG_USER", "puppygraph_ro"),
                    "password": os.environ.get("PG_PASSWORD", "poc-readonly-123"),
                    "jdbcUri": os.environ.get("PG_JDBC_URI", "jdbc:postgresql://db:5432/ontology"),
                },
            }
        ]


if __name__ == "__main__":
    # Optional: "type,source_type,target_type" lines on stdin restrict edges to combinations present in data.
    pairs = None
    if not sys.stdin.isatty():
        lines = [l.strip() for l in sys.stdin if l.strip()]
        pairs = {tuple(l.split("|")) for l in lines} if lines else None
    mode = sys.argv[1] if len(sys.argv) > 1 else "generic"
    json.dump(build(pairs, mode), sys.stdout, indent=2)

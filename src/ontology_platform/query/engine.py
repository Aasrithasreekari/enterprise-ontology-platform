"""Query & retrieval engine: graph operations with evidence, plus hybrid search.

All reads go through a ``GraphRepository`` bound to the caller's ``Scope`` so tenant isolation
and customer visibility apply to every traversal step.
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field
from typing import Any, Literal

from sqlalchemy.orm import Session

from ontology_platform.config import Settings
from ontology_platform.errors import NotFoundError, ValidationError
from ontology_platform.ontology.keys import canonical_key, normalize_name
from ontology_platform.ontology.schema import ENTITY_TYPES, EntityType, RelationType, normalize_tier
from ontology_platform.search.bm25 import BM25Index, IndexCache
from ontology_platform.storage.orm import Entity, Relationship
from ontology_platform.storage.repository import Direction, GraphRepository, Neighbor, Scope

E, R = EntityType, RelationType

_DEFAULT_CACHE = IndexCache()
_NAME_KEYED = {t for t, spec in ENTITY_TYPES.items() if spec.identity_fields == ("name",)}


# ----------------------------------------------------------------------------- serialization
def entity_summary(e: Entity) -> dict[str, Any]:
    return {"id": e.id, "type": e.type, "name": e.name, "key": e.canonical_key, "status": e.status}


def entity_full(e: Entity) -> dict[str, Any]:
    return {
        **entity_summary(e),
        "description": e.description,
        "identity": e.identity,
        "properties": e.properties,
        "customer_scope": e.customer_scope,
        "version": e.version,
        "updated_at": e.updated_at.isoformat() if e.updated_at else None,
    }


@dataclass
class PathStep:
    relationship: Relationship
    source: Entity
    target: Entity


@dataclass
class GraphPath:
    nodes: list[Entity]
    steps: list[PathStep] = field(default_factory=list)

    @property
    def end(self) -> Entity:
        return self.nodes[-1]

    def extend(self, nb: Neighbor) -> "GraphPath":
        rel = nb.relationship
        src, tgt = (self.end, nb.entity) if nb.direction == "out" else (nb.entity, self.end)
        return GraphPath(self.nodes + [nb.entity], self.steps + [PathStep(rel, src, tgt)])


@dataclass(frozen=True)
class Step:
    relations: tuple[str, ...]
    direction: Direction
    target_types: tuple[str, ...] = ()


class QueryEngine:
    def __init__(self, session: Session, scope: Scope, settings: Settings, cache: IndexCache | None = None) -> None:
        self.repo = GraphRepository(session, scope)
        self.scope = scope
        self.settings = settings
        self.cache = cache or _DEFAULT_CACHE

    # ================================================================== resolution of user input
    def resolve(self, ref: str, types: list[str] | None = None) -> Entity:
        """Resolve an entity id, canonical key, or name/alias (optionally restricted by type)."""
        ref = (ref or "").strip()
        if not ref:
            raise ValidationError("entity reference is empty")
        if len(ref) == 32 and ref.isalnum():
            try:
                return self.repo.get_entity(ref)
            except NotFoundError:
                pass
        if ":" in ref:
            e = self.repo.find_by_key(ref)
            if e is not None:
                return e
            prefix, _, rest = ref.partition(":")
            if prefix in {t.value for t in EntityType} and rest:
                # "type:name" typed by a user/agent: normalize like stored keys and restrict the type.
                if types and prefix not in types:
                    raise ValidationError(f"expected one of {types}, got '{prefix}'")
                types, ref = [prefix], rest
                if EntityType(prefix) in _NAME_KEYED:
                    e = self.repo.find_by_key(canonical_key(EntityType(prefix), {"name": rest}))
                    if e is not None:
                        return e
        norm = normalize_name(ref)
        candidates: dict[str, Entity] = {}
        for etype in types or [t.value for t in EntityType]:
            for e in self.repo.find_by_alias(etype, norm):
                candidates[e.id] = e
        if not candidates:
            for e in self.repo.find_entities(types=types, name_norms=[norm], include_stale=True):
                candidates[e.id] = e
        live = [e for e in candidates.values() if e.status != "stale"] or list(candidates.values())
        if len(live) == 1:
            return live[0]
        if not live:
            raise NotFoundError(f"no entity matches '{ref}'" + (f" (types {types})" if types else ""))
        options = ", ".join(f"{e.type}:{e.name} ({e.id})" for e in live[:10])
        raise ValidationError(f"'{ref}' is ambiguous; specify a type or id. Candidates: {options}")

    # ================================================================== search
    def _entity_index(self) -> BM25Index:
        key = ("entities", self.scope.tenant_id, self.repo.revision(), self.scope.allowed_customers)

        def build() -> BM25Index:
            entities = self.repo.find_entities()
            aliases = self.repo.aliases_for(e.id for e in entities)
            docs = []
            for e in entities:
                props = " ".join(_flatten_text(e.properties or {}))[:2000]
                text = f"{e.name} {e.name} {' '.join(aliases.get(e.id, []))} {e.type.replace('_', ' ')} {e.description or ''} {props}"
                docs.append((e.id, text))
            return BM25Index(docs)

        return self.cache.get_or_build(key, build)  # type: ignore[return-value]

    def _chunk_index(self) -> tuple[BM25Index, dict[int, dict[str, Any]]]:
        key = ("chunks", self.scope.tenant_id, self.repo.revision(), self.scope.allowed_customers)

        def build() -> tuple[BM25Index, dict[int, dict[str, Any]]]:
            meta: dict[int, dict[str, Any]] = {}
            docs = []
            for chunk, doc, ent in self.repo.visible_document_chunks():
                meta[chunk.id] = {"chunk_id": chunk.id, "document_entity_id": ent.id, "title": doc.title, "url": doc.url, "text": chunk.text}
                docs.append((chunk.id, f"{doc.title} {chunk.text}"))
            return BM25Index(docs), meta

        return self.cache.get_or_build(key, build)  # type: ignore[return-value]

    def search_entities(
        self, query: str, types: list[str] | None = None, limit: int = 10, property_filters: dict[str, Any] | None = None
    ) -> list[dict[str, Any]]:
        limit = min(max(limit, 1), self.settings.max_results)
        results: list[dict[str, Any]] = []
        exact_ids: set[str] = set()
        if query.strip():
            # Exact name/alias hits rank first, then BM25.
            norm = normalize_name(query)
            for etype in types or [t.value for t in EntityType]:
                for e in self.repo.find_by_alias(etype, norm):
                    if e.status != "stale" and e.id not in exact_ids:
                        exact_ids.add(e.id)
            hits = self._entity_index().search(query, limit=limit * 5)
            scored = [(i, 100.0) for i in exact_ids] + [(h.doc_id, h.score) for h in hits if h.doc_id not in exact_ids]
        else:
            scored = [(e.id, 0.0) for e in self.repo.find_entities(types=types, limit=limit * 5)]
        ids = [i for i, _ in scored]
        entities = {e.id: e for e in self.repo.find_entities(ids=ids, types=types, property_filters=property_filters)}
        for eid, score in scored:
            e = entities.get(eid)
            if e is None:
                continue
            results.append({**entity_summary(e), "description": e.description, "score": score, "match": "exact" if eid in exact_ids else "lexical"})
            if len(results) >= limit:
                break
        return results

    def search_documentation(self, query: str, entity_ref: str | None = None, limit: int = 5) -> dict[str, Any]:
        limit = min(max(limit, 1), 50)
        index, meta = self._chunk_index()
        allowed_docs: set[str] | None = None
        about: Entity | None = None
        if entity_ref:
            about = self.resolve(entity_ref)
            allowed_docs = {nb.entity.id for nb in self.repo.neighbors([about.id], [R.DESCRIBES.value], "in", [E.DOCUMENTATION.value])}
            if about.type == E.DOCUMENTATION.value:
                allowed_docs.add(about.id)
        hits = index.search(
            query or (about.name if about else ""),
            limit=limit,
            allowed=(lambda cid: meta[cid]["document_entity_id"] in allowed_docs) if allowed_docs is not None else None,
        )
        results = [{**meta[h.doc_id], "score": h.score} for h in hits]
        if about is not None and not results:
            # No lexical overlap: still return the documents linked to the entity.
            for doc_id in list(allowed_docs or [])[:limit]:
                doc = self.repo.get_entity(doc_id)
                results.append({"document_entity_id": doc.id, "title": doc.name, "url": (doc.properties or {}).get("url"), "text": None, "score": 0.0})
        return {"query": query, "about": entity_summary(about) if about else None, "results": results}

    # ================================================================== entity details
    def get_entity_details(self, ref: str, include_provenance: bool = True, include_history: bool = False, max_relationships: int = 100) -> dict[str, Any]:
        e = self.resolve(ref)
        neighbors = self.repo.neighbors([e.id], direction="both", include_stale=False)
        rels = []
        for nb in neighbors[:max_relationships]:
            rels.append(
                {
                    "relationship_id": nb.relationship.id,
                    "type": nb.relationship.type,
                    "direction": nb.direction,
                    "confidence": nb.relationship.confidence,
                    "other": entity_summary(nb.entity),
                }
            )
        out: dict[str, Any] = {
            "entity": entity_full(e),
            "aliases": self.repo.aliases_for([e.id])[e.id],
            "relationships": rels,
            "relationship_count": len(neighbors),
        }
        if include_provenance:
            out["provenance"] = [_prov(p) for p in self.repo.provenance_for("entity", [e.id])[e.id]]
        if include_history:
            out["history"] = [
                {"version": c.version, "change": c.change_type, "at": c.changed_at.isoformat(), "run_id": c.run_id, "snapshot": c.snapshot}
                for c in self.repo.history("entity", e.id)
            ]
        return out

    # ================================================================== traversal primitives
    def traverse(self, start: list[Entity], steps: list[Step], max_paths: int | None = None) -> list[GraphPath]:
        max_paths = max_paths or self.settings.max_results * 5
        paths = [GraphPath([s]) for s in start]
        for step in steps:
            if not paths:
                break
            nbs = self.repo.neighbors(
                [p.end.id for p in paths], list(step.relations) or None, step.direction, list(step.target_types) or None
            )
            by_anchor: dict[str, list[Neighbor]] = {}
            for nb in nbs:
                anchor = nb.relationship.source_id if nb.direction == "out" else nb.relationship.target_id
                by_anchor.setdefault(anchor, []).append(nb)
            nxt: list[GraphPath] = []
            for p in paths:
                for nb in by_anchor.get(p.end.id, []):
                    if any(n.id == nb.entity.id for n in p.nodes):
                        continue  # no cycles
                    nxt.append(p.extend(nb))
                    if len(nxt) >= max_paths:
                        break
            paths = nxt
        return paths

    def get_related(
        self,
        ref: str,
        relation_types: list[str] | None = None,
        direction: Direction = "both",
        target_types: list[str] | None = None,
        depth: int = 1,
    ) -> dict[str, Any]:
        depth = _bounded(depth, self.settings.max_traversal_depth)
        root = self.resolve(ref)
        seen: dict[str, tuple[int, GraphPath]] = {}
        frontier = [GraphPath([root])]
        for level in range(1, depth + 1):
            nbs = self.repo.neighbors([p.end.id for p in frontier], relation_types, direction)
            by_anchor: dict[str, list[Neighbor]] = {}
            for nb in nbs:
                anchor = nb.relationship.source_id if nb.direction == "out" else nb.relationship.target_id
                by_anchor.setdefault(anchor, []).append(nb)
            nxt = []
            for p in frontier:
                for nb in by_anchor.get(p.end.id, []):
                    if nb.entity.id == root.id or nb.entity.id in seen:
                        continue
                    np_ = p.extend(nb)
                    seen[nb.entity.id] = (level, np_)
                    nxt.append(np_)
            frontier = nxt
            if not frontier or len(seen) >= self.settings.max_results:
                break
        items = [
            {"entity": entity_summary(path.end), "depth": lvl, "path": self._path_repr(path, provenance=False)}
            for lvl, path in seen.values()
            if not target_types or path.end.type in target_types
        ]
        return {"root": entity_summary(root), "count": len(items), "related": items[: self.settings.max_results]}

    def find_relationships(self, source_ref: str, target_ref: str, max_depth: int = 4, max_paths: int = 5) -> dict[str, Any]:
        """All shortest undirected paths (up to ``max_paths``) between two entities, with evidence."""
        max_depth = _bounded(max_depth, self.settings.max_traversal_depth)
        src, tgt = self.resolve(source_ref), self.resolve(target_ref)
        if src.id == tgt.id:
            raise ValidationError("source and target are the same entity")
        parents: dict[str, list[tuple[str, Neighbor]]] = {src.id: []}
        entities: dict[str, Entity] = {src.id: src}
        level = [src.id]
        found = False
        for _ in range(max_depth):
            nbs = self.repo.neighbors(level, direction="both")
            next_level: list[str] = []
            for nb in nbs:
                anchor = nb.relationship.source_id if nb.direction == "out" else nb.relationship.target_id
                oid = nb.entity.id
                if oid in parents and oid not in next_level:
                    continue
                if oid not in parents:
                    parents[oid] = []
                    next_level.append(oid)
                    entities[oid] = nb.entity
                parents[oid].append((anchor, nb))
            if tgt.id in parents:
                found = True
                break
            level = next_level
            if not level:
                break
        paths: list[GraphPath] = []
        if found:
            def walk(node_id: str, suffix: list[Neighbor]) -> None:
                if len(paths) >= max_paths:
                    return
                if node_id == src.id:
                    p = GraphPath([src])
                    for nb in suffix:
                        p = p.extend(nb)
                    paths.append(p)
                    return
                for anchor, nb in parents[node_id]:
                    walk(anchor, [nb] + suffix)

            walk(tgt.id, [])
        return {
            "source": entity_summary(src),
            "target": entity_summary(tgt),
            "connected": found,
            "paths": [self._path_repr(p) for p in paths],
        }

    # ================================================================== dependency / impact analysis
    def trace_dependencies(self, ref: str, direction: Literal["downstream", "upstream"] = "downstream", depth: int = 4) -> dict[str, Any]:
        depth = _bounded(depth, self.settings.max_traversal_depth)
        root = self.resolve(ref, [E.APPLICATION.value, E.SERVICE.value, E.DATABASE.value, E.API.value, E.CLOUD_RESOURCE.value])
        # An application's dependencies are those of its own services too.
        starts = [GraphPath([root])]
        if root.type == E.APPLICATION.value:
            starts += [GraphPath([root]).extend(nb) for nb in self.repo.neighbors([root.id], [R.CONTAINS.value], "out", [E.SERVICE.value])]
        if direction == "upstream" and root.type == E.SERVICE.value:
            # Consumers may depend on the API the service exposes rather than on the service itself.
            starts += [GraphPath([root]).extend(nb) for nb in self.repo.neighbors([root.id], [R.EXPOSES.value], "out", [E.API.value])]
        if direction == "upstream" and root.type == E.DATABASE.value:
            starts += [GraphPath([root]).extend(nb) for nb in self.repo.neighbors([root.id], [R.CONTAINS.value], "in", [E.APPLICATION.value])]
        rel_dir: Direction = "out" if direction == "downstream" else "in"
        seen: dict[str, GraphPath] = {root.id: starts[0]}
        for p in starts[1:]:
            seen.setdefault(p.end.id, p)
        frontier = list(starts)
        for _ in range(depth):
            nbs = self.repo.neighbors([p.end.id for p in frontier], [R.DEPENDS_ON.value], rel_dir)
            by_anchor: dict[str, list[Neighbor]] = {}
            for nb in nbs:
                anchor = nb.relationship.source_id if nb.direction == "out" else nb.relationship.target_id
                by_anchor.setdefault(anchor, []).append(nb)
            nxt = []
            for p in frontier:
                for nb in by_anchor.get(p.end.id, []):
                    if nb.entity.id in seen:
                        continue
                    np_ = p.extend(nb)
                    seen[nb.entity.id] = np_
                    nxt.append(np_)
            frontier = nxt
            if not frontier:
                break
        dependency_paths = [p for eid, p in seen.items() if eid != root.id and p.steps and p.steps[-1].relationship.type == R.DEPENDS_ON.value]
        result: dict[str, Any] = {
            "root": entity_summary(root),
            "direction": direction,
            "items": [
                {"entity": entity_summary(p.end), "hops": sum(1 for s in p.steps if s.relationship.type == R.DEPENDS_ON.value), "path": self._path_repr(p)}
                for p in dependency_paths
            ],
        }
        if root.type == E.APPLICATION.value:
            result["components"] = [entity_summary(p.end) for p in starts[1:] if p.end.type == E.SERVICE.value]
        if direction == "upstream":
            # The failing entity's own application/deployments are affected as well as its dependents.
            result["impact"] = self._impact_summary([root] + [p.end for p in dependency_paths])
        return result

    def _impact_summary(self, affected: list[Entity]) -> dict[str, Any]:
        """Roll affected services up to applications, their deployments and customers."""
        services = [e for e in affected if e.type == E.SERVICE.value]
        apps: dict[str, Entity] = {e.id: e for e in affected if e.type == E.APPLICATION.value}
        for nb in self.repo.neighbors([s.id for s in services], [R.CONTAINS.value], "in", [E.APPLICATION.value]):
            apps.setdefault(nb.entity.id, nb.entity)
        deployments = {nb.entity.id: nb.entity for nb in self.repo.neighbors(list(apps), [R.DEPLOYS.value], "in", [E.DEPLOYMENT.value])}
        customers = {e.identity.get("customer") for e in deployments.values() if e.identity}
        return {
            "affected_services": [entity_summary(e) for e in services],
            "affected_applications": [entity_summary(e) for e in apps.values()],
            "affected_deployments": [entity_summary(e) for e in deployments.values()],
            "affected_customers": sorted(c for c in customers if c),
        }

    # ================================================================== deployments
    def get_deployment_information(
        self, application_ref: str, customer: str | None = None, environment: str | None = None
    ) -> dict[str, Any]:
        app = self.resolve(application_ref, [E.APPLICATION.value, E.SERVICE.value])
        tier = None
        if environment:
            tier = normalize_tier(environment)
            if tier is None:
                raise ValidationError(f"unknown environment '{environment}'")
        customer_norm = None
        if customer:
            customer_norm = normalize_name(self.resolve(customer, [E.CUSTOMER.value]).identity.get("name", customer))
        deployments = [nb.entity for nb in self.repo.neighbors([app.id], [R.DEPLOYS.value], "in", [E.DEPLOYMENT.value])]
        items = []
        for d in sorted(deployments, key=lambda x: (x.identity.get("customer", ""), x.identity.get("tier", ""))):
            ident = d.identity or {}
            if tier and normalize_tier(ident.get("tier")) != tier:
                continue
            if customer_norm and normalize_name(ident.get("customer", "")) != customer_norm:
                continue
            nbs = self.repo.neighbors([d.id], direction="out")
            env = [nb.entity for nb in nbs if nb.relationship.type == R.DEPLOYED_IN.value]
            clusters = [nb.entity for nb in nbs if nb.relationship.type == R.RUNS_ON.value]
            uses = [nb.entity for nb in nbs if nb.relationship.type == R.USES.value]
            workloads = {k.split(":", 1)[1]: v for k, v in (d.properties or {}).items() if k.startswith("workload:")}
            prov = self.repo.provenance_for("entity", [d.id])[d.id]
            items.append(
                {
                    "deployment": entity_summary(d),
                    "customer": ident.get("customer"),
                    "environment_tier": ident.get("tier"),
                    "environment": [entity_summary(e) for e in env],
                    "clusters": [entity_summary(c) for c in clusters],
                    "namespace": (d.properties or {}).get("namespace"),
                    "workloads": workloads,
                    "cloud_resources": [{**entity_summary(u), "resource_type": (u.properties or {}).get("resource_type")} for u in uses],
                    "sources": sorted({p.source_system for p in prov}),
                }
            )
        return {"application": entity_summary(app), "filters": {"customer": customer, "environment": tier.value if tier else None}, "deployments": items}

    # ================================================================== evidence helpers
    def _path_repr(self, path: GraphPath, provenance: bool = True) -> list[dict[str, Any]]:
        prov = self.repo.provenance_for("relationship", [s.relationship.id for s in path.steps]) if provenance else {}
        out = []
        for s in path.steps:
            item = {
                "relationship_id": s.relationship.id,
                "type": s.relationship.type,
                "source": f"{s.source.type}:{s.source.name}",
                "target": f"{s.target.type}:{s.target.name}",
                "confidence": s.relationship.confidence,
            }
            if provenance:
                item["evidence"] = [_prov(p) for p in prov.get(s.relationship.id, [])[:3]]
            out.append(item)
        return out


def _prov(p) -> dict[str, Any]:
    return {
        "source_system": p.source_system,
        "source_url": p.source_url,
        "extractor": p.extractor,
        "confidence": p.confidence,
        "evidence": p.evidence,
        "observed_at": p.observed_at.isoformat() if p.observed_at else None,
    }


def _bounded(value: int, maximum: int) -> int:
    if value < 1:
        raise ValidationError("depth must be >= 1")
    return min(value, maximum)


def _flatten_text(value: Any) -> list[str]:
    if isinstance(value, str):
        return [value]
    if isinstance(value, dict):
        return [s for k, v in value.items() for s in ([str(k)] + _flatten_text(v))]
    if isinstance(value, list):
        return [s for v in value for s in _flatten_text(v)]
    return [str(value)] if value is not None else []

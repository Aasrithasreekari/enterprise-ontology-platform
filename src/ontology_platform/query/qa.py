"""Question answering over the knowledge graph.

Pipeline: entity linking (longest n-gram match of question tokens against entity names and
aliases, with type hints such as "customer X") → intent detection (keyword rules) → a graph
traversal pattern → structured results + evidence + a short templated answer.

No language model is involved; the calling agent does the language reasoning, the platform
returns grounded facts. Unrecognized questions fall back to hybrid retrieval.
"""

from __future__ import annotations

import re
from collections import defaultdict
from dataclasses import dataclass
from typing import Any

from ontology_platform.ontology.keys import normalize_name
from ontology_platform.ontology.schema import TIER_ALIASES, EntityType, RelationType, normalize_tier
from ontology_platform.query.engine import GraphPath, QueryEngine, Step, entity_summary
from ontology_platform.storage.orm import Entity

E, R = EntityType, RelationType

_TOKEN = re.compile(r"[A-Za-z0-9]+")
_TYPE_HINTS = {
    "customer": E.CUSTOMER, "client": E.CUSTOMER, "product": E.PRODUCT, "application": E.APPLICATION, "app": E.APPLICATION,
    "system": E.APPLICATION, "service": E.SERVICE, "microservice": E.SERVICE, "component": E.SERVICE, "repository": E.REPOSITORY,
    "repo": E.REPOSITORY, "api": E.API, "database": E.DATABASE, "db": E.DATABASE, "cluster": E.KUBERNETES_CLUSTER,
}
_IGNORED = {
    "which", "what", "where", "who", "the", "is", "are", "of", "a", "an", "to", "in", "for", "by", "on", "and", "if", "does",
    "do", "s", "its", "it", "goes", "down", "all", "list", "show", "me", "belong", "belongs", "exposed", "deployed",
} | set(TIER_ALIASES) | set(_TYPE_HINTS)
MAX_NGRAM = 6


@dataclass
class Linked:
    entity: Entity
    text: str
    hinted: bool


class EntityLinker:
    """Links question text to entities. The alias index is built once per tenant graph revision and
    visibility scope (cached), not on every question."""

    def __init__(self, engine: QueryEngine) -> None:
        self.engine = engine
        scope = engine.scope
        key = ("linker", scope.tenant_id, engine.repo.revision(), scope.allowed_customers)
        self.index: dict[str, list[tuple[str, str]]] = engine.cache.get_or_build(key, lambda: _build_alias_index(engine))  # type: ignore[assignment]

    def link(self, question: str) -> list[Linked]:
        matches = list(_TOKEN.finditer(question))
        tokens = [t.group() for t in matches]
        norms = [normalize_name(t) for t in tokens]
        hits: list[tuple[str, str, str, bool]] = []
        i = 0
        while i < len(tokens):
            matched = 0
            for n in range(min(MAX_NGRAM, len(tokens) - i), 0, -1):
                key = "-".join(norms[i : i + n])
                cands = self.index.get(key)
                if not cands:
                    continue
                hint = _TYPE_HINTS.get(norms[i - 1]) if i > 0 else None
                hint_after = _TYPE_HINTS.get(norms[i + n]) if i + n < len(norms) else None
                hint = hint or hint_after
                if hint and any(t == hint.value for _, t in cands):
                    cands = [c for c in cands if c[1] == hint.value]
                for eid, etype in cands:
                    hits.append((eid, etype, question[matches[i].start() : matches[i + n - 1].end()], bool(hint)))
                matched = n
                break
            i += matched or 1
        out: list[Linked] = []
        seen: set[str] = set()
        ents = {e.id: e for e in self.engine.repo.find_entities(ids=[h[0] for h in hits])}
        for eid, _t, text, hinted in hits:
            if eid in ents and eid not in seen:
                seen.add(eid)
                out.append(Linked(ents[eid], text, hinted))
        return out


def _build_alias_index(engine: QueryEngine) -> dict[str, list[tuple[str, str]]]:
    index: dict[str, list[tuple[str, str]]] = defaultdict(list)
    for entity_id, etype, alias in engine.repo.all_aliases():
        norm = normalize_name(alias)
        if norm and norm not in _IGNORED and len(norm) >= 3 and (entity_id, etype) not in index[norm]:
            index[norm].append((entity_id, etype))
    return dict(index)


def _of(linked: list[Linked], *types: EntityType) -> list[Entity]:
    wanted = {t.value for t in types}
    return [l.entity for l in linked if l.entity.type in wanted]


def _tiers(question: str) -> list[str]:
    found = []
    for tok in _TOKEN.findall(question.lower()):
        tier = normalize_tier(tok)
        if tier and tier.value not in found:
            found.append(tier.value)
    return found


class QuestionAnswerer:
    def __init__(self, engine: QueryEngine) -> None:
        self.engine = engine

    def ask(self, question: str, limit: int = 50) -> dict[str, Any]:
        q = question.strip()
        if not q:
            from ontology_platform.errors import ValidationError

            raise ValidationError("question is empty")
        linked = EntityLinker(self.engine).link(q)
        # Intent keywords must not come from entity names (e.g. "document-intake", "claims-api").
        low = q.lower()
        for l in sorted(linked, key=lambda x: -len(x.text)):
            low = low.replace(l.text.lower(), " ")
        handlers = [
            (r"\b(affected|impact\w*|goes down|go down|outage|fail\w*|blast radius|break)\b", self._impact),
            (r"\b(doc\w*|describ\w*|runbook\w*|wiki|confluence|readme)\b", self._documentation),
            (r"\bclusters?\b", self._clusters),
            (r"\brepo(s|sitor(y|ies))?\b|\bsource code\b|\bcode\b", self._repositories),
            (r"\bapis?\b|\bendpoints?\b", self._apis),
            (r"\bdeploy\w*|\bwhere\b.*\b(run|running|hosted)\b|\benvironments?\b", self._deployments),
            (r"\bdepend\w*|\bdependenc\w*|\bcalls?\b|\bdownstream\b|\buses\b", self._dependencies),
        ]
        for pattern, handler in handlers:
            if re.search(pattern, low):
                answer = handler(q, linked, limit)
                if answer is not None:
                    return self._envelope(q, linked, answer)
        return self._envelope(q, linked, self._fallback(q, linked, limit))

    # ------------------------------------------------------------------ public handlers (used by agents)
    def repositories(self, q: str, linked: list[Linked], limit: int = 50) -> dict[str, Any] | None:
        return self._repositories(q, linked, limit)

    def apis(self, q: str, linked: list[Linked], limit: int = 50) -> dict[str, Any] | None:
        return self._apis(q, linked, limit)

    def clusters(self, q: str, linked: list[Linked], limit: int = 50) -> dict[str, Any] | None:
        return self._clusters(q, linked, limit)

    def documentation(self, q: str, linked: list[Linked], limit: int = 50) -> dict[str, Any] | None:
        return self._documentation(q, linked, limit)

    # ------------------------------------------------------------------ envelope
    def _envelope(self, question: str, linked: list[Linked], answer: dict[str, Any]) -> dict[str, Any]:
        return {
            "question": question,
            "intent": answer.pop("intent"),
            "linked_entities": [{**entity_summary(l.entity), "matched_text": l.text} for l in linked],
            **answer,
        }

    def _paths_result(self, paths: list[GraphPath], limit: int) -> list[dict[str, Any]]:
        grouped: dict[str, list[GraphPath]] = defaultdict(list)
        for p in paths:
            grouped[p.end.id].append(p)
        results = []
        selected = list(grouped.values())[:limit]
        reprs = iter(self.engine.paths_repr([p for ps in selected for p in ps[:3]]))
        for ps in selected:
            results.append({"entity": entity_summary(ps[0].end), "evidence_paths": [next(reprs) for _ in ps[:3]]})
        return results

    # ------------------------------------------------------------------ intents
    def _repositories(self, q: str, linked: list[Linked], limit: int) -> dict[str, Any] | None:
        customers, products = _of(linked, E.CUSTOMER), _of(linked, E.PRODUCT)
        apps, services = _of(linked, E.APPLICATION), _of(linked, E.SERVICE)
        eng = self.engine
        to_repo = [Step((R.CONTAINS.value,), "out", (E.APPLICATION.value,)), Step((R.HAS.value,), "out", (E.REPOSITORY.value,))]
        notes = []
        if products:
            starts = products
            if customers:
                owned = {
                    p.end.id
                    for p in eng.traverse(customers, [Step((R.OWNS.value,), "out", (E.PRODUCT.value,))])
                }
                starts = [p for p in products if p.id in owned]
                if not starts:
                    names = ", ".join(c.name for c in customers)
                    return {"intent": "repositories_of_product", "answer": f"{names} does not own {', '.join(p.name for p in products)} according to the graph.", "results": []}
                paths = eng.traverse(customers, [Step((R.OWNS.value,), "out", (E.PRODUCT.value,))] + to_repo)
                paths = [p for p in paths if p.nodes[1].id in {s.id for s in starts}]
            else:
                paths = eng.traverse(starts, to_repo)
        elif customers:
            paths = eng.traverse(customers, [Step((R.OWNS.value,), "out", (E.PRODUCT.value,))] + to_repo)
        elif apps or services:
            paths = eng.traverse(apps + services, [Step((R.HAS.value,), "out", (E.REPOSITORY.value,))])
            paths += eng.traverse(services, [Step((R.IMPLEMENTS.value,), "in", (E.REPOSITORY.value,))])
        else:
            return None
        results = self._paths_result(paths, limit)
        scope = " / ".join(e.name for e in customers + products + apps + services)
        names = ", ".join(r["entity"]["name"] for r in results) or "none found"
        return {"intent": "repositories", "answer": f"Repositories for {scope}: {names}.", "results": results, "notes": notes}

    def _apis(self, q: str, linked: list[Linked], limit: int) -> dict[str, Any] | None:
        owners = _of(linked, E.APPLICATION, E.SERVICE)
        if not owners:
            return None
        eng = self.engine
        paths = eng.traverse(owners, [Step((R.EXPOSES.value,), "out", (E.API.value,))])
        apps = [o for o in owners if o.type == E.APPLICATION.value]
        if apps:
            paths += eng.traverse(apps, [Step((R.CONTAINS.value,), "out", (E.SERVICE.value,)), Step((R.EXPOSES.value,), "out", (E.API.value,))])
        results = self._paths_result(paths, limit)
        for r in results:
            api = eng.repo.get_entity(r["entity"]["id"])
            r["endpoints"] = (api.properties or {}).get("endpoints", [])
            r["api_version"] = (api.properties or {}).get("api_version")
        names = ", ".join(r["entity"]["name"] for r in results) or "none found"
        return {"intent": "apis_exposed", "answer": f"APIs exposed by {', '.join(o.name for o in owners)}: {names}.", "results": results}

    def _deployments(self, q: str, linked: list[Linked], limit: int) -> dict[str, Any] | None:
        apps = _of(linked, E.APPLICATION) or _of(linked, E.SERVICE)
        if not apps:
            return None
        customers = _of(linked, E.CUSTOMER)
        tiers = _tiers(q)
        out = []
        for app in apps:
            info = self.engine.get_deployment_information(app.id)
            for d in info["deployments"]:
                if tiers and d["environment_tier"] not in tiers:
                    continue
                if customers and d["customer"] not in {normalize_name(c.identity.get("name", "")) for c in customers}:
                    continue
                out.append({"application": app.name, **d})
        lines = [
            f"{d['application']} -> {d['customer']} {str(d['environment_tier']).upper()} on {', '.join(c['name'] for c in d['clusters']) or 'unknown cluster'}"
            f" (namespace {d['namespace']})"
            for d in out[:limit]
        ]
        filt = f" in {', '.join(t.upper() for t in tiers)}" if tiers else ""
        return {
            "intent": "deployments",
            "answer": (f"Deployments{filt}: " + "; ".join(lines) + ".") if lines else f"No deployments found{filt}.",
            "results": out[:limit],
        }

    def _dependencies(self, q: str, linked: list[Linked], limit: int) -> dict[str, Any] | None:
        targets = _of(linked, E.APPLICATION, E.SERVICE)
        if not targets:
            return None
        root = targets[0]
        trace = self.engine.trace_dependencies(root.id, "downstream")
        items = trace["items"][:limit]
        names = ", ".join(f"{i['entity']['name']} ({i['entity']['type']})" for i in items) or "none found"
        components = trace.get("components") or []
        comp = f" (components: {', '.join(c['name'] for c in components)})" if components else ""
        return {"intent": "dependencies", "answer": f"{root.name}{comp} depends on: {names}.", "results": items, "components": components}

    def _impact(self, q: str, linked: list[Linked], limit: int) -> dict[str, Any] | None:
        targets = _of(linked, E.SERVICE, E.APPLICATION, E.DATABASE, E.API, E.KUBERNETES_CLUSTER, E.CLOUD_RESOURCE)
        if not targets:
            return None
        root = targets[0]
        if root.type == E.KUBERNETES_CLUSTER.value:
            paths = self.engine.traverse([root], [Step((R.RUNS_ON.value,), "in", (E.DEPLOYMENT.value, E.APPLICATION.value, E.SERVICE.value))])
            results = self._paths_result(paths, limit)
            deployments = [r["entity"] for r in results if r["entity"]["type"] == E.DEPLOYMENT.value]
            return {
                "intent": "impact_analysis",
                "answer": f"If cluster {root.name} fails, {len(deployments)} deployments are affected: {', '.join(d['name'] for d in deployments)}.",
                "results": results,
            }
        trace = self.engine.trace_dependencies(root.id, "upstream")
        impact = trace["impact"]
        apps = ", ".join(a["name"] for a in impact["affected_applications"]) or "none"
        custs = ", ".join(impact["affected_customers"]) or "none"
        return {
            "intent": "impact_analysis",
            "answer": f"If {root.name} goes down, affected applications: {apps}; affected customers: {custs}.",
            "results": trace["items"][:limit],
            "impact": impact,
        }

    def _clusters(self, q: str, linked: list[Linked], limit: int) -> dict[str, Any] | None:
        customers, apps = _of(linked, E.CUSTOMER), _of(linked, E.APPLICATION, E.SERVICE)
        eng = self.engine
        if customers:
            steps = [
                Step((R.BELONGS_TO.value,), "in", (E.ENVIRONMENT.value,)),
                Step((R.DEPLOYED_IN.value,), "in", (E.DEPLOYMENT.value,)),
                Step((R.RUNS_ON.value,), "out", (E.KUBERNETES_CLUSTER.value,)),
            ]
            paths = eng.traverse(customers, steps)
            if apps:
                app_ids = {a.id for a in apps}
                deps = {nb.entity.id for nb in eng.repo.neighbors(app_ids, [R.DEPLOYS.value], "in", [E.DEPLOYMENT.value])}
                paths = [p for p in paths if p.nodes[2].id in deps]
            tiers = _tiers(q)
            if tiers:
                paths = [p for p in paths if (p.nodes[1].identity or {}).get("tier") in tiers]
        elif apps:
            paths = eng.traverse(apps, [Step((R.RUNS_ON.value,), "out", (E.KUBERNETES_CLUSTER.value,))])
        else:
            return None
        results = self._paths_result(paths, limit)
        for r in results:
            hosted = sorted({p["source"].split(":", 1)[1] for ep in r["evidence_paths"] for p in ep if p["type"] == R.RUNS_ON.value})
            r["deployments"] = hosted
        subject = ", ".join(e.name for e in customers + apps)
        names = ", ".join(r["entity"]["name"] for r in results) or "none found"
        return {"intent": "clusters_hosting", "answer": f"Kubernetes clusters hosting {subject}: {names}.", "results": results}

    def _documentation(self, q: str, linked: list[Linked], limit: int) -> dict[str, Any] | None:
        subjects = [l.entity for l in linked if l.entity.type != E.DOCUMENTATION.value]
        eng = self.engine
        if not subjects:
            docs = eng.search_documentation(q, limit=min(limit, 10))
            return {"intent": "documentation_search", "answer": f"{len(docs['results'])} documentation passages match.", "results": docs["results"]}
        paths = eng.traverse(subjects, [Step((R.DESCRIBES.value,), "in", (E.DOCUMENTATION.value,))])
        results = self._paths_result(paths, limit)
        for r in results:
            doc = eng.repo.get_entity(r["entity"]["id"])
            r["url"] = (doc.properties or {}).get("url")
            r["confidence"] = max((step[-1]["confidence"] for step in r["evidence_paths"]), default=None)
        results.sort(key=lambda r: -(r["confidence"] or 0))
        passages = eng.search_documentation(q, entity_ref=subjects[0].id, limit=3)["results"]
        names = "; ".join(f"{r['entity']['name']}" for r in results) or "none found"
        return {
            "intent": "documentation_for_entity",
            "answer": f"Documentation describing {', '.join(s.name for s in subjects)}: {names}.",
            "results": results,
            "passages": passages,
        }

    def _fallback(self, q: str, linked: list[Linked], limit: int) -> dict[str, Any]:
        eng = self.engine
        neighborhood = []
        for l in linked[:3]:
            rel = eng.get_related(l.entity.id, depth=1)
            neighborhood.append({"entity": entity_summary(l.entity), "related": rel["related"][:limit]})
        return {
            "intent": "hybrid_search",
            "answer": "No specific graph pattern matched; returning linked entities' neighbourhoods, entity search and documentation hits.",
            "results": neighborhood,
            "entity_hits": eng.search_entities(q, limit=10),
            "passages": eng.search_documentation(q, limit=5)["results"],
        }

"""Specialist retrieval agents.

Each agent is a thin adapter over existing platform capabilities (QueryEngine and the question
handlers in query/qa.py); none contains its own graph or search logic. Agents receive a QueryEngine
bound to the caller's tenant and customer scope, so isolation rules apply to every call. They turn
results into `Fact`s carrying the graph path and provenance exactly as stored by ingestion.
"""

from __future__ import annotations

import itertools
import re
from typing import Any, Protocol

from ontology_platform.agents.models import AgentName, AgentOutput, EntityRefOut, Evidence, Fact, PlanStep
from ontology_platform.ontology.keys import normalize_name
from ontology_platform.ontology.schema import EntityType as E
from ontology_platform.ontology.schema import RelationType as R
from ontology_platform.query.engine import QueryEngine
from ontology_platform.query.qa import Linked, QuestionAnswerer, _tiers

_ids = itertools.count(1)


class Agent(Protocol):
    name: AgentName

    def run(self, engine: QueryEngine, step: PlanStep, question: str, upstream: dict[str, AgentOutput]) -> AgentOutput: ...


# ----------------------------------------------------------------------------- helpers
def _ref(summary: dict[str, Any] | None) -> EntityRefOut | None:
    if not summary:
        return None
    return EntityRefOut(id=summary["id"], type=summary["type"], name=summary["name"], key=summary.get("key"), status=summary.get("status"))


def _name(endpoint: str) -> str:
    return endpoint.split(":", 1)[1] if ":" in endpoint else endpoint


def render_path(path: list[dict[str, Any]]) -> str:
    """'Acme Bank -[owns]-> PayHub -[contains]-> payments-platform' (edges keep their stored direction)."""
    return "; ".join(f"{_name(e['source'])} -[{e['type']}]-> {_name(e['target'])}" for e in path)


def _path_evidence(path: list[dict[str, Any]]) -> list[Evidence]:
    out: list[Evidence] = []
    for edge in path:
        for ev in edge.get("evidence", []):
            out.append(
                Evidence(
                    source_system=ev["source_system"],
                    source_url=ev.get("source_url"),
                    extractor=ev.get("extractor"),
                    confidence=ev.get("confidence"),
                    excerpt=ev.get("evidence"),
                    relationship_id=edge.get("relationship_id"),
                )
            )
    return out


def _path_entities(path: list[dict[str, Any]]) -> list[str]:
    ids: list[str] = []
    for edge in path:
        for key in ("source_id", "target_id"):
            if edge.get(key) and edge[key] not in ids:
                ids.append(edge[key])
    return ids


def path_fact(agent: AgentName, kind: str, statement: str, path: list[dict[str, Any]], subject=None, obj=None) -> Fact:
    return Fact(
        id=f"f{next(_ids)}",
        kind=kind,
        statement=statement,
        subject=_ref(subject),
        object=_ref(obj),
        path=path,
        evidence=_path_evidence(path),
        entity_ids=_path_entities(path) or [x["id"] for x in (subject, obj) if x],
        produced_by=agent,
    )


def _linked(engine: QueryEngine, ids: list[str]) -> list[Linked]:
    out = []
    for eid in ids:
        e = engine.resolve(eid)
        out.append(Linked(e, e.name, True))
    return out


# ----------------------------------------------------------------------------- graph intelligence
class GraphIntelligenceAgent:
    """Entity lookup, relationship discovery and multi-hop traversal (repositories, APIs, clusters, paths)."""

    name = AgentName.GRAPH

    def run(self, engine: QueryEngine, step: PlanStep, question: str, upstream: dict[str, AgentOutput]) -> AgentOutput:
        action, ids = step.action, step.inputs.get("entity_ids", [])
        out = AgentOutput()
        qa = QuestionAnswerer(engine)
        if action in ("repositories", "apis", "clusters"):
            handler = {"repositories": qa.repositories, "apis": qa.apis, "clusters": qa.clusters}[action]
            out.tools.append(f"query_engine.{action}")
            result = handler(question, _linked(engine, ids), 200) or {"results": []}
            for item in result["results"]:
                target = item["entity"]
                for path in item.get("evidence_paths", [])[:1]:
                    verb = {"repositories": "repository", "apis": "API", "clusters": "cluster"}[action]
                    out.facts.append(path_fact(self.name, action[:-1] if action != "apis" else "api",
                                               f"{verb} {target['name']}: {render_path(path)}", path, obj=target))
                if action == "apis" and item.get("endpoints"):
                    out.notes.append(f"{target['name']} endpoints: {', '.join(item['endpoints'][:8])}")
            if not result["results"]:
                out.missing.append(f"No {action} found for {', '.join(l.entity.name for l in _linked(engine, ids))}.")
            if result.get("answer") and "does not own" in result["answer"]:
                out.missing.append(result["answer"])
            out.data["count"] = len(result["results"])
        elif action == "relationship_paths":
            out.tools.append("query_engine.find_relationships")
            for a, b in zip(ids, ids[1:]):
                r = engine.find_relationships(a, b, max_depth=4)
                if not r["connected"]:
                    out.missing.append(f"No connection found between {r['source']['name']} and {r['target']['name']} within 4 hops.")
                    continue
                for path in r["paths"][:2]:
                    out.facts.append(path_fact(self.name, "relationship", f"{r['source']['name']} → {r['target']['name']}: {render_path(path)}",
                                               path, r["source"], r["target"]))
        elif action == "entity_details":
            out.tools.append("query_engine.get_entity_details")
            for eid in ids:
                d = engine.get_entity_details(eid)
                ent = d["entity"]
                prov = d.get("provenance", [])
                out.facts.append(
                    Fact(
                        id=f"f{next(_ids)}",
                        kind="entity",
                        statement=f"{ent['name']} is a {ent['type']} with {d['relationship_count']} relationships",
                        subject=_ref(ent),
                        evidence=[Evidence(source_system=p["source_system"], source_url=p.get("source_url"), extractor=p.get("extractor"),
                                           confidence=p.get("confidence"), excerpt=p.get("evidence")) for p in prov[:5]],
                        entity_ids=[ent["id"]],
                        produced_by=self.name,
                    )
                )
                for rel in d["relationships"][:15]:
                    arrow = f"{ent['name']} -[{rel['type']}]-> {rel['other']['name']}" if rel["direction"] == "out" else f"{rel['other']['name']} -[{rel['type']}]-> {ent['name']}"
                    out.notes.append(arrow)
        else:
            raise ValueError(f"unknown graph action '{action}'")
        return out


# ----------------------------------------------------------------------------- dependency and impact
class DependencyImpactAgent:
    """Downstream dependencies, upstream dependents and impact roll-up to applications and customers."""

    name = AgentName.DEPENDENCY

    def run(self, engine: QueryEngine, step: PlanStep, question: str, upstream: dict[str, AgentOutput]) -> AgentOutput:
        out = AgentOutput()
        root_id = step.inputs["entity_id"]
        if step.action == "dependencies":
            out.tools.append("query_engine.trace_dependencies(downstream)")
            trace = engine.trace_dependencies(root_id, "downstream", step.inputs.get("depth", 4))
            root = trace["root"]
            for item in trace["items"]:
                out.facts.append(path_fact(self.name, "dependency", f"{root['name']} depends on {item['entity']['name']} ({item['hops']} hop(s)): {render_path(item['path'])}",
                                           item["path"], root, item["entity"]))
            if trace.get("components"):
                out.notes.append(f"{root['name']} components: {', '.join(c['name'] for c in trace['components'])}")
            if not trace["items"]:
                out.missing.append(f"No declared dependencies found for {root['name']}.")
            out.data = {"root": root, "dependency_ids": [i["entity"]["id"] for i in trace["items"]]}
            return out

        # impact / dependents: upstream trace, then roll up to applications
        out.tools.append("query_engine.trace_dependencies(upstream)")
        trace = engine.trace_dependencies(root_id, "upstream", step.inputs.get("depth", 4))
        root, impact = trace["root"], trace["impact"]
        path_by_entity = {item["entity"]["id"]: item["path"] for item in trace["items"]}
        for item in trace["items"]:
            out.facts.append(path_fact(self.name, "dependent", f"{item['entity']['name']} depends on {root['name']} ({item['hops']} hop(s)): {render_path(item['path'])}",
                                       item["path"], item["entity"], root))
        # Which affected services belong to which application (one query for all).
        service_ids = [s["id"] for s in impact["affected_services"]]
        contains = engine.repo.neighbors(service_ids, [R.CONTAINS.value], "in", [E.APPLICATION.value])
        contains_prov = engine.repo.provenance_for("relationship", [nb.relationship.id for nb in contains])
        # When the failing component is an application, it is the cause, not an affected application.
        affected_apps = [a for a in impact["affected_applications"] if a["id"] != root["id"]]
        for app in affected_apps:
            via = [nb for nb in contains if nb.entity.id == app["id"]]
            if not via:  # an application that itself depends on the root
                path = path_by_entity.get(app["id"], [])
                out.facts.append(path_fact(self.name, "impact", f"Application {app['name']} is potentially affected: {render_path(path) or 'direct dependency'}", path, app, root))
                continue
            nb = via[0]
            service = nb.relationship.target_id
            edge = {
                "relationship_id": nb.relationship.id, "type": R.CONTAINS.value,
                "source": f"application:{app['name']}", "target": f"service:{_service_name(impact, service, root)}",
                "source_id": app["id"], "target_id": service, "confidence": nb.relationship.confidence,
                "evidence": [{"source_system": p.source_system, "source_url": p.source_url, "extractor": p.extractor,
                              "confidence": p.confidence, "evidence": p.evidence} for p in contains_prov.get(nb.relationship.id, [])[:3]],
            }
            path = [edge] + path_by_entity.get(service, [])
            reason = "contains the failing service itself" if service == root["id"] else f"contains {_service_name(impact, service, root)}, which depends on {root['name']}"
            out.facts.append(path_fact(self.name, "impact", f"Application {app['name']} is potentially affected: it {reason}", path, app, root))
        out.data = {
            "root": root,
            "target_application": root["id"] if root["type"] == E.APPLICATION.value else None,
            "affected_applications": [a["id"] for a in affected_apps],
            "affected_application_names": [a["name"] for a in affected_apps],
            "affected_customers": impact["affected_customers"],
            "affected_deployments": len(impact["affected_deployments"]),
        }
        # Circular dependencies make impact ambiguous: report entities that are both upstream and downstream.
        down = engine.trace_dependencies(root_id, "downstream", step.inputs.get("depth", 4))
        cycle = {i["entity"]["name"] for i in down["items"]} & {i["entity"]["name"] for i in trace["items"]}
        if cycle:
            out.data["cycles"] = sorted(cycle)
            out.tools.append("query_engine.trace_dependencies(downstream)")
        if not trace["items"] and not affected_apps:
            out.notes.append(f"No other component declares a dependency on {root['name']}.")
        return out


def _service_name(impact: dict[str, Any], service_id: str, root: dict[str, Any]) -> str:
    if service_id == root["id"]:
        return root["name"]
    return next((s["name"] for s in impact["affected_services"] if s["id"] == service_id), service_id)


# ----------------------------------------------------------------------------- deployments
class DeploymentIntelligenceAgent:
    """Deployments per application, customer and environment tier, with clusters and namespaces."""

    name = AgentName.DEPLOYMENT

    def run(self, engine: QueryEngine, step: PlanStep, question: str, upstream: dict[str, AgentOutput]) -> AgentOutput:
        out = AgentOutput(tools=["query_engine.get_deployment_information"])
        ids: list[str] = list(step.inputs.get("entity_ids", []))
        source = step.inputs.get("applications_from")
        if source:
            # "Where is it deployed?" covers the failing application itself as well as the affected ones.
            data = upstream[source].data
            candidates = ([data["target_application"]] if data.get("target_application") else []) + data.get("affected_applications", [])
            ids += [i for i in candidates if i not in ids]
            if not ids:
                out.missing.append("No affected applications to look up deployments for.")
        tiers = _tiers(question)
        customers = {normalize_name(c) for c in step.inputs.get("customers", [])}
        for app_id in ids[: step.inputs.get("max_applications", 25)]:
            info = engine.get_deployment_information(app_id)
            app = info["application"]
            rows = [d for d in info["deployments"]
                    if (not tiers or d["environment_tier"] in tiers) and (not customers or d["customer"] in customers)]
            if not rows:
                scope = f" in {', '.join(t.upper() for t in tiers)}" if tiers else ""
                out.missing.append(f"No deployment records found for {app['name']}{scope}.")
            else:
                for tier in tiers:
                    if not any(d["environment_tier"] == tier for d in rows):
                        out.missing.append(f"No {tier.upper()} deployment recorded for {app['name']}.")
            for d in rows:
                clusters = ", ".join(c["name"] for c in d["clusters"]) or "no cluster recorded"
                if not d["clusters"]:
                    out.missing.append(f"Deployment {d['deployment']['name']} has no cluster information.")
                out.facts.append(
                    Fact(
                        id=f"f{next(_ids)}",
                        kind="deployment",
                        statement=f"{app['name']} is deployed for {d['customer']} in {str(d['environment_tier']).upper()} on {clusters}"
                                  + (f" (namespace {d['namespace']})" if d.get("namespace") else ""),
                        subject=_ref(app),
                        object=_ref(d["deployment"]),
                        evidence=[Evidence(source_system=p["source_system"], source_url=p.get("source_url"), extractor=p.get("extractor"),
                                           confidence=p.get("confidence"), excerpt=p.get("evidence")) for p in d.get("evidence", [])],
                        entity_ids=[app["id"], d["deployment"]["id"]] + [c["id"] for c in d["clusters"]],
                        produced_by=self.name,
                    )
                )
            out.data.setdefault("deployments", {})[app["name"]] = [
                {"customer": d["customer"], "tier": d["environment_tier"], "clusters": [c["name"] for c in d["clusters"]]} for d in rows
            ]
        return out


# ----------------------------------------------------------------------------- documentation
_DOC_INTENT_WORDS = {
    "which", "what", "where", "who", "is", "are", "the", "a", "an", "of", "for", "about", "there", "any", "me", "show", "find",
    "list", "give", "explain", "documentation", "documents", "document", "docs", "doc", "describes", "describe", "described",
    "describing", "pages", "page", "wiki", "confluence", "readme", "readmes", "runbook", "runbooks",
}


class DocumentationIntelligenceAgent:
    """Documents describing entities and the most relevant passages (BM25 keyword retrieval)."""

    name = AgentName.DOCUMENTATION

    def run(self, engine: QueryEngine, step: PlanStep, question: str, upstream: dict[str, AgentOutput]) -> AgentOutput:
        out = AgentOutput()
        ids = step.inputs.get("entity_ids", [])
        passages: list[dict[str, Any]] = []
        if ids:
            out.tools.append("query_engine.documentation_for_entity")
            result = QuestionAnswerer(engine).documentation(question, _linked(engine, ids), 20) or {"results": [], "passages": []}
            for item in result["results"][:10]:
                for path in item.get("evidence_paths", [])[:1]:
                    out.facts.append(path_fact(self.name, "document", f"'{item['entity']['name']}' describes {_name(path[-1]['target'])}", path, item["entity"]))
            passages = result.get("passages", [])
            if not result["results"]:
                out.missing.append("No documentation is linked to the entities in the question.")
        # Keyword passages for the question itself (e.g. "authentication"), restricted to the tenant.
        out.tools.append("query_engine.search_documentation")
        # Search for the subject only: words that state the intent ("which documentation describes")
        # would otherwise match every document with "document" in its name.
        query = " ".join(w for w in re.findall(r"[\w-]+", step.inputs.get("query") or question) if w.lower() not in _DOC_INTENT_WORDS)
        seen = {p.get("chunk_id") for p in passages}
        found = engine.search_documentation(query, limit=5)["results"] if query.strip() else []
        passages += [p for p in found if p.get("chunk_id") not in seen]
        doc_ids = list({p["document_entity_id"] for p in passages})
        prov = engine.repo.provenance_for("entity", doc_ids)
        for p in passages[:6]:
            src = prov.get(p["document_entity_id"], [])
            out.facts.append(
                Fact(
                    id=f"f{next(_ids)}",
                    kind="passage",
                    statement=f"Passage from '{p['title']}'" + (f" (relevance {p['score']:.2f})" if p.get("score") else ""),
                    evidence=[Evidence(source_system=s.source_system, source_url=p.get("url") or s.source_url, extractor="bm25_passage",
                                       confidence=None, excerpt=(p.get("text") or "")[:400]) for s in src[:1]],
                    entity_ids=[p["document_entity_id"]],
                    produced_by=self.name,
                )
            )
        if not passages:
            out.missing.append("No documentation passage matched the question.")
        return out


AGENTS: dict[AgentName, Agent] = {
    a.name: a for a in (GraphIntelligenceAgent(), DependencyImpactAgent(), DeploymentIntelligenceAgent(), DocumentationIntelligenceAgent())
}

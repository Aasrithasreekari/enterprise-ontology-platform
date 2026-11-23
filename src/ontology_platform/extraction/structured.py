"""Structured extractors: one per normalized record kind."""

from __future__ import annotations

import json
from typing import Any

import yaml

from ontology_platform.connectors.base import (
    CloudResourcePayload,
    DocumentPayload,
    K8sWorkloadPayload,
    RecordKind,
    RepositoryPayload,
    SourceRecord,
    validate_payload,
)
from ontology_platform.errors import ValidationError
from ontology_platform.extraction import conventions as cv
from ontology_platform.extraction.text import chunk_text
from ontology_platform.ontology.models import (
    DocumentContent,
    EntityCandidate,
    EntityRef,
    ExtractionResult,
    RelationshipCandidate,
)
from ontology_platform.ontology.schema import EntityType as E
from ontology_platform.ontology.schema import RelationType as R
from ontology_platform.ontology.schema import normalize_tier


def _rel(rtype: R, source: EntityRef, target: EntityRef, confidence: float = 1.0, evidence: str | None = None, **props: Any) -> RelationshipCandidate:
    return RelationshipCandidate(type=rtype, source=source, target=target, confidence=confidence, evidence=evidence, properties=props)


def _named(t: E, name: str) -> EntityRef:
    return EntityRef.named(t, name)


# ============================================================================ repositories
_BACKSTAGE_REF_KINDS = {"component": E.SERVICE, "system": E.APPLICATION, "resource": E.DATABASE, "api": E.API, "domain": E.PRODUCT}


def _parse_entity_ref(raw: str, default_kind: str) -> tuple[E, str] | None:
    """Backstage entity ref 'kind:namespace/name' → (type, name)."""
    text = str(raw).strip()
    kind, _, rest = text.partition(":") if ":" in text else (default_kind, "", text)
    name = rest.split("/")[-1]
    etype = _BACKSTAGE_REF_KINDS.get(kind.lower())
    return (etype, name) if etype and name else None


def _load_yaml_docs(text: str) -> list[dict]:
    try:
        return [d for d in yaml.safe_load_all(text) if isinstance(d, dict)]
    except yaml.YAMLError as exc:
        raise ValidationError(f"invalid YAML: {exc}") from exc


def extract_repository(record: SourceRecord) -> ExtractionResult:
    repo: RepositoryPayload = validate_payload(record)  # type: ignore[assignment]
    out = ExtractionResult()
    repo_ref = EntityRef(type=E.REPOSITORY, identity={"provider": repo.provider, "path": repo.path})
    out.entities.append(
        EntityCandidate(
            type=E.REPOSITORY,
            identity=repo_ref.identity,
            name=repo.path,
            description=repo.description,
            aliases=[repo.name] + ([repo.url] if repo.url else []),
            properties={
                "provider": repo.provider,
                "url": repo.url,
                "topics": repo.topics,
                "default_branch": repo.default_branch,
                "language": repo.language,
                "archived": repo.archived,
            },
        )
    )
    catalog = repo.files.get("catalog-info.yaml") or repo.files.get("catalog-info.yml")
    if catalog:
        try:
            _extract_backstage(_load_yaml_docs(catalog), repo_ref, out)
        except ValidationError as exc:
            out.warnings.append(f"{record.external_id}: catalog-info.yaml skipped: {exc}")
    for path in ("openapi.yaml", "openapi.yml", "openapi.json"):
        spec_text = repo.files.get(path)
        if spec_text:
            try:
                _extract_openapi(spec_text, path, repo_ref, out)
            except (ValidationError, ValueError) as exc:
                out.warnings.append(f"{record.external_id}: {path} skipped: {exc}")
    readme = repo.files.get("README.md")
    if readme:
        doc_ref = EntityRef(type=E.DOCUMENTATION, identity={"source": repo.provider, "external_id": f"{repo.path}/README.md"})
        out.entities.append(
            EntityCandidate(
                type=E.DOCUMENTATION,
                identity=doc_ref.identity,
                name=f"{repo.name} README",
                properties={"url": f"{repo.url}#readme" if repo.url else None, "kind": "readme", "repository": repo.path},
            )
        )
        out.relationships.append(_rel(R.DESCRIBES, doc_ref, repo_ref, evidence="README.md of the repository"))
        out.documents.append(DocumentContent(entity=doc_ref, title=f"{repo.name} README", url=repo.url, text=readme))
    return out


def _extract_backstage(docs: list[dict], repo_ref: EntityRef, out: ExtractionResult) -> None:
    for doc in docs:
        kind = str(doc.get("kind", "")).lower()
        meta = doc.get("metadata") or {}
        spec = doc.get("spec") or {}
        name = meta.get("name")
        if not name:
            out.warnings.append("catalog entry without metadata.name skipped")
            continue
        description = meta.get("description")
        props = {k: v for k, v in {"owner": spec.get("owner"), "lifecycle": spec.get("lifecycle"), "tags": meta.get("tags")}.items() if v}
        evidence = f"catalog-info.yaml {doc.get('kind')} '{name}'"
        system = spec.get("system")
        app_ref = _named(E.APPLICATION, system) if system else None
        if kind == "component":
            ctype = str(spec.get("type", "service")).lower()
            if ctype in ("website", "application") and not system:
                self_ref = _named(E.APPLICATION, name)
                out.entities.append(EntityCandidate(type=E.APPLICATION, identity={"name": name}, description=description, properties=props))
            else:
                self_ref = _named(E.SERVICE, name)
                out.entities.append(
                    EntityCandidate(type=E.SERVICE, identity={"name": name}, description=description, properties={**props, "component_type": ctype})
                )
                out.relationships.append(_rel(R.IMPLEMENTS, repo_ref, self_ref, evidence=evidence))
                if app_ref:
                    out.relationships.append(_rel(R.CONTAINS, app_ref, self_ref, evidence=evidence))
            out.relationships.append(_rel(R.HAS, app_ref or self_ref, repo_ref, evidence=evidence))
            for dep in spec.get("dependsOn") or []:
                parsed = _parse_entity_ref(dep, "component")
                if parsed:
                    out.relationships.append(_rel(R.DEPENDS_ON, self_ref, _named(*parsed), evidence=f"{evidence} dependsOn {dep}"))
            for api in spec.get("providesApis") or []:
                parsed = _parse_entity_ref(api, "api")
                if parsed:
                    api_ref = _named(E.API, parsed[1])
                    out.relationships.append(_rel(R.EXPOSES, self_ref, api_ref, evidence=f"{evidence} providesApis {api}"))
                    if app_ref:
                        out.relationships.append(_rel(R.EXPOSES, app_ref, api_ref, evidence=f"{evidence} providesApis {api}"))
            for api in spec.get("consumesApis") or []:
                parsed = _parse_entity_ref(api, "api")
                if parsed:
                    out.relationships.append(_rel(R.DEPENDS_ON, self_ref, _named(E.API, parsed[1]), evidence=f"{evidence} consumesApis {api}"))
        elif kind == "system":
            app = _named(E.APPLICATION, name)
            out.entities.append(EntityCandidate(type=E.APPLICATION, identity={"name": name}, description=description, properties=props))
            out.relationships.append(_rel(R.HAS, app, repo_ref, evidence=evidence))
            if spec.get("domain"):
                out.relationships.append(_rel(R.CONTAINS, _named(E.PRODUCT, spec["domain"]), app, evidence=evidence))
        elif kind == "domain":
            out.entities.append(EntityCandidate(type=E.PRODUCT, identity={"name": name}, description=description, properties=props))
        elif kind == "api":
            api_ref = _named(E.API, name)
            out.entities.append(
                EntityCandidate(type=E.API, identity={"name": name}, description=description, properties={**props, "api_type": spec.get("type")})
            )
            out.relationships.append(_rel(R.IMPLEMENTS, repo_ref, api_ref, evidence=evidence))
            if app_ref:
                out.relationships.append(_rel(R.EXPOSES, app_ref, api_ref, evidence=evidence))
        elif kind == "resource":
            if str(spec.get("type", "")).lower() in ("database", "datastore", "db"):
                db = _named(E.DATABASE, name)
                out.entities.append(EntityCandidate(type=E.DATABASE, identity={"name": name}, description=description, properties=props))
                if app_ref:
                    out.relationships.append(_rel(R.CONTAINS, app_ref, db, evidence=evidence))
        else:
            out.warnings.append(f"unsupported catalog kind '{doc.get('kind')}' for '{name}'")


def _extract_openapi(text: str, path: str, repo_ref: EntityRef, out: ExtractionResult) -> None:
    spec = json.loads(text) if path.endswith(".json") else yaml.safe_load(text)
    if not isinstance(spec, dict) or "info" not in spec:
        raise ValidationError("not an OpenAPI document")
    info = spec.get("info") or {}
    name = info.get("x-api-name") or info.get("title")
    if not name:
        raise ValidationError("OpenAPI info.title missing")
    endpoints = []
    for route, methods in (spec.get("paths") or {}).items():
        for method in (methods or {}).keys():
            if method.lower() in {"get", "post", "put", "patch", "delete", "head", "options"}:
                endpoints.append(f"{method.upper()} {route}")
    servers = [s.get("url") for s in spec.get("servers") or [] if isinstance(s, dict) and s.get("url")]
    api_ref = _named(E.API, name)
    out.entities.append(
        EntityCandidate(
            type=E.API,
            identity={"name": name},
            name=info.get("title") or name,
            description=info.get("description"),
            aliases=[info.get("title")] if info.get("title") and info.get("title") != name else [],
            properties={"api_type": "openapi", "api_version": info.get("version"), "endpoints": sorted(endpoints), "servers": servers},
        )
    )
    out.relationships.append(_rel(R.IMPLEMENTS, repo_ref, api_ref, evidence=f"{path} in repository"))


# ============================================================================ documents
def extract_document(record: SourceRecord) -> ExtractionResult:
    doc: DocumentPayload = validate_payload(record)  # type: ignore[assignment]
    out = ExtractionResult()
    ref = EntityRef(type=E.DOCUMENTATION, identity={"source": doc.source, "external_id": doc.external_id})
    out.entities.append(
        EntityCandidate(
            type=E.DOCUMENTATION,
            identity=ref.identity,
            name=doc.title,
            properties={"url": doc.url, "space": doc.space, "labels": doc.labels, "kind": "page"},
        )
    )
    for label in doc.labels:
        prefix, sep, value = label.partition("-") if "-" in label else label.partition(":")
        etype = cv.DOC_LABEL_PREFIXES.get(prefix.lower()) if sep else None
        if etype and value:
            target = _named(E(etype), value) if etype != "repository" else None
            if target is not None:
                out.relationships.append(_rel(R.DESCRIBES, ref, target, confidence=0.9, evidence=f"page label '{label}'"))
    if doc.body.strip():
        out.documents.append(DocumentContent(entity=ref, title=doc.title, url=doc.url, text=doc.body))
    return out


# ============================================================================ kubernetes
def _cloud_ref(resource_id: str) -> EntityRef | None:
    provider = cv.provider_from_resource_id(resource_id)
    if provider is None:
        return None
    return EntityRef(type=E.CLOUD_RESOURCE, identity={"provider": provider, "resource_id": resource_id})


def extract_k8s_workload(record: SourceRecord) -> ExtractionResult:
    w: K8sWorkloadPayload = validate_payload(record)  # type: ignore[assignment]
    out = ExtractionResult()
    evidence = f"{w.kind} {w.namespace}/{w.name} on cluster {w.cluster}"
    cluster = _named(E.KUBERNETES_CLUSTER, w.cluster)
    out.entities.append(
        EntityCandidate(type=E.KUBERNETES_CLUSTER, identity={"name": w.cluster}, properties={"provider": w.cluster_provider} if w.cluster_provider else {})
    )
    service_name = cv.first_label(w.labels, cv.K8S_SERVICE_LABELS) or w.name
    app_name = cv.first_label(w.labels, cv.K8S_APPLICATION_LABELS)
    customer = cv.first_label(w.labels, cv.K8S_CUSTOMER_LABELS)
    tier_raw = cv.first_label(w.labels, cv.K8S_ENVIRONMENT_LABELS)
    if not customer or not tier_raw:
        ns_customer, ns_tier = cv.customer_tier_from_namespace(w.namespace)
        customer, tier_raw = customer or ns_customer, tier_raw or ns_tier
    tier = normalize_tier(tier_raw)
    if tier_raw and tier is None:
        out.warnings.append(f"{record.external_id}: unknown environment '{tier_raw}'")

    service = _named(E.SERVICE, service_name)
    out.entities.append(EntityCandidate(type=E.SERVICE, identity={"name": service_name}))
    out.relationships.append(_rel(R.RUNS_ON, service, cluster, evidence=evidence))
    app = _named(E.APPLICATION, app_name) if app_name else None
    if app:
        out.relationships.append(_rel(R.CONTAINS, app, service, confidence=0.9, evidence=evidence))
        out.relationships.append(_rel(R.RUNS_ON, app, cluster, evidence=evidence))

    for dep in filter(None, (s.strip() for s in w.annotations.get(cv.K8S_DEPENDS_ON_ANNOTATION, "").split(","))):
        out.relationships.append(_rel(R.DEPENDS_ON, service, _named(E.SERVICE, dep), confidence=0.9, evidence=f"{evidence} annotation depends-on"))

    if customer and tier:
        customer_ref = _named(E.CUSTOMER, customer)
        env = EntityRef(type=E.ENVIRONMENT, identity={"customer": customer, "tier": tier.value})
        out.entities.append(EntityCandidate(type=E.ENVIRONMENT, identity=env.identity, properties={"tier": tier.value}))
        out.relationships.append(_rel(R.BELONGS_TO, env, customer_ref, evidence=evidence))
        deployed_app = app_name or service_name
        dep_ref = EntityRef(type=E.DEPLOYMENT, identity={"application": deployed_app, "customer": customer, "tier": tier.value})
        workload = {"kind": w.kind, "namespace": w.namespace, "images": w.images, "replicas": w.replicas, "cluster": w.cluster}
        out.entities.append(
            EntityCandidate(
                type=E.DEPLOYMENT,
                identity=dep_ref.identity,
                properties={"tier": tier.value, "namespace": w.namespace, f"workload:{w.cluster}/{w.name}": workload},
            )
        )
        out.relationships.append(_rel(R.DEPLOYS, dep_ref, app or service, evidence=evidence))
        if app:
            out.relationships.append(_rel(R.DEPLOYS, dep_ref, service, evidence=evidence))
            out.relationships.append(_rel(R.DEPLOYED_IN, app, env, evidence=evidence))
        else:
            out.relationships.append(_rel(R.DEPLOYED_IN, service, env, evidence=evidence))
        out.relationships.append(_rel(R.DEPLOYED_IN, dep_ref, env, evidence=evidence))
        out.relationships.append(_rel(R.RUNS_ON, dep_ref, cluster, evidence=evidence))
        for rid in filter(None, (s.strip() for s in w.annotations.get(cv.K8S_USES_ANNOTATION, "").split(","))):
            ref = _cloud_ref(rid)
            if ref is None:
                out.warnings.append(f"{record.external_id}: unrecognized resource id in uses annotation: {rid}")
                continue
            out.relationships.append(_rel(R.USES, dep_ref, ref, evidence=f"{evidence} annotation uses"))
    return out


# ============================================================================ cloud resources
def extract_cloud_resource(record: SourceRecord) -> ExtractionResult:
    r: CloudResourcePayload = validate_payload(record)  # type: ignore[assignment]
    out = ExtractionResult()
    rtype = r.resource_type.lower()
    customer = cv.first_tag(r.tags, cv.TAG_CUSTOMER)
    tier = normalize_tier(cv.first_tag(r.tags, cv.TAG_ENVIRONMENT))
    application = cv.first_tag(r.tags, cv.TAG_APPLICATION)
    evidence = f"{r.provider} resource {r.resource_id} tags {r.tags}"
    ref = EntityRef(type=E.CLOUD_RESOURCE, identity={"provider": r.provider, "resource_id": r.resource_id})
    out.entities.append(
        EntityCandidate(
            type=E.CLOUD_RESOURCE,
            identity=ref.identity,
            name=r.name,
            customer=customer,
            aliases=[r.name],
            properties={"resource_type": rtype, "region": r.region, "account": r.account, "tags": r.tags, "provider": r.provider},
        )
    )
    if rtype in cv.KUBERNETES_CLUSTER_RESOURCE_TYPES:
        cname = cv.first_tag(r.tags, cv.TAG_CLUSTER) or r.name
        cluster = _named(E.KUBERNETES_CLUSTER, cname)
        out.entities.append(EntityCandidate(type=E.KUBERNETES_CLUSTER, identity={"name": cname}, properties={"provider": r.provider, "region": r.region}))
        out.relationships.append(_rel(R.HOSTED_ON, cluster, ref, evidence=evidence))
    if rtype in cv.DATABASE_RESOURCE_TYPES or cv.first_tag(r.tags, cv.TAG_DATABASE):
        dbname = cv.first_tag(r.tags, cv.TAG_DATABASE) or r.name
        db = _named(E.DATABASE, dbname)
        out.entities.append(EntityCandidate(type=E.DATABASE, identity={"name": dbname}, properties={"engine_resource_type": rtype}))
        out.relationships.append(_rel(R.HOSTED_ON, db, ref, evidence=evidence))
    if customer and tier:
        env = EntityRef(type=E.ENVIRONMENT, identity={"customer": customer, "tier": tier.value})
        out.entities.append(EntityCandidate(type=E.ENVIRONMENT, identity=env.identity, properties={"tier": tier.value}))
        out.relationships.append(_rel(R.BELONGS_TO, env, _named(E.CUSTOMER, customer), evidence=evidence))
        out.relationships.append(_rel(R.BELONGS_TO, ref, env, evidence=evidence))
        if application:
            dep = EntityRef(type=E.DEPLOYMENT, identity={"application": application, "customer": customer, "tier": tier.value})
            out.relationships.append(_rel(R.USES, dep, ref, evidence=evidence))
    elif customer:
        out.relationships.append(_rel(R.BELONGS_TO, ref, _named(E.CUSTOMER, customer), evidence=evidence))
    return out


# ============================================================================ catalog
def _catalog_ref(raw: dict) -> EntityRef:
    if not isinstance(raw, dict) or "type" not in raw:
        raise ValidationError(f"catalog reference must have a type: {raw}")
    identity = raw.get("identity") or ({"name": raw["name"]} if raw.get("name") else None)
    if not identity:
        raise ValidationError(f"catalog reference needs name or identity: {raw}")
    return EntityRef(type=E(raw["type"]), identity=identity)


def extract_catalog(record: SourceRecord) -> ExtractionResult:
    out = ExtractionResult()
    for i, raw in enumerate(record.payload.get("entities", [])):
        try:
            ref = _catalog_ref(raw)
            out.entities.append(
                EntityCandidate(
                    type=ref.type,
                    identity=ref.identity,
                    name=raw.get("display_name") or raw.get("name"),
                    description=raw.get("description"),
                    properties=raw.get("properties") or {},
                    aliases=raw.get("aliases") or [],
                    customer=raw.get("customer"),
                )
            )
        except (ValidationError, ValueError) as exc:
            out.warnings.append(f"{record.external_id} entity #{i}: {exc}")
    for i, raw in enumerate(record.payload.get("relationships", [])):
        try:
            out.relationships.append(
                RelationshipCandidate(
                    type=R(raw["type"]),
                    source=_catalog_ref(raw["source"]),
                    target=_catalog_ref(raw["target"]),
                    properties=raw.get("properties") or {},
                    confidence=float(raw.get("confidence", 1.0)),
                    evidence=f"catalog {record.external_id}",
                )
            )
        except (ValidationError, ValueError, KeyError) as exc:
            out.warnings.append(f"{record.external_id} relationship #{i}: {exc}")
    return out


EXTRACTORS = {
    RecordKind.REPOSITORY: extract_repository,
    RecordKind.DOCUMENT: extract_document,
    RecordKind.K8S_WORKLOAD: extract_k8s_workload,
    RecordKind.CLOUD_RESOURCE: extract_cloud_resource,
    RecordKind.CATALOG: extract_catalog,
}


def extract(record: SourceRecord) -> ExtractionResult:
    return EXTRACTORS[record.kind](record)


__all__ = ["extract", "EXTRACTORS", "chunk_text"]

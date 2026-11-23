"""Ontology definition: entity types, identity rules and relationship domain/range rules.

The ontology is declared as data so that new types can be added without touching the
ingestion, resolution or query code.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum


class EntityType(StrEnum):
    ORGANIZATION = "organization"
    CUSTOMER = "customer"
    PRODUCT = "product"
    APPLICATION = "application"
    REPOSITORY = "repository"
    SERVICE = "service"
    API = "api"
    DATABASE = "database"
    CLOUD_RESOURCE = "cloud_resource"
    KUBERNETES_CLUSTER = "kubernetes_cluster"
    DEPLOYMENT = "deployment"
    ENVIRONMENT = "environment"
    DOCUMENTATION = "documentation"


class RelationType(StrEnum):
    OWNS = "owns"
    HAS_CUSTOMER = "has_customer"
    CONTAINS = "contains"
    HAS = "has"
    EXPOSES = "exposes"
    DEPENDS_ON = "depends_on"
    DEPLOYED_IN = "deployed_in"
    BELONGS_TO = "belongs_to"
    RUNS_ON = "runs_on"
    USES = "uses"
    DESCRIBES = "describes"
    DEPLOYS = "deploys"
    IMPLEMENTS = "implements"
    HOSTED_ON = "hosted_on"


class EntityStatus(StrEnum):
    ACTIVE = "active"
    STUB = "stub"  # referenced by another source but not yet defined by its own source
    STALE = "stale"  # no longer observed by any source


class EnvironmentTier(StrEnum):
    DEV = "dev"
    SIT = "sit"
    UAT = "uat"
    STAGING = "staging"
    PROD = "prod"


TIER_ALIASES: dict[str, EnvironmentTier] = {
    "dev": EnvironmentTier.DEV,
    "development": EnvironmentTier.DEV,
    "sit": EnvironmentTier.SIT,
    "int": EnvironmentTier.SIT,
    "integration": EnvironmentTier.SIT,
    "system-integration": EnvironmentTier.SIT,
    "uat": EnvironmentTier.UAT,
    "acceptance": EnvironmentTier.UAT,
    "stage": EnvironmentTier.STAGING,
    "staging": EnvironmentTier.STAGING,
    "preprod": EnvironmentTier.STAGING,
    "prod": EnvironmentTier.PROD,
    "prd": EnvironmentTier.PROD,
    "production": EnvironmentTier.PROD,
    "live": EnvironmentTier.PROD,
}


def normalize_tier(value: str | None) -> EnvironmentTier | None:
    if not value:
        return None
    return TIER_ALIASES.get(value.strip().lower())


@dataclass(frozen=True)
class EntityTypeSpec:
    type: EntityType
    description: str
    identity_fields: tuple[str, ...]
    # Entities of this type are visible only to principals allowed for their customer.
    customer_scoped: bool = False


@dataclass(frozen=True)
class RelationTypeSpec:
    type: RelationType
    description: str
    # Allowed (source_type, target_type) pairs.
    allowed: frozenset[tuple[EntityType, EntityType]] = field(default_factory=frozenset)


E = EntityType
R = RelationType

ENTITY_TYPES: dict[EntityType, EntityTypeSpec] = {
    s.type: s
    for s in [
        EntityTypeSpec(E.ORGANIZATION, "The enterprise or a business unit operating the platform.", ("name",)),
        EntityTypeSpec(E.CUSTOMER, "A customer for whom products are deployed.", ("name",), customer_scoped=True),
        EntityTypeSpec(E.PRODUCT, "A commercial product composed of applications.", ("name",)),
        EntityTypeSpec(E.APPLICATION, "A deployable application/system (Backstage 'System').", ("name",)),
        EntityTypeSpec(E.REPOSITORY, "A source code repository.", ("provider", "path")),
        EntityTypeSpec(E.SERVICE, "A runtime service/component (Backstage 'Component').", ("name",)),
        EntityTypeSpec(E.API, "An API surface exposed by an application or service.", ("name",)),
        EntityTypeSpec(E.DATABASE, "A logical database or data store.", ("name",)),
        EntityTypeSpec(E.CLOUD_RESOURCE, "A resource in AWS/Azure/GCP.", ("provider", "resource_id")),
        EntityTypeSpec(E.KUBERNETES_CLUSTER, "A Kubernetes cluster.", ("name",)),
        EntityTypeSpec(
            E.DEPLOYMENT,
            "One deployment of an application for a customer in an environment tier.",
            ("application", "customer", "tier"),
            customer_scoped=True,
        ),
        EntityTypeSpec(
            E.ENVIRONMENT,
            "A customer-specific environment such as SIT, UAT or Production.",
            ("customer", "tier"),
            customer_scoped=True,
        ),
        EntityTypeSpec(E.DOCUMENTATION, "A documentation page or README.", ("source", "external_id")),
    ]
}


def _pairs(sources: list[EntityType], targets: list[EntityType]) -> frozenset[tuple[EntityType, EntityType]]:
    return frozenset((s, t) for s in sources for t in targets)


RELATION_TYPES: dict[RelationType, RelationTypeSpec] = {
    s.type: s
    for s in [
        RelationTypeSpec(R.OWNS, "Ownership of a product.", _pairs([E.CUSTOMER, E.ORGANIZATION], [E.PRODUCT])),
        RelationTypeSpec(R.HAS_CUSTOMER, "Organization serves a customer.", _pairs([E.ORGANIZATION], [E.CUSTOMER])),
        RelationTypeSpec(
            R.CONTAINS,
            "Composition.",
            _pairs([E.PRODUCT], [E.APPLICATION]) | _pairs([E.APPLICATION], [E.SERVICE, E.DATABASE]),
        ),
        RelationTypeSpec(R.HAS, "Application has a source repository.", _pairs([E.APPLICATION, E.SERVICE], [E.REPOSITORY])),
        RelationTypeSpec(R.EXPOSES, "Exposes an API.", _pairs([E.APPLICATION, E.SERVICE], [E.API])),
        RelationTypeSpec(
            R.DEPENDS_ON,
            "Runtime dependency.",
            _pairs([E.APPLICATION, E.SERVICE], [E.SERVICE, E.APPLICATION, E.DATABASE, E.API, E.CLOUD_RESOURCE]),
        ),
        RelationTypeSpec(
            R.DEPLOYED_IN, "Deployed into an environment.", _pairs([E.APPLICATION, E.DEPLOYMENT, E.SERVICE], [E.ENVIRONMENT])
        ),
        RelationTypeSpec(
            R.BELONGS_TO,
            "Belongs to an owner/scope.",
            _pairs([E.ENVIRONMENT], [E.CUSTOMER])
            | _pairs([E.CLOUD_RESOURCE, E.KUBERNETES_CLUSTER], [E.ENVIRONMENT, E.CUSTOMER])
            | _pairs([E.REPOSITORY, E.CUSTOMER], [E.ORGANIZATION]),
        ),
        RelationTypeSpec(
            R.RUNS_ON,
            "Runs on compute.",
            _pairs([E.APPLICATION, E.DEPLOYMENT, E.SERVICE], [E.KUBERNETES_CLUSTER, E.CLOUD_RESOURCE]),
        ),
        RelationTypeSpec(R.USES, "Deployment uses infrastructure.", _pairs([E.DEPLOYMENT], [E.CLOUD_RESOURCE, E.DATABASE])),
        RelationTypeSpec(
            R.DESCRIBES,
            "Documentation describes an entity.",
            _pairs(
                [E.DOCUMENTATION],
                [E.APPLICATION, E.REPOSITORY, E.SERVICE, E.API, E.PRODUCT, E.DATABASE, E.KUBERNETES_CLUSTER, E.CUSTOMER],
            ),
        ),
        RelationTypeSpec(R.DEPLOYS, "Deployment deploys an application/service.", _pairs([E.DEPLOYMENT], [E.APPLICATION, E.SERVICE])),
        RelationTypeSpec(R.IMPLEMENTS, "Repository implements a service/API.", _pairs([E.REPOSITORY], [E.SERVICE, E.API, E.APPLICATION])),
        RelationTypeSpec(
            R.HOSTED_ON,
            "Logical entity hosted on a cloud resource.",
            _pairs([E.DATABASE, E.KUBERNETES_CLUSTER], [E.CLOUD_RESOURCE]),
        ),
    ]
}


def is_relation_allowed(rel: RelationType, source: EntityType, target: EntityType) -> bool:
    spec = RELATION_TYPES.get(rel)
    return spec is not None and (source, target) in spec.allowed


def schema_as_dict() -> dict:
    """Serializable description of the ontology (used by REST/MCP introspection)."""
    return {
        "entity_types": [
            {
                "type": s.type.value,
                "description": s.description,
                "identity_fields": list(s.identity_fields),
                "customer_scoped": s.customer_scoped,
            }
            for s in ENTITY_TYPES.values()
        ],
        "relationship_types": [
            {
                "type": s.type.value,
                "description": s.description,
                "allowed": sorted([[a.value, b.value] for a, b in s.allowed]),
            }
            for s in RELATION_TYPES.values()
        ],
        "environment_tiers": [t.value for t in EnvironmentTier],
    }

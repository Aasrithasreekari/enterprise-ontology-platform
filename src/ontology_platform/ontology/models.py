"""Candidate models produced by extractors and consumed by the resolver."""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator

from ontology_platform.ontology.keys import canonical_key, customer_scope_for
from ontology_platform.ontology.schema import ENTITY_TYPES, EntityType, RelationType, normalize_tier


class EntityRef(BaseModel):
    """Reference to an entity by type + identity (resolvable to a canonical key)."""

    model_config = ConfigDict(frozen=True)

    type: EntityType
    identity: dict[str, str]

    @field_validator("identity")
    @classmethod
    def _identity_complete(cls, v: dict[str, str], info: Any) -> dict[str, str]:
        return {k: str(val) for k, val in v.items() if val is not None}

    @property
    def key(self) -> str:
        return canonical_key(self.type, self.identity)

    def display_name(self) -> str:
        return default_display_name(self.type, self.identity)

    @classmethod
    def named(cls, entity_type: EntityType, name: str) -> "EntityRef":
        return cls(type=entity_type, identity={"name": name})


def default_display_name(entity_type: EntityType, identity: dict[str, str]) -> str:
    if entity_type == EntityType.DEPLOYMENT:
        tier = normalize_tier(identity.get("tier")) or identity.get("tier")
        return f"{identity['application']} @ {identity['customer']} {str(tier).upper()}"
    if entity_type == EntityType.ENVIRONMENT:
        tier = normalize_tier(identity.get("tier")) or identity.get("tier")
        return f"{identity['customer']} {str(tier).upper()}"
    if entity_type == EntityType.REPOSITORY:
        return identity["path"]
    if entity_type == EntityType.CLOUD_RESOURCE:
        return identity["resource_id"]
    if entity_type == EntityType.DOCUMENTATION:
        return identity["external_id"]
    return identity.get("name", "")


class EntityCandidate(BaseModel):
    type: EntityType
    identity: dict[str, str]
    name: str | None = None
    description: str | None = None
    properties: dict[str, Any] = Field(default_factory=dict)
    aliases: list[str] = Field(default_factory=list)
    customer: str | None = Field(default=None, description="Explicit customer scope for otherwise shared types")
    confidence: float = Field(default=1.0, ge=0.0, le=1.0)
    evidence: str | None = None

    @field_validator("identity")
    @classmethod
    def _stringify(cls, v: dict[str, Any]) -> dict[str, str]:
        return {k: str(val) for k, val in v.items() if val is not None}

    @property
    def ref(self) -> EntityRef:
        return EntityRef(type=self.type, identity=self.identity)

    @property
    def key(self) -> str:
        return canonical_key(self.type, self.identity)

    @property
    def display_name(self) -> str:
        return self.name or default_display_name(self.type, self.identity)

    @property
    def customer_scope(self) -> str | None:
        return customer_scope_for(self.type, self.identity, self.customer)

    def missing_identity_fields(self) -> list[str]:
        return [f for f in ENTITY_TYPES[self.type].identity_fields if not self.identity.get(f)]


class RelationshipCandidate(BaseModel):
    type: RelationType
    source: EntityRef
    target: EntityRef
    properties: dict[str, Any] = Field(default_factory=dict)
    confidence: float = Field(default=1.0, ge=0.0, le=1.0)
    evidence: str | None = None


class ExtractionResult(BaseModel):
    entities: list[EntityCandidate] = Field(default_factory=list)
    relationships: list[RelationshipCandidate] = Field(default_factory=list)
    # Plain-text content to index for search (documentation bodies), keyed by entity ref.
    documents: list["DocumentContent"] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)

    def extend(self, other: "ExtractionResult") -> None:
        self.entities.extend(other.entities)
        self.relationships.extend(other.relationships)
        self.documents.extend(other.documents)
        self.warnings.extend(other.warnings)


class DocumentContent(BaseModel):
    entity: EntityRef
    title: str
    url: str | None = None
    text: str


ExtractionResult.model_rebuild()

"""Normalization and canonical identity keys."""

from __future__ import annotations

import re
from collections.abc import Mapping

from ontology_platform.errors import ValidationError
from ontology_platform.ontology.schema import ENTITY_TYPES, EntityType, normalize_tier

_NON_SLUG = re.compile(r"[^a-z0-9]+")
_NON_KEY = re.compile(r"[^a-z0-9:/._@-]+")

# Token synonyms applied when comparing names (not when displaying them).
_TOKEN_SYNONYMS = {"svc": "service", "srv": "service", "db": "database", "k8s": "kubernetes"}


def slugify(value: str) -> str:
    """Lowercase, non-alphanumerics → '-', trimmed. 'Payments API v2' → 'payments-api-v2'."""
    return _NON_SLUG.sub("-", value.strip().lower()).strip("-")


def normalize_name(value: str) -> str:
    """Comparison form of a name: slug with token synonyms applied."""
    tokens = [t for t in slugify(value).split("-") if t]
    return "-".join(_TOKEN_SYNONYMS.get(t, t) for t in tokens)


def _normalize_key_part(field: str, value: object) -> str:
    if value is None or (isinstance(value, str) and not value.strip()):
        raise ValidationError(f"identity field '{field}' is empty")
    text = str(value)
    if field == "tier":
        tier = normalize_tier(text)
        if tier is None:
            raise ValidationError(f"unknown environment tier '{text}'")
        return tier.value
    if field in ("resource_id", "path", "external_id"):
        return _NON_KEY.sub("-", text.strip().lower()).strip("-")
    return normalize_name(text)


def canonical_key(entity_type: EntityType, identity: Mapping[str, object]) -> str:
    spec = ENTITY_TYPES[entity_type]
    parts = [_normalize_key_part(f, identity.get(f)) for f in spec.identity_fields]
    return f"{entity_type.value}:{'/'.join(parts)}"


def customer_scope_for(entity_type: EntityType, identity: Mapping[str, object], explicit: str | None) -> str | None:
    """Customer an entity is scoped to (for ABAC), or None for shared entities."""
    if entity_type == EntityType.CUSTOMER:
        return normalize_name(str(identity["name"]))
    if "customer" in ENTITY_TYPES[entity_type].identity_fields:
        return normalize_name(str(identity["customer"]))
    return normalize_name(explicit) if explicit else None

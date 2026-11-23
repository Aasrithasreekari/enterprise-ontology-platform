import pytest

from ontology_platform.errors import ValidationError
from ontology_platform.ontology.keys import canonical_key, customer_scope_for, normalize_name
from ontology_platform.ontology.models import EntityCandidate, EntityRef
from ontology_platform.ontology.schema import (
    ENTITY_TYPES,
    RELATION_TYPES,
    EntityType,
    RelationType,
    is_relation_allowed,
    normalize_tier,
    schema_as_dict,
)


def test_all_required_entity_types_present():
    required = {"organization", "customer", "product", "application", "repository", "service", "api", "database",
                "cloud_resource", "kubernetes_cluster", "deployment", "environment", "documentation"}
    assert required == {t.value for t in ENTITY_TYPES}


@pytest.mark.parametrize(
    "rel,src,tgt",
    [
        ("owns", "customer", "product"),
        ("contains", "product", "application"),
        ("has", "application", "repository"),
        ("exposes", "application", "api"),
        ("depends_on", "application", "service"),
        ("deployed_in", "application", "environment"),
        ("belongs_to", "environment", "customer"),
        ("runs_on", "application", "kubernetes_cluster"),
        ("uses", "deployment", "cloud_resource"),
        ("describes", "documentation", "application"),
    ],
)
def test_required_relationships_allowed(rel, src, tgt):
    assert is_relation_allowed(RelationType(rel), EntityType(src), EntityType(tgt))


def test_invalid_relationship_rejected():
    assert not is_relation_allowed(RelationType.OWNS, EntityType.REPOSITORY, EntityType.CUSTOMER)
    assert not is_relation_allowed(RelationType.DESCRIBES, EntityType.APPLICATION, EntityType.DOCUMENTATION)


@pytest.mark.parametrize("raw,expected", [("PROD", "prod"), ("Production", "prod"), ("prd", "prod"), ("SIT", "sit"), ("acceptance", "uat"), ("nope", None)])
def test_tier_normalization(raw, expected):
    tier = normalize_tier(raw)
    assert (tier.value if tier else None) == expected


def test_name_normalization_applies_synonyms():
    assert normalize_name("Payments SVC") == normalize_name("payments-service") == "payments-service"
    assert normalize_name("  Payments_API v2 ") == "payments-api-v2"


def test_canonical_keys_are_type_specific():
    assert canonical_key(EntityType.APPLICATION, {"name": "Payments Platform"}) == "application:payments-platform"
    assert canonical_key(EntityType.REPOSITORY, {"provider": "GitHub", "path": "Acme/Repo"}) == "repository:github/acme/repo"
    assert canonical_key(EntityType.ENVIRONMENT, {"customer": "Acme", "tier": "Production"}) == "environment:acme/prod"


def test_multi_customer_deployments_are_distinct():
    a = canonical_key(EntityType.DEPLOYMENT, {"application": "payments", "customer": "acme", "tier": "prod"})
    b = canonical_key(EntityType.DEPLOYMENT, {"application": "payments", "customer": "globex", "tier": "prod"})
    c = canonical_key(EntityType.DEPLOYMENT, {"application": "payments", "customer": "acme", "tier": "uat"})
    assert len({a, b, c}) == 3
    # Same deployment expressed with tier aliases resolves to the same key.
    assert a == canonical_key(EntityType.DEPLOYMENT, {"application": "Payments", "customer": "ACME", "tier": "production"})


def test_missing_identity_field_raises():
    with pytest.raises(ValidationError):
        canonical_key(EntityType.DEPLOYMENT, {"application": "x", "customer": "y"})
    with pytest.raises(ValidationError):
        canonical_key(EntityType.ENVIRONMENT, {"customer": "acme", "tier": "moon"})


def test_customer_scope():
    assert customer_scope_for(EntityType.CUSTOMER, {"name": "Acme"}, None) == "acme"
    assert customer_scope_for(EntityType.ENVIRONMENT, {"customer": "Acme", "tier": "prod"}, None) == "acme"
    assert customer_scope_for(EntityType.APPLICATION, {"name": "x"}, None) is None
    assert customer_scope_for(EntityType.CLOUD_RESOURCE, {"provider": "aws", "resource_id": "arn"}, "Globex") == "globex"


def test_candidate_helpers():
    cand = EntityCandidate(type=EntityType.DEPLOYMENT, identity={"application": "pay", "customer": "acme", "tier": "prod"})
    assert cand.display_name == "pay @ acme PROD"
    assert cand.customer_scope == "acme"
    assert cand.missing_identity_fields() == []
    assert EntityRef.named(EntityType.SERVICE, "x").key == "service:x"


def test_schema_dict_serializable():
    d = schema_as_dict()
    assert len(d["entity_types"]) == len(ENTITY_TYPES)
    assert len(d["relationship_types"]) == len(RELATION_TYPES)

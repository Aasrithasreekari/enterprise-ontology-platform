import pytest

from ontology_platform.ontology.models import EntityCandidate, EntityRef
from ontology_platform.ontology.schema import EntityType as E
from ontology_platform.resolution.resolver import EntityResolver
from ontology_platform.storage.repository import GraphRepository, Scope


@pytest.fixture
def repo(fresh):
    with fresh.db.session() as s:
        yield GraphRepository(s, Scope("t1"))


def test_exact_key_match_merges_properties(repo):
    r = EntityResolver(repo)
    a = r.resolve(EntityCandidate(type=E.SERVICE, identity={"name": "ledger-service"}, properties={"owner": "a"}), "run1")
    b = EntityResolver(repo).resolve(EntityCandidate(type=E.SERVICE, identity={"name": "Ledger Service"}, properties={"lang": "go"}), "run2")
    assert a.created and not b.created
    assert b.method == "key" and b.entity.id == a.entity.id
    assert b.entity.properties == {"owner": "a", "lang": "go"}
    assert b.entity.version == 2


def test_unchanged_observation_does_not_bump_version(repo):
    cand = EntityCandidate(type=E.SERVICE, identity={"name": "x-svc"}, properties={"a": 1})
    EntityResolver(repo).resolve(cand, "r1")
    res = EntityResolver(repo).resolve(cand, "r2")
    assert not res.changed and res.entity.version == 1


def test_alias_match(repo):
    first = EntityResolver(repo).resolve(
        EntityCandidate(type=E.CUSTOMER, identity={"name": "acme"}, name="Acme Bank", aliases=["Acme Bank Plc"]), None
    )
    res = EntityResolver(repo).resolve(EntityCandidate(type=E.CUSTOMER, identity={"name": "Acme Bank Plc"}), None)
    assert res.method == "alias" and res.entity.id == first.entity.id


def test_fuzzy_match_for_name_identified_types(repo):
    first = EntityResolver(repo).resolve(EntityCandidate(type=E.APPLICATION, identity={"name": "payments-platform"}), None)
    res = EntityResolver(repo, fuzzy_threshold=0.9).resolve(EntityCandidate(type=E.APPLICATION, identity={"name": "payment-platform"}), None)
    assert res.method == "fuzzy" and res.entity.id == first.entity.id


def test_no_fuzzy_match_below_threshold(repo):
    EntityResolver(repo).resolve(EntityCandidate(type=E.APPLICATION, identity={"name": "payments-platform"}), None)
    res = EntityResolver(repo).resolve(EntityCandidate(type=E.APPLICATION, identity={"name": "payroll-portal"}), None)
    assert res.created


def test_structured_identities_never_fuzzy_merge(repo):
    a = EntityResolver(repo).resolve(EntityCandidate(type=E.CLOUD_RESOURCE, identity={"provider": "aws", "resource_id": "arn:aws:rds:x:1:db:payments-db-1"}), None)
    b = EntityResolver(repo).resolve(EntityCandidate(type=E.CLOUD_RESOURCE, identity={"provider": "aws", "resource_id": "arn:aws:rds:x:1:db:payments-db-2"}), None)
    assert a.entity.id != b.entity.id


def test_deployments_of_different_customers_stay_distinct(repo):
    ids = set()
    for customer in ("acme", "globex"):
        res = EntityResolver(repo).resolve(
            EntityCandidate(type=E.DEPLOYMENT, identity={"application": "payments", "customer": customer, "tier": "prod"}), None
        )
        ids.add(res.entity.id)
        assert res.entity.customer_scope == customer
    assert len(ids) == 2


def test_stub_is_promoted_by_defining_source(repo):
    stub = EntityResolver(repo).resolve_ref(EntityRef.named(E.SERVICE, "fraud-detector"), "r1")
    assert stub.entity.status == "stub"
    defined = EntityResolver(repo).resolve(EntityCandidate(type=E.SERVICE, identity={"name": "fraud-detector"}, description="ML scoring"), "r2")
    assert defined.entity.id == stub.entity.id
    assert defined.entity.status == "active" and defined.entity.description == "ML scoring"
    history = repo.history("entity", defined.entity.id)
    assert [h.version for h in history] == [1, 2]


def test_reference_does_not_overwrite_defined_entity(repo):
    EntityResolver(repo).resolve(EntityCandidate(type=E.SERVICE, identity={"name": "auth"}, properties={"k": "v"}), "r1")
    ref = EntityResolver(repo).resolve_ref(EntityRef.named(E.SERVICE, "auth"), "r2")
    assert ref.entity.status == "active" and not ref.changed and ref.entity.properties == {"k": "v"}

"""Entity resolution: map candidates onto existing graph entities.

Resolution order (first hit wins):
1. canonical key (type-specific identity, normalized)
2. alias match (any name/alias previously observed for the same type)
3. fuzzy name match (same type, name-identified types only, SequenceMatcher ≥ threshold)

Types identified by structured ids (repository path, cloud resource id, deployment triple...)
never fuzzy-match: a near-identical ARN is a different resource. Deployments and environments
are additionally never merged across customers because the customer is part of their identity.
"""

from __future__ import annotations

from dataclasses import dataclass
from difflib import SequenceMatcher
from typing import Literal

from ontology_platform.observability import get_logger
from ontology_platform.ontology.keys import normalize_name
from ontology_platform.ontology.models import EntityCandidate, EntityRef
from ontology_platform.ontology.schema import ENTITY_TYPES, EntityStatus
from ontology_platform.storage.orm import Entity
from ontology_platform.storage.repository import GraphRepository

log = get_logger(__name__)

MatchMethod = Literal["key", "alias", "fuzzy", "new"]


@dataclass
class Resolution:
    entity: Entity
    method: MatchMethod
    score: float
    created: bool
    changed: bool


def _name_identified(entity_type) -> bool:
    return ENTITY_TYPES[entity_type].identity_fields == ("name",)


class EntityResolver:
    def __init__(self, repo: GraphRepository, fuzzy_threshold: float = 0.92) -> None:
        self.repo = repo
        self.fuzzy_threshold = fuzzy_threshold
        self._cache: dict[str, Entity] = {}  # canonical key → entity, valid for one run/session

    def match(self, candidate: EntityCandidate | EntityRef) -> tuple[Entity | None, MatchMethod, float]:
        key = candidate.key
        if key in self._cache:
            return self._cache[key], "key", 1.0
        entity = self.repo.find_by_key(key)
        if entity is not None:
            return entity, "key", 1.0
        etype = candidate.type
        if not _name_identified(etype):
            return None, "new", 0.0
        names = [candidate.identity.get("name", "")]
        if isinstance(candidate, EntityCandidate):
            names += [candidate.name or ""] + candidate.aliases
        for raw in names:
            norm = normalize_name(raw) if raw else ""
            if not norm:
                continue
            hits = self.repo.find_by_alias(etype.value, norm)
            if len(hits) == 1:
                return hits[0], "alias", 0.98
            if len(hits) > 1:
                log.warning("ambiguous alias", extra={"alias": norm, "type": etype.value, "count": len(hits)})
                return None, "new", 0.0
        target = normalize_name(candidate.identity.get("name", ""))
        best: tuple[Entity | None, float] = (None, 0.0)
        for other in self.repo.fuzzy_candidates(etype.value, target):
            score = SequenceMatcher(None, target, other.name_norm).ratio()
            if score > best[1]:
                best = (other, score)
        if best[0] is not None and best[1] >= self.fuzzy_threshold:
            return best[0], "fuzzy", best[1]
        return None, "new", 0.0

    def resolve(self, candidate: EntityCandidate, run_id: str | None, stub: bool = False) -> Resolution:
        """Find-or-create the entity for ``candidate`` and merge its observations."""
        entity, method, score = self.match(candidate)
        props = dict(candidate.properties)
        if entity is None:
            entity = self.repo.create_entity(
                entity_type=candidate.type.value,
                canonical_key=candidate.key,
                name=candidate.display_name,
                identity=candidate.identity,
                description=candidate.description,
                properties=props,
                customer_scope=candidate.customer_scope,
                status=EntityStatus.STUB if stub else EntityStatus.ACTIVE,
                run_id=run_id,
            )
            for alias in candidate.aliases:
                self.repo.add_alias(entity, alias)
            self._cache[candidate.key] = entity
            return Resolution(entity, "new", 1.0, created=True, changed=True)
        if method != "key":
            # Remember the candidate's own key form as an alias so the next lookup is exact.
            self.repo.add_alias(entity, candidate.display_name)
        for alias in candidate.aliases:
            self.repo.add_alias(entity, alias)
        if stub:
            # A mere reference must not overwrite or reactivate a defined entity.
            changed = False
            if entity.status == EntityStatus.STALE.value:
                changed = self.repo.update_entity(
                    entity, name=None, description=None, properties={}, customer_scope=None, promote=True, run_id=run_id
                )
        else:
            changed = self.repo.update_entity(
                entity,
                name=candidate.name,
                description=candidate.description,
                properties=props,
                customer_scope=candidate.customer_scope,
                promote=True,
                run_id=run_id,
            )
        self._cache[candidate.key] = entity
        return Resolution(entity, method, score, created=False, changed=changed)

    def resolve_ref(self, ref: EntityRef, run_id: str | None) -> Resolution:
        """Resolve a relationship endpoint; unknown endpoints become stub entities."""
        return self.resolve(EntityCandidate(type=ref.type, identity=ref.identity), run_id, stub=True)

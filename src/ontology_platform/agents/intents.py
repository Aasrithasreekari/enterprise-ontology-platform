"""Multi-label intent classification over a question, reusing the platform's entity linker.

A question can carry several intents ("which applications depend on X, and where are they
deployed?" → dependents + deployments). Keyword rules run on the question with linked entity names
masked out, so an entity called "document-intake" does not trigger the documentation intent.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from ontology_platform.agents.models import Intent
from ontology_platform.query.engine import QueryEngine
from ontology_platform.query.qa import EntityLinker, Linked

_RULES: list[tuple[Intent, str]] = [
    (Intent.IMPACT, r"\b(affected|impact\w*|goes? down|outage|fail\w*|blast radius|break\w*|happens? if)\b"),
    (Intent.DEPENDENTS, r"\b(depend\w* on|rel(y|ies) on|dependents|consumers? of|callers? of|uses?)\b"),
    (Intent.DEPENDENCIES, r"\b(depend\w*|dependenc\w*|calls?|downstream)\b"),
    (Intent.DEPLOYMENTS, r"\bdeploy\w*|\bwhere\b.*\b(run|running|hosted|live)\b|\benvironments?\b|\b(sit|uat|prod\w*)\b"),
    (Intent.CLUSTERS, r"\bclusters?\b|\bkubernetes\b|\bk8s\b"),
    (Intent.REPOSITORIES, r"\brepo(s|sitor(y|ies))?\b|\bsource code\b"),
    (Intent.APIS, r"\bapis?\b|\bendpoints?\b"),
    (Intent.DOCUMENTATION, r"\bdoc\w*|\bdescrib\w*|\brunbooks?\b|\bwiki\b|\bconfluence\b|\breadme\b|\bguide\b|\bauthenticat\w*"),
    (Intent.RELATIONSHIP, r"\brelationship\b|\brelated\b|\bconnected\b|\bconnection\b|\blinks?\b|\bhow\b.*\bconnect"),
]

# Phrases where the asked-about entity is the *target* of dependencies ("which X depend on Y").
_DEPENDENTS_PHRASE = re.compile(r"\b(which|what)\b[\w\s,]*\b(depend\w*|rel(y|ies)|call)\b\s+(on|upon)?\b", re.I)


_EXPLICIT_RELATIONSHIP = re.compile(r"relationship (between|of)|connection between|connected (to|with)|how (is|are).*(connected|related)", re.I)


@dataclass
class Classification:
    intents: list[Intent]
    linked: list[Linked]
    masked_question: str
    notes: list[str] = field(default_factory=list)


def classify(engine: QueryEngine, question: str) -> Classification:
    linked = EntityLinker(engine).link(question)
    masked = question.lower()
    for item in sorted(linked, key=lambda x: -len(x.text)):
        masked = masked.replace(item.text.lower(), " ")

    found: list[Intent] = [intent for intent, pattern in _RULES if re.search(pattern, masked)]
    notes: list[str] = []
    # "depends on X" asks for dependents (upstream); "what does X depend on" asks for dependencies.
    if Intent.DEPENDENTS in found and Intent.DEPENDENCIES in found:
        if _DEPENDENTS_PHRASE.search(masked) and not re.search(r"\bdoes\b|\bdo\b", masked):
            found.remove(Intent.DEPENDENCIES)
        else:
            found.remove(Intent.DEPENDENTS)
    # Impact analysis already includes the dependents.
    if Intent.IMPACT in found and Intent.DEPENDENTS in found:
        found.remove(Intent.DEPENDENTS)
    # "Uses" alone is too weak to mean dependents unless no other intent matched.
    if found == [Intent.DEPENDENTS] and not re.search(r"depend|rel(y|ies)|dependents|consumer|caller", masked):
        found = []
    # Environment words alone ("in prod") qualify other intents rather than asking for deployments.
    if Intent.DEPLOYMENTS in found and len(found) > 1 and not re.search(r"deploy|where|environments?", masked):
        found.remove(Intent.DEPLOYMENTS)
    # Words like "linked"/"related" inside repo/cluster questions don't make them relationship questions;
    # an explicit "relationship between"/"connected to" does.
    if Intent.RELATIONSHIP in found and len(found) > 1 and not _EXPLICIT_RELATIONSHIP.search(masked):
        found.remove(Intent.RELATIONSHIP)
    if not found:
        if len(linked) >= 2:
            found = [Intent.RELATIONSHIP]
            notes.append("no explicit intent; multiple entities linked, so their connection is explained")
        elif linked:
            found = [Intent.ENTITY_LOOKUP]
        else:
            found = [Intent.DOCUMENTATION]
            notes.append("no entity recognised; falling back to documentation search")
    return Classification(intents=found, linked=linked, masked_question=masked, notes=notes)

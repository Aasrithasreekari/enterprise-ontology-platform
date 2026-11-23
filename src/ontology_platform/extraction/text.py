"""Unstructured text processing: chunking and entity-mention extraction."""

from __future__ import annotations

import re
from collections import defaultdict
from dataclasses import dataclass

from ontology_platform.ontology.keys import normalize_name

_PARA_SPLIT = re.compile(r"\n\s*\n|\n(?=#)")
_TOKEN = re.compile(r"[A-Za-z0-9]+")

# Aliases too generic to be treated as mentions on their own.
GENERIC_ALIASES = {
    "api", "app", "application", "service", "services", "data", "core", "web", "platform", "prod", "production",
    "test", "main", "readme", "docs", "database", "cluster", "customer", "product", "the", "and", "uat", "sit", "dev",
}

MAX_NGRAM = 6


def chunk_text(text: str, max_chars: int = 900) -> list[str]:
    """Split text into paragraph-aligned chunks of at most ~max_chars."""
    paragraphs = [p.strip() for p in _PARA_SPLIT.split(text) if p.strip()]
    chunks: list[str] = []
    current = ""
    for para in paragraphs:
        while len(para) > max_chars:  # hard-wrap very long paragraphs
            head, para = para[:max_chars], para[max_chars:]
            if current:
                chunks.append(current)
                current = ""
            chunks.append(head)
        if current and len(current) + len(para) + 2 > max_chars:
            chunks.append(current)
            current = para
        else:
            current = f"{current}\n\n{para}" if current else para
    if current:
        chunks.append(current)
    return chunks


@dataclass(frozen=True)
class Mention:
    entity_id: str
    entity_type: str
    alias: str
    snippet: str
    in_title: bool


class MentionExtractor:
    """Finds mentions of known entities in text by n-gram matching against normalized aliases.

    An alias naming entities of different types (e.g. the ``payments-service`` repository and
    service) links to all of them; an alias naming two entities of the *same* type is ambiguous
    and ignored. Generic words and very short aliases are ignored to keep precision high.
    """

    def __init__(self, aliases: list[tuple[str, str, str]]) -> None:
        index: dict[str, set[tuple[str, str]]] = defaultdict(set)
        for entity_id, entity_type, alias in aliases:
            norm = normalize_name(alias)
            if len(norm) < 4 or norm in GENERIC_ALIASES:
                continue
            index[norm].add((entity_id, entity_type))
        self._index: dict[str, list[tuple[str, str]]] = {}
        for key, hits in index.items():
            types = [t for _, t in hits]
            if len(types) == len(set(types)):
                self._index[key] = sorted(hits)

    def find(self, text: str, title: str = "", exclude_ids: set[str] | None = None) -> list[Mention]:
        found: dict[str, Mention] = {}
        for source, is_title in ((title, True), (text, False)):
            if not source:
                continue
            tokens = list(_TOKEN.finditer(source))
            norms = [normalize_name(t.group()) for t in tokens]
            i = 0
            while i < len(tokens):
                matched = 0
                for n in range(min(MAX_NGRAM, len(tokens) - i), 0, -1):
                    key = "-".join(norms[i : i + n])
                    hit = self._index.get(key)
                    if hit is None:
                        continue
                    start, end = tokens[i].start(), tokens[i + n - 1].end()
                    snippet = source[max(0, start - 80) : min(len(source), end + 80)].replace("\n", " ").strip()
                    for entity_id, entity_type in hit:
                        if exclude_ids and entity_id in exclude_ids:
                            continue
                        prev = found.get(entity_id)
                        if prev is None or (is_title and not prev.in_title):
                            found[entity_id] = Mention(entity_id, entity_type, source[start:end], snippet, is_title)
                    matched = n
                    break
                i += matched or 1
        return list(found.values())

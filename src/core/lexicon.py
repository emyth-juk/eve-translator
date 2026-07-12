"""Longest-span replacement across SDE names, glossaries, and doctrine terms."""

from __future__ import annotations

import unicodedata
from dataclasses import dataclass
from typing import Iterable

from src.core.span_matching import LongestSpanMatcher, is_strict_latin_case


@dataclass(frozen=True)
class LexiconTerm:
    source: str
    target: str
    priority: int = 0


class LongestLexiconMatcher:
    """Replace longest matching terms, using priority only for equal spans.

    This is important for EVE's Chinese hull vocabulary: a player shorthand
    such as ``灾难`` must not replace the prefix of the official SDE name
    ``灾难级``. An exact custom/doctrine term may still override an equally long
    SDE term through a higher priority.
    """

    def __init__(self, terms: Iterable[LexiconTerm] = ()):
        deduped: dict[tuple[str, bool], LexiconTerm] = {}
        for item in terms:
            source = unicodedata.normalize("NFKC", item.source or "").strip()
            target = unicodedata.normalize("NFKC", item.target or "").strip()
            # Identity terms are deliberate no-op span guards. They prevent a
            # shorter player alias from rewriting part of a longer official
            # name that is spelled identically in both locales.
            if not source or not target:
                continue
            strict_case = is_strict_latin_case(source)
            key = (source if strict_case else source.casefold(), strict_case)
            existing = deduped.get(key)
            normalized = LexiconTerm(source, target, int(item.priority))
            if existing is None or normalized.priority > existing.priority:
                deduped[key] = normalized

        self._matcher = LongestSpanMatcher(
            deduped.values(),
            source=lambda term: term.source,
            strict_case=lambda term: is_strict_latin_case(term.source),
            order_key=lambda term: (-len(term.source), -term.priority, term.source),
        )

    @property
    def available(self) -> bool:
        return self._matcher.available

    def replace(self, text: str) -> str:
        if not text or not self.available:
            return text or ""
        parts: list[str] = []
        cursor = 0
        changed = False
        while cursor < len(text):
            match = self._matcher.match(text, cursor)
            if not match:
                parts.append(text[cursor])
                cursor += 1
                continue
            end, term = match
            if term.source == term.target:
                # A no-op official span is a guard, not a replacement. Keep
                # punctuation, possessives, casing, and whitespace byte-for-byte.
                parts.append(text[cursor:end])
            else:
                parts.append(f" {term.target} ")
                changed = True
            cursor = end
        replaced = "".join(parts)
        return " ".join(replaced.split()) if changed else replaced

"""Longest-span replacement across SDE names, glossaries, and doctrine terms."""

from __future__ import annotations

import unicodedata
from dataclasses import dataclass
from typing import Iterable


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
            strict_case = self._strict_case(source)
            key = (source if strict_case else source.casefold(), strict_case)
            existing = deduped.get(key)
            normalized = LexiconTerm(source, target, int(item.priority))
            if existing is None or normalized.priority > existing.priority:
                deduped[key] = normalized

        self._buckets: dict[str, list[tuple[LexiconTerm, bool]]] = {}
        for term in deduped.values():
            bucket = self._bucket_key(term.source)
            if not bucket:
                continue
            self._buckets.setdefault(bucket, []).append(
                (term, self._strict_case(term.source))
            )
        for candidates in self._buckets.values():
            candidates.sort(
                key=lambda item: (-len(item[0].source), -item[0].priority, item[0].source)
            )

    @property
    def available(self) -> bool:
        return bool(self._buckets)

    @staticmethod
    def _strict_case(text: str) -> bool:
        return any(
            character.isalpha()
            and (
                character.isascii()
                or "LATIN" in unicodedata.name(character, "")
            )
            for character in text
        )

    @staticmethod
    def _is_word_character(character: str) -> bool:
        return character.isalnum() or character == "_"

    @staticmethod
    def _is_cjk_script(character: str) -> bool:
        return bool(character) and (
            "\u3400" <= character <= "\u9fff"
            or "\u3040" <= character <= "\u30ff"
            or "\uac00" <= character <= "\ud7af"
        )

    @classmethod
    def _bucket_key(cls, text: str, start: int = 0) -> str:
        if start >= len(text):
            return ""
        if cls._is_cjk_script(text[start]):
            return text[start].casefold()
        if cls._is_word_character(text[start]):
            cursor = start
            while cursor < len(text) and cls._is_word_character(text[cursor]):
                cursor += 1
            return text[start:cursor].casefold()
        return text[start].casefold()

    @classmethod
    def _boundary_ok(cls, text: str, start: int, end: int, source: str) -> bool:
        if cls._is_cjk_script(source[0]):
            return True
        if start > 0 and cls._is_word_character(text[start - 1]):
            return False
        if end < len(text) and cls._is_word_character(text[end]):
            return False
        return True

    def _match(self, text: str, start: int) -> tuple[int, LexiconTerm] | None:
        if (
            start > 0
            and self._is_word_character(text[start - 1])
            and not self._is_cjk_script(text[start])
        ):
            return None
        for term, strict_case in self._buckets.get(self._bucket_key(text, start), ()):
            end = start + len(term.source)
            if end > len(text) or not self._boundary_ok(text, start, end, term.source):
                continue
            source = text[start:end]
            matched = (
                source == term.source
                if strict_case
                else source.casefold() == term.source.casefold()
            )
            if matched:
                return end, term
        return None

    def replace(self, text: str) -> str:
        if not text or not self.available:
            return text or ""
        parts: list[str] = []
        cursor = 0
        changed = False
        while cursor < len(text):
            match = self._match(text, cursor)
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

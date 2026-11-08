"""Reusable longest-span matching for EVE names and translation terms."""

from __future__ import annotations

import unicodedata
from collections.abc import Callable, Iterable
from typing import Generic, TypeVar

Candidate = TypeVar("Candidate")

# Kana, CJK ideographs and Hangul syllables. These scripts are written without
# spaces, so they act as word boundaries for adjacent Latin names.
CJK_CHARACTER_RANGES = "぀-ヿ㐀-鿿가-힯"
# Regex lookarounds equivalent to ``\b`` for Latin/number words, except that a
# neighbouring CJK character counts as a boundary. Python's Unicode ``\w``
# matches Han, Kana and Hangul, so plain ``(?<!\w)`` rejects "打Raven".
NOT_AFTER_NON_CJK_WORD = rf"(?<![^\W{CJK_CHARACTER_RANGES}])"
NOT_BEFORE_NON_CJK_WORD = rf"(?![^\W{CJK_CHARACTER_RANGES}])"


def is_strict_latin_case(text: str) -> bool:
    """Return whether a term contains cased Latin text."""
    return any(
        character.isalpha()
        and (
            character.isascii()
            or "LATIN" in unicodedata.name(character, "")
        )
        for character in text
    )


def _is_word_character(character: str) -> bool:
    return character.isalnum() or character == "_"


def _is_cjk_script(character: str) -> bool:
    return bool(character) and (
        "\u3400" <= character <= "\u9fff"
        or "\u3040" <= character <= "\u30ff"
        or "\uac00" <= character <= "\ud7af"
    )


def _is_latin_word_character(character: str) -> bool:
    """Word character for Latin/number boundaries; CJK separates words."""
    return _is_word_character(character) and not _is_cjk_script(character)


def _bucket_key(text: str, start: int = 0) -> str:
    if start >= len(text):
        return ""
    if _is_cjk_script(text[start]):
        return text[start].casefold()
    if _is_word_character(text[start]):
        cursor = start
        while cursor < len(text) and _is_latin_word_character(text[cursor]):
            cursor += 1
        return text[start:cursor].casefold()
    return text[start].casefold()


def _candidate_bucket_key(source: str) -> str:
    # Multi-character CJK terms are bucketed by their first two characters.
    # A first-character bucket for common characters (大, 小, 中) held 500+
    # names, so every CJK character of a line scanned hundreds of candidates.
    if source and _is_cjk_script(source[0]) and len(source) >= 2:
        return source[:2].casefold()
    return _bucket_key(source)


def _is_standalone_cjk_span(text: str, start: int, end: int) -> bool:
    """Return whether a CJK span is not glued to other CJK characters."""
    return (start == 0 or not _is_cjk_script(text[start - 1])) and (
        end >= len(text) or not _is_cjk_script(text[end])
    )


class LongestSpanMatcher(Generic[Candidate]):
    """Index candidates and return the longest valid match at a text offset.

    Latin terms use whole-word boundaries, where an adjacent CJK character
    also counts as a boundary ("\u6253Raven"). Multi-character CJK terms
    intentionally need no boundary: EVE chat commonly joins them directly to
    other characters. A single CJK character, however, is usually part of an
    ordinary word ("\u6536\u5230", "\u88c5\u7532"), so it only matches when it stands alone.
    Callers own candidate normalization and may provide an ordering policy for
    equal or overlapping spans.
    """

    def __init__(
        self,
        candidates: Iterable[Candidate] = (),
        *,
        source: Callable[[Candidate], str],
        strict_case: Callable[[Candidate], bool],
        order_key: Callable[[Candidate], object] | None = None,
    ):
        self._buckets: dict[str, list[tuple[Candidate, str, bool]]] = {}
        for candidate in candidates:
            candidate_source = source(candidate)
            bucket = _candidate_bucket_key(candidate_source)
            if not candidate_source or not bucket:
                continue
            # Source text and case policy are immutable matcher metadata. Cache
            # them once so hot message scans do not repeat Unicode inspection.
            self._buckets.setdefault(bucket, []).append(
                (candidate, candidate_source, bool(strict_case(candidate)))
            )

        if order_key is None:
            order_key = lambda item: -len(source(item))
        for bucket_candidates in self._buckets.values():
            bucket_candidates.sort(key=lambda item: order_key(item[0]))

    @property
    def available(self) -> bool:
        return bool(self._buckets)

    def _candidates_at(self, text: str, start: int):
        """Yield bucket candidates longest-first for a text offset."""
        if _is_cjk_script(text[start]):
            # Two-character bucket first: all of its terms are longer than
            # any single-character term, preserving longest-span order.
            if start + 1 < len(text):
                yield from self._buckets.get(text[start:start + 2].casefold(), ())
            yield from self._buckets.get(text[start].casefold(), ())
            return
        yield from self._buckets.get(_bucket_key(text, start), ())

    def match(self, text: str, start: int) -> tuple[int, Candidate] | None:
        """Return the first ordered candidate that validly starts at ``start``."""
        if start >= len(text) or (
            start > 0
            and _is_latin_word_character(text[start - 1])
            and not _is_cjk_script(text[start])
        ):
            return None

        for candidate, candidate_source, strict_case in self._candidates_at(text, start):
            end = start + len(candidate_source)
            if end > len(text):
                continue
            if _is_cjk_script(candidate_source[0]):
                if len(candidate_source) == 1 and not _is_standalone_cjk_span(
                    text, start, end
                ):
                    continue
            elif (
                (start > 0 and _is_latin_word_character(text[start - 1]))
                or (end < len(text) and _is_latin_word_character(text[end]))
            ):
                continue
            source_span = text[start:end]
            matched = (
                source_span == candidate_source
                if strict_case
                else source_span.casefold() == candidate_source.casefold()
            )
            if matched:
                return end, candidate
        return None

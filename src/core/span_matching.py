"""Reusable longest-span matching for EVE names and translation terms."""

from __future__ import annotations

import unicodedata
from collections.abc import Callable, Iterable
from typing import Generic, TypeVar

Candidate = TypeVar("Candidate")


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


def _bucket_key(text: str, start: int = 0) -> str:
    if start >= len(text):
        return ""
    if _is_cjk_script(text[start]):
        return text[start].casefold()
    if _is_word_character(text[start]):
        cursor = start
        while cursor < len(text) and _is_word_character(text[cursor]):
            cursor += 1
        return text[start:cursor].casefold()
    return text[start].casefold()


class LongestSpanMatcher(Generic[Candidate]):
    """Index candidates and return the longest valid match at a text offset.

    Latin terms use whole-word boundaries. CJK terms intentionally do not:
    EVE chat commonly joins them directly to other characters. Callers own
    candidate normalization and may provide an ordering policy for equal or
    overlapping spans.
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
            bucket = _bucket_key(candidate_source)
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

    def match(self, text: str, start: int) -> tuple[int, Candidate] | None:
        """Return the first ordered candidate that validly starts at ``start``."""
        if start >= len(text) or (
            start > 0
            and _is_word_character(text[start - 1])
            and not _is_cjk_script(text[start])
        ):
            return None

        for candidate, candidate_source, strict_case in self._buckets.get(
            _bucket_key(text, start),
            (),
        ):
            end = start + len(candidate_source)
            if end > len(text):
                continue
            if not _is_cjk_script(candidate_source[0]) and (
                (start > 0 and _is_word_character(text[start - 1]))
                or (end < len(text) and _is_word_character(text[end]))
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

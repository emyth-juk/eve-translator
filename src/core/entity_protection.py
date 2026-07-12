"""Runtime protection for names extracted from a converted EVE SDE database."""

from __future__ import annotations

import json
import logging
import threading
import unicodedata
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

from src.core.languages import normalize_language_code
from src.utils.paths import get_resource_path

logger = logging.getLogger(__name__)
ENTITY_INDEX_SCHEMA_VERSION = 2
SUPPORTED_ENTITY_INDEX_SCHEMAS = frozenset({1, ENTITY_INDEX_SCHEMA_VERSION})


@dataclass(frozen=True)
class ProtectedEntity:
    name: str
    category: str
    strict_case: bool = True


@dataclass(frozen=True)
class EntityProtectionResult:
    text: str
    tokens: dict[str, str]


@dataclass(frozen=True)
class LocalizedEntity:
    type_id: int
    category: str
    names: Mapping[str, str]


@dataclass(frozen=True)
class _EntityAlias:
    source: str
    target: str
    strict_case: bool


class ProtectedEntityIndex:
    """A compact token/CJK bucket index built from app-specific JSON."""

    def __init__(
        self,
        entities: Iterable[ProtectedEntity] = (),
        metadata=None,
        localized_entities: Iterable[LocalizedEntity] = (),
    ):
        self.metadata = dict(metadata or {})
        self._buckets: dict[str, list[ProtectedEntity]] = {}
        self.entity_count = 0
        seen: set[tuple[str, str]] = set()
        for entity in entities:
            name = unicodedata.normalize("NFKC", entity.name or "").strip()
            key = (name.casefold(), entity.category)
            if len(name) < 2 or key in seen or "__EVEENTITY_" in name:
                continue
            seen.add(key)
            normalized_entity = ProtectedEntity(name, entity.category, entity.strict_case)
            bucket = self._bucket_key(name)
            if not bucket:
                continue
            self._buckets.setdefault(bucket, []).append(normalized_entity)
            self.entity_count += 1
        for candidates in self._buckets.values():
            candidates.sort(key=lambda item: len(item.name), reverse=True)
        self.localized_entities = tuple(localized_entities)
        self._translation_buckets: dict[
            tuple[str, str], dict[str, list[_EntityAlias]]
        ] = {}
        self._translation_maps: dict[tuple[str, str], dict[str, str]] = {}
        self._lexicon_maps: dict[tuple[str, str], dict[str, str]] = {}
        self._translation_lock = threading.Lock()

    @classmethod
    def empty(cls) -> "ProtectedEntityIndex":
        return cls()

    @classmethod
    def load(cls, path: str | Path | None = None) -> "ProtectedEntityIndex":
        resolved = Path(path) if path else Path(get_resource_path(
            "data/generated/protected_entities.json"
        ))
        if not resolved.exists():
            logger.info("Protected EVE entity index not installed: %s", resolved)
            return cls.empty()
        try:
            with resolved.open("r", encoding="utf-8") as index_file:
                payload = json.load(index_file)
            if payload.get("schema_version") not in SUPPORTED_ENTITY_INDEX_SCHEMAS:
                raise ValueError("unsupported entity-index schema")
            categories = payload.get("entities", {})
            if not isinstance(categories, dict):
                raise ValueError("entities must be grouped by category")
            entities: list[ProtectedEntity] = []
            for category, names in categories.items():
                if not isinstance(category, str) or not isinstance(names, list):
                    continue
                for item in names:
                    if isinstance(item, str):
                        entities.append(ProtectedEntity(item, category, True))
                    elif isinstance(item, dict) and isinstance(item.get("name"), str):
                        entities.append(ProtectedEntity(
                            item["name"], category, bool(item.get("strict_case", True))
                        ))
            localized_entities: list[LocalizedEntity] = []
            raw_localized = payload.get("localized_entities", [])
            if isinstance(raw_localized, list):
                for item in raw_localized:
                    if not isinstance(item, dict):
                        continue
                    type_id = item.get("type_id")
                    category = item.get("category")
                    names = item.get("names")
                    if (
                        isinstance(type_id, int)
                        and isinstance(category, str)
                        and isinstance(names, dict)
                    ):
                        normalized_names = {
                            cls._normalize_locale(locale): unicodedata.normalize(
                                "NFKC", name
                            ).strip()
                            for locale, name in names.items()
                            if isinstance(locale, str)
                            and isinstance(name, str)
                            and len(name.strip()) >= 2
                        }
                        if normalized_names.get("en"):
                            localized_entities.append(LocalizedEntity(
                                type_id=type_id,
                                category=category,
                                names=normalized_names,
                            ))
            metadata = payload.get("source", {})
            index = cls(
                entities,
                metadata if isinstance(metadata, dict) else {},
                localized_entities,
            )
            logger.info(
                "Loaded %s protected EVE entities from SDE %s",
                index.entity_count,
                index.sde_version or "unknown",
            )
            return index
        except (OSError, ValueError, TypeError, json.JSONDecodeError) as exc:
            logger.warning("Ignoring invalid protected entity index %s: %s", resolved, exc)
            return cls.empty()

    @property
    def available(self) -> bool:
        return self.entity_count > 0

    @property
    def sde_version(self) -> str:
        value = self.metadata.get("sde_build") or self.metadata.get("release") or ""
        return str(value)

    @staticmethod
    def _is_word_character(character: str) -> bool:
        return character.isalnum() or character == "_"

    @staticmethod
    def _is_cjk_script(character: str) -> bool:
        return bool(character) and (
            '\u3400' <= character <= '\u9fff'
            or '\u3040' <= character <= '\u30ff'
            or '\uac00' <= character <= '\ud7af'
        )

    def _boundary_ok(self, text: str, start: int, end: int) -> bool:
        if start > 0 and self._is_word_character(text[start - 1]):
            return False
        if end < len(text) and self._is_word_character(text[end]):
            return False
        return True

    @staticmethod
    def _normalize_locale(language: str | None) -> str:
        return normalize_language_code(language, "")

    @staticmethod
    def _strict_case(name: str) -> bool:
        return any(
            character.isalpha()
            and (
                character.isascii()
                or "LATIN" in unicodedata.name(character, "")
            )
            for character in name
        )

    @classmethod
    def _bucket_key(cls, text: str, start: int = 0) -> str:
        if start >= len(text):
            return ""
        cursor = start
        if cls._is_cjk_script(text[cursor]):
            return text[cursor].casefold()
        if cls._is_word_character(text[cursor]):
            while cursor < len(text) and cls._is_word_character(text[cursor]):
                cursor += 1
            return text[start:cursor].casefold()
        return text[start].casefold()

    def _longest_match(self, text: str, start: int) -> tuple[int, ProtectedEntity] | None:
        if (
            start > 0
            and self._is_word_character(text[start - 1])
            and not self._is_cjk_script(text[start])
        ):
            return None
        candidates = self._buckets.get(self._bucket_key(text, start), ())
        for entity in candidates:
            end = start + len(entity.name)
            is_cjk_name = self._is_cjk_script(entity.name[0])
            if end > len(text) or (
                not is_cjk_name and not self._boundary_ok(text, start, end)
            ):
                continue
            source = text[start:end]
            if entity.strict_case:
                matched = source == entity.name
            else:
                matched = source.casefold() == entity.name.casefold()
            if matched:
                return end, entity
        return None

    def _build_translation_buckets(
        self,
        source_language: str,
        target_language: str,
    ) -> dict[str, list[_EntityAlias]]:
        pair = (
            self._normalize_locale(source_language),
            self._normalize_locale(target_language) or "en",
        )
        cached = self._translation_buckets.get(pair)
        if cached is not None:
            return cached
        with self._translation_lock:
            cached = self._translation_buckets.get(pair)
            if cached is not None:
                return cached
            source_locale, target_locale = pair
            candidates: dict[str, tuple[str, dict[str, str]]] = {}
            for entity in self.localized_entities:
                source = entity.names.get(source_locale)
                target = entity.names.get(target_locale) or entity.names.get("en")
                if not source or not target:
                    continue
                key = source.casefold()
                source_value, targets = candidates.setdefault(key, (source, {}))
                if (source.casefold(), source) < (source_value.casefold(), source_value):
                    source_value = source
                    candidates[key] = (source_value, targets)
                targets[target.casefold()] = target

            buckets: dict[str, list[_EntityAlias]] = {}
            lexicon_map: dict[str, str] = {}
            for source, targets in candidates.values():
                # Ambiguous source names must not be guessed across type IDs.
                if len(targets) != 1:
                    # Still reserve the complete official span so a shorter
                    # glossary/doctrine alias cannot create a hybrid name.
                    lexicon_map[source] = source
                    continue
                target = next(iter(targets.values()))
                lexicon_map[source] = target
                alias = _EntityAlias(source, target, self._strict_case(source))
                bucket = self._bucket_key(source)
                if bucket:
                    buckets.setdefault(bucket, []).append(alias)
            for aliases in buckets.values():
                aliases.sort(key=lambda item: len(item.source), reverse=True)
            self._translation_maps[pair] = {
                alias.source: alias.target
                for aliases in buckets.values()
                for alias in aliases
            }
            self._lexicon_maps[pair] = lexicon_map
            # Publish the bucket cache last; its presence is the ready marker
            # read outside the lock by all three public views.
            self._translation_buckets[pair] = buckets
            return buckets

    def translations_for(
        self,
        source_language: str,
        target_language: str,
    ) -> Mapping[str, str]:
        """Return unambiguous official source-to-target type-name mappings."""
        pair = (
            self._normalize_locale(source_language),
            self._normalize_locale(target_language) or "en",
        )
        self._build_translation_buckets(*pair)
        return self._translation_maps.get(pair, {})

    def lexicon_terms_for(
        self,
        source_language: str,
        target_language: str,
    ) -> Mapping[str, str]:
        """Return official span mappings, including no-op ambiguity guards."""
        pair = (
            self._normalize_locale(source_language),
            self._normalize_locale(target_language) or "en",
        )
        self._build_translation_buckets(*pair)
        return self._lexicon_maps.get(pair, {})

    def _longest_alias_match(
        self,
        text: str,
        start: int,
        buckets: dict[str, list[_EntityAlias]],
    ) -> tuple[int, _EntityAlias] | None:
        if (
            start > 0
            and self._is_word_character(text[start - 1])
            and not self._is_cjk_script(text[start])
        ):
            return None
        for alias in buckets.get(self._bucket_key(text, start), ()):
            end = start + len(alias.source)
            is_cjk_name = self._is_cjk_script(alias.source[0])
            if end > len(text) or (
                not is_cjk_name and not self._boundary_ok(text, start, end)
            ):
                continue
            source = text[start:end]
            matched = (
                source == alias.source
                if alias.strict_case
                else source.casefold() == alias.source.casefold()
            )
            if matched:
                return end, alias
        return None

    def canonicalize(
        self,
        text: str,
        source_language: str,
        target_language: str,
    ) -> str:
        """Replace official localized type names with their target-locale name."""
        if not text or not self.localized_entities:
            return text or ""
        buckets = self._build_translation_buckets(source_language, target_language)
        if not buckets:
            return text
        parts: list[str] = []
        cursor = 0
        while cursor < len(text):
            match = self._longest_alias_match(text, cursor, buckets)
            if not match:
                parts.append(text[cursor])
                cursor += 1
                continue
            end, alias = match
            parts.append(alias.target)
            cursor = end
        return "".join(parts)

    def protect(self, text: str) -> EntityProtectionResult:
        if not text or not self.available:
            return EntityProtectionResult(text or "", {})
        parts: list[str] = []
        tokens: dict[str, str] = {}
        cursor = 0
        while cursor < len(text):
            match = self._longest_match(text, cursor)
            if not match:
                parts.append(text[cursor])
                cursor += 1
                continue
            end, _entity = match
            placeholder = f"__EVEENTITY_{len(tokens) + 1}__"
            while placeholder in text:
                placeholder = f"_{placeholder}_"
            tokens[placeholder] = text[cursor:end]
            parts.append(placeholder)
            cursor = end
        return EntityProtectionResult("".join(parts), tokens)

    @staticmethod
    def restore(text: str, tokens: dict[str, str]) -> str:
        result = text
        for placeholder, original in tokens.items():
            result = result.replace(placeholder, original)
        return result

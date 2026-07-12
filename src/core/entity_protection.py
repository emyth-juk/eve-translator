"""Runtime protection for names extracted from a converted EVE SDE database."""

from __future__ import annotations

import json
import logging
import threading
import unicodedata
from collections import OrderedDict
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

from src.core.languages import normalize_language_code
from src.core.span_matching import LongestSpanMatcher, is_strict_latin_case
from src.utils.paths import get_resource_path

logger = logging.getLogger(__name__)
ENTITY_INDEX_SCHEMA_VERSION = 2
SUPPORTED_ENTITY_INDEX_SCHEMAS = frozenset({1, ENTITY_INDEX_SCHEMA_VERSION})
ENTITY_TRANSLATION_CACHE_SIZE = 16


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
        self.entity_count = 0
        seen: set[tuple[str, str]] = set()
        normalized_entities: list[ProtectedEntity] = []
        for entity in entities:
            name = unicodedata.normalize("NFKC", entity.name or "").strip()
            key = (name.casefold(), entity.category)
            if len(name) < 2 or key in seen or "__EVEENTITY_" in name:
                continue
            seen.add(key)
            normalized_entity = ProtectedEntity(name, entity.category, entity.strict_case)
            normalized_entities.append(normalized_entity)
            self.entity_count += 1
        self._matcher = LongestSpanMatcher(
            normalized_entities,
            source=lambda entity: entity.name,
            strict_case=lambda entity: entity.strict_case,
        )
        self.localized_entities = tuple(localized_entities)
        self._translation_buckets: OrderedDict[
            tuple[str, str], LongestSpanMatcher[_EntityAlias]
        ] = OrderedDict()
        self._translation_maps: OrderedDict[
            tuple[str, str], dict[str, str]
        ] = OrderedDict()
        self._lexicon_maps: OrderedDict[
            tuple[str, str], dict[str, str]
        ] = OrderedDict()
        # Public cache views build and touch the same three LRU maps as one
        # atomic operation.  Re-entrancy keeps that invariant when a public
        # lookup calls the shared builder while holding the lock.
        self._translation_lock = threading.RLock()

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
    def _normalize_locale(language: str | None) -> str:
        return normalize_language_code(language, "")

    def _build_translation_buckets(
        self,
        source_language: str,
        target_language: str,
    ) -> LongestSpanMatcher[_EntityAlias]:
        pair = (
            self._normalize_locale(source_language),
            self._normalize_locale(target_language) or "en",
        )
        with self._translation_lock:
            cached = self._translation_buckets.get(pair)
            if cached is not None:
                self._touch_translation_pair(pair)
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

            aliases: list[_EntityAlias] = []
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
                aliases.append(_EntityAlias(source, target, is_strict_latin_case(source)))
            matcher = LongestSpanMatcher(
                aliases,
                source=lambda alias: alias.source,
                strict_case=lambda alias: alias.strict_case,
            )
            self._translation_maps[pair] = {
                alias.source: alias.target
                for alias in aliases
            }
            self._lexicon_maps[pair] = lexicon_map
            # Publish the bucket cache last; its presence is the ready marker
            # shared by all three synchronized public views.
            self._translation_buckets[pair] = matcher
            self._touch_translation_pair(pair)
            while len(self._translation_buckets) > ENTITY_TRANSLATION_CACHE_SIZE:
                expired_pair, _ = self._translation_buckets.popitem(last=False)
                self._translation_maps.pop(expired_pair, None)
                self._lexicon_maps.pop(expired_pair, None)
            return matcher

    def _touch_translation_pair(self, pair: tuple[str, str]) -> None:
        """Keep the three pair caches in the same least-recently-used order."""
        for cache in (
            self._translation_buckets,
            self._translation_maps,
            self._lexicon_maps,
        ):
            if pair in cache:
                cache.move_to_end(pair)

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
        with self._translation_lock:
            self._build_translation_buckets(*pair)
            self._touch_translation_pair(pair)
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
        with self._translation_lock:
            self._build_translation_buckets(*pair)
            self._touch_translation_pair(pair)
            return self._lexicon_maps.get(pair, {})

    def canonicalize(
        self,
        text: str,
        source_language: str,
        target_language: str,
    ) -> str:
        """Replace official localized type names with their target-locale name."""
        if not text or not self.localized_entities:
            return text or ""
        matcher = self._build_translation_buckets(source_language, target_language)
        if not matcher.available:
            return text
        parts: list[str] = []
        cursor = 0
        while cursor < len(text):
            match = matcher.match(text, cursor)
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
            match = self._matcher.match(text, cursor)
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

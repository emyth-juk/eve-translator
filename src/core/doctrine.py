"""Safe import and loading for local doctrine translation packs.

Doctrine packs are deliberately data-only.  A pack is a flat directory or ZIP
containing these four base files::

    manifest.yml
    terms.yml
    protected_entities.yml
    commands.yml

``manifest.yml`` identifies schema version 1 or 2, ``terms.yml`` maps normalized
``source_target`` language pairs to exact term translations,
``protected_entities.yml`` lists names that must survive translation, and
``commands.yml`` maps combat command codes to literal phrases. Schema v2 may
also contain ``corrections.yml`` and constrained custom command definitions.
Packs cannot nominate trusted speakers and command phrases are never
interpreted as regular expressions.
"""

from __future__ import annotations

import hashlib
import os
import re
import shutil
import stat
import tempfile
import unicodedata
import zipfile
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from types import MappingProxyType
from typing import Any

import yaml

from src.core.combat import COMMAND_CATEGORY
from src.core.languages import normalize_locale
from src.core.translation_memory import (
    CorrectionEntry,
    normalize_correction_text,
)


SCHEMA_VERSION = 2
SUPPORTED_SCHEMA_VERSIONS = frozenset({1, SCHEMA_VERSION})
REQUIRED_FILES = frozenset({
    "manifest.yml",
    "terms.yml",
    "protected_entities.yml",
    "commands.yml",
})
OPTIONAL_FILES = frozenset({"corrections.yml"})
ALLOWED_FILES = REQUIRED_FILES | OPTIONAL_FILES

MAX_FILES = 8
MAX_FILE_BYTES = 1024 * 1024
MAX_TOTAL_BYTES = 3 * 1024 * 1024
MAX_TERMS = 5_000
MAX_PROTECTED_ENTITIES = 10_000
MAX_COMMAND_PHRASES = 500
MAX_CORRECTIONS = 5_000
MAX_CUSTOM_COMMANDS = 100

CUSTOM_COMMAND_CATEGORIES = frozenset({
    "HOLD",
    "MOVE",
    "TARGET",
    "TACKLE",
    "SUPPORT",
    "INTEL",
})
CORRECTION_SCOPES = frozenset({"global", "pack"})

_PACK_ID = re.compile(r"^[a-z0-9](?:[a-z0-9._-]{0,62}[a-z0-9])?$")
_VERSION = re.compile(r"^[0-9A-Za-z](?:[0-9A-Za-z._+-]{0,62}[0-9A-Za-z])?$")
_LANGUAGE = re.compile(r"^[a-z]{2,8}(?:-[a-z0-9]{1,8})*$")
_CUSTOM_COMMAND_CODE = re.compile(r"^[A-Z][A-Z0-9_]{1,39}$")


class DoctrinePackError(ValueError):
    """Raised when a doctrine pack is unsafe or uses an unsupported schema."""


class _StrictSafeLoader(yaml.SafeLoader):
    """SafeLoader variant that also rejects aliases and duplicate keys."""

    def compose_node(self, parent: Any, index: Any) -> Any:
        if self.check_event(yaml.events.AliasEvent):
            raise yaml.constructor.ConstructorError(
                None,
                None,
                "YAML aliases are not allowed in doctrine packs",
                self.peek_event().start_mark,
            )
        return super().compose_node(parent, index)


def _construct_unique_mapping(
    loader: _StrictSafeLoader,
    node: yaml.nodes.MappingNode,
    deep: bool = False,
) -> dict[Any, Any]:
    loader.flatten_mapping(node)
    result: dict[Any, Any] = {}
    for key_node, value_node in node.value:
        key = loader.construct_object(key_node, deep=deep)
        try:
            duplicate = key in result
        except TypeError as exc:
            raise yaml.constructor.ConstructorError(
                "while constructing a mapping",
                node.start_mark,
                "unhashable mapping key",
                key_node.start_mark,
            ) from exc
        if duplicate:
            raise yaml.constructor.ConstructorError(
                "while constructing a mapping",
                node.start_mark,
                f"duplicate key: {key!r}",
                key_node.start_mark,
            )
        result[key] = loader.construct_object(value_node, deep=deep)
    return result


_StrictSafeLoader.add_constructor(
    yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG,
    _construct_unique_mapping,
)


def normalize_language_code(language: str) -> str:
    """Normalize a BCP-47-like language code without applying fallbacks."""

    normalized = normalize_locale(language)
    return normalized if _LANGUAGE.fullmatch(normalized) else ""


def language_pair_key(source_language: str, target_language: str) -> str:
    """Return the exact canonical pair key or an empty string if invalid."""

    source = normalize_language_code(source_language)
    target = normalize_language_code(target_language)
    return f"{source}_{target}" if source and target else ""


def _parse_pair_key(value: object) -> str:
    if not isinstance(value, str):
        raise DoctrinePackError("terms.yml language-pair keys must be strings")
    normalized = unicodedata.normalize("NFKC", value).strip()
    if "->" in normalized:
        parts = normalized.split("->")
    else:
        parts = normalized.split("_")
    if len(parts) != 2:
        raise DoctrinePackError(
            f"invalid language pair {value!r}; use source_target or source->target"
        )
    pair = language_pair_key(parts[0], parts[1])
    if not pair:
        raise DoctrinePackError(f"invalid language pair {value!r}")
    return pair


def _bounded_string(value: object, label: str, maximum: int, *, allow_empty: bool = False) -> str:
    if not isinstance(value, str):
        raise DoctrinePackError(f"{label} must be a string")
    normalized = " ".join(unicodedata.normalize("NFKC", value).split())
    if not normalized and not allow_empty:
        raise DoctrinePackError(f"{label} must not be empty")
    if len(normalized) > maximum:
        raise DoctrinePackError(f"{label} exceeds {maximum} characters")
    if any(unicodedata.category(character).startswith("C") for character in normalized):
        raise DoctrinePackError(f"{label} contains control characters")
    return normalized


def _require_mapping(value: object, label: str) -> Mapping[object, object]:
    if not isinstance(value, Mapping):
        raise DoctrinePackError(f"{label} must be a mapping")
    return value


def _require_exact_keys(
    value: Mapping[object, object],
    label: str,
    *,
    required: set[str] | frozenset[str],
    optional: set[str] | frozenset[str] = frozenset(),
) -> None:
    keys = set(value)
    if not all(isinstance(key, str) for key in keys):
        raise DoctrinePackError(f"{label} keys must be strings")
    missing = set(required) - keys
    unknown = keys - set(required) - set(optional)
    if missing:
        raise DoctrinePackError(f"{label} is missing: {', '.join(sorted(missing))}")
    if unknown:
        raise DoctrinePackError(f"{label} contains unknown fields: {', '.join(sorted(unknown))}")


def _validate_data_shape(value: object, label: str) -> None:
    nodes = 0

    def visit(item: object, depth: int) -> None:
        nonlocal nodes
        nodes += 1
        if nodes > 25_000:
            raise DoctrinePackError(f"{label} contains too many values")
        if depth > 8:
            raise DoctrinePackError(f"{label} is nested too deeply")
        if isinstance(item, str):
            if len(item) > 4_096:
                raise DoctrinePackError(f"{label} contains an oversized string")
            return
        if isinstance(item, Mapping):
            for key, nested in item.items():
                visit(key, depth + 1)
                visit(nested, depth + 1)
            return
        if isinstance(item, (list, tuple)):
            for nested in item:
                visit(nested, depth + 1)
            return
        if item is None or isinstance(item, (bool, int, float)):
            return
        raise DoctrinePackError(f"{label} contains an unsupported YAML value")

    visit(value, 0)


def _load_yaml(content: bytes, filename: str) -> object:
    try:
        text = content.decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        raise DoctrinePackError(f"{filename} must be UTF-8") from exc
    if "\x00" in text:
        raise DoctrinePackError(f"{filename} contains NUL bytes")
    try:
        value = yaml.load(text, Loader=_StrictSafeLoader)
    except (yaml.YAMLError, RecursionError) as exc:
        raise DoctrinePackError(f"invalid safe YAML in {filename}: {exc}") from exc
    if value is None:
        value = {}
    _validate_data_shape(value, filename)
    return value


def _safe_archive_name(name: str) -> str:
    if not name or "\x00" in name:
        raise DoctrinePackError("ZIP contains an invalid filename")
    portable = name.replace("\\", "/")
    path = PurePosixPath(portable)
    if (
        path.is_absolute()
        or len(path.parts) != 1
        or any(part in {"", ".", ".."} for part in path.parts)
        or ":" in portable
    ):
        raise DoctrinePackError(f"ZIP path is not a safe flat filename: {name!r}")
    return path.name


def _read_zip(path: Path) -> dict[str, bytes]:
    try:
        archive = zipfile.ZipFile(path)
    except (OSError, zipfile.BadZipFile) as exc:
        raise DoctrinePackError(f"cannot read doctrine ZIP: {exc}") from exc
    with archive:
        entries = archive.infolist()
        if len(entries) > MAX_FILES:
            raise DoctrinePackError(f"doctrine pack exceeds {MAX_FILES} files")
        result: dict[str, bytes] = {}
        total = 0
        seen_casefolded: set[str] = set()
        for entry in entries:
            if entry.is_dir():
                raise DoctrinePackError("doctrine ZIP must be flat and contain no directories")
            filename = _safe_archive_name(entry.filename)
            folded = filename.casefold()
            if folded in seen_casefolded:
                raise DoctrinePackError(f"duplicate ZIP filename: {filename}")
            seen_casefolded.add(folded)
            unix_mode = entry.external_attr >> 16
            if stat.S_IFMT(unix_mode) == stat.S_IFLNK:
                raise DoctrinePackError(f"ZIP symlinks are not allowed: {filename}")
            if entry.flag_bits & 0x1:
                raise DoctrinePackError("encrypted doctrine ZIP entries are not allowed")
            if filename not in ALLOWED_FILES:
                raise DoctrinePackError(f"unsupported doctrine-pack file: {filename}")
            if entry.file_size > MAX_FILE_BYTES:
                raise DoctrinePackError(f"{filename} exceeds {MAX_FILE_BYTES} bytes")
            total += entry.file_size
            if total > MAX_TOTAL_BYTES:
                raise DoctrinePackError(f"doctrine pack exceeds {MAX_TOTAL_BYTES} bytes")
            with archive.open(entry, "r") as stream:
                content = stream.read(MAX_FILE_BYTES + 1)
            if len(content) != entry.file_size or len(content) > MAX_FILE_BYTES:
                raise DoctrinePackError(f"invalid or oversized ZIP entry: {filename}")
            result[filename] = content
        return result


def _read_directory(path: Path) -> dict[str, bytes]:
    if path.is_symlink():
        raise DoctrinePackError("doctrine-pack directories may not be symlinks")
    try:
        entries = list(path.iterdir())
    except OSError as exc:
        raise DoctrinePackError(f"cannot read doctrine-pack directory: {exc}") from exc
    if len(entries) > MAX_FILES:
        raise DoctrinePackError(f"doctrine pack exceeds {MAX_FILES} files")
    result: dict[str, bytes] = {}
    total = 0
    seen_casefolded: set[str] = set()
    for entry in entries:
        folded = entry.name.casefold()
        if folded in seen_casefolded:
            raise DoctrinePackError(f"duplicate doctrine filename: {entry.name}")
        seen_casefolded.add(folded)
        if entry.name not in ALLOWED_FILES:
            raise DoctrinePackError(f"unsupported doctrine-pack file: {entry.name}")
        if entry.is_symlink():
            raise DoctrinePackError(f"doctrine-pack symlinks are not allowed: {entry.name}")
        if not entry.is_file():
            raise DoctrinePackError(f"doctrine-pack entries must be regular files: {entry.name}")
        try:
            size = entry.stat().st_size
        except OSError as exc:
            raise DoctrinePackError(f"cannot inspect {entry.name}: {exc}") from exc
        if size > MAX_FILE_BYTES:
            raise DoctrinePackError(f"{entry.name} exceeds {MAX_FILE_BYTES} bytes")
        total += size
        if total > MAX_TOTAL_BYTES:
            raise DoctrinePackError(f"doctrine pack exceeds {MAX_TOTAL_BYTES} bytes")
        try:
            with entry.open("rb") as stream:
                content = stream.read(MAX_FILE_BYTES + 1)
        except OSError as exc:
            raise DoctrinePackError(f"cannot read {entry.name}: {exc}") from exc
        if len(content) > MAX_FILE_BYTES:
            raise DoctrinePackError(f"{entry.name} exceeds {MAX_FILE_BYTES} bytes")
        result[entry.name] = content
    return result


def _read_pack_files(source: os.PathLike[str] | str) -> tuple[dict[str, bytes], Path]:
    path = Path(source).expanduser()
    if not path.exists():
        raise DoctrinePackError(f"doctrine pack does not exist: {path}")
    if path.is_dir():
        files = _read_directory(path)
    else:
        if path.is_symlink():
            raise DoctrinePackError("doctrine-pack ZIP may not be a symlink")
        files = _read_zip(path)
    missing = REQUIRED_FILES - set(files)
    if missing:
        raise DoctrinePackError(f"doctrine pack is missing: {', '.join(sorted(missing))}")
    return files, path.resolve()


def _content_digest(files: Mapping[str, bytes]) -> str:
    digest = hashlib.sha256()
    for filename in sorted(files):
        content = files[filename]
        digest.update(filename.encode("utf-8"))
        digest.update(b"\x00")
        digest.update(len(content).to_bytes(8, "big"))
        digest.update(content)
    return digest.hexdigest()


def _parse_manifest(value: object) -> tuple[int, str, str, str, str]:
    manifest = _require_mapping(value, "manifest.yml")
    _require_exact_keys(
        manifest,
        "manifest.yml",
        required={"schema_version", "id", "name", "version"},
        optional={"description"},
    )
    schema_version = manifest["schema_version"]
    if (
        type(schema_version) is not int
        or schema_version not in SUPPORTED_SCHEMA_VERSIONS
    ):
        supported = ", ".join(str(item) for item in sorted(SUPPORTED_SCHEMA_VERSIONS))
        raise DoctrinePackError(f"unsupported doctrine schema; expected one of {supported}")
    pack_id = _bounded_string(manifest["id"], "manifest id", 64)
    if not _PACK_ID.fullmatch(pack_id):
        raise DoctrinePackError("manifest id must be a lowercase filesystem-safe slug")
    name = _bounded_string(manifest["name"], "manifest name", 100)
    version = _bounded_string(manifest["version"], "manifest version", 64)
    if not _VERSION.fullmatch(version):
        raise DoctrinePackError("manifest version contains unsupported characters")
    description = _bounded_string(
        manifest.get("description", ""),
        "manifest description",
        500,
        allow_empty=True,
    )
    return schema_version, pack_id, name, version, description


def _parse_terms(value: object) -> Mapping[str, Mapping[str, str]]:
    document = _require_mapping(value, "terms.yml")
    _require_exact_keys(document, "terms.yml", required={"pairs"})
    pairs = _require_mapping(document["pairs"], "terms.yml pairs")
    parsed: dict[str, Mapping[str, str]] = {}
    count = 0
    for raw_pair, raw_terms in pairs.items():
        pair = _parse_pair_key(raw_pair)
        if pair in parsed:
            raise DoctrinePackError(f"duplicate normalized language pair: {pair}")
        terms = _require_mapping(raw_terms, f"terms for {pair}")
        pair_terms: dict[str, str] = {}
        seen: set[str] = set()
        for source, target in terms.items():
            source_term = _bounded_string(source, f"{pair} source term", 160)
            target_term = _bounded_string(target, f"{pair} target term", 160)
            key = source_term.casefold()
            if key in seen:
                raise DoctrinePackError(f"duplicate normalized source term in {pair}: {source_term}")
            seen.add(key)
            pair_terms[source_term] = target_term
            count += 1
            if count > MAX_TERMS:
                raise DoctrinePackError(f"doctrine pack exceeds {MAX_TERMS} terms")
        parsed[pair] = MappingProxyType(pair_terms)
    return MappingProxyType(parsed)


def _parse_protected_entities(value: object) -> tuple[str, ...]:
    document = _require_mapping(value, "protected_entities.yml")
    _require_exact_keys(document, "protected_entities.yml", required={"entities"})
    raw_entities = document["entities"]
    if not isinstance(raw_entities, list):
        raise DoctrinePackError("protected_entities.yml entities must be a list")
    if len(raw_entities) > MAX_PROTECTED_ENTITIES:
        raise DoctrinePackError(
            f"doctrine pack exceeds {MAX_PROTECTED_ENTITIES} protected entities"
        )
    entities: list[str] = []
    seen: set[str] = set()
    for raw_entity in raw_entities:
        entity = _bounded_string(raw_entity, "protected entity", 160)
        key = entity.casefold()
        if key in seen:
            raise DoctrinePackError(f"duplicate protected entity: {entity}")
        seen.add(key)
        entities.append(entity)
    return tuple(entities)


@dataclass(frozen=True)
class DoctrineCommandDefinition:
    """A pack-defined command type constrained to a safe display taxonomy."""

    code: str
    category: str
    display_label: str
    phrases: tuple[str, ...]
    source_language: str = ""


def _parse_commands(
    value: object,
    schema_version: int,
) -> tuple[
    Mapping[str, tuple[str, ...]],
    Mapping[str, DoctrineCommandDefinition],
    Mapping[str, str],
]:
    document = _require_mapping(value, "commands.yml")
    _require_exact_keys(
        document,
        "commands.yml",
        required={"commands"},
        optional={"custom_commands"} if schema_version >= 2 else frozenset(),
    )
    raw_commands = _require_mapping(document["commands"], "commands.yml commands")
    commands: dict[str, tuple[str, ...]] = {}
    definitions: dict[str, DoctrineCommandDefinition] = {}
    source_languages: dict[str, str] = {}
    seen_codes: set[str] = set()
    seen_phrases: dict[str, str] = {}
    count = 0

    def parse_phrases(code: str, value: object) -> tuple[str, ...]:
        nonlocal count
        if not isinstance(value, list):
            raise DoctrinePackError(f"command {code} phrases must be a list")
        phrases: list[str] = []
        for raw_phrase in value:
            phrase = _bounded_string(raw_phrase, f"{code} command phrase", 160)
            normalized_phrase = phrase.casefold()
            previous = seen_phrases.get(normalized_phrase)
            if previous is not None:
                raise DoctrinePackError(
                    f"duplicate exact command phrase {phrase!r} in {previous} and {code}"
                )
            seen_phrases[normalized_phrase] = code
            phrases.append(phrase)
            count += 1
            if count > MAX_COMMAND_PHRASES:
                raise DoctrinePackError(
                    f"doctrine pack exceeds {MAX_COMMAND_PHRASES} command phrases"
                )
        return tuple(phrases)

    def parse_source_language(code: str, value: object) -> str:
        language = normalize_language_code(
            _bounded_string(value, f"{code} command source language", 32)
        )
        if not language or language in {"auto", "unknown"}:
            raise DoctrinePackError(
                f"command {code} contains an invalid source language"
            )
        return language

    def parse_builtin_entry(code: str, value: object) -> tuple[tuple[str, ...], str]:
        if isinstance(value, list):
            return parse_phrases(code, value), ""
        if schema_version < 2:
            raise DoctrinePackError(f"command {code} phrases must be a list")
        definition = _require_mapping(value, f"command {code}")
        _require_exact_keys(
            definition,
            f"command {code}",
            required={"phrases"},
            optional={"source_language"},
        )
        language = (
            parse_source_language(code, definition["source_language"])
            if "source_language" in definition
            else ""
        )
        return parse_phrases(code, definition["phrases"]), language

    for raw_code, raw_phrases in raw_commands.items():
        if not isinstance(raw_code, str):
            raise DoctrinePackError("command codes must be strings")
        code = raw_code.strip().upper()
        if code not in COMMAND_CATEGORY:
            raise DoctrinePackError(f"unknown combat command code: {raw_code}")
        if code in seen_codes:
            raise DoctrinePackError(f"duplicate normalized command code: {code}")
        seen_codes.add(code)
        phrases, source_language = parse_builtin_entry(code, raw_phrases)
        commands[code] = phrases
        if source_language:
            source_languages[code] = source_language

    raw_custom_commands = document.get("custom_commands", {})
    custom_commands = _require_mapping(
        raw_custom_commands,
        "commands.yml custom_commands",
    )
    if len(custom_commands) > MAX_CUSTOM_COMMANDS:
        raise DoctrinePackError(
            f"doctrine pack exceeds {MAX_CUSTOM_COMMANDS} custom commands"
        )
    for raw_code, raw_definition in custom_commands.items():
        if not isinstance(raw_code, str):
            raise DoctrinePackError("custom command codes must be strings")
        code = raw_code.strip().upper()
        if not _CUSTOM_COMMAND_CODE.fullmatch(code):
            raise DoctrinePackError(
                f"custom command code must use 2-40 uppercase letters, digits, or underscores: {raw_code}"
            )
        if code in COMMAND_CATEGORY:
            raise DoctrinePackError(f"custom command code collides with built-in code: {code}")
        if code in seen_codes:
            raise DoctrinePackError(f"duplicate normalized command code: {code}")
        seen_codes.add(code)
        definition = _require_mapping(
            raw_definition,
            f"custom command {code}",
        )
        _require_exact_keys(
            definition,
            f"custom command {code}",
            required={"category", "label", "phrases"},
            optional={"source_language"},
        )
        category = _bounded_string(
            definition["category"],
            f"custom command {code} category",
            20,
        ).upper()
        if category not in CUSTOM_COMMAND_CATEGORIES:
            allowed = ", ".join(sorted(CUSTOM_COMMAND_CATEGORIES))
            raise DoctrinePackError(
                f"custom command {code} category must be one of {allowed}"
            )
        label = _bounded_string(
            definition["label"],
            f"custom command {code} label",
            80,
        )
        phrases = parse_phrases(code, definition["phrases"])
        source_language = (
            parse_source_language(code, definition["source_language"])
            if "source_language" in definition
            else ""
        )
        commands[code] = phrases
        if source_language:
            source_languages[code] = source_language
        definitions[code] = DoctrineCommandDefinition(
            code=code,
            category=category,
            display_label=label,
            phrases=phrases,
            source_language=source_language,
        )
    return (
        MappingProxyType(commands),
        MappingProxyType(definitions),
        MappingProxyType(source_languages),
    )


@dataclass(frozen=True)
class DoctrineCorrection:
    """One exact schema-v2 correction scoped globally or to the active pack."""

    source_text: str
    translated_text: str
    source_language: str
    target_language: str
    scope: str


def _parse_corrections(value: object) -> tuple[DoctrineCorrection, ...]:
    document = _require_mapping(value, "corrections.yml")
    _require_exact_keys(document, "corrections.yml", required={"corrections"})
    raw_corrections = document["corrections"]
    if not isinstance(raw_corrections, list):
        raise DoctrinePackError("corrections.yml corrections must be a list")
    if len(raw_corrections) > MAX_CORRECTIONS:
        raise DoctrinePackError(
            f"doctrine pack exceeds {MAX_CORRECTIONS} exact corrections"
        )
    parsed: list[DoctrineCorrection] = []
    seen: set[tuple[str, str, str, str]] = set()
    for index, raw_correction in enumerate(raw_corrections, 1):
        label = f"corrections.yml entry {index}"
        correction = _require_mapping(raw_correction, label)
        _require_exact_keys(
            correction,
            label,
            required={
                "source_language",
                "target_language",
                "scope",
                "source",
                "translation",
            },
        )
        source_language = normalize_language_code(
            _bounded_string(correction["source_language"], f"{label} source language", 32)
        )
        target_language = normalize_language_code(
            _bounded_string(correction["target_language"], f"{label} target language", 32)
        )
        if not source_language or not target_language:
            raise DoctrinePackError(f"{label} contains an invalid language code")
        scope = _bounded_string(correction["scope"], f"{label} scope", 16).casefold()
        if scope not in CORRECTION_SCOPES:
            raise DoctrinePackError(f"{label} scope must be global or pack")
        source_text = normalize_correction_text(
            _bounded_string(correction["source"], f"{label} source", 4_000)
        )
        translated_text = normalize_correction_text(
            _bounded_string(correction["translation"], f"{label} translation", 4_000)
        )
        key = (scope, source_language, target_language, source_text)
        if key in seen:
            raise DoctrinePackError(
                f"duplicate exact correction for {source_language}->{target_language} "
                f"in {scope} scope: {source_text!r}"
            )
        seen.add(key)
        parsed.append(DoctrineCorrection(
            source_text=source_text,
            translated_text=translated_text,
            source_language=source_language,
            target_language=target_language,
            scope=scope,
        ))
    return tuple(parsed)


@dataclass(frozen=True)
class DoctrinePack:
    """Validated, immutable doctrine data for one active pack."""

    schema_version: int
    pack_id: str
    name: str
    version: str
    description: str
    digest: str
    terms_by_pair: Mapping[str, Mapping[str, str]]
    protected_entities: tuple[str, ...]
    command_phrases: Mapping[str, tuple[str, ...]]
    command_definitions: Mapping[str, DoctrineCommandDefinition]
    command_source_languages: Mapping[str, str]
    corrections: tuple[DoctrineCorrection, ...]
    source_path: Path = field(compare=False, repr=False)
    _command_lookup: Mapping[str, str] = field(compare=False, repr=False)
    _correction_lookup: Mapping[tuple[str, str, str, str], str] = field(
        compare=False,
        repr=False,
    )

    @property
    def scope(self) -> str:
        return f"{self.pack_id}@{self.version}"

    @property
    def revision(self) -> str:
        return f"{self.scope}:{self.digest[:12]}"

    @property
    def protected_names(self) -> tuple[str, ...]:
        """Compatibility name for entity-protection consumers."""

        return self.protected_entities

    @property
    def metadata(self) -> Mapping[str, object]:
        """Return stable metadata without machine-local timestamps or paths."""

        return MappingProxyType({
            "schema_version": self.schema_version,
            "id": self.pack_id,
            "name": self.name,
            "version": self.version,
            "description": self.description,
            "digest": self.digest,
        })

    def terms_for(self, source_language: str, target_language: str) -> Mapping[str, str]:
        """Return only the exact normalized pair; no language fallback is used."""

        pair = language_pair_key(source_language, target_language)
        return self.terms_by_pair.get(pair, MappingProxyType({}))

    def phrases_for(self, command_code: str) -> tuple[str, ...]:
        return self.command_phrases.get((command_code or "").strip().upper(), ())

    def command_category(self, command_code: str) -> str | None:
        """Return the built-in or validated custom display category."""

        code = (command_code or "").strip().upper()
        if code in COMMAND_CATEGORY:
            return COMMAND_CATEGORY[code]
        definition = self.command_definitions.get(code)
        return definition.category if definition is not None else None

    def command_label(self, command_code: str) -> str:
        """Return a custom display label or a readable built-in fallback."""

        code = (command_code or "").strip().upper()
        definition = self.command_definitions.get(code)
        return definition.display_label if definition is not None else code.replace("_", " ")

    def command_source_language(self, command_code: str) -> str:
        """Return an explicit pack-authored language hint, when present."""

        code = (command_code or "").strip().upper()
        return self.command_source_languages.get(code, "")

    def command_for_phrase(self, phrase: str) -> str | None:
        """Match one complete literal phrase; regex-looking text stays literal."""

        try:
            normalized = _bounded_string(phrase, "command phrase", 160).casefold()
        except DoctrinePackError:
            return None
        return self._command_lookup.get(normalized)

    def corrections_for(
        self,
        source_language: str,
        target_language: str,
        scope: str = "pack",
    ) -> Mapping[str, str]:
        """Return exact corrections for one validated pair and scope."""

        source = normalize_language_code(source_language)
        target = normalize_language_code(target_language)
        normalized_scope = (scope or "").strip().casefold()
        if not source or not target or normalized_scope not in CORRECTION_SCOPES:
            return MappingProxyType({})
        result = {
            correction.source_text: correction.translated_text
            for correction in self.corrections
            if correction.source_language == source
            and correction.target_language == target
            and correction.scope == normalized_scope
        }
        return MappingProxyType(result)

    def correction_for(
        self,
        source_text: str,
        source_language: str,
        target_language: str,
        *,
        include_global: bool = True,
    ) -> str | None:
        """Return an exact pack correction, then its optional global fallback."""

        source = normalize_language_code(source_language)
        target = normalize_language_code(target_language)
        text = normalize_correction_text(source_text)
        if not source or not target or not text:
            return None
        result = self._correction_lookup.get(("pack", source, target, text))
        if result is None and include_global:
            result = self._correction_lookup.get(("global", source, target, text))
        return result


def _build_pack(files: Mapping[str, bytes], source_path: Path) -> DoctrinePack:
    manifest = _parse_manifest(_load_yaml(files["manifest.yml"], "manifest.yml"))
    schema_version, pack_id, name, version, description = manifest
    if schema_version == 1 and "corrections.yml" in files:
        raise DoctrinePackError("corrections.yml requires doctrine schema 2")
    terms = _parse_terms(_load_yaml(files["terms.yml"], "terms.yml"))
    protected_entities = _parse_protected_entities(
        _load_yaml(files["protected_entities.yml"], "protected_entities.yml")
    )
    commands, command_definitions, command_source_languages = _parse_commands(
        _load_yaml(files["commands.yml"], "commands.yml"),
        schema_version,
    )
    corrections = (
        _parse_corrections(_load_yaml(files["corrections.yml"], "corrections.yml"))
        if "corrections.yml" in files
        else ()
    )
    command_lookup = MappingProxyType({
        phrase.casefold(): code
        for code, phrases in commands.items()
        for phrase in phrases
    })
    correction_lookup = MappingProxyType({
        (
            correction.scope,
            correction.source_language,
            correction.target_language,
            correction.source_text,
        ): correction.translated_text
        for correction in corrections
    })
    return DoctrinePack(
        schema_version=schema_version,
        pack_id=pack_id,
        name=name,
        version=version,
        description=description,
        digest=_content_digest(files),
        terms_by_pair=terms,
        protected_entities=protected_entities,
        command_phrases=commands,
        command_definitions=command_definitions,
        command_source_languages=command_source_languages,
        corrections=corrections,
        source_path=source_path,
        _command_lookup=command_lookup,
        _correction_lookup=correction_lookup,
    )


def load_doctrine_pack(source: os.PathLike[str] | str) -> DoctrinePack:
    """Validate and load one doctrine pack from a directory or ZIP."""

    files, source_path = _read_pack_files(source)
    return _build_pack(files, source_path)


def import_doctrine_pack(
    source: os.PathLike[str] | str,
    base_dir: os.PathLike[str] | str,
) -> DoctrinePack:
    """Validate then atomically install one pack under ``base_dir/<pack-id>``.

    Existing pack IDs are rejected instead of being overwritten.  Updating a
    pack is therefore an explicit remove/re-import operation by the caller.
    """

    files, source_path = _read_pack_files(source)
    pack = _build_pack(files, source_path)
    destination_root = Path(base_dir).expanduser()
    if destination_root.exists() and destination_root.is_symlink():
        raise DoctrinePackError("doctrine base directory may not be a symlink")
    try:
        destination_root.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise DoctrinePackError(f"cannot create doctrine base directory: {exc}") from exc
    root = destination_root.resolve()
    destination = root / pack.pack_id
    if destination.exists():
        raise DoctrinePackError(f"doctrine pack is already installed: {pack.pack_id}")
    if destination.parent != root:
        raise DoctrinePackError("doctrine pack destination escaped the base directory")

    try:
        staging = Path(tempfile.mkdtemp(prefix=f".{pack.pack_id}-", dir=root))
    except OSError as exc:
        raise DoctrinePackError(f"cannot stage doctrine pack: {exc}") from exc
    try:
        for filename in sorted(files):
            target = staging / filename
            with target.open("xb") as stream:
                stream.write(files[filename])
        try:
            staging.rename(destination)
        except OSError as exc:
            raise DoctrinePackError(f"cannot install doctrine pack: {exc}") from exc
    finally:
        if staging.exists():
            shutil.rmtree(staging)
    return load_doctrine_pack(destination)


def _yaml_bytes(value: object) -> bytes:
    return yaml.safe_dump(
        value,
        allow_unicode=True,
        sort_keys=False,
    ).encode("utf-8")


def export_correction_pack(
    translation_memory: object,
    destination: os.PathLike[str] | str,
    *,
    pack_id: str,
    name: str,
    version: str = "1.0.0",
    description: str = "Exact fleet-chat corrections exported locally.",
    doctrine_scope: str = "",
    include_global: bool = True,
    overwrite: bool = False,
) -> DoctrinePack:
    """Export global and one active-doctrine scope as a schema-v2 ZIP.

    Full-message corrections remain exact correction records; they are never
    converted into glossary terms. Non-global rows are exported as ``pack``
    scope so they bind to the receiving pack rather than targeting an arbitrary
    doctrine ID on another pilot's machine.
    """

    read_corrections = getattr(translation_memory, "corrections", None)
    if not callable(read_corrections):
        raise TypeError("translation_memory must expose corrections(scope)")
    selected_scope = normalize_correction_text(doctrine_scope)
    rows: list[CorrectionEntry] = []
    if include_global:
        rows.extend(read_corrections(""))
    if selected_scope:
        rows.extend(read_corrections(selected_scope))
    if not rows:
        raise DoctrinePackError("no exact corrections were selected for export")

    correction_rows: list[dict[str, str]] = []
    for row in rows:
        if not isinstance(row, CorrectionEntry):
            raise TypeError("translation memory returned an invalid correction entry")
        correction_rows.append({
            "source_language": row.source_language,
            "target_language": row.target_language,
            "scope": "global" if not row.doctrine_scope else "pack",
            "source": row.source_text,
            "translation": row.translated_text,
        })

    files = {
        "manifest.yml": _yaml_bytes({
            "schema_version": SCHEMA_VERSION,
            "id": pack_id,
            "name": name,
            "version": version,
            "description": description,
        }),
        "terms.yml": _yaml_bytes({"pairs": {}}),
        "protected_entities.yml": _yaml_bytes({"entities": []}),
        "commands.yml": _yaml_bytes({"commands": {}}),
        "corrections.yml": _yaml_bytes({"corrections": correction_rows}),
    }

    path = Path(destination).expanduser()
    if path.exists() and path.is_symlink():
        raise DoctrinePackError("correction-pack destination may not be a symlink")
    if path.exists() and not overwrite:
        raise DoctrinePackError(f"correction-pack destination already exists: {path}")
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise DoctrinePackError(f"cannot create correction-pack directory: {exc}") from exc
    _build_pack(files, path.resolve())

    try:
        handle, temporary_name = tempfile.mkstemp(
            prefix=f".{path.name}-",
            suffix=".tmp",
            dir=path.parent,
        )
        os.close(handle)
    except OSError as exc:
        raise DoctrinePackError(f"cannot stage correction pack: {exc}") from exc
    temporary = Path(temporary_name)
    try:
        with zipfile.ZipFile(temporary, "w", zipfile.ZIP_DEFLATED) as archive:
            for filename in sorted(files):
                archive.writestr(filename, files[filename])
        os.replace(temporary, path)
    except OSError as exc:
        raise DoctrinePackError(f"cannot write correction pack: {exc}") from exc
    finally:
        temporary.unlink(missing_ok=True)
    return load_doctrine_pack(path)


def import_doctrine_corrections(
    source: DoctrinePack | os.PathLike[str] | str,
    translation_memory: object,
    *,
    doctrine_scope: str | None = None,
    include_global: bool = True,
) -> int:
    """Import a pack's exact corrections into local correction memory.

    ``pack`` rows bind to ``doctrine_scope`` or, by default, the loaded pack's
    own stable scope. No serialized scope can write into an unrelated doctrine.
    """

    pack = source if isinstance(source, DoctrinePack) else load_doctrine_pack(source)
    remember_many = getattr(translation_memory, "remember_many", None)
    if not callable(remember_many):
        raise TypeError("translation_memory must expose remember_many(entries)")
    target_scope = normalize_correction_text(doctrine_scope or pack.scope)
    entries = tuple(
        CorrectionEntry(
            source_text=correction.source_text,
            translated_text=correction.translated_text,
            source_language=correction.source_language,
            target_language=correction.target_language,
            doctrine_scope="" if correction.scope == "global" else target_scope,
        )
        for correction in pack.corrections
        if include_global or correction.scope != "global"
    )
    return int(remember_many(entries))

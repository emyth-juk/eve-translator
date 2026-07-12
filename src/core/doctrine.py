"""Safe import and loading for local doctrine translation packs.

Doctrine packs are deliberately data-only.  A pack is a flat directory or ZIP
containing exactly these files::

    manifest.yml
    terms.yml
    protected_entities.yml
    commands.yml

``manifest.yml`` identifies schema version 1, ``terms.yml`` maps normalized
``source_target`` language pairs to exact term translations,
``protected_entities.yml`` lists names that must survive translation, and
``commands.yml`` maps existing combat command codes to literal phrases.  Packs
cannot nominate trusted speakers and command phrases are never interpreted as
regular expressions.
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


SCHEMA_VERSION = 1
REQUIRED_FILES = frozenset({
    "manifest.yml",
    "terms.yml",
    "protected_entities.yml",
    "commands.yml",
})
ALLOWED_FILES = REQUIRED_FILES

MAX_FILES = 8
MAX_FILE_BYTES = 1024 * 1024
MAX_TOTAL_BYTES = 3 * 1024 * 1024
MAX_TERMS = 5_000
MAX_PROTECTED_ENTITIES = 10_000
MAX_COMMAND_PHRASES = 500

_PACK_ID = re.compile(r"^[a-z0-9](?:[a-z0-9._-]{0,62}[a-z0-9])?$")
_VERSION = re.compile(r"^[0-9A-Za-z](?:[0-9A-Za-z._+-]{0,62}[0-9A-Za-z])?$")
_LANGUAGE = re.compile(r"^[a-z]{2,8}(?:-[a-z0-9]{1,8})*$")


class DoctrinePackError(ValueError):
    """Raised when a doctrine pack is unsafe or does not match schema v1."""


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

    if not isinstance(language, str):
        return ""
    normalized = unicodedata.normalize("NFKC", language).strip().casefold()
    normalized = normalized.replace("_", "-")
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


def _parse_manifest(value: object) -> tuple[str, str, str, str]:
    manifest = _require_mapping(value, "manifest.yml")
    _require_exact_keys(
        manifest,
        "manifest.yml",
        required={"schema_version", "id", "name", "version"},
        optional={"description"},
    )
    if type(manifest["schema_version"]) is not int or manifest["schema_version"] != SCHEMA_VERSION:
        raise DoctrinePackError(f"unsupported doctrine schema; expected {SCHEMA_VERSION}")
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
    return pack_id, name, version, description


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


def _parse_commands(value: object) -> Mapping[str, tuple[str, ...]]:
    document = _require_mapping(value, "commands.yml")
    _require_exact_keys(document, "commands.yml", required={"commands"})
    raw_commands = _require_mapping(document["commands"], "commands.yml commands")
    commands: dict[str, tuple[str, ...]] = {}
    seen_codes: set[str] = set()
    seen_phrases: dict[str, str] = {}
    count = 0
    for raw_code, raw_phrases in raw_commands.items():
        if not isinstance(raw_code, str):
            raise DoctrinePackError("command codes must be strings")
        code = raw_code.strip().upper()
        if code not in COMMAND_CATEGORY:
            raise DoctrinePackError(f"unknown combat command code: {raw_code}")
        if code in seen_codes:
            raise DoctrinePackError(f"duplicate normalized command code: {code}")
        seen_codes.add(code)
        if not isinstance(raw_phrases, list):
            raise DoctrinePackError(f"command {code} phrases must be a list")
        phrases: list[str] = []
        for raw_phrase in raw_phrases:
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
        commands[code] = tuple(phrases)
    return MappingProxyType(commands)


@dataclass(frozen=True)
class DoctrinePack:
    """Validated, immutable doctrine data for one active pack."""

    pack_id: str
    name: str
    version: str
    description: str
    digest: str
    terms_by_pair: Mapping[str, Mapping[str, str]]
    protected_entities: tuple[str, ...]
    command_phrases: Mapping[str, tuple[str, ...]]
    source_path: Path = field(compare=False, repr=False)
    _command_lookup: Mapping[str, str] = field(compare=False, repr=False)

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
            "schema_version": SCHEMA_VERSION,
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

    def command_for_phrase(self, phrase: str) -> str | None:
        """Match one complete literal phrase; regex-looking text stays literal."""

        try:
            normalized = _bounded_string(phrase, "command phrase", 160).casefold()
        except DoctrinePackError:
            return None
        return self._command_lookup.get(normalized)


def _build_pack(files: Mapping[str, bytes], source_path: Path) -> DoctrinePack:
    manifest = _parse_manifest(_load_yaml(files["manifest.yml"], "manifest.yml"))
    terms = _parse_terms(_load_yaml(files["terms.yml"], "terms.yml"))
    protected_entities = _parse_protected_entities(
        _load_yaml(files["protected_entities.yml"], "protected_entities.yml")
    )
    commands = _parse_commands(_load_yaml(files["commands.yml"], "commands.yml"))
    command_lookup = MappingProxyType({
        phrase.casefold(): code
        for code, phrases in commands.items()
        for phrase in phrases
    })
    pack_id, name, version, description = manifest
    return DoctrinePack(
        pack_id=pack_id,
        name=name,
        version=version,
        description=description,
        digest=_content_digest(files),
        terms_by_pair=terms,
        protected_entities=protected_entities,
        command_phrases=commands,
        source_path=source_path,
        _command_lookup=command_lookup,
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

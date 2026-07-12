"""Build the app's compact protected-entity index from a NoirSoldats SQLite SDE.

This is intentionally an extractor, not an SDE converter.  Conversion and SDE
format compatibility belong to https://github.com/noirsoldats/eve-sde-converter.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sqlite3
import sys
import tempfile
import unicodedata
from collections.abc import Iterable
from pathlib import Path
from typing import Any


SCHEMA_VERSION = 2
CONVERTER_REPO = "https://github.com/noirsoldats/eve-sde-converter"
TYPE_NAME_TRANSLATION_CATEGORY_ID = 8

# Inventory categories that routinely appear in fleet commands, target calls,
# fittings, and on-grid reports.
COMBAT_CATEGORY_NAMES = {
    6: "ship",
    7: "module",
    8: "charge",
    18: "drone",
    22: "deployable",
    23: "starbase",
    65: "structure",
    66: "structure_module",
    87: "fighter",
}
COMBAT_CATEGORY_IDS = tuple(COMBAT_CATEGORY_NAMES)

DEFAULT_LOCALES = ("de", "en", "es", "fr", "ja", "ko", "ru", "zh")
LOCALE_ALIASES = {
    "de": "de",
    "de-de": "de",
    "en": "en",
    "en-gb": "en",
    "en-us": "en",
    "es": "es",
    "es-es": "es",
    "fr": "fr",
    "fr-fr": "fr",
    "it": "it",
    "it-it": "it",
    "ja": "ja",
    "ja-jp": "ja",
    "ko": "ko",
    "ko-kr": "ko",
    "ru": "ru",
    "ru-ru": "ru",
    "zh": "zh",
    "zh-cn": "zh",
    "zh-hans": "zh",
    "zh-hant": "zh",
    "zh-tw": "zh",
}

REQUIRED_SCHEMA = {
    "invTypes": {"typeID", "groupID", "typeName", "published"},
    "invGroups": {"groupID", "categoryID", "published"},
    "invCategories": {"categoryID", "published"},
    "mapRegions": {"regionID", "regionName"},
    "mapSolarSystems": {"solarSystemID", "solarSystemName"},
}
OPTIONAL_SCHEMA = {
    "trnTranslations": {"tcID", "keyID", "languageID", "text"},
    "mapConstellations": {"constellationID", "constellationName"},
}


class SDEIndexError(RuntimeError):
    """Raised when an input database cannot produce a trustworthy index."""


def normalize_name(value: Any) -> str:
    """Return an NFC name with all whitespace runs collapsed."""
    if not isinstance(value, str):
        return ""
    return " ".join(unicodedata.normalize("NFC", value).split())


def normalize_locales(values: Iterable[str]) -> tuple[str, ...]:
    """Resolve EVE locale aliases and return a stable, de-duplicated tuple."""
    resolved: set[str] = set()
    for value in values:
        for part in value.split(","):
            requested = part.strip().replace("_", "-").lower()
            if not requested:
                continue
            locale = LOCALE_ALIASES.get(requested)
            if locale is None:
                supported = ", ".join(sorted(LOCALE_ALIASES))
                raise SDEIndexError(
                    f"unsupported locale {part.strip()!r}; supported aliases: {supported}"
                )
            resolved.add(locale)
    if not resolved:
        raise SDEIndexError("at least one locale is required")
    return tuple(sorted(resolved))


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as database_file:
        for chunk in iter(lambda: database_file.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _normalize_expected_sha256(value: str | None) -> str | None:
    if value is None or not value.strip():
        return None
    expected = value.strip().lower()
    if expected.startswith("sha256:"):
        expected = expected.removeprefix("sha256:")
    if not re.fullmatch(r"[0-9a-f]{64}", expected):
        raise SDEIndexError("expected SHA-256 must contain exactly 64 hexadecimal characters")
    return expected


def _open_read_only(path: Path) -> sqlite3.Connection:
    """Open a SQLite file without creating journals or permitting writes."""
    try:
        connection = sqlite3.connect(f"{path.resolve().as_uri()}?mode=ro", uri=True)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA query_only = ON")
        connection.execute("PRAGMA trusted_schema = OFF")
        # quick_check walks every table b-tree. The converter's stripped asset
        # already exceeds SQLite's small default page cache, so use a bounded
        # 64 MiB read cache to avoid needless page churn during release builds.
        connection.execute("PRAGMA cache_size = -65536")
        try:
            connection.enable_load_extension(False)
        except (AttributeError, sqlite3.Error):
            pass
        return connection
    except sqlite3.Error as exc:
        raise SDEIndexError(f"could not open SQLite database {path}: {exc}") from exc


def _table_columns(connection: sqlite3.Connection, table: str) -> set[str] | None:
    row = connection.execute(
        "SELECT name FROM sqlite_master WHERE type = 'table' AND name = ?", (table,)
    ).fetchone()
    if row is None:
        return None
    # Table names come exclusively from the fixed schema dictionaries above.
    return {str(column["name"]) for column in connection.execute(f'PRAGMA table_info("{table}")')}


def validate_database(connection: sqlite3.Connection) -> dict[str, bool]:
    """Validate database integrity and the converter schema we depend on."""
    try:
        quick_check = [str(row[0]) for row in connection.execute("PRAGMA quick_check")]
    except sqlite3.Error as exc:
        raise SDEIndexError(f"SQLite quick_check failed: {exc}") from exc
    if quick_check != ["ok"]:
        details = "; ".join(quick_check[:5]) or "no result"
        raise SDEIndexError(f"SQLite quick_check reported corruption: {details}")

    for table, required_columns in REQUIRED_SCHEMA.items():
        columns = _table_columns(connection, table)
        if columns is None:
            raise SDEIndexError(f"incompatible NoirSoldats database: missing table {table}")
        missing = sorted(required_columns - columns)
        if missing:
            raise SDEIndexError(
                f"incompatible NoirSoldats database: {table} is missing columns "
                f"{', '.join(missing)}"
            )

    capabilities: dict[str, bool] = {}
    for table, required_columns in OPTIONAL_SCHEMA.items():
        columns = _table_columns(connection, table)
        if columns is None:
            capabilities[table] = False
            continue
        missing = sorted(required_columns - columns)
        if missing:
            raise SDEIndexError(
                f"incompatible NoirSoldats database: optional table {table} is present but "
                f"missing columns {', '.join(missing)}"
            )
        capabilities[table] = True
    return capabilities


def _strict_case(name: str) -> bool:
    # Common English terms such as "Raven" are dangerous as case-insensitive
    # matches.  Non-Latin localized names can safely tolerate chat casing.
    return any(
        character.isalpha()
        and (character.isascii() or "LATIN" in unicodedata.name(character, ""))
        for character in name
    )


def _category_name(category_id: int) -> str:
    return COMBAT_CATEGORY_NAMES.get(category_id, f"type_category_{category_id}")


def _published_type_query(profile: str, selected_column: str) -> tuple[str, tuple[int, ...]]:
    category_clause = ""
    parameters: tuple[int, ...] = ()
    if profile == "combat":
        placeholders = ", ".join("?" for _ in COMBAT_CATEGORY_IDS)
        category_clause = f" AND g.categoryID IN ({placeholders})"
        parameters = COMBAT_CATEGORY_IDS
    return (
        f"""
        SELECT t.typeID AS type_id, g.categoryID AS category_id,
               {selected_column} AS entity_name
        FROM invTypes AS t
        JOIN invGroups AS g ON g.groupID = t.groupID
        JOIN invCategories AS c ON c.categoryID = g.categoryID
        WHERE t.published = 1 AND g.published = 1 AND c.published = 1
        {category_clause}
        """,
        parameters,
    )


def _published_translation_query(
    profile: str, locales: tuple[str, ...]
) -> tuple[str, tuple[Any, ...]]:
    """Return localized type names using the same publication/profile scope."""
    if not locales:
        raise SDEIndexError("localized type query requires at least one locale")

    category_clause = ""
    category_parameters: tuple[int, ...] = ()
    if profile == "combat":
        category_placeholders = ", ".join("?" for _ in COMBAT_CATEGORY_IDS)
        category_clause = f" AND g.categoryID IN ({category_placeholders})"
        category_parameters = COMBAT_CATEGORY_IDS

    locale_placeholders = ", ".join("?" for _ in locales)
    return (
        f"""
        SELECT t.typeID AS type_id, g.categoryID AS category_id,
               tr.languageID AS language_id, tr.text AS entity_name
        FROM trnTranslations AS tr
        JOIN invTypes AS t ON t.typeID = tr.keyID
        JOIN invGroups AS g ON g.groupID = t.groupID
        JOIN invCategories AS c ON c.categoryID = g.categoryID
        WHERE tr.tcID = ? AND tr.languageID IN ({locale_placeholders})
          AND t.published = 1 AND g.published = 1 AND c.published = 1
        {category_clause}
        ORDER BY t.typeID, tr.languageID, tr.text
        """,
        (
            TYPE_NAME_TRANSLATION_CATEGORY_ID,
            *locales,
            *category_parameters,
        ),
    )


def _collect_entities(
    connection: sqlite3.Connection,
    *,
    profile: str,
    locales: tuple[str, ...],
    schema_capabilities: dict[str, bool],
) -> tuple[dict[str, list[str | dict[str, Any]]], dict[str, Any]]:
    buckets: dict[str, dict[str, bool]] = {}

    def add(category: str, value: Any) -> None:
        name = normalize_name(value)
        if len(name) < 2 or "__EVEENTITY_" in name:
            return
        strict_case = _strict_case(name)
        previous = buckets.setdefault(category, {}).get(name, False)
        buckets[category][name] = previous or strict_case

    base_query, base_parameters = _published_type_query(profile, "t.typeName")
    for row in connection.execute(base_query, base_parameters):
        add(_category_name(int(row["category_id"])), row["entity_name"])

    available_locales: list[str] = []
    extracted_locales: list[str] = []
    if schema_capabilities["trnTranslations"]:
        available_locales = [
            str(row[0])
            for row in connection.execute(
                """
                SELECT DISTINCT languageID
                FROM trnTranslations
                WHERE tcID = ?
                ORDER BY languageID
                """,
                (TYPE_NAME_TRANSLATION_CATEGORY_ID,),
            )
        ]
        extracted_locales = sorted(set(locales).intersection(available_locales))
        if extracted_locales:
            translation_query, translation_parameters = _published_translation_query(
                profile, tuple(extracted_locales)
            )
            for row in connection.execute(translation_query, translation_parameters):
                add(_category_name(int(row["category_id"])), row["entity_name"])

    for row in connection.execute("SELECT regionName FROM mapRegions"):
        add("region", row["regionName"])
    for row in connection.execute("SELECT solarSystemName FROM mapSolarSystems"):
        add("solar_system", row["solarSystemName"])
    if schema_capabilities["mapConstellations"]:
        for row in connection.execute("SELECT constellationName FROM mapConstellations"):
            add("constellation", row["constellationName"])

    serialized: dict[str, list[str | dict[str, Any]]] = {}
    for category, names in buckets.items():
        items: list[str | dict[str, Any]] = []
        for name, strict_case in sorted(
            names.items(), key=lambda item: (item[0].casefold(), item[0])
        ):
            if strict_case:
                items.append(name)
            else:
                items.append({"name": name, "strict_case": False})
        if items:
            serialized[category] = items

    capabilities = {
        "trn_translations": schema_capabilities["trnTranslations"],
        "map_constellations": schema_capabilities["mapConstellations"],
        "requested_locales": list(locales),
        "available_locales": available_locales,
        "extracted_locales": extracted_locales,
    }
    return serialized, capabilities


def _collect_localized_entities(
    connection: sqlite3.Connection,
    *,
    profile: str,
    extracted_locales: tuple[str, ...],
    translations_available: bool,
) -> list[dict[str, Any]]:
    """Collect canonical type IDs with their requested SDE-localized names."""
    records: dict[int, dict[str, Any]] = {}
    base_query, base_parameters = _published_type_query(profile, "t.typeName")
    for row in connection.execute(base_query, base_parameters):
        type_id = int(row["type_id"])
        records[type_id] = {
            "type_id": type_id,
            "category": _category_name(int(row["category_id"])),
            "names": {"en": normalize_name(row["entity_name"])},
        }

    if translations_available and extracted_locales:
        translation_query, translation_parameters = _published_translation_query(
            profile, extracted_locales
        )
        for row in connection.execute(translation_query, translation_parameters):
            record = records.get(int(row["type_id"]))
            if record is None:
                continue
            locale = str(row["language_id"])
            name = normalize_name(row["entity_name"])
            if locale == "en" or not name:
                continue
            # Duplicate converter rows should not make output ordering or values
            # depend on SQLite's unspecified row order. The query is sorted and
            # the first normalized value wins deterministically.
            record["names"].setdefault(locale, name)

    return [records[type_id] for type_id in sorted(records)]


def _sde_build_from_release(release: str) -> str:
    match = re.search(r"(?<!\d)(\d{6,})(?!\d)", release)
    return match.group(1) if match else ""


def build_index(
    database: str | Path,
    *,
    release: str = "",
    commit: str = "",
    expected_sha256: str | None = None,
    profile: str = "combat",
    locales: Iterable[str] = DEFAULT_LOCALES,
) -> dict[str, Any]:
    """Build and return a protected-entity index payload."""
    database_path = Path(database)
    if not database_path.is_file():
        raise SDEIndexError(f"database does not exist or is not a file: {database_path}")
    if profile not in {"combat", "all"}:
        raise SDEIndexError("profile must be 'combat' or 'all'")

    normalized_locales = normalize_locales(locales)
    actual_sha256 = sha256_file(database_path)
    expected = _normalize_expected_sha256(expected_sha256)
    if expected is not None and actual_sha256 != expected:
        raise SDEIndexError(
            "database SHA-256 mismatch: "
            f"expected {expected}, calculated {actual_sha256}"
        )

    connection = _open_read_only(database_path)
    try:
        schema_capabilities = validate_database(connection)
        entities, capabilities = _collect_entities(
            connection,
            profile=profile,
            locales=normalized_locales,
            schema_capabilities=schema_capabilities,
        )
        localized_entities = _collect_localized_entities(
            connection,
            profile=profile,
            extracted_locales=tuple(capabilities["extracted_locales"]),
            translations_available=schema_capabilities["trnTranslations"],
        )
    except sqlite3.Error as exc:
        raise SDEIndexError(f"failed to read NoirSoldats database: {exc}") from exc
    finally:
        connection.close()

    return {
        "schema_version": SCHEMA_VERSION,
        "source": {
            "converter_repo": CONVERTER_REPO,
            "release": release,
            "commit": commit,
            "asset_sha256": actual_sha256,
            "sde_build": _sde_build_from_release(release),
            "profile": profile,
        },
        "capabilities": capabilities,
        "entities": entities,
        "localized_entities": localized_entities,
    }


def write_index(payload: dict[str, Any], output: str | Path) -> None:
    """Atomically write deterministic UTF-8 JSON."""
    output_path = Path(output)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    temporary_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            "w",
            encoding="utf-8",
            newline="\n",
            dir=output_path.parent,
            prefix=f".{output_path.name}.",
            suffix=".tmp",
            delete=False,
        ) as output_file:
            temporary_path = Path(output_file.name)
            json.dump(payload, output_file, ensure_ascii=False, indent=2, sort_keys=True)
            output_file.write("\n")
        os.replace(temporary_path, output_path)
    finally:
        if temporary_path is not None and temporary_path.exists():
            temporary_path.unlink()


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Extract a compact EVE protected-entity index from a SQLite database "
            "produced by noirsoldats/eve-sde-converter."
        )
    )
    parser.add_argument("--database", required=True, type=Path, help="NoirSoldats SQLite asset")
    parser.add_argument("--output", required=True, type=Path, help="destination JSON index")
    parser.add_argument("--release", default="", help="converter release tag, e.g. sde-3430261")
    parser.add_argument("--commit", default="", help="converter commit SHA used for the asset")
    parser.add_argument(
        "--sha",
        "--sha256",
        dest="expected_sha256",
        help="expected asset SHA-256 (plain hex or sha256:hex)",
    )
    parser.add_argument(
        "--profile",
        choices=("combat", "all"),
        default="combat",
        help="combat categories only (default), or every published inventory category",
    )
    parser.add_argument(
        "--locales",
        nargs="+",
        default=list(DEFAULT_LOCALES),
        metavar="LOCALE",
        help="locale aliases, separated by spaces and/or commas",
    )
    return parser


def main(arguments: list[str] | None = None) -> int:
    args = _parser().parse_args(arguments)
    if args.database.resolve() == args.output.resolve():
        print("error: output must not replace the input SQLite database", file=sys.stderr)
        return 2
    try:
        payload = build_index(
            args.database,
            release=args.release,
            commit=args.commit,
            expected_sha256=args.expected_sha256,
            profile=args.profile,
            locales=args.locales,
        )
        write_index(payload, args.output)
    except (OSError, SDEIndexError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    entity_count = sum(len(names) for names in payload["entities"].values())
    print(f"Wrote {entity_count} protected entities to {args.output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

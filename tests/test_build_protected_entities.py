import hashlib
import json
import sqlite3

import pytest

from scripts import build_protected_entities as extractor


def _create_database(path, *, optional=True):
    connection = sqlite3.connect(path)
    connection.executescript(
        """
        CREATE TABLE invCategories (
            categoryID INTEGER PRIMARY KEY,
            categoryName TEXT,
            published INTEGER
        );
        CREATE TABLE invGroups (
            groupID INTEGER PRIMARY KEY,
            categoryID INTEGER,
            published INTEGER
        );
        CREATE TABLE invTypes (
            typeID INTEGER PRIMARY KEY,
            groupID INTEGER,
            typeName TEXT,
            published INTEGER
        );
        CREATE TABLE mapRegions (regionID INTEGER PRIMARY KEY, regionName TEXT);
        CREATE TABLE mapSolarSystems (
            solarSystemID INTEGER PRIMARY KEY,
            solarSystemName TEXT
        );
        """
    )
    if optional:
        connection.executescript(
            """
            CREATE TABLE trnTranslations (
                tcID INTEGER,
                keyID INTEGER,
                languageID TEXT,
                text TEXT
            );
            CREATE TABLE mapConstellations (
                constellationID INTEGER PRIMARY KEY,
                constellationName TEXT
            );
            """
        )

    connection.executemany(
        "INSERT INTO invCategories VALUES (?, ?, ?)",
        [
            (6, "Ship", 1),
            (7, "Module", 1),
            (8, "Charge", 0),
            (99, "Test category", 1),
        ],
    )
    connection.executemany(
        "INSERT INTO invGroups VALUES (?, ?, ?)",
        [
            (100, 6, 1),
            (101, 7, 0),
            (102, 99, 1),
            (103, 8, 1),
            (104, 7, 1),
        ],
    )
    connection.executemany(
        "INSERT INTO invTypes VALUES (?, ?, ?, ?)",
        [
            (1000, 100, "Raven", 1),
            (1001, 101, "Hidden group module", 1),
            (1002, 100, "Unpublished ship", 0),
            (1003, 100, "  Rave\u0301n   Navy  ", 1),
            (1004, 102, "All Profile Relic", 1),
            (1005, 103, "Hidden category charge", 1),
            (1006, 104, "Stasis Webifier", 1),
        ],
    )
    connection.executemany(
        "INSERT INTO mapRegions VALUES (?, ?)",
        [(1, " The   Forge "), (2, "")],
    )
    connection.executemany(
        "INSERT INTO mapSolarSystems VALUES (?, ?)",
        [(10, "Jita"), (11, "X47L-Q")],
    )
    if optional:
        connection.executemany(
            "INSERT INTO trnTranslations VALUES (?, ?, ?, ?)",
            [
                (8, 1000, "de", "Rabe"),
                (8, 1000, "zh", "渡鸦"),
                (8, 1003, "zh", "海军型 渡鸦"),
                (8, 1004, "zh", "仅完整配置"),
                (8, 1002, "zh", "不应发布"),
                (33, 1000, "zh", "description, not a type name"),
            ],
        )
        connection.execute("INSERT INTO mapConstellations VALUES (?, ?)", (20, "Kimotoro"))
    connection.commit()
    connection.close()


def _entity_names(items):
    return {item if isinstance(item, str) else item["name"] for item in items}


def test_combat_profile_filters_published_joins_and_normalizes_names(tmp_path):
    database = tmp_path / "eve.db"
    _create_database(database)

    payload = extractor.build_index(
        database,
        release="sde-3430261",
        commit="abc123",
        profile="combat",
        locales=["en-US", "zh-CN", "de-DE"],
    )

    assert payload["schema_version"] == 2
    assert payload["source"]["converter_repo"] == extractor.CONVERTER_REPO
    assert payload["source"]["sde_build"] == "3430261"
    assert payload["source"]["profile"] == "combat"
    assert payload["source"]["asset_sha256"] == hashlib.sha256(database.read_bytes()).hexdigest()

    ships = payload["entities"]["ship"]
    ship_names = _entity_names(ships)
    assert {"Raven", "Ravén Navy", "Rabe", "渡鸦", "海军型 渡鸦"} <= ship_names
    assert "Unpublished ship" not in ship_names
    assert "All Profile Relic" not in ship_names
    assert "Hidden group module" not in _entity_names(payload["entities"]["module"])
    assert "Stasis Webifier" in _entity_names(payload["entities"]["module"])
    assert "Hidden category charge" not in {
        name for items in payload["entities"].values() for name in _entity_names(items)
    }
    assert _entity_names(payload["entities"]["region"]) == {"The Forge"}
    assert _entity_names(payload["entities"]["solar_system"]) == {"Jita", "X47L-Q"}
    assert _entity_names(payload["entities"]["constellation"]) == {"Kimotoro"}

    chinese = next(item for item in ships if isinstance(item, dict) and item["name"] == "渡鸦")
    assert chinese == {"name": "渡鸦", "strict_case": False}
    assert "Raven" in ships

    localized = {item["type_id"]: item for item in payload["localized_entities"]}
    assert localized[1000] == {
        "type_id": 1000,
        "category": "ship",
        "names": {"de": "Rabe", "en": "Raven", "zh": "渡鸦"},
    }
    assert localized[1006] == {
        "type_id": 1006,
        "category": "module",
        "names": {"en": "Stasis Webifier"},
    }
    assert 1002 not in localized
    assert 1004 not in localized


def test_all_profile_adds_every_published_category(tmp_path):
    database = tmp_path / "eve.db"
    _create_database(database)

    payload = extractor.build_index(database, profile="all", locales=["en"])

    assert _entity_names(payload["entities"]["type_category_99"]) == {"All Profile Relic"}
    assert "charge" not in payload["entities"]


def test_optional_tables_are_reported_as_capabilities_not_requirements(tmp_path):
    database = tmp_path / "eve-stripped.db"
    _create_database(database, optional=False)

    payload = extractor.build_index(database, locales=["zh-CN"])

    assert payload["capabilities"] == {
        "trn_translations": False,
        "map_constellations": False,
        "requested_locales": ["zh"],
        "available_locales": [],
        "extracted_locales": [],
    }
    assert "constellation" not in payload["entities"]
    assert "渡鸦" not in _entity_names(payload["entities"]["ship"])
    raven = next(item for item in payload["localized_entities"] if item["type_id"] == 1000)
    assert raven == {
        "type_id": 1000,
        "category": "ship",
        "names": {"en": "Raven"},
    }


def test_locales_are_aliased_deduplicated_and_limited_to_available_rows(tmp_path):
    database = tmp_path / "eve.db"
    _create_database(database)

    payload = extractor.build_index(
        database,
        locales=["zh_CN,en-GB", "zh-hans", "fr-FR"],
    )

    assert payload["capabilities"]["requested_locales"] == ["en", "fr", "zh"]
    assert payload["capabilities"]["available_locales"] == ["de", "zh"]
    assert payload["capabilities"]["extracted_locales"] == ["zh"]
    assert "渡鸦" in _entity_names(payload["entities"]["ship"])


def test_output_is_deterministic_and_cli_verifies_prefixed_sha(tmp_path, capsys):
    database = tmp_path / "eve.db"
    first = tmp_path / "first.json"
    second = tmp_path / "second.json"
    _create_database(database)
    digest = hashlib.sha256(database.read_bytes()).hexdigest()
    arguments = [
        "--database",
        str(database),
        "--release",
        "sde-3430261",
        "--commit",
        "abc123",
        "--sha",
        f"sha256:{digest}",
        "--profile",
        "combat",
        "--locales",
        "zh-CN,en-US",
    ]

    assert extractor.main([*arguments, "--output", str(first)]) == 0
    assert extractor.main([*arguments, "--output", str(second)]) == 0

    assert first.read_bytes() == second.read_bytes()
    payload = json.loads(first.read_text(encoding="utf-8"))
    assert payload["source"]["asset_sha256"] == digest
    assert [item["type_id"] for item in payload["localized_entities"]] == sorted(
        item["type_id"] for item in payload["localized_entities"]
    )
    assert "Wrote" in capsys.readouterr().out


def test_sha_mismatch_fails_before_writing_output(tmp_path):
    database = tmp_path / "eve.db"
    output = tmp_path / "entities.json"
    _create_database(database)

    with pytest.raises(extractor.SDEIndexError, match="SHA-256 mismatch"):
        extractor.build_index(database, expected_sha256="0" * 64)

    assert not output.exists()


def test_cli_refuses_to_replace_its_input_database(tmp_path, capsys):
    database = tmp_path / "eve.db"
    _create_database(database)
    original = database.read_bytes()

    exit_code = extractor.main(
        ["--database", str(database), "--output", str(database), "--release", "sde-3430261"]
    )

    assert exit_code == 2
    assert database.read_bytes() == original
    assert "must not replace" in capsys.readouterr().err


@pytest.mark.parametrize(
    ("table", "definition", "expected"),
    [
        ("", "", "missing table invTypes"),
        (
            "invTypes",
            "CREATE TABLE invTypes (typeID INTEGER, groupID INTEGER, typeName TEXT)",
            "invTypes is missing columns published",
        ),
    ],
)
def test_required_schema_failures_are_explicit(tmp_path, table, definition, expected):
    database = tmp_path / "bad-schema.db"
    connection = sqlite3.connect(database)
    if table:
        connection.execute(definition)
    connection.close()

    with pytest.raises(extractor.SDEIndexError, match=expected):
        extractor.build_index(database)


def test_present_but_incompatible_optional_table_is_not_silently_ignored(tmp_path):
    database = tmp_path / "eve.db"
    _create_database(database, optional=False)
    connection = sqlite3.connect(database)
    connection.execute("CREATE TABLE trnTranslations (tcID INTEGER, keyID INTEGER)")
    connection.close()

    with pytest.raises(extractor.SDEIndexError, match="optional table trnTranslations"):
        extractor.build_index(database)


def test_corrupt_input_fails_quick_check(tmp_path):
    database = tmp_path / "not-sqlite.db"
    database.write_bytes(b"this is not a SQLite database")

    with pytest.raises(extractor.SDEIndexError, match="SQLite"):
        extractor.build_index(database)


def test_database_connection_is_query_only(tmp_path):
    database = tmp_path / "eve.db"
    _create_database(database)

    connection = extractor._open_read_only(database)
    try:
        assert connection.execute("PRAGMA query_only").fetchone()[0] == 1
        assert connection.execute("PRAGMA cache_size").fetchone()[0] == -65536
        with pytest.raises(sqlite3.OperationalError, match="readonly"):
            connection.execute("DELETE FROM invTypes")
    finally:
        connection.close()


def test_unknown_or_empty_locales_fail_closed(tmp_path):
    database = tmp_path / "eve.db"
    _create_database(database)

    with pytest.raises(extractor.SDEIndexError, match="unsupported locale"):
        extractor.build_index(database, locales=["klingon"])
    with pytest.raises(extractor.SDEIndexError, match="at least one locale"):
        extractor.build_index(database, locales=[])

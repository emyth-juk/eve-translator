import json

from src.core.entity_protection import (
    LocalizedEntity,
    ProtectedEntity,
    ProtectedEntityIndex,
)


def test_entity_index_protects_longest_exact_names_and_restores():
    index = ProtectedEntityIndex([
        ProtectedEntity("Raven", "ship"),
        ProtectedEntity("Raven Navy Issue", "ship"),
        ProtectedEntity("X47L-Q", "solar_system"),
    ])

    result = index.protect("Primary Raven Navy Issue in X47L-Q")

    assert "Raven Navy Issue" not in result.text
    assert "X47L-Q" not in result.text
    assert index.restore(result.text, result.tokens) == "Primary Raven Navy Issue in X47L-Q"


def test_strict_case_avoids_common_english_false_positive():
    index = ProtectedEntityIndex([ProtectedEntity("Raven", "ship", strict_case=True)])

    assert index.protect("a raven landed").tokens == {}
    assert index.protect("primary Raven").tokens


def test_word_boundaries_avoid_partial_entity_matches():
    index = ProtectedEntityIndex([ProtectedEntity("Raven", "ship")])
    assert index.protect("Ravenous").tokens == {}


def test_invalid_or_missing_index_falls_back_without_blocking(tmp_path):
    assert ProtectedEntityIndex.load(tmp_path / "missing.json").available is False
    invalid = tmp_path / "invalid.json"
    invalid.write_text("not json", encoding="utf-8")
    assert ProtectedEntityIndex.load(invalid).available is False


def test_loads_versioned_generated_index(tmp_path):
    path = tmp_path / "entities.json"
    path.write_text(json.dumps({
        "schema_version": 1,
        "source": {"sde_build": "3430261"},
        "entities": {"ship": ["Raven"], "solar_system": ["Jita"]},
    }), encoding="utf-8")

    index = ProtectedEntityIndex.load(path)

    assert index.entity_count == 2
    assert index.sde_version == "3430261"


def test_bundled_noirsoldats_index_has_current_provenance_and_real_coverage():
    index = ProtectedEntityIndex.load()

    assert index.sde_version == "3430261"
    assert index.entity_count > 40_000
    assert len(index.localized_entities) == 6_263
    assert index.metadata["converter_repo"] == (
        "https://github.com/noirsoldats/eve-sde-converter"
    )
    protected = index.protect("Primary Raven Navy Issue in X47L-Q")
    assert len(protected.tokens) == 2
    assert index.canonicalize("末日沙场级 空堡 铁壁 星城", "zh", "en") == (
        "Armageddon Astrahus Fortizar Keepstar"
    )


def test_official_localized_names_canonicalize_by_type_id_and_target_locale():
    index = ProtectedEntityIndex(
        [
            ProtectedEntity("Astrahus", "structure"),
            ProtectedEntity("Astralhus", "structure"),
            ProtectedEntity("空堡", "structure", strict_case=False),
        ],
        localized_entities=[LocalizedEntity(
            type_id=35832,
            category="structure",
            names={"en": "Astrahus", "de": "Astralhus", "zh": "空堡"},
        )],
    )

    assert index.canonicalize("空堡 on grid", "zh-CN", "en-US") == (
        "Astrahus on grid"
    )
    assert index.canonicalize("空堡 on grid", "zh", "de") == (
        "Astralhus on grid"
    )
    assert index.canonicalize("小堡 on grid", "zh", "en") == "小堡 on grid"


def test_ambiguous_official_source_name_is_not_guessed():
    index = ProtectedEntityIndex(localized_entities=[
        LocalizedEntity(1, "ship", {"en": "One", "zh": "同名"}),
        LocalizedEntity(2, "ship", {"en": "Two", "zh": "同名"}),
    ])

    assert index.canonicalize("同名", "zh", "en") == "同名"
    assert "同名" not in index.translations_for("zh", "en")
    assert index.lexicon_terms_for("zh", "en")["同名"] == "同名"


def test_identical_locale_name_remains_an_official_span_mapping():
    index = ProtectedEntityIndex(localized_entities=[
        LocalizedEntity(
            1,
            "ship",
            {"en": "Raven Navy Issue", "fr": "Raven Navy Issue"},
        ),
    ])

    assert index.translations_for("en", "fr")["Raven Navy Issue"] == (
        "Raven Navy Issue"
    )

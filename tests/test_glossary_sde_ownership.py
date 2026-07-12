import json
from pathlib import Path

import yaml


ROOT = Path(__file__).resolve().parents[1]


def _flatten_glossary(path: Path) -> dict[str, str]:
    document = yaml.safe_load(path.read_text(encoding="utf-8"))
    terms: dict[str, str] = {}

    def visit(value):
        if not isinstance(value, dict):
            return
        for key, nested in value.items():
            if key == "meta":
                continue
            if isinstance(nested, str):
                terms[key] = nested
            else:
                visit(nested)

    visit(document)
    return terms


def _official_pairs(source: str, target: str) -> set[tuple[str, str]]:
    payload = json.loads(
        (ROOT / "data/generated/protected_entities.json").read_text(encoding="utf-8")
    )
    assert payload["schema_version"] == 2
    pairs = set()
    for entity in payload["localized_entities"]:
        names = entity["names"]
        source_name = names.get(source)
        target_name = names.get(target) or names.get("en")
        if source_name and target_name:
            pairs.add((source_name, target_name))
    return pairs


def test_bundled_glossaries_do_not_override_vanilla_sde_source_names():
    for pair in ("zh_en", "zh_de"):
        source, target = pair.split("_")
        terms = _flatten_glossary(ROOT / f"data/glossaries/{pair}.yml")
        official_sources = {
            source_name for source_name, _target_name in _official_pairs(source, target)
        }
        assert set(terms).intersection(official_sources) == set()


def test_player_aliases_and_short_hull_stems_remain_in_yaml():
    english = _flatten_glossary(ROOT / "data/glossaries/zh_en.yml")

    assert english["灾难"] == "Apocalypse"
    assert english["末日沙场"] == "Armageddon"
    assert english["万王"] == "Megathron"
    assert english["YST"] == "Ishtar"
    assert english["DMS"] == "Deimos"


def test_removed_structure_terms_are_supplied_by_same_id_sde_mappings():
    official = _official_pairs("zh", "en")
    english = _flatten_glossary(ROOT / "data/glossaries/zh_en.yml")

    assert "空堡" not in english
    assert "铁壁" not in english
    assert ("空堡", "Astrahus") in official
    assert ("铁壁", "Fortizar") in official
    assert ("星城", "Keepstar") in official
    assert ("铁壁", "Keepstar") not in official

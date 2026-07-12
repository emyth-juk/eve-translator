import stat
import zipfile
from pathlib import Path

import pytest

from src.core.doctrine import (
    MAX_FILE_BYTES,
    DoctrinePackError,
    import_doctrine_pack,
    load_doctrine_pack,
)


VALID_FILES = {
    "manifest.yml": b"""\
schema_version: 1
id: armor-hacs
name: Armor HACs
version: 1.2.0
description: Exact fleet terminology for the armor wing.
""",
    "terms.yml": """\
pairs:
  zh_en:
    \"重型突击巡洋舰\": Heavy Assault Cruiser
    \"守门\": Hold the gate
  de->en:
    Ziel: Target
""".encode(),
    "protected_entities.yml": b"""\
entities:
  - X47L-Q
  - Muninn
  - Baltec Megathron
""",
    "commands.yml": b"""\
commands:
  ALIGN:
    - form on the outgate
  WARP:
    - warp.*
  CEASE_FIRE:
    - red cycle
""",
}


def _write_pack(path: Path, overrides: dict[str, bytes] | None = None) -> Path:
    path.mkdir()
    files = dict(VALID_FILES)
    files.update(overrides or {})
    for filename, content in files.items():
        (path / filename).write_bytes(content)
    return path


def _write_zip(path: Path, extras: dict[str, bytes] | None = None) -> Path:
    files = dict(VALID_FILES)
    files.update(extras or {})
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as archive:
        for filename, content in files.items():
            archive.writestr(filename, content)
    return path


def test_load_selects_only_the_exact_normalized_language_pair(tmp_path):
    pack = load_doctrine_pack(_write_pack(tmp_path / "pack"))

    assert dict(pack.terms_for("ZH", "en")) == {
        "重型突击巡洋舰": "Heavy Assault Cruiser",
        "守门": "Hold the gate",
    }
    assert dict(pack.terms_for("de", "EN")) == {"Ziel": "Target"}
    assert dict(pack.terms_for("zh-Hans", "en")) == {}
    assert pack.scope == "armor-hacs@1.2.0"
    assert pack.revision.startswith("armor-hacs@1.2.0:")
    assert pack.metadata["digest"] == pack.digest
    assert "source_path" not in pack.metadata


def test_protected_names_and_exact_literal_command_phrases(tmp_path):
    pack = load_doctrine_pack(_write_pack(tmp_path / "pack"))

    assert pack.protected_entities == ("X47L-Q", "Muninn", "Baltec Megathron")
    assert pack.protected_names is pack.protected_entities
    assert pack.phrases_for("cease_fire") == ("red cycle",)
    assert pack.command_for_phrase("  RED   CYCLE ") == "CEASE_FIRE"
    assert pack.command_for_phrase("warp.*") == "WARP"
    assert pack.command_for_phrase("warp to gate") is None


def test_import_from_zip_installs_only_validated_files_and_rejects_duplicate_id(tmp_path):
    source = _write_zip(tmp_path / "armor-hacs.zip")
    imported = import_doctrine_pack(source, tmp_path / "installed")

    assert imported.source_path == (tmp_path / "installed" / "armor-hacs").resolve()
    assert {item.name for item in imported.source_path.iterdir()} == set(VALID_FILES)
    assert imported.digest == load_doctrine_pack(source).digest
    with pytest.raises(DoctrinePackError, match="already installed"):
        import_doctrine_pack(source, tmp_path / "installed")


def test_zip_path_traversal_is_rejected_before_import(tmp_path):
    source = _write_zip(tmp_path / "malicious.zip", {"../escaped.yml": b"bad"})

    with pytest.raises(DoctrinePackError, match="safe flat filename"):
        import_doctrine_pack(source, tmp_path / "installed")
    assert not (tmp_path / "escaped.yml").exists()


def test_zip_symlink_is_rejected(tmp_path):
    source = tmp_path / "symlink.zip"
    with zipfile.ZipFile(source, "w") as archive:
        for filename, content in VALID_FILES.items():
            archive.writestr(filename, content)
        link = zipfile.ZipInfo("extra-link")
        link.create_system = 3
        link.external_attr = (stat.S_IFLNK | 0o777) << 16
        archive.writestr(link, "manifest.yml")

    with pytest.raises(DoctrinePackError, match="symlinks are not allowed"):
        load_doctrine_pack(source)


def test_oversized_file_is_rejected_from_compressed_zip(tmp_path):
    source = _write_zip(
        tmp_path / "oversized.zip",
        {"terms.yml": b"x" * (MAX_FILE_BYTES + 1)},
    )

    with pytest.raises(DoctrinePackError, match="terms.yml exceeds"):
        load_doctrine_pack(source)


@pytest.mark.parametrize("schema", [0, 2, "1"])
def test_invalid_or_unsupported_schema_is_rejected(tmp_path, schema):
    manifest = f"""\
schema_version: {schema!r}
id: armor-hacs
name: Armor HACs
version: 1.2.0
""".encode()
    source = _write_pack(tmp_path / f"pack-{schema}", {"manifest.yml": manifest})

    with pytest.raises(DoctrinePackError, match="unsupported doctrine schema"):
        load_doctrine_pack(source)


def test_unknown_fields_cannot_grant_speaker_trust(tmp_path):
    commands = b"""\
commands:
  JUMP:
    - take the gate
trusted_speakers:
  Fleet Boss: fc
"""
    source = _write_pack(tmp_path / "pack", {"commands.yml": commands})

    with pytest.raises(DoctrinePackError, match="unknown fields: trusted_speakers"):
        load_doctrine_pack(source)


@pytest.mark.parametrize(
    ("filename", "content", "match"),
    [
        (
            "terms.yml",
            b"pairs:\n  zh_en:\n    Primary: One\n    primary: Two\n",
            "duplicate normalized source term",
        ),
        (
            "protected_entities.yml",
            b"entities:\n  - Muninn\n  - muninn\n",
            "duplicate protected entity",
        ),
        (
            "commands.yml",
            b"commands:\n  HOLD:\n    - stay here\n  JUMP:\n    - STAY HERE\n",
            "duplicate exact command phrase",
        ),
    ],
)
def test_normalized_content_duplicates_are_rejected(tmp_path, filename, content, match):
    source = _write_pack(tmp_path / "pack", {filename: content})

    with pytest.raises(DoctrinePackError, match=match):
        load_doctrine_pack(source)


def test_yaml_aliases_and_duplicate_keys_are_rejected(tmp_path):
    aliased = b"pairs:\n  zh_en: &terms\n    foo: bar\n  de_en: *terms\n"
    duplicate = b"entities:\n  - Muninn\nentities:\n  - Eagle\n"

    with pytest.raises(DoctrinePackError, match="aliases are not allowed"):
        load_doctrine_pack(_write_pack(tmp_path / "alias", {"terms.yml": aliased}))
    with pytest.raises(DoctrinePackError, match="duplicate key"):
        load_doctrine_pack(
            _write_pack(tmp_path / "duplicate", {"protected_entities.yml": duplicate})
        )

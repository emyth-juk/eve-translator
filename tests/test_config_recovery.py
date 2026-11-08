"""An unreadable settings file is preserved, never silently replaced."""

import json
from pathlib import Path
from unittest.mock import patch

from src.app_config import ConfigStore


def _store(tmp_path):
    config_path = tmp_path / "config" / "translator_config.json"
    config_path.parent.mkdir(parents=True)
    return config_path, ConfigStore(path=config_path, legacy_dir=tmp_path)


def test_utf8_bom_config_is_read(tmp_path):
    config_path, store = _store(tmp_path)
    document = {
        "schema_version": 1,
        "shared": {
            "translation_provider": "deepl",
            "deepl_api_key": "key:fx",
            "provider_choice_confirmed": True,
        },
    }
    config_path.write_bytes(b"\xef\xbb\xbf" + json.dumps(document).encode("utf-8"))

    config = store.load()

    assert config["shared"]["deepl_api_key"] == "key:fx"
    assert config["shared"]["translation_provider"] == "deepl"
    assert store.recovered_corrupt_path is None


def test_invalid_json_is_moved_aside_before_defaults_are_saved(tmp_path):
    config_path, store = _store(tmp_path)
    damaged = '{"shared": {"deepl_api_key": "key:fx",}}'
    config_path.write_text(damaged, encoding="utf-8")

    config = store.load()

    assert store.recovered_corrupt_path is not None
    assert store.recovered_corrupt_path.name.startswith("translator_config.json.corrupt-")
    assert store.recovered_corrupt_path.read_text(encoding="utf-8") == damaged
    assert store.save(config) is True
    assert store.recovered_corrupt_path.read_text(encoding="utf-8") == damaged


def test_unreadable_config_blocks_saving(tmp_path):
    config_path, store = _store(tmp_path)
    config_path.write_text('{"shared": {}}', encoding="utf-8")
    real_open = open

    def deny_primary(path, *args, **kwargs):
        mode = args[0] if args else kwargs.get("mode", "r")
        if Path(path) == config_path and "r" in mode:
            raise PermissionError("locked")
        return real_open(path, *args, **kwargs)

    with patch("builtins.open", side_effect=deny_primary):
        config = store.load()

    assert store.unreadable_config_path == config_path
    assert store.save(config) is False
    assert config_path.read_text(encoding="utf-8") == '{"shared": {}}'

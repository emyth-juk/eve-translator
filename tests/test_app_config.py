import json
import tempfile
import unittest
from pathlib import Path

from src.app_config import ConfigStore, sanitize_config_for_log


class TestConfigStore(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.primary = self.root / ".eve_translator" / "translator_config.json"
        self.store = ConfigStore(path=self.primary, legacy_dir=self.root)

    def tearDown(self):
        self.tmp.cleanup()

    def test_load_migrates_legacy_cwd_config(self):
        legacy_config = {
            'shared': {'opacity': 0.4},
            'sessions': {'local': {'enabled': True}},
        }
        with open(self.root / "translator_config.json", 'w', encoding='utf-8') as config_file:
            json.dump(legacy_config, config_file)

        config = self.store.load()

        self.assertEqual(config['shared']['opacity'], 0.4)
        self.assertTrue(config['sessions']['local']['enabled'])

    def test_primary_config_wins_over_legacy(self):
        with open(self.root / "overlay_config.json", 'w', encoding='utf-8') as config_file:
            json.dump({'opacity': 0.7}, config_file)
        with open(self.root / "translator_config.json", 'w', encoding='utf-8') as config_file:
            json.dump({'shared': {'opacity': 0.5}}, config_file)

        self.primary.parent.mkdir(parents=True, exist_ok=True)
        with open(self.primary, 'w', encoding='utf-8') as config_file:
            json.dump({'shared': {'opacity': 0.2}}, config_file)

        config = self.store.load()

        self.assertEqual(config['shared']['opacity'], 0.2)

    def test_save_writes_primary_path_and_filters_shared_session_keys(self):
        self.store.save({
            'shared': {'opacity': 0.8, 'deepl_api_key': 'secret'},
            'sessions': {'fleet': {'x': 100, 'opacity': 0.2}},
        })

        self.assertTrue(self.primary.exists())
        with open(self.primary, 'r', encoding='utf-8') as config_file:
            saved = json.load(config_file)

        self.assertEqual(saved['sessions']['fleet']['x'], 100)
        self.assertNotIn('opacity', saved['sessions']['fleet'])

    def test_sanitize_config_for_log_redacts_deepl_key(self):
        config = {
            'shared': {'deepl_api_key': 'secret-key'},
            'sessions': {'fleet': {'deepl_api_key': 'dirty-secret'}},
        }

        sanitized = sanitize_config_for_log(config)

        self.assertEqual(sanitized['shared']['deepl_api_key'], '<redacted>')
        self.assertEqual(sanitized['sessions']['fleet']['deepl_api_key'], '<redacted>')
        self.assertEqual(config['shared']['deepl_api_key'], 'secret-key')


if __name__ == '__main__':
    unittest.main()

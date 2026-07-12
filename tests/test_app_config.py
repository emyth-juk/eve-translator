import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from src.app_config import (
    CONFIG_SCHEMA_VERSION,
    ConfigStore,
    get_default_config,
    sanitize_config_for_log,
)


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
        self.assertEqual(config['shared']['translation_provider'], 'disabled')
        self.assertTrue(self.store.provider_selection_required)

    def test_new_config_disables_cloud_translation(self):
        config = get_default_config()

        self.assertEqual(config['schema_version'], CONFIG_SCHEMA_VERSION)
        self.assertEqual(config['shared']['translation_provider'], 'disabled')
        self.assertFalse(config['shared']['provider_choice_confirmed'])
        self.assertEqual(config['shared']['deepl_api_key'], '')
        self.assertFalse(config['shared']['provider_fallback_enabled'])
        self.assertNotIn('interaction_locked', config['shared'])
        self.assertEqual(config['shared']['global_hotkey'], 'Ctrl+Shift+L')
        self.assertEqual(config['shared']['compose_hotkey'], 'Ctrl+Shift+T')

    def test_disabled_provider_clears_fallback_consent(self):
        config = get_default_config()
        config['shared']['provider_fallback_enabled'] = True

        self.assertTrue(self.store.save(config))
        loaded = self.store.load()

        self.assertEqual(loaded['shared']['translation_provider'], 'disabled')
        self.assertFalse(loaded['shared']['provider_fallback_enabled'])

    def test_enabled_provider_preserves_fallback_consent(self):
        config = get_default_config()
        config['shared']['translation_provider'] = 'deepl'
        config['shared']['deepl_api_key'] = 'configured-key'
        config['shared']['provider_fallback_enabled'] = True

        self.assertTrue(self.store.save(config))
        loaded = self.store.load()

        self.assertEqual(loaded['shared']['translation_provider'], 'deepl')
        self.assertTrue(loaded['shared']['provider_fallback_enabled'])

    def test_automatic_save_does_not_silence_provider_setup_guidance(self):
        config = get_default_config()
        self.assertTrue(self.store.save(config))

        reloaded = ConfigStore(path=self.primary, legacy_dir=self.root)
        loaded = reloaded.load()

        self.assertEqual(loaded['shared']['translation_provider'], 'disabled')
        self.assertFalse(loaded['shared']['provider_choice_confirmed'])
        self.assertTrue(reloaded.provider_selection_required)

    def test_confirmed_disabled_choice_silences_provider_setup_guidance(self):
        config = get_default_config()
        config['shared']['provider_choice_confirmed'] = True
        self.assertTrue(self.store.save(config))

        reloaded = ConfigStore(path=self.primary, legacy_dir=self.root)
        loaded = reloaded.load()

        self.assertTrue(loaded['shared']['provider_choice_confirmed'])
        self.assertFalse(reloaded.provider_selection_required)

    def test_legacy_deepl_key_preserves_deepl_provider(self):
        with open(self.root / "translator_config.json", 'w', encoding='utf-8') as config_file:
            json.dump({'shared': {'deepl_api_key': 'legacy-secret'}}, config_file)

        config = self.store.load()

        self.assertEqual(config['shared']['translation_provider'], 'deepl')
        self.assertEqual(config['shared']['deepl_api_key'], 'legacy-secret')
        self.assertFalse(self.store.provider_selection_required)

    def test_explicit_provider_wins_over_stored_deepl_key(self):
        with open(self.root / "translator_config.json", 'w', encoding='utf-8') as config_file:
            json.dump({
                'shared': {
                    'translation_provider': 'google',
                    'deepl_api_key': 'preserved-secret',
                },
            }, config_file)

        config = self.store.load()

        self.assertEqual(config['shared']['translation_provider'], 'google')
        self.assertEqual(config['shared']['deepl_api_key'], 'preserved-secret')
        self.assertFalse(self.store.provider_selection_required)

    def test_empty_legacy_key_cannot_erase_explicit_overlay_provider(self):
        with open(self.root / "overlay_config.json", 'w', encoding='utf-8') as config_file:
            json.dump({'translation_provider': 'google'}, config_file)
        with open(self.root / "translator_config.json", 'w', encoding='utf-8') as config_file:
            json.dump({'shared': {'deepl_api_key': ''}}, config_file)

        config = self.store.load()

        self.assertEqual(config['shared']['translation_provider'], 'google')
        self.assertTrue(config['shared']['provider_choice_confirmed'])
        self.assertFalse(self.store.provider_selection_required)
        with open(self.primary, 'r', encoding='utf-8') as config_file:
            persisted = json.load(config_file)
        self.assertEqual(persisted['schema_version'], CONFIG_SCHEMA_VERSION)
        self.assertEqual(persisted['shared']['translation_provider'], 'google')

    def test_empty_legacy_key_cannot_erase_explicit_deepl_credentials(self):
        with open(self.root / "overlay_config.json", 'w', encoding='utf-8') as config_file:
            json.dump({
                'translation_provider': 'deepl',
                'deepl_api_key': 'explicit-secret',
            }, config_file)
        with open(self.root / "translator_config.json", 'w', encoding='utf-8') as config_file:
            json.dump({'shared': {'deepl_api_key': ''}}, config_file)

        config = self.store.load()

        self.assertEqual(config['shared']['translation_provider'], 'deepl')
        self.assertEqual(config['shared']['deepl_api_key'], 'explicit-secret')
        self.assertTrue(config['shared']['provider_choice_confirmed'])

    def test_migrated_schema_is_not_rewritten_on_the_next_load(self):
        with open(self.root / "translator_config.json", 'w', encoding='utf-8') as config_file:
            json.dump({'shared': {'deepl_api_key': 'legacy-secret'}}, config_file)

        first = self.store.load()
        self.assertEqual(first['shared']['translation_provider'], 'deepl')

        reloaded = ConfigStore(path=self.primary, legacy_dir=self.root)
        with patch.object(reloaded, 'save', wraps=reloaded.save) as save:
            second = reloaded.load()

        self.assertEqual(second['schema_version'], CONFIG_SCHEMA_VERSION)
        self.assertEqual(second['shared']['translation_provider'], 'deepl')
        save.assert_not_called()

    def test_invalid_explicit_provider_fails_closed(self):
        with open(self.root / "translator_config.json", 'w', encoding='utf-8') as config_file:
            json.dump({
                'shared': {
                    'translation_provider': 'surprise-cloud',
                    'deepl_api_key': 'preserved-secret',
                },
            }, config_file)

        config = self.store.load()

        self.assertEqual(config['shared']['translation_provider'], 'disabled')
        self.assertTrue(self.store.provider_selection_required)

    def test_invalid_provider_cannot_keep_confirmation_true(self):
        with open(self.root / "translator_config.json", 'w', encoding='utf-8') as config_file:
            json.dump({
                'shared': {
                    'translation_provider': 'surprise-cloud',
                    'provider_choice_confirmed': True,
                },
            }, config_file)

        config = self.store.load()

        self.assertEqual(config['shared']['translation_provider'], 'disabled')
        self.assertFalse(config['shared']['provider_choice_confirmed'])
        self.assertTrue(self.store.provider_selection_required)

    def test_string_false_confirmation_remains_unconfirmed(self):
        with open(self.root / "translator_config.json", 'w', encoding='utf-8') as config_file:
            json.dump({
                'shared': {
                    'translation_provider': 'disabled',
                    'provider_choice_confirmed': 'false',
                },
            }, config_file)

        config = self.store.load()

        self.assertFalse(config['shared']['provider_choice_confirmed'])
        self.assertTrue(self.store.provider_selection_required)

    def test_legacy_empty_key_requests_explicit_provider_selection(self):
        with open(self.root / "translator_config.json", 'w', encoding='utf-8') as config_file:
            json.dump({'shared': {'deepl_api_key': ''}}, config_file)

        config = self.store.load()

        self.assertEqual(config['shared']['translation_provider'], 'disabled')
        self.assertTrue(self.store.provider_selection_required)
        self.assertTrue(self.store.migrated_from_implicit_google)

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

    def test_malformed_runtime_values_are_normalized_on_load(self):
        with open(self.root / "translator_config.json", 'w', encoding='utf-8') as config_file:
            json.dump({
                'shared': {
                    'opacity': 'bad',
                    'font_size': 999,
                    'auto_scroll': 'false',
                    'target_language': 123,
                    'polling_interval': float('nan'),
                    'fleet_scan_interval': -5,
                },
                'sessions': {
                    'fleet': {'w': 'bad', 'h': 20, 'enabled': 'true'},
                },
            }, config_file)

        config = self.store.load()

        self.assertEqual(config['shared']['opacity'], 0.8)
        self.assertEqual(config['shared']['font_size'], 36)
        self.assertFalse(config['shared']['auto_scroll'])
        self.assertEqual(config['shared']['target_language'], 'en')
        self.assertEqual(config['shared']['polling_interval'], 1.0)
        self.assertEqual(config['shared']['fleet_scan_interval'], 5)
        self.assertEqual(config['sessions']['fleet']['w'], 300)
        self.assertEqual(config['sessions']['fleet']['h'], 120)
        self.assertTrue(config['sessions']['fleet']['enabled'])

    def test_save_writes_primary_path_and_filters_shared_session_keys(self):
        saved_ok = self.store.save({
            'shared': {'opacity': 0.8, 'deepl_api_key': 'secret'},
            'sessions': {'fleet': {'x': 100, 'opacity': 0.2}},
        })

        self.assertTrue(saved_ok)
        self.assertTrue(self.primary.exists())
        with open(self.primary, 'r', encoding='utf-8') as config_file:
            saved = json.load(config_file)

        self.assertEqual(saved['sessions']['fleet']['x'], 100)
        self.assertNotIn('opacity', saved['sessions']['fleet'])
        self.assertEqual(saved['schema_version'], CONFIG_SCHEMA_VERSION)

    def test_failed_atomic_save_keeps_previous_config_and_returns_false(self):
        self.primary.parent.mkdir(parents=True, exist_ok=True)
        self.primary.write_text('{"old": true}', encoding='utf-8')

        with patch('src.app_config.os.replace', side_effect=OSError('disk full')):
            saved_ok = self.store.save({'shared': {}, 'sessions': {}})

        self.assertFalse(saved_ok)
        self.assertEqual(self.primary.read_text(encoding='utf-8'), '{"old": true}')
        self.assertEqual(list(self.primary.parent.glob('*.tmp')), [])

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

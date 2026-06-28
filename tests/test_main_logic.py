import json
import os
import sys
import tempfile
import shutil
import time
import unittest
from unittest.mock import MagicMock, patch
from pathlib import Path


class TestSharedConfigKeys(unittest.TestCase):
    """Verify the SHARED_CONFIG_KEYS constant is correct and complete."""

    def test_shared_config_keys_exists(self):
        from src.main import SHARED_CONFIG_KEYS
        self.assertIsInstance(SHARED_CONFIG_KEYS, frozenset)

    def test_shared_config_keys_contains_expected(self):
        from src.main import SHARED_CONFIG_KEYS
        expected = {
            'opacity', 'font_size', 'auto_scroll',
            'ignored_languages', 'target_language', 'deepl_api_key',
            'color_default', 'color_translated', 'color_highlight', 'log_dir',
            'fleet_inactive_threshold', 'fleet_auto_switch', 'fleet_scan_interval',
            'fleet_history_lines', 'polling_interval'
        }
        self.assertEqual(SHARED_CONFIG_KEYS, expected)

    def test_session_keys_not_in_shared(self):
        """Position/size/background should NOT be in shared keys."""
        from src.main import SHARED_CONFIG_KEYS
        session_only = {'x', 'y', 'w', 'h', 'background_color', 'enabled', 'title_prefix'}
        self.assertTrue(session_only.isdisjoint(SHARED_CONFIG_KEYS))


class TestConfigLoadSave(unittest.TestCase):
    """Tests for TranslatorManager config loading and saving."""

    def setUp(self):
        self.test_dir = tempfile.mkdtemp()
        self.original_cwd = os.getcwd()
        os.chdir(self.test_dir)
        self.config_path = Path(self.test_dir) / ".eve_translator" / "translator_config.json"

    def tearDown(self):
        os.chdir(self.original_cwd)
        time.sleep(0.05)
        shutil.rmtree(self.test_dir, ignore_errors=True)

    def _make_manager(self):
        """Create a TranslatorManager with __init__ bypassed."""
        with patch('src.main.TranslatorManager.__init__', lambda self: None):
            from src.app_config import ConfigStore
            from src.main import TranslatorManager
            manager = TranslatorManager()
            manager.config_store = ConfigStore(path=self.config_path, legacy_dir=self.test_dir)
            return manager

    def test_load_config_defaults(self):
        """Loading config with no file should return defaults."""
        mgr = self._make_manager()
        from src.main import TranslatorManager
        config = TranslatorManager._load_config(mgr)

        self.assertIn('shared', config)
        self.assertIn('sessions', config)
        self.assertEqual(config['shared']['opacity'], 0.8)
        self.assertEqual(config['shared']['font_size'], 10)
        self.assertTrue(config['shared']['auto_scroll'])
        self.assertTrue(config['sessions']['fleet']['enabled'])
        self.assertFalse(config['sessions']['local']['enabled'])

    def test_load_config_merges_existing(self):
        """Existing config file should be merged with defaults."""
        partial = {
            'shared': {'opacity': 0.5, 'font_size': 14},
            'sessions': {
                'fleet': {'x': 500, 'y': 600},
                'local': {'enabled': True}
            }
        }
        self.config_path.parent.mkdir(parents=True, exist_ok=True)
        with open(self.config_path, 'w') as f:
            json.dump(partial, f)

        mgr = self._make_manager()
        from src.main import TranslatorManager
        config = TranslatorManager._load_config(mgr)

        # Overridden values
        self.assertEqual(config['shared']['opacity'], 0.5)
        self.assertEqual(config['shared']['font_size'], 14)
        self.assertEqual(config['sessions']['fleet']['x'], 500)
        self.assertTrue(config['sessions']['local']['enabled'])

        # Default values still present
        self.assertTrue(config['shared']['auto_scroll'])
        self.assertEqual(config['shared']['target_language'], 'en')

    def test_load_config_legacy_migration(self):
        """Legacy overlay_config.json values should be picked up in defaults."""
        legacy = {'opacity': 0.6, 'font_size': 12, 'x': 200, 'y': 300}
        with open('overlay_config.json', 'w') as f:
            json.dump(legacy, f)

        mgr = self._make_manager()
        from src.main import TranslatorManager
        config = TranslatorManager._load_config(mgr)

        # Legacy values should inform defaults
        self.assertEqual(config['shared']['opacity'], 0.6)
        self.assertEqual(config['shared']['font_size'], 12)
        self.assertEqual(config['sessions']['fleet']['x'], 200)

    def test_load_config_corrupt_file(self):
        """Corrupt config file should fall back to defaults."""
        self.config_path.parent.mkdir(parents=True, exist_ok=True)
        with open(self.config_path, 'w') as f:
            f.write("not valid json {{{")

        mgr = self._make_manager()
        from src.main import TranslatorManager
        config = TranslatorManager._load_config(mgr)

        # Should still get valid defaults
        self.assertIn('shared', config)
        self.assertEqual(config['shared']['opacity'], 0.8)

    def test_load_config_legacy_trumped_by_config(self):
        """translator_config.json should override legacy overlay_config.json."""
        legacy = {'opacity': 0.6}
        with open('overlay_config.json', 'w') as f:
            json.dump(legacy, f)

        config = {'shared': {'opacity': 0.3}}
        self.config_path.parent.mkdir(parents=True, exist_ok=True)
        with open(self.config_path, 'w') as f:
            json.dump(config, f)

        mgr = self._make_manager()
        from src.main import TranslatorManager
        result = TranslatorManager._load_config(mgr)

        # translator_config should win over legacy
        self.assertEqual(result['shared']['opacity'], 0.3)


class TestBuildSessionConfig(unittest.TestCase):
    """Tests for _build_session_config merging shared + session."""

    def test_merges_shared_and_session(self):
        with patch('src.main.TranslatorManager.__init__', lambda self: None):
            from src.main import TranslatorManager
            mgr = TranslatorManager()
            mgr.config = {
                'shared': {'opacity': 0.5, 'font_size': 12, 'target_language': 'de'},
                'sessions': {
                    'fleet': {'x': 100, 'y': 200, 'background_color': '#33001a'},
                    'local': {'x': 300, 'y': 400, 'background_color': '#001a33'},
                }
            }

            fleet_config = mgr._build_session_config('fleet')
            self.assertEqual(fleet_config['opacity'], 0.5)
            self.assertEqual(fleet_config['x'], 100)
            self.assertEqual(fleet_config['background_color'], '#33001a')
            self.assertEqual(fleet_config['target_language'], 'de')

            local_config = mgr._build_session_config('local')
            self.assertEqual(local_config['x'], 300)
            self.assertEqual(local_config['background_color'], '#001a33')

    def test_session_overrides_shared(self):
        """Session-specific values should override shared."""
        with patch('src.main.TranslatorManager.__init__', lambda self: None):
            from src.main import TranslatorManager
            mgr = TranslatorManager()
            mgr.config = {
                'shared': {'opacity': 0.5},
                'sessions': {'fleet': {'opacity': 0.9}},
            }

            config = mgr._build_session_config('fleet')
            self.assertEqual(config['opacity'], 0.9)


class TestDisconnectSessionSignals(unittest.TestCase):
    """Tests for _disconnect_session_signals helper."""

    def test_disconnects_all_signals(self):
        with patch('src.main.TranslatorManager.__init__', lambda self: None):
            from src.main import TranslatorManager
            mgr = TranslatorManager()
            mgr.worker = MagicMock()

            mock_session = MagicMock()
            mock_session.overlay = MagicMock()
            mock_session.overlay.fleet_selected = MagicMock()

            mgr._disconnect_session_signals(mock_session)

            mock_session.lines_ready.disconnect.assert_called_once()
            mock_session.request_toggle.disconnect.assert_called_once()
            mock_session.request_settings.disconnect.assert_called_once()
            mock_session.config_changed.disconnect.assert_called_once()
            mock_session.character_selected.disconnect.assert_called_once()
            mock_session.exit_requested.disconnect.assert_called_once()
            mock_session.overlay.fleet_selected.disconnect.assert_called_once()

    def test_handles_runtime_errors(self):
        """Should not raise even if signals were already disconnected."""
        with patch('src.main.TranslatorManager.__init__', lambda self: None):
            from src.main import TranslatorManager
            mgr = TranslatorManager()
            mgr.worker = MagicMock()

            mock_session = MagicMock()
            mock_session.lines_ready.disconnect.side_effect = RuntimeError
            mock_session.request_toggle.disconnect.side_effect = RuntimeError
            mock_session.request_settings.disconnect.side_effect = RuntimeError
            mock_session.config_changed.disconnect.side_effect = RuntimeError
            mock_session.character_selected.disconnect.side_effect = RuntimeError
            mock_session.exit_requested.disconnect.side_effect = RuntimeError
            mock_session.overlay = MagicMock()
            mock_session.overlay.fleet_selected.disconnect.side_effect = RuntimeError

            mgr._disconnect_session_signals(mock_session)  # Should not raise

    def test_handles_no_fleet_selected_attr(self):
        """Should handle overlay without fleet_selected signal."""
        with patch('src.main.TranslatorManager.__init__', lambda self: None):
            from src.main import TranslatorManager
            mgr = TranslatorManager()
            mgr.worker = MagicMock()

            mock_session = MagicMock()
            mock_session.overlay = MagicMock(spec=[])  # No attributes

            mgr._disconnect_session_signals(mock_session)  # Should not raise


class TestLogProcessingWorker(unittest.TestCase):
    """Tests for LogProcessingWorker."""

    def _make_worker(self, provider=None):
        from src.main import LogProcessingWorker
        from src.services.translator import TranslationService, GoogleTransProvider
        parser = MagicMock()
        tokenizer = MagicMock()
        detector = MagicMock()
        if provider is None:
            provider = GoogleTransProvider()
        translator_service = TranslationService(provider=provider)
        return LogProcessingWorker(parser, tokenizer, detector, translator_service)

    def test_update_config_switches_to_deepl(self):
        """Setting a DeepL key should switch provider."""
        from src.services.translator import GoogleTransProvider
        worker = self._make_worker(GoogleTransProvider())
        self.assertEqual(worker.translator_service.provider.name, "Google")

        # Patch DeepLProvider to avoid needing real API key
        with patch('src.services.translator.DeepLProvider') as MockDeepL:
            mock_provider = MagicMock()
            mock_provider.name = "DeepL"
            MockDeepL.return_value = mock_provider
            worker.update_config({'deepl_api_key': 'test-key', 'ignored_languages': ['en']})
            MockDeepL.assert_called_once_with('test-key')

    def test_update_config_recreates_deepl_when_key_changes(self):
        """Changing the DeepL key should recreate the provider."""
        from src.services.translator import GoogleTransProvider
        worker = self._make_worker(GoogleTransProvider())

        with patch('src.services.translator.DeepLProvider') as MockDeepL:
            mock_provider = MagicMock()
            mock_provider.name = "DeepL"
            MockDeepL.return_value = mock_provider

            worker.update_config({'deepl_api_key': 'first-key', 'ignored_languages': ['en']})
            worker.update_config({'deepl_api_key': 'second-key', 'ignored_languages': ['en']})

            self.assertEqual(MockDeepL.call_count, 2)
            MockDeepL.assert_any_call('first-key')
            MockDeepL.assert_any_call('second-key')

    def test_update_config_removes_deepl_when_key_removed(self):
        """Removing the DeepL key should switch back to Google."""
        from src.services.translator import GoogleTransProvider
        worker = self._make_worker(GoogleTransProvider())

        with patch('src.services.translator.DeepLProvider') as MockDeepL, \
             patch('src.services.translator.GoogleTransProvider') as MockGoogle:
            deepl_provider = MagicMock()
            deepl_provider.name = "DeepL"
            google_provider = MagicMock()
            google_provider.name = "Google"
            MockDeepL.return_value = deepl_provider
            MockGoogle.return_value = google_provider

            worker.update_config({'deepl_api_key': 'test-key', 'ignored_languages': ['en']})
            worker.update_config({'deepl_api_key': '', 'ignored_languages': ['en']})

            MockGoogle.assert_called_once_with()
            self.assertEqual(worker.translator_service.provider.name, "Google")

    def test_update_config_stays_on_google_when_no_key(self):
        """Without API key, should keep Google provider."""
        from src.services.translator import GoogleTransProvider
        worker = self._make_worker(GoogleTransProvider())
        worker.update_config({'deepl_api_key': '', 'ignored_languages': ['en']})
        self.assertEqual(worker.translator_service.provider.name, "Google")

    def test_process_lines_handles_errors(self):
        """Errors processing individual lines should not crash the batch."""
        from src.main import LogProcessingWorker

        parser = MagicMock()
        parser.parse.side_effect = [
            Exception("parse error"),
            MagicMock(
                message="hello", sender="Player",
                timestamp=MagicMock(strftime=MagicMock(return_value="08:00:00"))
            )
        ]
        tokenizer = MagicMock()
        tokenizer.tokenize.return_value = MagicMock(cleaned="hello", tokens={})
        detector = MagicMock()
        detector.should_translate.return_value = (False, None)
        translator_service = MagicMock()

        worker = LogProcessingWorker(parser, tokenizer, detector, translator_service)
        # Should not raise despite first line erroring
        worker.process_lines("fleet", ["bad line", "good line"])

    def test_process_lines_skips_unparseable(self):
        """Lines that don't parse should be skipped."""
        from src.main import LogProcessingWorker

        parser = MagicMock()
        parser.parse.return_value = None  # Unparseable
        tokenizer = MagicMock()
        detector = MagicMock()
        translator_service = MagicMock()

        worker = LogProcessingWorker(parser, tokenizer, detector, translator_service)
        worker.process_lines("fleet", ["header line"])
        # Tokenizer should never be called since parse returned None
        tokenizer.tokenize.assert_not_called()


class TestHandleSessionConfigChange(unittest.TestCase):
    """Tests for _handle_session_config_change."""

    def test_extracts_shared_keys(self):
        with patch('src.main.TranslatorManager.__init__', lambda self: None):
            from src.main import TranslatorManager
            mgr = TranslatorManager()
            mgr.config = {
                'shared': {'opacity': 0.5, 'font_size': 10},
                'sessions': {'fleet': {'x': 100}, 'local': {'x': 200}},
            }
            mgr.worker = MagicMock()
            mgr.sessions = {}
            mgr._save_config = MagicMock()

            mgr._handle_session_config_change('fleet', {
                'opacity': 0.7,
                'x': 500,
                'y': 600,
            })

            self.assertEqual(mgr.config['shared']['opacity'], 0.7)
            self.assertEqual(mgr.config['sessions']['fleet']['x'], 500)
            self.assertEqual(mgr.config['sessions']['fleet']['y'], 600)

    def test_notifies_worker(self):
        with patch('src.main.TranslatorManager.__init__', lambda self: None):
            from src.main import TranslatorManager
            mgr = TranslatorManager()
            mgr.config = {
                'shared': {'font_size': 10},
                'sessions': {'fleet': {}},
            }
            mgr.worker = MagicMock()
            mgr.sessions = {}
            mgr._save_config = MagicMock()

            mgr._handle_session_config_change('fleet', {'font_size': 14})
            mgr.worker.update_config.assert_called_once()


if __name__ == '__main__':
    unittest.main()

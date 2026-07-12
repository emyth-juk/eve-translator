import json
import os
import sys
import tempfile
import shutil
import time
import unittest
from unittest.mock import MagicMock, call, patch
from pathlib import Path

from PySide6.QtCore import QObject


class TestSharedConfigKeys(unittest.TestCase):
    """Verify the SHARED_CONFIG_KEYS constant is correct and complete."""

    def test_shared_config_keys_exists(self):
        from src.main import SHARED_CONFIG_KEYS
        self.assertIsInstance(SHARED_CONFIG_KEYS, frozenset)

    def test_shared_config_keys_contains_expected(self):
        from src.main import SHARED_CONFIG_KEYS
        expected = {
            'opacity', 'font_size', 'auto_scroll',
            'ignored_languages', 'target_language', 'translation_provider',
            'provider_choice_confirmed', 'deepl_api_key',
            'color_default', 'color_translated', 'color_highlight', 'log_dir',
            'fleet_inactive_threshold', 'fleet_auto_switch', 'fleet_scan_interval',
            'fleet_history_lines', 'polling_interval',
            'speaker_roles', 'global_hotkey',
            'entity_protection', 'entity_index_path', 'burst_optimization',
            'burst_batch_size', 'context_assist', 'context_messages',
            'active_doctrine_pack',
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


class TestScannerWatcherConfig(unittest.TestCase):
    """Tests for scanner fallback interval selection."""

    def test_watched_directory_uses_relaxed_fallback_interval(self):
        with patch('src.main.TranslatorManager.__init__', lambda self: None):
            from src.main import TranslatorManager

            mgr = TranslatorManager()
            mgr.config = {'shared': {'fleet_scan_interval': 10}}
            mgr._log_dir_watch_active = True
            self.assertEqual(mgr._scanner_interval_ms(), 60000)

            mgr.config['shared']['fleet_scan_interval'] = 120
            self.assertEqual(mgr._scanner_interval_ms(), 120000)

            mgr._log_dir_watch_active = False
            self.assertEqual(mgr._scanner_interval_ms(), 120000)


class TestSessionRuntimeState(unittest.TestCase):
    """Tests for waiting-session recovery and truthful tray state."""

    def _make_manager(self, fleet_enabled=False, local_enabled=False):
        with patch('src.main.TranslatorManager.__init__', lambda self: None):
            from src.main import SessionRuntimeState, TranslatorManager

            mgr = TranslatorManager()
            QObject.__init__(mgr)

        mgr.config = {
            'shared': {},
            'sessions': {
                'fleet': {'enabled': fleet_enabled},
                'local': {'enabled': local_enabled},
            },
        }
        mgr.sessions = {'fleet': None, 'local': None}
        mgr._session_runtime_states = {
            'fleet': (
                SessionRuntimeState.WAITING
                if fleet_enabled
                else SessionRuntimeState.DISABLED
            ),
            'local': (
                SessionRuntimeState.WAITING
                if local_enabled
                else SessionRuntimeState.DISABLED
            ),
        }
        mgr._session_runtime_errors = {}
        mgr._sessions_initialized = True
        mgr.worker = MagicMock()
        mgr.worker.request_config_update.return_value = 1
        mgr.fleet_action = MagicMock()
        mgr.local_action = MagicMock()
        mgr.tray_icon = MagicMock()
        mgr._save_config = MagicMock()
        mgr._broadcast_session_states = MagicMock()
        mgr._broadcast_character_list = MagicMock()
        return mgr

    def test_enabling_without_log_enters_waiting_state(self):
        from src.main import SessionRuntimeState

        mgr = self._make_manager()
        mgr._get_log_path = MagicMock(return_value=None)

        started = mgr.start_session('fleet')

        self.assertFalse(started)
        self.assertTrue(mgr.config['sessions']['fleet']['enabled'])
        self.assertIsNone(mgr.sessions['fleet'])
        self.assertEqual(
            mgr._get_session_runtime_state('fleet'),
            SessionRuntimeState.WAITING,
        )
        self.assertIn('Waiting', mgr.fleet_action.setText.call_args.args[0])

    def test_toggling_waiting_session_disables_it(self):
        from src.main import SessionRuntimeState

        mgr = self._make_manager(fleet_enabled=True)
        mgr.start_session = MagicMock()

        mgr.toggle_session('fleet')

        self.assertFalse(mgr.config['sessions']['fleet']['enabled'])
        self.assertEqual(
            mgr._get_session_runtime_state('fleet'),
            SessionRuntimeState.DISABLED,
        )
        mgr.start_session.assert_not_called()
        mgr._save_config.assert_called_once_with()
        self.assertIn('Off', mgr.fleet_action.setText.call_args.args[0])

    def test_periodic_scan_recovers_waiting_session_when_log_appears(self):
        from src.main import SessionRuntimeState

        mgr = self._make_manager()
        mgr._get_log_path = MagicMock(side_effect=[None, 'C:/logs/Fleet_test.txt'])
        mgr._scan_characters = MagicMock()
        mgr._scan_fleets = MagicMock()

        self.assertFalse(mgr.start_session('fleet'))
        self.assertEqual(
            mgr._get_session_runtime_state('fleet'),
            SessionRuntimeState.WAITING,
        )

        session = MagicMock()
        session.overlay = MagicMock()
        with patch('src.main.ChatSession', return_value=session) as session_cls:
            mgr._periodic_scan()

        mgr._scan_characters.assert_called_once_with()
        mgr._scan_fleets.assert_called_once_with()
        session_cls.assert_called_once_with(
            'fleet',
            'C:/logs/Fleet_test.txt',
            {'enabled': True},
            parent=mgr,
        )
        session.start.assert_called_once_with()
        self.assertIs(mgr.sessions['fleet'], session)
        self.assertEqual(
            mgr._get_session_runtime_state('fleet'),
            SessionRuntimeState.ACTIVE,
        )
        self.assertIn('Active', mgr.fleet_action.setText.call_args.args[0])

    def test_recovery_checks_fleet_and_local_waiting_sessions(self):
        mgr = self._make_manager(fleet_enabled=True, local_enabled=True)
        mgr.start_session = MagicMock()

        mgr._recover_waiting_sessions()

        self.assertEqual(
            mgr.start_session.call_args_list,
            [call('fleet'), call('local')],
        )

    def test_recovery_preserves_waiting_state_for_open_stale_overlay(self):
        from src.main import SessionRuntimeState

        mgr = self._make_manager(fleet_enabled=True)
        mgr.sessions['fleet'] = MagicMock()
        mgr._set_session_runtime_state('fleet', SessionRuntimeState.WAITING)
        mgr.start_session = MagicMock()

        mgr._recover_waiting_sessions()

        self.assertEqual(
            mgr._get_session_runtime_state('fleet'),
            SessionRuntimeState.WAITING,
        )
        mgr.start_session.assert_not_called()

    def test_log_lookup_error_is_visible_and_can_be_disabled(self):
        from src.main import SessionRuntimeState

        mgr = self._make_manager()
        mgr._get_log_path = MagicMock(side_effect=OSError('log directory unavailable'))

        self.assertFalse(mgr.start_session('local'))
        self.assertEqual(
            mgr._get_session_runtime_state('local'),
            SessionRuntimeState.ERROR,
        )
        self.assertIn('Error', mgr.local_action.setText.call_args.args[0])

        mgr.toggle_session('local')

        self.assertFalse(mgr.config['sessions']['local']['enabled'])
        self.assertEqual(
            mgr._get_session_runtime_state('local'),
            SessionRuntimeState.DISABLED,
        )

    def test_tray_menu_exposes_settings_action(self):
        mgr = self._make_manager()
        mgr.app = MagicMock()
        fleet_action = MagicMock()
        local_action = MagicMock()
        lock_action = MagicMock()
        settings_action = MagicMock()
        exit_action = MagicMock()

        with patch('src.main.QSystemTrayIcon') as tray_cls, \
                patch('src.main.QMenu') as menu_cls, \
                patch('src.main.QAction') as action_cls, \
                patch('src.main.QIcon'), \
                patch('src.main.WindowsGlobalHotkey') as hotkey_cls:
            action_cls.side_effect = [
                fleet_action,
                local_action,
                lock_action,
                settings_action,
                exit_action,
            ]
            hotkey_cls.return_value.register.return_value = True
            mgr._setup_system_tray()

        self.assertIs(mgr.settings_action, settings_action)
        settings_action.triggered.connect.assert_called_once_with(mgr.open_settings_dialog)
        menu_cls.return_value.addAction.assert_any_call(settings_action)
        tray_cls.return_value.messageClicked.connect.assert_called_once_with(
            mgr.open_settings_dialog
        )
        tray_cls.return_value.show.assert_called_once_with()
        self.assertIs(mgr.lock_overlays_action, lock_action)

    def test_interaction_lock_is_applied_to_every_live_overlay(self):
        mgr = self._make_manager()
        mgr.overlay_locked = False
        mgr.lock_overlays_action = MagicMock()
        fleet = MagicMock()
        local = MagicMock()
        mgr.sessions = {'fleet': fleet, 'local': local}
        mgr._update_overlay_status = MagicMock()

        mgr.set_overlay_locked(True)

        fleet.overlay.set_interaction_locked.assert_called_once_with(True)
        local.overlay.set_interaction_locked.assert_called_once_with(True)
        self.assertTrue(mgr.overlay_locked)
        mgr.lock_overlays_action.setChecked.assert_any_call(True)

    def test_provider_setup_notice_explains_google_migration(self):
        mgr = self._make_manager()
        mgr.config_store = MagicMock(
            provider_selection_required=True,
            migrated_from_implicit_google=True,
        )

        mgr._show_provider_setup_notice()

        mgr.tray_icon.showMessage.assert_called_once()
        self.assertIn(
            'automatic Google fallback',
            mgr.tray_icon.showMessage.call_args.args[1],
        )

    def test_disabled_local_stays_off_when_character_log_rotates(self):
        from src.core.character_info import CharacterInfo

        mgr = self._make_manager(local_enabled=False)
        mgr.config['shared']['log_dir'] = 'C:/logs'
        mgr.selected_character_id = '123'
        mgr.character_registry = {
            '123': CharacterInfo('123', 'Pilot', 'old.txt', 1000, 'Jita', True),
        }
        new_registry = {
            '123': CharacterInfo('123', 'Pilot', 'new.txt', 2000, 'Amarr', True),
        }
        mgr._switch_local_character = MagicMock()
        mgr._broadcast_character_list = MagicMock()

        with patch('src.main.os.path.exists', return_value=True), \
                patch('src.main.LocalChatDetector') as detector_cls:
            detector_cls.return_value.scan_active_characters.return_value = new_registry
            mgr._scan_characters()

        mgr._switch_local_character.assert_not_called()
        self.assertFalse(mgr.config['sessions']['local']['enabled'])
        self.assertEqual(mgr.character_registry, new_registry)

    def test_enabled_local_rotation_publishes_new_registry_before_restart(self):
        from src.core.character_info import CharacterInfo

        mgr = self._make_manager(local_enabled=True)
        mgr.config['shared']['log_dir'] = 'C:/logs'
        mgr.selected_character_id = '123'
        mgr.character_registry = {
            '123': CharacterInfo('123', 'Pilot', 'old.txt', 1000, 'Jita', True),
        }
        new_registry = {
            '123': CharacterInfo('123', 'Pilot', 'new.txt', 2000, 'Amarr', True),
        }

        def assert_new_registry(char_id):
            self.assertEqual(char_id, '123')
            self.assertEqual(
                mgr.character_registry[char_id].latest_log_path,
                'new.txt',
            )

        mgr._switch_local_character = MagicMock(side_effect=assert_new_registry)
        mgr._broadcast_character_list = MagicMock()

        with patch('src.main.os.path.exists', return_value=True), \
                patch('src.main.LocalChatDetector') as detector_cls:
            detector_cls.return_value.scan_active_characters.return_value = new_registry
            mgr._scan_characters()

        mgr._switch_local_character.assert_called_once_with('123')

    def test_stale_fleet_log_keeps_session_waiting(self):
        mgr = self._make_manager(fleet_enabled=True)
        mgr.config['shared']['fleet_inactive_threshold'] = 60

        with tempfile.TemporaryDirectory() as temp_dir:
            stale_log = Path(temp_dir) / 'Fleet_20260710_100000_123.txt'
            stale_log.write_text('old fleet', encoding='utf-8')
            stale_time = time.time() - 3600
            os.utime(stale_log, (stale_time, stale_time))
            mgr.config['shared']['log_dir'] = temp_dir

            self.assertIsNone(mgr._find_latest_fleet_log())

    def test_inactive_fleet_downgrades_open_overlay_to_waiting(self):
        from src.core.fleet_info import FleetInfo
        from src.main import SessionRuntimeState

        mgr = self._make_manager(fleet_enabled=True)
        mgr.config['shared'].update({
            'log_dir': 'C:/logs',
            'fleet_inactive_threshold': 1800,
            'fleet_auto_switch': False,
        })
        mgr.sessions['fleet'] = MagicMock()
        mgr.selected_fleet_id = 'old'
        mgr.fleet_registry = {
            'old': FleetInfo('old', 'Pilot', 'old.txt', 1000, 1000.0, True),
        }

        with patch('src.main.os.path.exists', return_value=True), \
                patch('src.services.fleet_detector.FleetDetector') as detector_cls:
            detector_cls.return_value.scan_active_fleets.return_value = {}
            mgr._scan_fleets()

        self.assertEqual(mgr.selected_fleet_id, 'old')
        self.assertEqual(
            mgr._get_session_runtime_state('fleet'),
            SessionRuntimeState.WAITING,
        )

    def test_live_fleet_log_restores_open_overlay_to_active(self):
        from src.core.fleet_info import FleetInfo
        from src.main import SessionRuntimeState

        mgr = self._make_manager(fleet_enabled=True)
        mgr.config['shared'].update({
            'log_dir': 'C:/logs',
            'fleet_inactive_threshold': 1800,
            'fleet_auto_switch': False,
        })
        mgr.sessions['fleet'] = MagicMock()
        mgr.selected_fleet_id = 'old'
        mgr._set_session_runtime_state('fleet', SessionRuntimeState.WAITING)
        fleet = FleetInfo('old', 'Pilot', 'old.txt', 1000, 2000.0, True)
        mgr.fleet_registry = {'old': fleet}

        with patch('src.main.os.path.exists', return_value=True), \
                patch('src.services.fleet_detector.FleetDetector') as detector_cls:
            detector_cls.return_value.scan_active_fleets.return_value = {'old': fleet}
            mgr._scan_fleets()

        self.assertEqual(
            mgr._get_session_runtime_state('fleet'),
            SessionRuntimeState.ACTIVE,
        )

    def test_inactive_selected_character_keeps_local_waiting(self):
        from src.core.character_info import CharacterInfo

        mgr = self._make_manager(local_enabled=True)
        mgr.selected_character_id = '123'
        mgr.character_registry = {
            '123': CharacterInfo(
                '123', 'Pilot', 'stale-local.txt', 1000, 'Jita', False
            ),
        }

        with tempfile.TemporaryDirectory() as temp_dir:
            mgr.config['shared']['log_dir'] = temp_dir

            self.assertIsNone(mgr._find_latest_local_log())

    def test_inactive_local_character_downgrades_open_overlay_to_waiting(self):
        from src.core.character_info import CharacterInfo
        from src.main import SessionRuntimeState

        mgr = self._make_manager(local_enabled=True)
        mgr.config['shared']['log_dir'] = 'C:/logs'
        mgr.sessions['local'] = MagicMock()
        mgr.selected_character_id = '123'
        active = CharacterInfo('123', 'Pilot', 'local.txt', 1000, 'Jita', True)
        inactive = CharacterInfo('123', 'Pilot', 'local.txt', 1000, 'Jita', False)
        mgr.character_registry = {'123': active}

        with patch('src.main.os.path.exists', return_value=True), \
                patch('src.main.LocalChatDetector') as detector_cls:
            detector_cls.return_value.scan_active_characters.return_value = {
                '123': inactive,
            }
            mgr._scan_characters()

        self.assertEqual(
            mgr._get_session_runtime_state('local'),
            SessionRuntimeState.WAITING,
        )

    def test_active_local_character_restores_open_overlay_to_active(self):
        from src.core.character_info import CharacterInfo
        from src.main import SessionRuntimeState

        mgr = self._make_manager(local_enabled=True)
        mgr.config['shared']['log_dir'] = 'C:/logs'
        mgr.sessions['local'] = MagicMock()
        mgr.selected_character_id = '123'
        active = CharacterInfo('123', 'Pilot', 'local.txt', 2000, 'Jita', True)
        mgr.character_registry = {'123': active}
        mgr._set_session_runtime_state('local', SessionRuntimeState.WAITING)

        with patch('src.main.os.path.exists', return_value=True), \
                patch('src.main.LocalChatDetector') as detector_cls:
            detector_cls.return_value.scan_active_characters.return_value = {
                '123': active,
            }
            mgr._scan_characters()

        self.assertEqual(
            mgr._get_session_runtime_state('local'),
            SessionRuntimeState.ACTIVE,
        )

    def test_most_recent_active_character_is_selected_for_local(self):
        from src.core.character_info import CharacterInfo

        mgr = self._make_manager(local_enabled=True)
        mgr.character_registry = {
            'old': CharacterInfo('old', 'Old', 'old.txt', 1000, 'Jita', True),
            'new': CharacterInfo('new', 'New', 'new.txt', 2000, 'Amarr', True),
        }

        with tempfile.TemporaryDirectory() as temp_dir:
            mgr.config['shared']['log_dir'] = temp_dir

            self.assertEqual(mgr._find_latest_local_log(), 'new.txt')

        self.assertEqual(mgr.selected_character_id, 'new')
        self.assertEqual(mgr.config['sessions']['local']['character_name'], 'New')

    def test_fleet_scan_publishes_new_registry_before_switch(self):
        from src.core.fleet_info import FleetInfo

        mgr = self._make_manager(fleet_enabled=True)
        mgr.config['shared'].update({
            'log_dir': 'C:/logs',
            'fleet_inactive_threshold': 1800,
            'fleet_auto_switch': True,
        })
        mgr.sessions['fleet'] = MagicMock()
        mgr.selected_fleet_id = 'old'
        mgr.fleet_registry = {
            'old': FleetInfo('old', 'Pilot', 'old.txt', 1000, 1000.0, True),
        }
        new_fleet = FleetInfo('new', 'Pilot', 'new.txt', 2000, 2000.0, True)

        def assert_new_registry(fleet_id):
            self.assertEqual(fleet_id, 'new')
            self.assertIs(mgr.fleet_registry[fleet_id], new_fleet)

        mgr.switch_fleet = MagicMock(side_effect=assert_new_registry)
        mgr._broadcast_fleet_list = MagicMock()

        with patch('src.main.os.path.exists', return_value=True), \
                patch('src.services.fleet_detector.FleetDetector') as detector_cls:
            detector = detector_cls.return_value
            detector.scan_active_fleets.return_value = {'new': new_fleet}
            detector.get_most_recent_fleet.return_value = new_fleet
            mgr._scan_fleets()

        mgr.switch_fleet.assert_called_once_with('new')

    def test_route_drops_result_from_previous_session_source(self):
        from src.core.message import ProcessedMessage, TranslationState

        mgr = self._make_manager(fleet_enabled=True)
        session = MagicMock()
        session.source_id = 'current-source'
        mgr.sessions['fleet'] = session
        stale = ProcessedMessage(
            message_id='fleet:old-source:1',
            session_id='fleet',
            source_id='old-source',
            text='old command',
            sender='FC',
            timestamp='12:00:00',
            state=TranslationState.TRANSLATED,
        )

        mgr._route_message(stale)
        session.handle_processed_message.assert_not_called()

        current = ProcessedMessage(
            message_id='fleet:current-source:2',
            session_id='fleet',
            source_id='current-source',
            text='current command',
            sender='FC',
            timestamp='12:00:01',
            state=TranslationState.TRANSLATED,
        )
        mgr._route_message(current)
        session.handle_processed_message.assert_called_once_with(current)

    def test_cancelling_settings_persists_restored_provider_choice(self):
        mgr = self._make_manager()
        mgr.config['shared'] = {
            'translation_provider': 'disabled',
            'deepl_api_key': '',
        }
        mgr._apply_config_update = MagicMock()
        mgr._apply_scanner_config = MagicMock()

        def preview_then_cancel():
            # Simulate live preview plus a timer-driven save while the modal
            # dialog is still open.
            mgr.config['shared']['translation_provider'] = 'google'
            return False

        with patch('src.main.SettingsDialog') as dialog_cls:
            dialog_cls.return_value.exec.side_effect = preview_then_cancel
            mgr.open_settings_dialog()

        self.assertEqual(
            mgr.config['shared']['translation_provider'],
            'disabled',
        )
        mgr._save_config.assert_called_once_with()

    def test_saving_settings_confirms_provider_choice(self):
        mgr = self._make_manager()
        mgr.config['shared']['provider_choice_confirmed'] = False
        mgr._apply_config_update = MagicMock()
        mgr._apply_scanner_config = MagicMock()
        saved_config = {
            'shared': {
                **mgr.config['shared'],
                'translation_provider': 'disabled',
            },
            'sessions': mgr.config['sessions'],
        }

        with patch('src.main.SettingsDialog') as dialog_cls:
            dialog_cls.return_value.exec.return_value = True
            dialog_cls.return_value.get_settings.return_value = saved_config
            mgr.open_settings_dialog()

        self.assertTrue(mgr.config['shared']['provider_choice_confirmed'])
        mgr._save_config.assert_called_once_with()

    def test_local_correction_is_saved_and_replaces_visible_message(self):
        from src.core.translation_memory import TranslationMemory

        mgr = self._make_manager()
        mgr.config['shared'].update({
            'target_language': 'en',
            'active_doctrine_pack': '',
        })
        with tempfile.TemporaryDirectory() as directory:
            mgr.translation_memory = TranslationMemory(
                Path(directory) / 'corrections.sqlite3'
            )
            session = MagicMock()
            session.source_id = 'source'
            session.overlay.chat_history = [{
                'message_id': 'fleet:1',
                'original_text': 'Ne sautez pas',
                'text': 'Do not jump',
                'sender': 'FC',
                'timestamp': '12:00:00',
                'detected_language': 'fr',
                'speaker_role': 'fc',
                'commands': (),
                'revision': 2,
            }]
            mgr.sessions['fleet'] = session

            mgr._handle_correction_submitted('fleet', {
                'message_id': 'fleet:1',
                'source_text': 'Ne sautez pas',
                'translated_text': 'Do not take the gate',
                'source_language': 'fr',
                'target_language': 'en',
            })

            self.assertEqual(
                mgr.translation_memory.lookup('Ne sautez pas', 'fr', 'en'),
                'Do not take the gate',
            )
            updated = session.handle_processed_message.call_args.args[0]
            self.assertEqual(updated.provider, 'Local correction')
            self.assertEqual(updated.revision, 3)
    def test_live_preview_does_not_activate_provider_or_key(self):
        mgr = self._make_manager()
        mgr.config['shared'] = {
            'opacity': 0.8,
            'translation_provider': 'disabled',
            'deepl_api_key': '',
        }
        mgr._apply_config_update = MagicMock()

        mgr.preview_settings({
            'shared': {
                'opacity': 0.5,
                'translation_provider': 'deepl',
                'deepl_api_key': 'preview-secret',
            },
            'sessions': mgr.config['sessions'],
        })

        self.assertEqual(mgr.config['shared']['opacity'], 0.5)
        self.assertEqual(mgr.config['shared']['translation_provider'], 'disabled')
        self.assertEqual(mgr.config['shared']['deepl_api_key'], '')
        mgr._apply_config_update.assert_called_once_with()


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
        """Removing the DeepL key without an explicit provider should fail closed."""
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

            MockGoogle.assert_not_called()
            self.assertEqual(worker.translator_service.provider.name, "Disabled")

    def test_update_config_disables_cloud_provider_when_no_key(self):
        """Legacy no-key config must not silently keep Google enabled."""
        from src.services.translator import GoogleTransProvider
        worker = self._make_worker(GoogleTransProvider())
        worker.update_config({'deepl_api_key': '', 'ignored_languages': ['en']})
        self.assertEqual(worker.translator_service.provider.name, "Disabled")

    def test_update_config_selects_google_when_explicit(self):
        """Google is available after an explicit provider selection."""
        from src.services.translator import DisabledTranslationProvider
        worker = self._make_worker(DisabledTranslationProvider())

        with patch('src.services.translator.GoogleTransProvider') as MockGoogle:
            google_provider = MagicMock()
            google_provider.name = "Google"
            MockGoogle.return_value = google_provider

            worker.update_config({
                'translation_provider': 'google',
                'deepl_api_key': '',
                'ignored_languages': ['en'],
            })

            MockGoogle.assert_called_once_with()
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
        worker.process_lines("fleet", "source-1", ["bad line", "good line"])

    def test_process_lines_skips_unparseable(self):
        """Lines that don't parse should be skipped."""
        from src.main import LogProcessingWorker

        parser = MagicMock()
        parser.parse.return_value = None  # Unparseable
        tokenizer = MagicMock()
        detector = MagicMock()
        translator_service = MagicMock()

        worker = LogProcessingWorker(parser, tokenizer, detector, translator_service)
        worker.process_lines("fleet", "source-1", ["header line"])
        # Tokenizer should never be called since parse returned None
        tokenizer.tokenize.assert_not_called()


class TestHandleSessionConfigChange(unittest.TestCase):
    """Tests for _handle_session_config_change."""

    def test_extracts_shared_keys(self):
        with patch('src.main.TranslatorManager.__init__', lambda self: None):
            from src.main import TranslatorManager
            mgr = TranslatorManager()
            QObject.__init__(mgr)
            mgr.config = {
                'shared': {'opacity': 0.5, 'font_size': 10},
                'sessions': {'fleet': {'x': 100}, 'local': {'x': 200}},
            }
            mgr.worker = MagicMock()
            mgr.worker.request_config_update.return_value = 1
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
            mgr._queue_worker_config = MagicMock()

            mgr._handle_session_config_change('fleet', {'font_size': 14})
            mgr._queue_worker_config.assert_called_once_with()

    def test_worker_config_signal_receives_an_isolated_snapshot(self):
        with patch('src.main.TranslatorManager.__init__', lambda self: None):
            from src.main import TranslatorManager

            mgr = TranslatorManager()
            QObject.__init__(mgr)

        mgr.config = {'shared': {'ignored_languages': ['en']}}
        mgr.worker = MagicMock()
        mgr.worker.request_config_update.return_value = 7
        received = []
        mgr.worker_config_changed.connect(
            lambda generation, config: received.append((generation, config))
        )

        mgr._queue_worker_config()
        mgr.config['shared']['ignored_languages'].append('de')

        self.assertEqual(received, [(7, {'ignored_languages': ['en']})])


if __name__ == '__main__':
    unittest.main()

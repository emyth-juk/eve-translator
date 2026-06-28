import os
import sys
import tempfile
import shutil
import time
import unittest
from unittest.mock import MagicMock, patch
from pathlib import Path

# Patch paths: session.py uses local imports:
#   from src.core.tailer import FleetLogTailer
#   from src.gui.overlay import OverlayWindow
PATCH_TAILER = 'src.core.tailer.FleetLogTailer'
PATCH_OVERLAY = 'src.gui.overlay.OverlayWindow'

# Shared test config
def _make_config(**overrides):
    config = {
        'polling_interval': 1.0,
        'fleet_history_lines': 0,
        'fleet_inactive_threshold': 1800,
        'x': 100, 'y': 100, 'w': 300, 'h': 200,
        'opacity': 0.8, 'font_size': 10,
        'background_color': '#000',
        'color_default': '#e0e0e0',
        'color_translated': '#00ffff',
        'color_highlight': 'yellow',
        'auto_scroll': True,
        'title_prefix': '[FLEET]',
    }
    config.update(overrides)
    return config


class TestSessionConfigUpdate(unittest.TestCase):
    """Tests for ChatSession config update and polling logic."""

    def setUp(self):
        self.test_dir = tempfile.mkdtemp()
        self.log_path = self._create_log("Fleet_test.txt")

    def tearDown(self):
        time.sleep(0.05)
        shutil.rmtree(self.test_dir, ignore_errors=True)

    def _create_log(self, filename):
        path = os.path.join(self.test_dir, filename)
        content = "\r\n".join([
            "Channel ID:      (None)",
            "Channel Name:    Fleet",
            "Listener:        TestChar",
            "Session started: 2025.12.16 08:24:57",
            "",
            "[ 2025.12.16 08:25:00 ] Player > Hello",
        ])
        with open(path, 'w', encoding='utf-16-le') as f:
            f.write(content)
        return path

    @patch(PATCH_OVERLAY)
    @patch(PATCH_TAILER)
    def test_update_config_changes_polling_interval(self, MockTailer, MockOW):
        """Changing polling interval should restart the poll timer."""
        from src.core.session import ChatSession
        config = _make_config()

        session = ChatSession('fleet', self.log_path, config)
        session.start()

        # Update with new polling interval
        session.update_config({'polling_interval': 2.5})
        self.assertEqual(session.config['polling_interval'], 2.5)

        session.stop()

    @patch(PATCH_OVERLAY)
    @patch(PATCH_TAILER)
    def test_handle_config_update_emits_signal(self, MockTailer, MockOW):
        """Config changes from overlay should be forwarded via signal."""
        from src.core.session import ChatSession
        config = _make_config()

        session = ChatSession('fleet', self.log_path, config)

        signal_spy = MagicMock()
        session.config_changed.connect(signal_spy)

        # Simulate overlay config change
        session._handle_config_update({'opacity': 0.5})
        signal_spy.assert_called_once()
        args = signal_spy.call_args[0]
        self.assertEqual(args[0], 'fleet')

    @patch(PATCH_OVERLAY)
    @patch(PATCH_TAILER)
    def test_session_uses_config_threshold(self, MockTailer, MockOW):
        """Session should use fleet_inactive_threshold from config, not hardcoded."""
        from src.core.session import ChatSession
        config = _make_config(fleet_history_lines=5, fleet_inactive_threshold=60)

        # Create a log that's 2 minutes old (passes 1800s but fails 60s)
        old_time = time.time() - 120
        os.utime(self.log_path, (old_time, old_time))

        mock_tailer = MagicMock()
        MockTailer.return_value = mock_tailer

        session = ChatSession('fleet', self.log_path, config)
        session.start()

        # With 60s threshold and 120s-old file, history should NOT be loaded
        mock_tailer.read_last_n_lines.assert_not_called()
        session.stop()

    @patch(PATCH_OVERLAY)
    @patch(PATCH_TAILER)
    def test_poll_emits_lines(self, MockTailer, MockOW):
        """Polling should emit lines_ready when new lines exist."""
        from src.core.session import ChatSession
        config = _make_config()

        mock_tailer = MagicMock()
        mock_tailer.read_new_lines.return_value = ["[ 2025.12.16 09:00:00 ] FC > Jump"]
        MockTailer.return_value = mock_tailer

        session = ChatSession('fleet', self.log_path, config)

        signal_spy = MagicMock()
        session.lines_ready.connect(signal_spy)

        session.start()
        session._poll_log()

        signal_spy.assert_called()
        args = signal_spy.call_args[0]
        self.assertEqual(args[0], 'fleet')
        self.assertEqual(len(args[1]), 1)

        session.stop()

    @patch(PATCH_OVERLAY)
    @patch(PATCH_TAILER)
    def test_switch_fleet_log(self, MockTailer, MockOW):
        """Switching fleet log should create new tailer and clear overlay."""
        from src.core.session import ChatSession
        config = _make_config()
        new_log_path = self._create_log("Fleet_new.txt")

        mock_tailer = MagicMock()
        MockTailer.return_value = mock_tailer
        mock_ow = MagicMock()
        MockOW.return_value = mock_ow

        session = ChatSession('fleet', self.log_path, config)
        session.start()

        # Switch to new log
        session.switch_fleet_log(new_log_path, "NewPilot")

        # Verify old tailer was closed and new one created
        mock_tailer.close.assert_called()
        mock_ow.clear_messages.assert_called()
        self.assertEqual(str(session.log_path), new_log_path)

        session.stop()

    @patch(PATCH_OVERLAY)
    @patch(PATCH_TAILER)
    def test_switch_fleet_log_not_running(self, MockTailer, MockOW):
        """Switching log when session is not running should do nothing."""
        from src.core.session import ChatSession
        config = _make_config()

        mock_ow = MagicMock()
        MockOW.return_value = mock_ow

        session = ChatSession('fleet', self.log_path, config)
        # Don't start it
        session.switch_fleet_log("/some/path", "Pilot")
        mock_ow.clear_messages.assert_not_called()

    @patch(PATCH_OVERLAY)
    @patch(PATCH_TAILER)
    def test_add_message_forwards_to_overlay(self, MockTailer, MockOW):
        """add_message should forward to overlay."""
        from src.core.session import ChatSession
        config = _make_config()

        mock_ow = MagicMock()
        MockOW.return_value = mock_ow

        session = ChatSession('fleet', self.log_path, config)
        session.add_message("Hello", "Player", "08:00:00", "", False)
        mock_ow.add_message.assert_called_once_with(
            "Hello", "Player", "08:00:00", "", False
        )

    @patch(PATCH_OVERLAY)
    @patch(PATCH_TAILER)
    def test_get_config_returns_overlay_config(self, MockTailer, MockOW):
        """get_config should return current overlay config."""
        from src.core.session import ChatSession
        config = _make_config()

        mock_ow = MagicMock()
        mock_ow.get_current_config.return_value = {'x': 200, 'y': 300}
        MockOW.return_value = mock_ow

        session = ChatSession('fleet', self.log_path, config)
        result = session.get_config()
        self.assertEqual(result, {'x': 200, 'y': 300})

    @patch(PATCH_OVERLAY)
    @patch(PATCH_TAILER)
    def test_handle_session_toggle_emits(self, MockTailer, MockOW):
        """_handle_session_toggle should forward to request_toggle signal."""
        from src.core.session import ChatSession
        config = _make_config()

        session = ChatSession('fleet', self.log_path, config)

        signal_spy = MagicMock()
        session.request_toggle.connect(signal_spy)

        session._handle_session_toggle('local')
        signal_spy.assert_called_once_with('local')

    @patch(PATCH_OVERLAY)
    @patch(PATCH_TAILER)
    def test_overlay_exit_signal_is_connected(self, MockTailer, MockOW):
        """Overlay exit should be bubbled through the session."""
        from src.core.session import ChatSession
        config = _make_config()

        mock_ow = MagicMock()
        MockOW.return_value = mock_ow

        session = ChatSession('fleet', self.log_path, config)

        mock_ow.exit_requested.connect.assert_called_once_with(session.exit_requested)

    @patch(PATCH_OVERLAY)
    @patch(PATCH_TAILER)
    def test_update_session_states_forwards(self, MockTailer, MockOW):
        """update_session_states should forward to overlay."""
        from src.core.session import ChatSession
        config = _make_config()

        mock_ow = MagicMock()
        MockOW.return_value = mock_ow

        session = ChatSession('fleet', self.log_path, config)
        session.update_session_states({
            'fleet': {'enabled': True},
            'local': {'enabled': False},
        })
        mock_ow.update_session_states.assert_called_once()

    @patch(PATCH_OVERLAY)
    @patch(PATCH_TAILER)
    def test_start_idempotent(self, MockTailer, MockOW):
        """Starting an already-running session should be a no-op."""
        from src.core.session import ChatSession
        config = _make_config()

        session = ChatSession('fleet', self.log_path, config)
        session.start()
        self.assertTrue(session.is_running)

        # Start again — should not crash or reset
        session.start()
        self.assertTrue(session.is_running)

        session.stop()

    @patch(PATCH_OVERLAY)
    @patch(PATCH_TAILER)
    def test_stop_idempotent(self, MockTailer, MockOW):
        """Stopping a non-running session should be a no-op."""
        from src.core.session import ChatSession
        config = _make_config()

        session = ChatSession('fleet', self.log_path, config)
        # Don't start
        session.stop()  # Should not crash
        self.assertFalse(session.is_running)


if __name__ == '__main__':
    unittest.main()

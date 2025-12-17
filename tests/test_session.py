import unittest
from unittest.mock import MagicMock, patch
from src.core.session import ChatSession

class TestChatSession(unittest.TestCase):
    @patch('src.core.tailer.FleetLogTailer')
    @patch('src.gui.overlay.OverlayWindow')
    def test_startup_shutdown(self, mock_overlay_cls, mock_tailer_cls):
        # Mock instances
        mock_overlay = mock_overlay_cls.return_value
        mock_tailer = mock_tailer_cls.return_value

        config = {'x': 0, 'y': 0}
        session = ChatSession('fleet', 'dummy.log', config)
        
        # Test Start
        session.start()
        self.assertTrue(session.is_running)
        mock_tailer.seek_to_end.assert_called_once()
        mock_overlay.show.assert_called_once()
        
        # Test Stop
        session.stop()
        self.assertFalse(session.is_running)
        mock_tailer.close.assert_called_once()
    @patch('src.core.tailer.FleetLogTailer')
    @patch('src.gui.overlay.OverlayWindow')
    def test_session_state_update(self, mock_overlay_cls, mock_tailer):
        # Setup
        mock_overlay = mock_overlay_cls.return_value
        session = ChatSession('fleet', 'dummy.log', {})
        
        # Simulate Manager broadcast (Full Config Dict)
        full_states = {
            'fleet': {'enabled': True, 'x': 100},
            'local': {'enabled': False, 'x': 200}
        }
        
        session.update_session_states(full_states)
        
        # Verify Overlay receives simplified Boolean dict
        expected_simple = {'fleet': True, 'local': False}
        mock_overlay.update_session_states.assert_called_with(expected_simple)

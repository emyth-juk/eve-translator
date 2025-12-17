
import unittest
from unittest.mock import MagicMock, patch, PropertyMock
import time
from datetime import datetime
from pathlib import Path

from src.core.fleet_info import FleetInfo
from src.services.fleet_detector import FleetDetector
from src.core.session import ChatSession

class TestFleetFeatures(unittest.TestCase):

    def setUp(self):
        self.detector = FleetDetector()

    def test_fleet_info_is_active(self):
        """Test active status logic based on mtime."""
        now = time.time()
        
        # Active fleet (10 mins old)
        active_fleet = FleetInfo(
            fleet_id="active",
            listener_name="Char1",
            log_path="/tmp/active.txt",
            log_mtime=now - 600,
            created_time=now - 3600,
            is_active=True
        )
        self.assertTrue(active_fleet.is_active)

    def test_detector_back_to_back_logic(self):
        """
        Simulate the logic used in main.py for back-to-back detection.
        (Logic is effectively: Same Character + Newer Created Time)
        """
        now = time.time()
        
        old_fleet = FleetInfo(
            fleet_id="old_log",
            listener_name="PilotA",
            log_path="/logs/old.txt",
            log_mtime=now - 100,
            created_time=now - 3600
        )
        
        new_fleet = FleetInfo(
            fleet_id="new_log",
            listener_name="PilotA",
            log_path="/logs/new.txt",
            log_mtime=now - 10,
            created_time=now - 300 # Created more recently
        )
        
        # Verify conditions
        self.assertEqual(old_fleet.listener_name, new_fleet.listener_name)
        self.assertGreater(new_fleet.created_time, old_fleet.created_time)

    def test_timestamp_parsing(self):
        """Test regex parsing of fleet log filenames."""
        filename = "Fleet_20251216_082457_1117005149.txt"
        ts = self.detector.parse_timestamp_from_filename(filename)
        self.assertIsNotNone(ts)
        
        dt = datetime.fromtimestamp(ts)
        self.assertEqual(dt.year, 2025)
        self.assertEqual(dt.month, 12)
        self.assertEqual(dt.day, 16)
        self.assertEqual(dt.hour, 8)
        self.assertEqual(dt.minute, 24)
        
    @patch('src.core.tailer.FleetLogTailer')
    @patch('src.gui.overlay.OverlayWindow')
    def test_session_history_limit(self, MockOverlay, MockTailer):
        """Test that session respects fleet_history_lines config."""
        
        # Mock Config with limit 5
        config = {'fleet_history_lines': 5}
        
        # We need to mock the internal imports logic by having the classes mocked BEFORE instantiation
        # unittest.mock.patch does this if we patch where the class is DEFINED.
        
        session = ChatSession('fleet', 'dummy_path.txt', config)
        
        # Mock Tailer behavior
        mock_tailer_instance = MockTailer.return_value
        mock_tailer_instance.read_last_n_lines.return_value = ["Line 1", "Line 2"]
        
        # Mock Path.stat().st_mtime to be FRESH (now)
        with patch('pathlib.Path.stat') as mock_stat:
            mock_stat.return_value.st_mtime = time.time()
            
            session.start()
            
            # Should call read_last_n_lines(5)
            mock_tailer_instance.read_last_n_lines.assert_called_with(5)

    @patch('src.core.tailer.FleetLogTailer')
    @patch('src.gui.overlay.OverlayWindow')
    def test_session_stale_rejection(self, MockOverlay, MockTailer):
        """Test that session ignores history if log is stale (>30m)."""
        
        config = {'fleet_history_lines': 5}
        session = ChatSession('fleet', 'dummy_path.txt', config)
        mock_tailer_instance = MockTailer.return_value
        
        # Mock Path.stat().st_mtime to be STALE (1 hour old)
        with patch('pathlib.Path.stat') as mock_stat:
            mock_stat.return_value.st_mtime = time.time() - 3600 # 1 hour ago
            
            session.start()
            
            # Should NOT call read_last_n_lines
            mock_tailer_instance.read_last_n_lines.assert_not_called()
            # But should still seek active tailing
            mock_tailer_instance.seek_to_end.assert_called()

if __name__ == '__main__':
    unittest.main()

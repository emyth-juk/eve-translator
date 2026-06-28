import os
import tempfile
import shutil
import time
import unittest
from unittest.mock import patch
from pathlib import Path
from datetime import datetime

from src.services.fleet_detector import FleetDetector
from src.core.fleet_info import FleetInfo


class TestFleetDetectorParsing(unittest.TestCase):
    """Tests for FleetDetector parsing methods."""

    def setUp(self):
        self.detector = FleetDetector()
        self.test_dir = tempfile.mkdtemp()

    def tearDown(self):
        time.sleep(0.05)
        shutil.rmtree(self.test_dir, ignore_errors=True)

    def _create_fleet_log(self, filename, listener_name="TestPilot", messages=None):
        """Create a fleet log file with proper header."""
        path = os.path.join(self.test_dir, filename)
        header = [
            "\ufeff",  # BOM
            "Channel ID:      (None)",
            "Channel Name:    Fleet",
            f"Listener:        {listener_name}",
            "Session started: 2025.12.16 08:24:57",
            "",
        ]
        if messages:
            header.extend(messages)
        content = "\r\n".join(header)
        with open(path, 'w', encoding='utf-16-le') as f:
            f.write(content)
        return path

    # --- parse_listener_from_log ---

    def test_parse_listener_from_log_valid(self):
        path = self._create_fleet_log("Fleet_20251216_082457.txt", "Emyth Juk")
        result = self.detector.parse_listener_from_log(path)
        self.assertEqual(result, "Emyth Juk")

    def test_parse_listener_from_log_no_listener(self):
        path = os.path.join(self.test_dir, "Fleet_test.txt")
        with open(path, 'w', encoding='utf-16-le') as f:
            f.write("Channel ID: (None)\r\nChannel Name: Fleet\r\n")
        result = self.detector.parse_listener_from_log(path)
        self.assertIsNone(result)

    def test_parse_listener_from_log_nonexistent(self):
        result = self.detector.parse_listener_from_log("/nonexistent/path.txt")
        self.assertIsNone(result)

    def test_parse_listener_from_log_uses_metadata_cache(self):
        path = self._create_fleet_log("Fleet_20251216_082457.txt", "CachedPilot")

        with patch("builtins.open", wraps=open) as mock_open:
            self.assertEqual(self.detector.parse_listener_from_log(path), "CachedPilot")
            self.assertEqual(self.detector.parse_listener_from_log(path), "CachedPilot")

        self.assertEqual(mock_open.call_count, 1)

    def test_parse_listener_from_log_invalidates_cache_on_change(self):
        path = self._create_fleet_log("Fleet_20251216_082457.txt", "OldPilot")

        with patch("builtins.open", wraps=open) as mock_open:
            self.assertEqual(self.detector.parse_listener_from_log(path), "OldPilot")
            with open(path, 'w', encoding='utf-16-le') as f:
                f.write("Listener:        NewPilotLonger\r\n")
            self.assertEqual(self.detector.parse_listener_from_log(path), "NewPilotLonger")

        self.assertEqual(mock_open.call_count, 3)

    # --- parse_timestamp_from_filename ---

    def test_parse_timestamp_standard_filename(self):
        ts = self.detector.parse_timestamp_from_filename("Fleet_20251216_082457.txt")
        self.assertIsNotNone(ts)
        dt = datetime.fromtimestamp(ts)
        self.assertEqual(dt.year, 2025)
        self.assertEqual(dt.month, 12)
        self.assertEqual(dt.day, 16)
        self.assertEqual(dt.hour, 8)
        self.assertEqual(dt.minute, 24)

    def test_parse_timestamp_with_char_id(self):
        ts = self.detector.parse_timestamp_from_filename("Fleet_20251216_082457_1117005149.txt")
        self.assertIsNotNone(ts)

    def test_parse_timestamp_invalid_filename(self):
        self.assertIsNone(self.detector.parse_timestamp_from_filename("Local_20251216.txt"))
        self.assertIsNone(self.detector.parse_timestamp_from_filename("Fleet_invalid.txt"))
        self.assertIsNone(self.detector.parse_timestamp_from_filename("random.txt"))


class TestFleetDetectorScanning(unittest.TestCase):
    """Tests for scan_active_fleets and get_most_recent_fleet."""

    def setUp(self):
        self.detector = FleetDetector()
        self.test_dir = tempfile.mkdtemp()

    def tearDown(self):
        time.sleep(0.05)
        shutil.rmtree(self.test_dir, ignore_errors=True)

    def _create_fleet_log(self, filename, listener_name="TestPilot", touch_time=None):
        """Create a fleet log file and optionally set its mtime."""
        path = os.path.join(self.test_dir, filename)
        header = [
            "\ufeff",
            "Channel ID:      (None)",
            "Channel Name:    Fleet",
            f"Listener:        {listener_name}",
            "Session started: 2025.12.16 08:24:57",
            "",
            "[ 2025.12.16 08:25:00 ] Player > Hello",
        ]
        content = "\r\n".join(header)
        with open(path, 'w', encoding='utf-16-le') as f:
            f.write(content)

        if touch_time is not None:
            os.utime(path, (touch_time, touch_time))
        return path

    # --- scan_active_fleets ---

    def test_scan_finds_active_fleets(self):
        self._create_fleet_log("Fleet_20251216_082457.txt", "Pilot1")
        self._create_fleet_log("Fleet_20251216_092457.txt", "Pilot2")

        result = self.detector.scan_active_fleets(self.test_dir)
        self.assertEqual(len(result), 2)

        listener_names = {f.listener_name for f in result.values()}
        self.assertIn("Pilot1", listener_names)
        self.assertIn("Pilot2", listener_names)

    def test_scan_excludes_inactive_fleets(self):
        # Create an old file (modified 2 hours ago)
        old_time = time.time() - 7200
        self._create_fleet_log("Fleet_20251216_062457.txt", "OldPilot", touch_time=old_time)
        # Create a recent file
        self._create_fleet_log("Fleet_20251216_082457.txt", "ActivePilot")

        result = self.detector.scan_active_fleets(self.test_dir)
        self.assertEqual(len(result), 1)
        fleet = list(result.values())[0]
        self.assertEqual(fleet.listener_name, "ActivePilot")

    def test_scan_empty_directory(self):
        result = self.detector.scan_active_fleets(self.test_dir)
        self.assertEqual(len(result), 0)

    def test_scan_nonexistent_directory(self):
        result = self.detector.scan_active_fleets("/nonexistent/path")
        self.assertEqual(len(result), 0)

    def test_scan_ignores_non_fleet_files(self):
        # Create a Local_ file (should be ignored)
        path = os.path.join(self.test_dir, "Local_20251216_082457_12345.txt")
        with open(path, 'w', encoding='utf-16-le') as f:
            f.write("test")
        # Create a fleet log
        self._create_fleet_log("Fleet_20251216_082457.txt", "Pilot1")

        result = self.detector.scan_active_fleets(self.test_dir)
        self.assertEqual(len(result), 1)

    def test_scan_skips_logs_without_listener(self):
        """Fleet logs where listener can't be parsed should be skipped."""
        path = os.path.join(self.test_dir, "Fleet_20251216_082457.txt")
        with open(path, 'w', encoding='utf-16-le') as f:
            f.write("No header here\r\n")

        result = self.detector.scan_active_fleets(self.test_dir)
        self.assertEqual(len(result), 0)

    def test_scan_custom_threshold(self):
        """Custom threshold should change what's considered active."""
        # File modified 10 minutes ago
        ten_min_ago = time.time() - 600
        self._create_fleet_log("Fleet_20251216_082457.txt", "Pilot", touch_time=ten_min_ago)

        # Default threshold (1800s = 30min) should include it
        result = self.detector.scan_active_fleets(self.test_dir, active_threshold_seconds=1800)
        self.assertEqual(len(result), 1)

        # Tight threshold (60s) should exclude it
        result = self.detector.scan_active_fleets(self.test_dir, active_threshold_seconds=60)
        self.assertEqual(len(result), 0)

    def test_scan_fleet_info_fields(self):
        """Verify all FleetInfo fields are populated correctly."""
        self._create_fleet_log("Fleet_20251216_082457.txt", "TestPilot")

        result = self.detector.scan_active_fleets(self.test_dir)
        self.assertEqual(len(result), 1)

        fleet = list(result.values())[0]
        self.assertEqual(fleet.listener_name, "TestPilot")
        self.assertTrue(fleet.is_active)
        self.assertIsNotNone(fleet.created_time)
        self.assertIsNotNone(fleet.log_mtime)
        self.assertTrue(fleet.log_path.endswith(".txt"))

    # --- get_most_recent_fleet ---

    def test_get_most_recent_fleet(self):
        fleets = {
            "fleet1": FleetInfo("fleet1", "OldPilot", "/path1", 1000, 100.0, True),
            "fleet2": FleetInfo("fleet2", "NewPilot", "/path2", 2000, 200.0, True),
            "fleet3": FleetInfo("fleet3", "MidPilot", "/path3", 1500, 150.0, True),
        }
        result = self.detector.get_most_recent_fleet(fleets)
        self.assertEqual(result.listener_name, "NewPilot")

    def test_get_most_recent_fleet_empty(self):
        result = self.detector.get_most_recent_fleet({})
        self.assertIsNone(result)

    def test_get_most_recent_fleet_single(self):
        fleets = {
            "fleet1": FleetInfo("fleet1", "SoloPilot", "/path1", 1000, 100.0, True),
        }
        result = self.detector.get_most_recent_fleet(fleets)
        self.assertEqual(result.listener_name, "SoloPilot")


if __name__ == '__main__':
    unittest.main()

import os
import tempfile
import shutil
import time
import unittest
from unittest.mock import patch
from datetime import datetime, timezone

from src.services.fleet_detector import FleetDetector
from src.core.fleet_info import FleetInfo
from src.services.log_discovery import UTF16HeaderCache, parse_eve_log_filename


class TestSharedLogDiscoveryParsing(unittest.TestCase):
    """Tests for the shared header and filename parsers used by Fleet."""

    def setUp(self):
        self.header_cache = UTF16HeaderCache()
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

    # --- UTF16HeaderCache ---

    def test_header_cache_parses_listener(self):
        path = self._create_fleet_log("Fleet_20251216_082457.txt", "Emyth Juk")
        header = self.header_cache.read(path)
        self.assertIsNotNone(header)
        self.assertEqual(header.listener_name, "Emyth Juk")

    def test_header_cache_preserves_empty_listener(self):
        path = os.path.join(self.test_dir, "Fleet_test.txt")
        with open(path, 'w', encoding='utf-16-le') as f:
            f.write("Channel ID: (None)\r\nChannel Name: Fleet\r\n")
        header = self.header_cache.read(path)
        self.assertIsNotNone(header)
        self.assertEqual(header.listener_name, "")

    def test_header_cache_returns_none_for_nonexistent_file(self):
        self.assertIsNone(self.header_cache.read("/nonexistent/path.txt"))

    def test_header_cache_uses_metadata_version(self):
        path = self._create_fleet_log("Fleet_20251216_082457.txt", "CachedPilot")

        with patch("builtins.open", wraps=open) as mock_open:
            first = self.header_cache.read(path)
            second = self.header_cache.read(path)

        self.assertEqual(first.listener_name, "CachedPilot")
        self.assertEqual(second.listener_name, "CachedPilot")
        self.assertEqual(mock_open.call_count, 1)

    def test_header_cache_invalidates_on_change(self):
        path = self._create_fleet_log("Fleet_20251216_082457.txt", "OldPilot")

        with patch("builtins.open", wraps=open) as mock_open:
            old_header = self.header_cache.read(path)
            with open(path, 'w', encoding='utf-16-le') as f:
                f.write("Listener:        NewPilotLonger\r\n")
            new_header = self.header_cache.read(path)

        self.assertEqual(old_header.listener_name, "OldPilot")
        self.assertEqual(new_header.listener_name, "NewPilotLonger")
        self.assertEqual(mock_open.call_count, 3)

    # --- parse_eve_log_filename ---

    def test_filename_parser_reads_fleet_timestamp(self):
        parsed = parse_eve_log_filename("Fleet_20251216_082457.txt")
        self.assertIsNotNone(parsed)
        self.assertEqual(parsed.channel_name, "Fleet")
        dt = datetime.fromtimestamp(parsed.started_time, timezone.utc)
        self.assertEqual(dt.year, 2025)
        self.assertEqual(dt.month, 12)
        self.assertEqual(dt.day, 16)
        self.assertEqual(dt.hour, 8)
        self.assertEqual(dt.minute, 24)

    def test_filename_parser_reads_character_id(self):
        parsed = parse_eve_log_filename(
            "Fleet_20251216_082457_1117005149.txt"
        )
        self.assertIsNotNone(parsed)
        self.assertEqual(parsed.character_id, "1117005149")

    def test_filename_parser_is_channel_agnostic_and_rejects_invalid_names(self):
        local = parse_eve_log_filename("Local_20251216_082457.txt")
        self.assertIsNotNone(local)
        self.assertEqual(local.channel_name, "Local")
        self.assertIsNone(parse_eve_log_filename("Fleet_invalid.txt"))
        self.assertIsNone(parse_eve_log_filename("random.txt"))


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
        with self.assertRaises(FileNotFoundError):
            self.detector.scan_active_fleets("/nonexistent/path")

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

    def test_scan_allows_small_clock_skew_but_rejects_far_future_mtime(self):
        now = time.time()
        self._create_fleet_log(
            "Fleet_20251216_082457.txt",
            "SmallSkew",
            touch_time=now + 1.5,
        )
        self._create_fleet_log(
            "Fleet_20251216_092457.txt",
            "FarFuture",
            touch_time=now + 3600,
        )

        with patch("src.services.fleet_detector.time.time", return_value=now):
            result = self.detector.scan_active_fleets(self.test_dir)

        self.assertEqual(
            {fleet.listener_name for fleet in result.values()},
            {"SmallSkew"},
        )

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
        result = FleetDetector.get_most_recent_fleet(fleets)
        self.assertEqual(result.listener_name, "NewPilot")

    def test_get_most_recent_fleet_empty(self):
        result = FleetDetector.get_most_recent_fleet({})
        self.assertIsNone(result)

    def test_get_most_recent_fleet_single(self):
        fleets = {
            "fleet1": FleetInfo("fleet1", "SoloPilot", "/path1", 1000, 100.0, True),
        }
        result = FleetDetector.get_most_recent_fleet(fleets)
        self.assertEqual(result.listener_name, "SoloPilot")


if __name__ == '__main__':
    unittest.main()

import os
import sys
import tempfile
import shutil
import time
import unittest
from unittest.mock import patch

from src.services.local_detector import LocalChatDetector


class TestLocalChatDetectorExtended(unittest.TestCase):
    """Extended tests for LocalChatDetector covering edge cases and more methods."""

    def setUp(self):
        self.detector = LocalChatDetector()
        self.test_dir = tempfile.mkdtemp()

    def tearDown(self):
        time.sleep(0.05)
        shutil.rmtree(self.test_dir, ignore_errors=True)

    def _create_local_log(self, char_id, char_name="TestPilot", system_name=None, touch_time=None):
        """Create a Local chat log file."""
        filename = f"Local_20251201_095136_{char_id}.txt"
        path = os.path.join(self.test_dir, filename)
        lines = [
            "\ufeff",
            "Channel ID:      (None)",
            "Channel Name:    Local",
            f"Listener:        {char_name}",
            "Session started: 2025.12.01 09:51:36",
            "",
        ]
        if system_name:
            lines.append(f"[ 2025.12.01 09:51:40 ] EVE System > Channel changed to Local : {system_name}")
        lines.append(f"[ 2025.12.01 09:52:00 ] {char_name} > Hello local")

        content = "\r\n".join(lines)
        with open(path, 'w', encoding='utf-16-le') as f:
            f.write(content)

        if touch_time is not None:
            os.utime(path, (touch_time, touch_time))
        return path

    # --- is_character_window_open ---

    def test_window_check_empty_name(self):
        """Empty character name should return False."""
        self.assertFalse(self.detector.is_character_window_open(""))
        self.assertFalse(self.detector.is_character_window_open(None))

    @unittest.skipUnless(sys.platform == 'win32', "Windows-only test")
    def test_window_check_nonexistent_window(self):
        """Non-existent window should return False."""
        self.assertFalse(self.detector.is_character_window_open("NonExistentCharacter_12345"))

    @unittest.skipIf(sys.platform == 'win32', "Non-Windows platform guard test")
    def test_window_check_non_windows(self):
        """On non-Windows platforms, should return False."""
        self.assertFalse(self.detector.is_character_window_open("AnyName"))

    # --- extract_system_name ---

    def test_extract_system_name(self):
        path = self._create_local_log("111", "Pilot", system_name="Jita")
        result = self.detector.extract_system_name(path)
        self.assertEqual(result, "Jita")

    def test_extract_system_name_none(self):
        path = self._create_local_log("111", "Pilot", system_name=None)
        result = self.detector.extract_system_name(path)
        self.assertIsNone(result)

    def test_extract_system_name_multiple_changes(self):
        """Should return the LAST system, not the first."""
        path = os.path.join(self.test_dir, "Local_20251201_095136_111.txt")
        lines = [
            "\ufeff",
            "Channel ID:      (None)",
            "Channel Name:    Local",
            "Listener:        Pilot",
            "",
            "[ 2025.12.01 09:51:40 ] EVE System > Channel changed to Local : Jita",
            "[ 2025.12.01 09:55:00 ] EVE System > Channel changed to Local : Perimeter",
            "[ 2025.12.01 10:00:00 ] EVE System > Channel changed to Local : Amarr",
        ]
        content = "\r\n".join(lines)
        with open(path, 'w', encoding='utf-16-le') as f:
            f.write(content)

        result = self.detector.extract_system_name(path)
        self.assertEqual(result, "Amarr")

    def test_extract_system_name_invalidates_cache_on_change(self):
        path = self._create_local_log("111", "Pilot", system_name="Jita")

        with patch("builtins.open", wraps=open) as mock_open:
            self.assertEqual(self.detector.extract_system_name(path), "Jita")
            with open(path, 'a', encoding='utf-16-le') as f:
                f.write("\r\n[ 2025.12.01 10:00:00 ] EVE System > Channel changed to Local : Amarr")
            self.assertEqual(self.detector.extract_system_name(path), "Amarr")

        self.assertEqual(mock_open.call_count, 3)

    def test_extract_system_name_reads_only_append_and_reuses_one_cache_entry(self):
        path = self._create_local_log("111", "Pilot", system_name="Jita")
        self.assertEqual(self.detector.extract_system_name(path), "Jita")
        initial_state = next(iter(self.detector._system_cache.values()))
        initial_offset = initial_state.offset

        with open(path, 'ab') as log_file:
            log_file.write(
                (
                    "\r\n[ 2025.12.01 10:00:00 ] EVE System > "
                    "Channel changed to Local : Amarr"
                ).encode('utf-16-le')
            )

        actual_open = open
        seek_offsets = []

        class TrackingReader:
            def __init__(self, wrapped):
                self._wrapped = wrapped

            def __enter__(self):
                self._wrapped.__enter__()
                return self

            def __exit__(self, *args):
                return self._wrapped.__exit__(*args)

            def seek(self, offset, *args):
                seek_offsets.append(offset)
                return self._wrapped.seek(offset, *args)

            def read(self, *args):
                return self._wrapped.read(*args)

        def tracked_open(*args, **kwargs):
            return TrackingReader(actual_open(*args, **kwargs))

        with patch("builtins.open", side_effect=tracked_open):
            self.assertEqual(self.detector.extract_system_name(path), "Amarr")

        self.assertEqual(seek_offsets, [initial_offset])
        self.assertEqual(len(self.detector._system_cache), 1)
        updated_state = next(iter(self.detector._system_cache.values()))
        self.assertGreater(updated_state.offset, initial_offset)

    # --- scan_active_characters ---

    def test_scan_active_characters(self):
        self._create_local_log("111", "Pilot1", "Jita")
        self._create_local_log("222", "Pilot2", "Amarr")

        result = self.detector.scan_active_characters(self.test_dir)
        self.assertEqual(len(result), 2)
        self.assertIn("111", result)
        self.assertIn("222", result)
        self.assertEqual(result["111"].character_name, "Pilot1")
        self.assertEqual(result["222"].system_name, "Amarr")

    def test_scan_active_characters_empty_dir(self):
        result = self.detector.scan_active_characters(self.test_dir)
        self.assertEqual(len(result), 0)

    def test_scan_active_characters_nonexistent_dir(self):
        with self.assertRaises(FileNotFoundError):
            self.detector.scan_active_characters("/nonexistent/path")

    def test_scan_fallback_character_name(self):
        """If listener can't be parsed, should fallback to Character_<id>."""
        path = os.path.join(self.test_dir, "Local_20251201_095136_999.txt")
        with open(path, 'w', encoding='utf-16-le') as f:
            f.write("No listener line\r\n")

        result = self.detector.scan_active_characters(self.test_dir)
        if "999" in result:
            self.assertTrue(result["999"].character_name.startswith("Character_"))

    # --- parse_character_id_from_filename ---

    def test_parse_valid_filenames(self):
        cases = [
            ("Local_20251201_095136_1117005149.txt", "1117005149"),
            ("Local_20250101_000000_999.txt", "999"),
        ]
        for filename, expected in cases:
            with self.subTest(filename=filename):
                self.assertEqual(self.detector.parse_character_id_from_filename(filename), expected)

    def test_parse_invalid_filenames(self):
        cases = [
            "Fleet_20251216_082457.txt",
            "Local_nodate.txt",
            "random.txt",
            "",
        ]
        for filename in cases:
            with self.subTest(filename=filename):
                self.assertIsNone(self.detector.parse_character_id_from_filename(filename))


if __name__ == '__main__':
    unittest.main()

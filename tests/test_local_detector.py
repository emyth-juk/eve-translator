import unittest
from unittest.mock import patch
import time
from src.services.local_detector import LocalChatDetector
from src.services.log_discovery import (
    EVELogEntry,
    EVELogHeader,
    parse_eve_log_filename,
)

class TestLocalChatDetector(unittest.TestCase):
    def setUp(self):
        self.detector = LocalChatDetector()

    def test_parse_character_id(self):
        filename = "Local_20251201_095136_1117005149.txt"
        char_id = self.detector.parse_character_id_from_filename(filename)
        self.assertEqual(char_id, "1117005149")

        filename_no_id = "Local_20251201_095136.txt"
        self.assertIsNone(self.detector.parse_character_id_from_filename(filename_no_id))

    @patch('builtins.open', new_callable=unittest.mock.mock_open, read_data="Header\n"*15 + 
           "[ 10:00:00 ] EVE System > Channel changed to Local : Jita\n" +
           "Chat line...\n"*50 +
           "[ 11:00:00 ] EVE System > Channel changed to Local : Amarr\n")
    def test_extract_system_name(self, mock_file):
        # Should pick the LAST one ("Amarr")
        system_name = self.detector.extract_system_name("dummy_path")
        self.assertEqual(system_name, "Amarr")

    # test_scan_active_characters removed as it was incomplete and covered by _robust version.
        
    @patch('src.services.local_detector.LocalChatDetector._extract_system_name')
    def test_scan_active_characters_robust(self, mock_extract_system):
        # Scenario: 
        # Char123: Latest log (Old MTime, No Name) -> Older log (Old MTime, Name="CharC") -> Window Open ("EVE - CharC") -> Active?
        
        # Latest Log (Old, Missing Name)
        latest_name = "Local_20250101_100000_123.txt"
        entry1 = EVELogEntry(
            name=latest_name,
            path="/logs/Local_123_Latest.txt",
            size=100,
            mtime=time.time() - 3600,
            mtime_ns=100,
            ctime=0,
            filename=parse_eve_log_filename(latest_name),
        )
        
        # Older Log (Old, Has Name)
        older_name = "Local_20250101_090000_123.txt"
        entry2 = EVELogEntry(
            name=older_name,
            path="/logs/Local_123_Older.txt",
            size=100,
            mtime=time.time() - 7200,
            mtime_ns=200,
            ctime=0,
            filename=parse_eve_log_filename(older_name),
        )

        def header_side_effect(path, _size, _mtime_ns):
            if "Latest" in path: return None
            if "Older" in path: return EVELogHeader(listener_name="CharC")
            return None
        mock_extract_system.return_value = "Jita"

        # Mock Window detection to succeed only for "CharC".
        with (
            patch.object(
                self.detector._header_cache,
                'read',
                side_effect=header_side_effect,
            ),
            patch.object(
                self.detector,
                'is_character_window_open',
                side_effect=lambda name: name == "CharC",
            ),
        ):
            registry = self.detector.scan_active_characters(
                "/logs",
                entries=(entry1, entry2),
            )
            
            self.assertIn("123", registry)
            char_info = registry["123"]
            
            # Verify name resolved from older log
            self.assertEqual(char_info.character_name, "CharC")
            self.assertEqual(char_info.latest_log_path, entry1.path)
            
            # Verify active because window found for "CharC"
            self.assertTrue(char_info.is_active)

    def test_scan_allows_small_clock_skew_but_rejects_far_future_mtime(self):
        now = 1_750_000_000.0

        def make_entry(char_id, mtime):
            name = f"Local_20250101_100000_{char_id}.txt"
            return EVELogEntry(
                name=name,
                path=f"/logs/{name}",
                size=100,
                mtime=mtime,
                mtime_ns=int(mtime * 1_000_000_000),
                ctime=0,
                filename=parse_eve_log_filename(name),
            )

        entries = (
            make_entry("1", now + 1.5),
            make_entry("2", now + 3600),
        )
        with (
            patch.object(
                self.detector._header_cache,
                "read",
                return_value=EVELogHeader(listener_name="Pilot"),
            ),
            patch.object(
                self.detector,
                "_extract_system_name",
                return_value=None,
            ),
            patch.object(
                self.detector,
                "is_character_window_open",
                return_value=False,
            ),
            patch("src.services.local_detector.time.time", return_value=now),
        ):
            result = self.detector.scan_active_characters(
                "/logs",
                entries=entries,
            )

        self.assertTrue(result["1"].is_active)
        self.assertFalse(result["2"].is_active)

if __name__ == '__main__':
    unittest.main()

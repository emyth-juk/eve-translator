import unittest
from unittest.mock import MagicMock, patch
import os
import time
from src.services.local_detector import LocalChatDetector

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
        
    @patch('src.services.local_detector.os.scandir')
    @patch('src.services.local_detector.LocalChatDetector.get_character_from_log')
    @patch('src.services.local_detector.LocalChatDetector.extract_system_name')
    def test_scan_active_characters_robust(self, mock_extract_system, mock_get_char, mock_scandir):
        # Scenario: 
        # Char123: Latest log (Old MTime, No Name) -> Older log (Old MTime, Name="CharC") -> Window Open ("EVE - CharC") -> Active?
        
        # Latest Log (Old, Missing Name)
        entry1 = MagicMock()
        entry1.is_file.return_value = True
        entry1.name = "Local_20250101_100000_123.txt"
        entry1.path = "/logs/Local_123_Latest.txt"
        entry1.stat.return_value.st_mtime = time.time() - 3600 # 1 hour ago
        
        # Older Log (Old, Has Name)
        entry2 = MagicMock()
        entry2.is_file.return_value = True
        entry2.name = "Local_20250101_090000_123.txt"
        entry2.path = "/logs/Local_123_Older.txt"
        entry2.stat.return_value.st_mtime = time.time() - 7200
        
        mock_scandir.return_value.__enter__.return_value = [entry1, entry2]
        
        def get_char_side_effect(path):
            if "Latest" in path: return None
            if "Older" in path: return "CharC"
            return None
        mock_get_char.side_effect = get_char_side_effect
        mock_extract_system.return_value = "Jita"
        
        # Mock Window detection to succeed only for "CharC"
        with patch.object(self.detector, 'is_character_window_open', side_effect=lambda name: name == "CharC"):
            registry = self.detector.scan_active_characters("/logs")
            
            self.assertIn("123", registry)
            char_info = registry["123"]
            
            # Verify name resolved from older log
            self.assertEqual(char_info.character_name, "CharC")
            
            # Verify active because window found for "CharC"
            self.assertTrue(char_info.is_active)

    @patch('src.services.local_detector.os.scandir')
    def test_get_latest_log_for_character(self, mock_scandir):
        entry1 = MagicMock()
        entry1.is_file.return_value = True
        entry1.name = "Local_20250101_100000_123.txt"
        entry1.path = "/logs/old.txt"
        entry1.stat.return_value.st_mtime = 1000

        entry2 = MagicMock()
        entry2.is_file.return_value = True
        entry2.name = "Local_20250101_110000_123.txt"
        entry2.path = "/logs/new.txt"
        entry2.stat.return_value.st_mtime = 2000

        mock_scandir.return_value.__enter__.return_value = [entry1, entry2]
        
        path = self.detector.get_latest_log_for_character("/logs", "123")
        self.assertEqual(path, "/logs/new.txt")

if __name__ == '__main__':
    unittest.main()

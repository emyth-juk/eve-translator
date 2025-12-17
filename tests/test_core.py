import os
import pytest
from datetime import datetime
from src.core.parser import LineParser, ChatMessage
from src.core.tokenizer import EVELinkTokenizer
from src.core.tailer import FleetLogTailer
from src.core.detector import LanguageDetector

# --- Parser Tests ---
import unittest

class TestCoreComponents(unittest.TestCase):

    def test_parser_valid_message(self):
        parser = LineParser()
        line = "[ 2025.12.16 08:38:43 ] Eric Atlantis > Hello World"
        msg = parser.parse(line, 1)
        
        self.assertIsNotNone(msg)
        self.assertEqual(msg.timestamp_str, "2025.12.16 08:38:43")
        self.assertEqual(msg.sender, "Eric Atlantis")
        self.assertEqual(msg.message, "Hello World")
        self.assertFalse(msg.is_system)

    def test_parser_system_message(self):
        parser = LineParser()
        line = "[ 2025.12.16 08:38:43 ] EVE System > Connection Lost"
        msg = parser.parse(line, 2)
        
        self.assertIsNotNone(msg)
        self.assertEqual(msg.sender, "EVE System")
        self.assertTrue(msg.is_system)

    def test_parser_invalid_lines(self):
        parser = LineParser()
        self.assertIsNone(parser.parse("", 1))
        self.assertIsNone(parser.parse("Not a timestamp", 1))
        self.assertIsNone(parser.parse("----------------", 1))

    # --- Tokenizer Tests ---

    def test_tokenizer_simple(self):
        tokenizer = EVELinkTokenizer()
        original = "Click \x1Ahere\x1A for info"
        tokenized = tokenizer.tokenize(original)
        
        self.assertEqual(tokenized.original, original)
        self.assertIn("__EVELINK_1__", tokenized.cleaned)
        self.assertIn("__EVELINK_2__", tokenized.cleaned)
        self.assertEqual(tokenized.cleaned, "Click __EVELINK_1__here__EVELINK_2__ for info")
        
        restored = tokenizer.restore(tokenized.cleaned, tokenized.tokens)
        self.assertEqual(restored, original)

    def test_tokenizer_complex_link(self):
        tokenizer = EVELinkTokenizer()
        complex_token = "\u000eT\x1A\x03"
        original = f"Doctrine/{complex_token} *WC CFI"
        
        tokenized = tokenizer.tokenize(original)
        self.assertEqual(tokenized.cleaned, "Doctrine/__EVELINK_1__ *WC CFI")
        self.assertEqual(tokenized.tokens["__EVELINK_1__"], complex_token)
        
        restored = tokenizer.restore(tokenized.cleaned, tokenized.tokens)
        self.assertEqual(restored, original)

    # --- Tailer Tests ---

    def test_tailer_read(self):
        # Using tempfile module or just a local dummy? 
        # Unittest doesn't have tmp_path fixture easily without subclassing or setup.
        # I'll use a local file and clean it up.
        import tempfile
        import shutil
        
        tmp_dir = tempfile.mkdtemp()
        try:
            log_file = os.path.join(tmp_dir, "Fleet_Test.txt")
            
            content = "[ 2025.01.01 12:00:00 ] User > Msg 1\n"
            with open(log_file, 'w', encoding='utf-16-le') as f:
                f.write(content)
            
            tailer = FleetLogTailer(str(log_file))
            tailer.seek_to_end()
            
            self.assertEqual(tailer.read_new_lines(), [])
            
            new_content = "[ 2025.01.01 12:00:01 ] User > Msg 2\n"
            with open(log_file, "a", encoding="utf-16-le") as f:
                f.write(new_content)
                
            lines = tailer.read_new_lines()
            self.assertEqual(len(lines), 1)
            self.assertIn("Msg 2", lines[0])
            
            tailer.close()
        finally:
            shutil.rmtree(tmp_dir)

    # --- Language Detector Tests ---

    def test_detector_force_ignore(self):
        detector = LanguageDetector()
        text = "HyperNet offer: Avatar 390m"
        should, _ = detector.should_translate(text, ignored_langs={'en'})
        self.assertFalse(should)

    def test_detector_force_ignore_contracts(self):
        detector = LanguageDetector()
        text = "WTS Avatar 390m contract"
        should, _ = detector.should_translate(text, ignored_langs={'en'})
        self.assertFalse(should)

    def test_detector_force_ignore_blueprint(self):
        detector = LanguageDetector()
        text = "Monitor Blueprint"
        should, _ = detector.should_translate(text, ignored_langs={'en'})
        self.assertFalse(should)

    def test_detector_force_ignore_skill(self):
        detector = LanguageDetector()
        text = "Skill Extractor x 92"
        should, _ = detector.should_translate(text, ignored_langs={'en'})
        self.assertFalse(should)

    def test_detector_normal(self):
        detector = LanguageDetector()
        text = "Hola Mundo"
        should, lang = detector.should_translate(text, ignored_langs={'en'})
        self.assertTrue(should)
        # detected lang might differ ('es' or similar)

    def test_detector_eve_chinese_cases(self):
        """
        Verify robust detection for common EVE Chinese slang/terms.
        """
        cases = [
            ("1.2b收", "zh"),          # Market: Buy 1.2b (Mixed Num/Char)
            ("出大航", "zh"),          # Market: Sell Supercarrier
            ("抓到了", "zh"),          # Combat: Tackled / Caught
            ("9命", "zh"),            # Slang: Help! (Jiu Ming -> 9 Ming)
            ("Jita收", "zh"),         # Mixed: Buying in Jita
            ("49m收", "zh"),          # The original bug report
            ("呜呜呜", "zh"),          # Crying (Strings of repeating chars)
            ("跳跳跳", "zh"),          # Combat: Jump Jump Jump
            ("本地人", "zh"),          # Intel: Locals / Local chat
            ("刷怪", "zh"),            # PvE: Ratting / Farming
        ]
        
        detector = LanguageDetector()
        for text, expected_lang in cases:
            with self.subTest(text=text):
                should, lang = detector.should_translate(text, ignored_langs={'en'})
                self.assertTrue(should, f"Should translate: {text}")
                if expected_lang == 'zh':
                    self.assertTrue(lang.startswith('zh'), f"Expected zh*, got {lang}")
                else:
                    self.assertEqual(lang, expected_lang, f"Lang detection incorrect for: {text}")

    def test_detector_ignore_abyssal_items(self):
        """Test ignore list for common item spam."""
        detector = LanguageDetector()
        text = "Capital Abyssal Armor Repairer"
        should, _ = detector.should_translate(text, ignored_langs={'en'})
        self.assertFalse(should, "Should ignore English Item Names")

    def test_detector_ignore_internet_slang(self):
        """Test ignore list for common internet slang."""
        cases = [
            "LOL", "lol", "loool",
            "LMAO", "lmao",
            "ROFL",
            "WTF",
            "AFK",
            "BRB",
            "o7", "o777"
        ]
        detector = LanguageDetector()
        for text in cases:
            with self.subTest(text=text):
                should, _ = detector.should_translate(text, ignored_langs={'en'})
                self.assertFalse(should, f"Should ignore slang: {text}")

    def test_detector_ignored_slang(self):
        """Verify that pure number slang is usually IGNORED (as it's universal)."""
        cases = [
            "666",   # Awesome (Universal/Number)
            "111",   # Roger (Universal)
            "++++",  # Plus (Universal)
        ]
        detector = LanguageDetector()
        for text in cases:
            should, _ = detector.should_translate(text, ignored_langs={'en'})
            self.assertFalse(should, f"Should NOT translate pure symbols/numbers: {text}")

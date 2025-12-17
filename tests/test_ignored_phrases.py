import unittest
import os
import shutil
import tempfile
import yaml
from unittest.mock import patch, MagicMock
from pathlib import Path
from src.core.detector import LanguageDetector

class TestIgnoredPhrases(unittest.TestCase):
    def setUp(self):
        # Create a temporary directory for mocking file operations
        self.test_dir = tempfile.mkdtemp()
        self.glossary_dir = Path(self.test_dir) / "data" / "glossaries"
        self.glossary_dir.mkdir(parents=True, exist_ok=True)
        
        # Define mock file path
        self.config_path = self.glossary_dir / "ignored_phrases.yml"

    def tearDown(self):
        shutil.rmtree(self.test_dir)

    def test_load_fallback(self):
        """Test usage of hardcoded fallback when file missing."""
        # Patch get_resource_path to point to non-existent file
        with patch('src.core.detector.get_resource_path', return_value=str(self.config_path)):
             detector = LanguageDetector()
             
             # Fallback WTS should be there
             self.assertIn(r'WTS', detector.force_ignore_patterns) 
             # Fallback 'gf' should be there
             self.assertTrue(any('gf' in p for p in detector.internet_slang_patterns))

    def test_load_yaml(self):
        """Test loading patterns from YAML."""
        # Create mock YAML
        data = {
            'force_ignore': ['MyCustomIgnoredWord', 'SpamTerm'],
            'slang': ['^kek$', '^pog$']
        }
        with open(self.config_path, 'w', encoding='utf-8') as f:
            yaml.dump(data, f)
            
        with patch('src.core.detector.get_resource_path', return_value=str(self.config_path)):
            detector = LanguageDetector()
            
            # Check custom patterns
            self.assertIn('MyCustomIgnoredWord', detector.force_ignore_patterns)
            self.assertIn('^kek$', detector.internet_slang_patterns)
            
            # Check default fallback is NOT present (unless merged? Code says overwrite)
            self.assertNotIn('WTS', detector.force_ignore_patterns)

    def test_matching_logic(self):
        """Test that loaded patterns actually block translation."""
        # Create mock YAML with specific patterns
        data = {
            'force_ignore': ['DoNotTranslateMe'],
            'slang': ['^o/$'] # Salute
        }
        with open(self.config_path, 'w', encoding='utf-8') as f:
            yaml.dump(data, f)
            
        with patch('src.core.detector.get_resource_path', return_value=str(self.config_path)):
            detector = LanguageDetector()
            
            # 1. Ignored Keyword
            should, lang = detector.should_translate("Please DoNotTranslateMe thanks", 'en')
            self.assertFalse(should)
            self.assertEqual(lang, 'ignored_keyword')
            
            # 2. Ignored Slang (Regex)
            should, lang = detector.should_translate("o/", 'en')
            self.assertFalse(should)
            self.assertEqual(lang, 'ignored_keyword')
            
            # 3. Normal text
            text = "The quick brown fox jumps over the lazy dog."
            should, lang = detector.should_translate(text, ignored_langs=set())
            self.assertEqual((should, lang), (True, 'en'))

if __name__ == '__main__':
    unittest.main()

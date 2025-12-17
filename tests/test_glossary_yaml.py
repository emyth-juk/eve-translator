
import unittest
import os
import shutil
import tempfile
from pathlib import Path
from src.core.glossary import EVEGlossary

# Mock get_resource_path for testing
import sys
if not hasattr(sys, '_MEIPASS'):
    # We are in dev mode, so get_resource_path in glossary.py uses relative path from glossary.py
    # But for tests, we might want to ensure it points to real data or our temp data.
    # The EVEGlossary implementation uses src.main.get_resource_path or a fallback.
    pass

from unittest.mock import patch

class TestGlossaryYAML(unittest.TestCase):
    def setUp(self):
        # Create a temporary directory for custom user glossaries
        self.test_dir = tempfile.mkdtemp()
        self.original_home = Path.home()
        
        # Patch Path.home
        self.patcher = patch('pathlib.Path.home', return_value=Path(self.test_dir))
        self.mock_home = self.patcher.start()
        
        # Create structure
        self.glossary_dir = Path(self.test_dir) / ".eve_translator" / "glossaries"
        self.glossary_dir.mkdir(parents=True, exist_ok=True)
        
    def tearDown(self):
        self.patcher.stop()
        shutil.rmtree(self.test_dir)

    def test_load_bundled_terms(self):
        """Test that bundled zh_en.yml is loaded correctly."""
        # This relies on the actual file existing in data/glossaries/
        glossary = EVEGlossary('zh', 'en')
        
        # Check for a known term
        self.assertIn("穿梭机", glossary.terms)
        self.assertEqual(glossary.terms["穿梭机"], "Shuttle")
        
        # Check flattening
        self.assertIn("吉他", glossary.terms)
        self.assertEqual(glossary.terms["吉他"], "Jita")

    def test_sorting_order(self):
        """Test that terms are sorted by length descending."""
        # Create a custom glossary with conflicting prefix terms
        custom_file = self.glossary_dir / "custom_zh_en.yml"
        with open(custom_file, 'w', encoding='utf-8') as f:
            f.write("""
ships:
  test:
    大鱼: Small Whale
    大鱼王: Big Whale
""")
        
        glossary = EVEGlossary('zh', 'en')
        
        # Verify sorting
        keys = [x[0] for x in glossary.sorted_terms]
        
        # "大鱼王" (3 chars) should come before "大鱼" (2 chars)
        idx_long = keys.index("大鱼王")
        idx_short = keys.index("大鱼")
        self.assertLess(idx_long, idx_short)

    def test_custom_override(self):
        """Test that user custom glossary overrides bundled terms."""
        # "穿梭机" is "Shuttle" in bundled
        
        custom_file = self.glossary_dir / "custom_zh_en.yml"
        with open(custom_file, 'w', encoding='utf-8') as f:
            f.write("""
meta:
  source_lang: zh
  target_lang: en

ships:
  override:
    穿梭机: Super Shuttle
""")
        
        glossary = EVEGlossary('zh', 'en')
        self.assertEqual(glossary.terms["穿梭机"], "Super Shuttle")

    def test_language_switch_fallback(self):
        """Test loading a language pair with no bundled file."""
        # zh_fr doesn't exist
        glossary = EVEGlossary('zh', 'fr')
        
        # Should be empty (unless we add valid fallback logic for unknown pairs)
        # Current implementation: returns empty dict for unknown pairs (no hardcoded fallback for non-zh-en)
        self.assertEqual(len(glossary.terms), 0)

    def test_zh_de_placeholder(self):
        """Test loading the placeholder zh_de glossary."""
        glossary = EVEGlossary('zh', 'de')
        # We put "打得不错": "Gutes Gefecht" in placeholder (removed (GF))
        self.assertIn("打得不错", glossary.terms)
        self.assertEqual(glossary.terms["打得不错"], "Gutes Gefecht")

if __name__ == '__main__':
    import unittest.mock
    unittest.main()

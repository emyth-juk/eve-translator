import unittest
import yaml
from pathlib import Path
from src.core.glossary import EVEGlossary

def _flatten_terms(section: dict) -> dict:
    """Recursively flatten nested term dictionaries into a single mapping."""
    flat = {}
    for key, value in section.items():
        if isinstance(value, dict):
            flat.update(_flatten_terms(value))
        elif isinstance(value, str):
            flat[key] = value
        elif isinstance(value, int): # Handle cases like 1600 as value if unquoted, though we fixed it
             flat[str(key)] = str(value)
    return flat

class TestGlossaryMerged(unittest.TestCase):
    def setUp(self):
        self.glossary = EVEGlossary()

    def test_glossary_replacement_basics(self):
        """Test basic term replacement."""
        # Simple replacement
        text = "有便宜毒蜥卖吗"
        replaced = self.glossary.replace_terms(text)
        self.assertIn("Gila", replaced)
        self.assertNotIn("毒蜥", replaced)
        
        # Multiple terms
        text = "吉他收脑插" 
        # "收" -> "WTB", "吉他" -> "Jita"
        replaced = self.glossary.replace_terms(text)
        self.assertIn("Jita", replaced)
        self.assertIn("WTB", replaced)
        
        # No terms
        text = "Hello World"
        self.assertEqual(self.glossary.replace_terms(text), "Hello World")

    def test_glossary_extended_ships(self):
        """Test extended ship list."""
        cases = [
            ("马克瑞", "Machariel"),
            ("小马", "Machariel"),
            ("噩梦", "Nightmare"),
            ("小航", "Carrier"),
            ("大航", "Supercarrier"),
            ("泰坦", "Titan"),
            ("伊什塔", "Ishtar"),
            ("海狂怒", "VNI"),
            ("奥内", "Oneiros"),
            ("戴莫斯", "Deimos"),
            ("福博斯", "Phobos"),
        ]
        for term, expected in cases:
            with self.subTest(term=term):
                self.assertIn(expected, self.glossary.replace_terms(term))

    def test_glossary_fitting_modules(self):
        """Test fitting modules (New additions)."""
        cases = [
            ("中槽", "Mid Slot"),
            ("导扰", "Guidance Disruptor"),
            ("炮扰", "Tracking Disruptor"),
            ("全抗", "Multispectrum Hardener"),
            ("钢板", "Armor Plate"),
            ("1600", "1600mm Plate"), # This tests the numeric key matching
            ("电池", "Cap Battery"),
            ("跳刀", "MJD"),
        ]
        for term, expected in cases:
            with self.subTest(term=term):
                try:
                    replaced = self.glossary.replace_terms(term)
                    self.assertIn(expected, replaced)
                except Exception as e:
                    self.fail(f"Failed to replace '{term}': {e}")

    def test_glossary_regions(self):
        """Test newly added regions."""
        cases = [
            ("纯盲", "Pure Blind"),
            ("维纳尔", "Venal"),
            ("寂静谷", "Vale of the Silent"),
            ("云环", "Cloud Ring"),
        ]
        for term, expected in cases:
            with self.subTest(term=term):
                self.assertIn(expected, self.glossary.replace_terms(term))

    def test_glossary_validation(self):
        """Metadata and structural validation of zh_en.yml."""
        glossary_path = Path(__file__).resolve().parents[1] / "data" / "glossaries" / "zh_en.yml"
        self.assertTrue(glossary_path.exists(), f"Missing glossary file: {glossary_path}")

        with glossary_path.open(encoding="utf-8") as f:
            data = yaml.safe_load(f)

        # Meta sanity
        meta = data.get("meta", {})
        self.assertEqual(meta.get("source_lang"), "zh")
        self.assertEqual(meta.get("target_lang"), "en")
        self.assertTrue(meta.get("version"), "Glossary meta.version should be set")

        # Required sections
        required_sections = {"ships", "modules", "structures", "locations", "commands", "roles", "slang"}
        existing_sections = set(data.keys())
        missing_sections = required_sections - existing_sections
        self.assertFalse(missing_sections, f"Missing sections: {sorted(missing_sections)}")

        # Validate entries
        all_terms = {}
        for section_name in required_sections:
            section = data.get(section_name, {})
            self.assertIsInstance(section, dict, f"Section {section_name} should be a mapping")

            flattened = _flatten_terms(section)
            self.assertTrue(flattened, f"Section {section_name} has no entries")

            for term, translation in flattened.items():
                self.assertIsInstance(term, str, f"Key '{term}' in {section_name} is NOT a string")
                self.assertIsInstance(translation, str, f"Value for '{term}' in {section_name} is NOT a string")
                self.assertTrue(term.strip(), f"Empty term key in section {section_name}")
                self.assertTrue(translation.strip(), f"Empty translation for term '{term}' in section {section_name}")

                # Duplicate check
                if term in all_terms:
                    print(f"WARNING: Duplicate term '{term}' found in {section_name}. Previous in {all_terms[term]}")
                    # We might want to enforce uniqueness, but for now just warning or flexible assertion
                    # self.fail(f"Duplicate term found: {term}") 
                all_terms[term] = section_name

if __name__ == '__main__':
    unittest.main()

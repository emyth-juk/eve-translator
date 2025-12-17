
import unittest
from unittest.mock import MagicMock, patch
from src.services.translator import DeepLProvider, GoogleTransProvider

class TestTranslatorErrors(unittest.TestCase):

    def test_deepl_api_exception(self):
        """Test DeepL provider handling API errors cleanly."""
        mock_api_key = "dummy_key"
        
        # Patch the internal 'deepl' module import inside __init__? 
        # No, src.services.translator imports deepl inside __init__.
        # We need to patch 'src.services.translator.deepl' ? 
        # Wait, the code says:
        # def __init__(self, api_key: str):
        #    import deepl
        #    self.translator = deepl.Translator(api_key)
        
        # We can mock `deepl` in sys.modules before creating instance, or rely on patching.
        
        with patch.dict('sys.modules', {'deepl': MagicMock()}):
             import deepl # this gets the mock
             # Setup the mock translator to raise exception
             mock_translator_instance = deepl.Translator.return_value
             # Simulate API Error
             mock_translator_instance.translate_text.side_effect = Exception("403 Forbidden")
             
             provider = DeepLProvider(mock_api_key)
             
             # Should return None, not crash
             result = provider.translate("Hello", "zh")
             self.assertIsNone(result)

    def test_google_exception(self):
        """Test Google provider handling network errors."""
        with patch('src.services.translator.GoogleTranslator') as MockGT:
            mock_gt_instance = MockGT.return_value
            mock_gt_instance.translate.side_effect = Exception("Connection Timeout")
            
            provider = GoogleTransProvider()
            result = provider.translate("Hello", "fr")
            
            self.assertIsNone(result)

if __name__ == '__main__':
    unittest.main()

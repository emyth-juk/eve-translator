
import unittest
import threading
import time
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

    def test_google_request_stops_blocking_after_deadline(self):
        release_request = threading.Event()
        with patch('src.services.translator.GoogleTranslator') as MockGT:
            mock_gt_instance = MockGT.return_value
            mock_gt_instance.translate.side_effect = (
                lambda _text: release_request.wait(1.0)
            )
            provider = GoogleTransProvider()
            provider._call_runner.timeout_seconds = 0.01

            result = provider.translate("Hello", "fr")
            release_request.set()
            deadline = time.monotonic() + 0.5
            while not provider.available and time.monotonic() < deadline:
                time.sleep(0.005)

        self.assertIsNone(result)
        self.assertTrue(provider.available)

    def test_timed_out_google_provider_reports_unavailable_until_orphan_finishes(self):
        release_request = threading.Event()
        with patch('src.services.translator.GoogleTranslator') as MockGT:
            MockGT.return_value.translate.side_effect = (
                lambda _text: release_request.wait(1.0)
            )
            provider = GoogleTransProvider()
            provider._call_runner.timeout_seconds = 0.01

            self.assertIsNone(provider.translate("Hello", "fr"))
            self.assertFalse(provider.available)
            self.assertIn("timed out", provider.unavailable_reason)

            release_request.set()
            deadline = time.monotonic() + 0.5
            while not provider.available and time.monotonic() < deadline:
                time.sleep(0.005)

        self.assertTrue(provider.available)

    def test_timed_out_provider_scope_does_not_start_more_orphan_requests(self):
        release_request = threading.Event()
        with patch('src.services.translator.GoogleTranslator') as MockGT:
            mock_translator = MockGT.return_value
            mock_translator.translate.side_effect = (
                lambda _text: release_request.wait(1.0)
            )
            first_provider = GoogleTransProvider()
            first_provider._call_runner.timeout_seconds = 0.01

            self.assertIsNone(first_provider.translate("first", "fr"))
            self.assertEqual(mock_translator.translate.call_count, 1)

            replacement_provider = GoogleTransProvider()
            self.assertFalse(replacement_provider.available)
            self.assertIsNone(replacement_provider.translate("second", "fr"))
            self.assertEqual(mock_translator.translate.call_count, 1)

            release_request.set()
            deadline = time.monotonic() + 0.5
            while not replacement_provider.available and time.monotonic() < deadline:
                time.sleep(0.005)

        self.assertTrue(replacement_provider.available)

if __name__ == '__main__':
    unittest.main()

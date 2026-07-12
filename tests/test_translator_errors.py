
import unittest
import threading
import time
from types import SimpleNamespace
from unittest.mock import MagicMock, patch
import deep_translator.google as deep_translator_google

from src.services.translator import (
    GOOGLE_CONNECT_TIMEOUT_SECONDS,
    GOOGLE_READ_TIMEOUT_SECONDS,
    DeepLProvider,
    GoogleTransProvider,
    ProviderCallTimeout,
    _BoundedCallRunner,
)

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

    def test_google_transport_applies_connect_and_read_timeouts(self):
        with patch('src.services.translator.GoogleTranslator') as MockGT:
            provider = GoogleTransProvider()
            provider._http_session.get = MagicMock(
                return_value=SimpleNamespace(text="bonjour")
            )
            MockGT.return_value.translate.side_effect = (
                lambda _text: deep_translator_google.requests.get(
                    "https://translate.invalid"
                ).text
            )

            assert provider.translate("Hello", "fr") == "bonjour"

        provider._http_session.get.assert_called_once_with(
            "https://translate.invalid",
            timeout=(GOOGLE_CONNECT_TIMEOUT_SECONDS, GOOGLE_READ_TIMEOUT_SECONDS),
        )

    def test_google_usage_is_a_local_count_of_started_requests(self):
        with patch('src.services.translator.GoogleTranslator') as MockGT:
            MockGT.return_value.translate.return_value = "bonjour"
            provider = GoogleTransProvider()

            self.assertEqual(
                provider.get_usage(),
                {'available': True, 'estimated_characters': 0},
            )
            self.assertEqual(provider.translate("Hello", "fr"), "bonjour")

        self.assertEqual(
            provider.get_usage(),
            {'available': True, 'estimated_characters': 5},
        )

    def test_deepl_usage_returns_exact_character_limit_snapshot(self):
        with patch.dict('sys.modules', {'deepl': MagicMock()}):
            import deepl

            deepl.Translator.return_value.get_usage.return_value = SimpleNamespace(
                character=SimpleNamespace(count=800, limit=1000)
            )
            provider = DeepLProvider("dummy-key")

            self.assertEqual(
                provider.get_usage(refresh=True),
                {'available': True, 'used': 800, 'limit': 1000},
            )

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

    def test_stuck_provider_call_recovers_after_bounded_orphan_cooldown(self):
        release_request = threading.Event()
        scope = f"provider-recovery-{id(release_request)}"
        runner = _BoundedCallRunner(
            scope,
            timeout_seconds=0.01,
            orphan_cooldown_seconds=0.02,
        )
        calls = []

        def stuck_request():
            calls.append("stuck")
            release_request.wait()
            return "late"

        try:
            with self.assertRaises(ProviderCallTimeout):
                runner.run(stuck_request)
            with self.assertRaises(ProviderCallTimeout):
                runner.run(lambda: calls.append("too-early"))
            self.assertEqual(calls, ["stuck"])

            deadline = time.monotonic() + 0.5
            while not runner.available and time.monotonic() < deadline:
                time.sleep(0.005)

            self.assertTrue(runner.available)
            self.assertEqual(runner.run(lambda: "recovered"), "recovered")
            self.assertFalse(release_request.is_set())
        finally:
            release_request.set()

    def test_only_one_live_orphan_can_be_quarantined_per_provider_scope(self):
        first_release = threading.Event()
        second_release = threading.Event()
        scope = f"provider-one-orphan-{id(first_release)}"
        runner = _BoundedCallRunner(
            scope,
            timeout_seconds=0.01,
            orphan_cooldown_seconds=0.02,
        )
        calls = []

        def wait_for(release, label):
            calls.append(label)
            release.wait()
            return label

        try:
            with self.assertRaises(ProviderCallTimeout):
                runner.run(lambda: wait_for(first_release, "first"))
            deadline = time.monotonic() + 0.5
            while not runner.available and time.monotonic() < deadline:
                time.sleep(0.005)
            self.assertTrue(runner.available)

            with self.assertRaises(ProviderCallTimeout):
                runner.run(lambda: wait_for(second_release, "second"))
            time.sleep(0.04)

            self.assertFalse(runner.available)
            with self.assertRaises(ProviderCallTimeout):
                runner.run(lambda: calls.append("third"))
            self.assertEqual(calls, ["first", "second"])
        finally:
            first_release.set()
            second_release.set()

if __name__ == '__main__':
    unittest.main()

import pytest
import threading
from unittest.mock import MagicMock, patch

from src.core.detector import LanguageDetector
from src.services.translator import TranslationService, MockTranslator


class CountingProvider:
    name = "Counting"

    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = 0

    def translate(self, text: str, target_lang: str = 'en', source_lang: str = None):
        self.calls += 1
        if self.responses:
            return self.responses.pop(0)
        return f"{target_lang}:{source_lang}:{text}"

def test_detector_cjk():
    detector = LanguageDetector()
    assert detector.is_cjk("你好") is True
    assert detector.is_cjk("Hello") is False
    assert detector.should_translate("你好", ignored_langs={'en'})[0] is True

def test_detector_langdetect():
    detector = LanguageDetector()
    # "Bonjour" is French
    # Use longer text for reliability
    fr_text = "Bonjour tout le monde, comment ça va aujourd'hui?"
    assert detector.detect_language(fr_text) == 'fr'
    
    # Ignore check
    en_text = "The quick brown fox jumps over the lazy dog."
    assert detector.should_translate(en_text, ignored_langs={'en'})[0] is False
    
    de_text = "Das ist ein einfacher Test für die deutsche Sprache."
    assert detector.should_translate(de_text, ignored_langs={'en', 'de'})[0] is False
    
    # Should translate French
    assert detector.should_translate(fr_text, ignored_langs={'en', 'de'})[0] is True

def test_translator_mock():
    service = TranslationService(provider=MockTranslator())
    res = service.translate_message("Test", "en")
    # translate_message returns (text, success, provider_name)
    assert res == ("[MOCK] Test", True, "Mock")


def test_translation_service_starts_disabled_without_constructing_cloud_provider():
    google_factory = MagicMock()
    deepl_factory = MagicMock()
    service = TranslationService(provider_factories={
        'google': google_factory,
        'deepl': deepl_factory,
    })

    assert service.get_provider_status() == {
        'mode': 'disabled',
        'name': 'Disabled',
        'available': False,
        'reason': 'Translation provider is disabled.',
    }
    assert service.translate_message("Bonjour", "en", "fr") == (
        "Bonjour", False, "Disabled"
    )
    google_factory.assert_not_called()
    deepl_factory.assert_not_called()


def test_explicit_google_selection_constructs_google_provider():
    provider = CountingProvider(["hello"])
    provider.name = "Google"
    google_factory = MagicMock(return_value=provider)
    service = TranslationService(provider_factories={'google': google_factory})

    service.set_config({
        'translation_provider': 'google',
        'target_language': 'en',
        'deepl_api_key': 'preserved-deepl-secret',
    })

    assert service.provider_mode == 'google'
    assert service.provider_name == 'Google'
    assert service.provider_available is True
    assert service.translate_message("Bonjour", "en", "fr") == (
        "hello", True, "Google"
    )
    google_factory.assert_called_once_with('')


def test_legacy_deepl_key_selects_deepl_but_no_key_disables():
    provider = CountingProvider(["hello"])
    provider.name = "DeepL"
    deepl_factory = MagicMock(return_value=provider)
    service = TranslationService(provider_factories={'deepl': deepl_factory})

    service.set_config({'target_language': 'en', 'deepl_api_key': 'legacy-key'})
    assert service.provider_mode == 'deepl'
    deepl_factory.assert_called_once_with('legacy-key')

    service.set_config({'target_language': 'en', 'deepl_api_key': ''})
    assert service.provider_mode == 'disabled'
    assert service.provider_available is False


def test_explicit_deepl_without_key_is_unavailable_and_does_not_fallback():
    deepl_factory = MagicMock()
    google_factory = MagicMock()
    service = TranslationService(provider_factories={
        'deepl': deepl_factory,
        'google': google_factory,
    })

    service.set_config({
        'translation_provider': 'deepl',
        'target_language': 'en',
        'deepl_api_key': '',
    })

    assert service.get_provider_status() == {
        'mode': 'deepl',
        'name': 'DeepL',
        'available': False,
        'reason': 'DeepL API key is required.',
    }
    deepl_factory.assert_not_called()
    google_factory.assert_not_called()


def test_invalid_deepl_key_type_fails_closed_without_crashing():
    service = TranslationService()

    service.set_config({
        'translation_provider': 'deepl',
        'target_language': 'en',
        'deepl_api_key': 12345,
    })

    assert service.get_provider_status() == {
        'mode': 'deepl',
        'name': 'DeepL',
        'available': False,
        'reason': 'DeepL API key is required.',
    }


def test_invalid_target_language_type_falls_back_to_english():
    service = TranslationService()

    service.set_config({
        'translation_provider': 'disabled',
        'target_language': 12345,
    })

    assert service.glossary.target_lang == 'en'
    assert service.translate_message('Bonjour', 12345, 'fr') == (
        'Bonjour', False, 'Disabled'
    )


def test_unavailable_provider_retries_when_same_config_is_applied():
    provider = CountingProvider(["hello"])
    provider.name = "Google"
    google_factory = MagicMock(side_effect=[RuntimeError("offline"), provider])
    service = TranslationService(provider_factories={'google': google_factory})
    config = {'translation_provider': 'google', 'target_language': 'en'}

    service.set_config(config)
    assert service.provider_available is False

    service.set_config(config)
    assert service.provider_available is True
    assert service.provider_name == 'Google'
    assert google_factory.call_count == 2


def test_provider_status_uses_one_atomic_provider_snapshot():
    available_started = threading.Event()
    release_available = threading.Event()

    class SlowOldProvider:
        name = "Old"

        @property
        def available(self):
            available_started.set()
            release_available.wait(1.0)
            return True

        unavailable_reason = ""

    new_provider = CountingProvider([])
    new_provider.name = "Google"
    service = TranslationService(
        provider=SlowOldProvider(),
        provider_factories={'google': MagicMock(return_value=new_provider)},
    )
    captured = []
    reader = threading.Thread(target=lambda: captured.append(service.get_provider_status()))
    reader.start()
    assert available_started.wait(0.5)

    service.set_provider('google')
    release_available.set()
    reader.join(1.0)

    assert captured == [{
        'mode': 'old',
        'name': 'Old',
        'available': True,
        'reason': '',
    }]
    assert service.get_provider_status()['name'] == 'Google'


def test_provider_test_is_injectable_and_does_not_change_active_provider():
    provider = CountingProvider(["Verbindung test"])
    provider.name = "Google"
    google_factory = MagicMock(return_value=provider)
    service = TranslationService(provider_factories={'google': google_factory})

    result = service.test_provider(
        'google', deepl_api_key='do-not-forward-this-key', target_lang='en'
    )

    assert result.success is True
    assert result.provider_name == 'Google'
    assert service.provider_mode == 'disabled'
    google_factory.assert_called_once_with('')


def test_provider_test_failure_does_not_expose_api_key():
    secret = 'super-secret-key'
    deepl_factory = MagicMock(side_effect=RuntimeError(secret))
    service = TranslationService(provider_factories={'deepl': deepl_factory})

    result = service.test_provider('deepl', secret, 'en')

    assert result.success is False
    assert secret not in result.message


def test_translation_cache_hit_avoids_provider_call():
    provider = CountingProvider(["translated"])
    service = TranslationService(provider=provider)

    first = service.translate_message("cache me", "en", source_lang="fr")
    second = service.translate_message("cache me", "en", source_lang="fr")

    assert first == ("translated", True, "Counting")
    assert second == ("translated", True, "Counting")
    assert provider.calls == 1


def test_translation_failures_are_not_cached():
    provider = CountingProvider([None, "retry succeeded"])
    service = TranslationService(provider=provider)

    first = service.translate_message("retry me", "en", source_lang="fr")
    second = service.translate_message("retry me", "en", source_lang="fr")

    assert first == ("retry me", False, "Counting")
    assert second == ("retry succeeded", True, "Counting")
    assert provider.calls == 2


def test_translation_cache_clears_when_target_language_changes():
    service = TranslationService()
    service._store_cached_translation(("google", "en", "fr", "bonjour"), "hello")

    service.set_config({'target_language': 'de', 'deepl_api_key': ''})

    assert service._translation_cache == {}


def test_detector_should_translate_cache_avoids_repeat_detection():
    with patch('src.core.detector.detect', return_value='fr') as mock_detect:
        detector = LanguageDetector()
        detector.force_ignore_patterns = []
        detector.internet_slang_patterns = []
        detector._compile_ignore_patterns()

        first = detector.should_translate("Bonjour tout le monde", ignored_langs={'en'})
        second = detector.should_translate("Bonjour tout le monde", ignored_langs={'en'})

    assert first == (True, 'fr')
    assert second == (True, 'fr')
    assert mock_detect.call_count == 1

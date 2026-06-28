import pytest
from unittest.mock import patch

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

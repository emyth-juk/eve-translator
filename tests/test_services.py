import pytest
from src.core.detector import LanguageDetector
from src.services.translator import TranslationService, MockTranslator

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

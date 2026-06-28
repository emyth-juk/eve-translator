import logging
from collections import OrderedDict
from abc import ABC, abstractmethod
from deep_translator import GoogleTranslator
from typing import Optional

logger = logging.getLogger(__name__)
TRANSLATION_CACHE_SIZE = 512

class TranslationProvider(ABC):
    @property
    @abstractmethod
    def name(self) -> str:
        pass

    @abstractmethod
    def translate(self, text: str, target_lang: str = 'en', source_lang: str = None) -> Optional[str]:
        pass

class MockTranslator(TranslationProvider):
    @property
    def name(self) -> str:
        return "Mock"

    def translate(self, text: str, target_lang: str = 'en', source_lang: str = None) -> Optional[str]:
        return f"[MOCK] {text}"

class GoogleTransProvider(TranslationProvider):
    def __init__(self):
        # deep_translator checks source=auto by default
        self.translator = GoogleTranslator(source='auto', target='en')

    @property
    def name(self) -> str:
        return "Google"

    def translate(self, text: str, target_lang: str = 'en', source_lang: str = None) -> Optional[str]:
        try:
            self.translator.target = target_lang
            if source_lang and source_lang != 'auto' and source_lang != 'unknown':
                self.translator.source = source_lang
            else:
                self.translator.source = 'auto'
                
            return self.translator.translate(text)
        except Exception as e:
            logger.error(f"Translation Error (Google): {e}")
            return None

class DeepLProvider(TranslationProvider):
    # List of supported source languages for DeepL (as of 2024)
    # DeepL is strict about source_lang codes.
    DEEPL_SOURCE_LANGS = {
        'BG', 'CS', 'DA', 'DE', 'EL', 'EN', 'ES', 'ET', 'FI', 'FR', 'HU', 'ID', 'IT', 'JA', 
        'KO', 'LT', 'LV', 'NB', 'NL', 'PL', 'PT', 'RO', 'RU', 'SK', 'SL', 'SV', 'TR', 'UK', 'ZH'
    }

    def __init__(self, api_key: str):
        import deepl
        self.translator = deepl.Translator(api_key)

    @property
    def name(self) -> str:
        return "DeepL"

    def translate(self, text: str, target_lang: str = 'en', source_lang: str = None) -> Optional[str]:
        # Map target languages usually 'en' -> 'EN-US' for DeepL logic
        lang_map = {
            'en': 'EN-US',
            'gb': 'EN-GB',
            'pt': 'PT-PT',
            'pt-br': 'PT-BR'
        }
        target = lang_map.get(target_lang.lower(), target_lang.upper())
        
        args = {'target_lang': target}
        
        if source_lang and source_lang not in ['auto', 'unknown']:
            # Normalize source lang
            sl = source_lang.upper()
            if '-' in sl:
                 sl = sl.split('-')[0]
            
            # Additional Mapping
            if sl == 'NO': sl = 'NB' # Norwegian fix

            # Validate against supported list
            if sl in self.DEEPL_SOURCE_LANGS:
                args['source_lang'] = sl
            else:
                # If detected language is not supported by DeepL (e.g. 'so', 'af'), 
                # do NOT pass it. Let DeepL auto-detect. 
                # DeepL might detect it as something else valid, or return error if really unsupported,
                # but we avoid 400 "Value not supported" for the param itself.
                pass
            
        try:
            result = self.translator.translate_text(text, **args)
            return result.text
        except Exception as e:
            logger.error(f"Translation Error (DeepL): {e}")
            return None

from src.core.glossary import EVEGlossary

class TranslationService:
    def __init__(self, provider: TranslationProvider = None):
        if provider is None:
            # Prepare default provider
            self.provider = GoogleTransProvider()
        else:
            self.provider = provider

        self._provider_mode = self.provider.name.lower()
        self._deepl_api_key = ""
        self._translation_cache = OrderedDict()
        
        # Initialize Glossary
        self.glossary = EVEGlossary(source_lang='zh', target_lang='en')

    def set_config(self, config: dict):
        """Update configuration and reload glossary if needed."""
        target_lang = config.get('target_language', 'en')
        
        # Reload glossary if target language changed
        if target_lang != self.glossary.target_lang:
            # Assume 'zh' as source for now (Glossary is primarily Chinese-focused)
            # In future, source could also be configurable
            self.glossary = EVEGlossary(source_lang='zh', target_lang=target_lang)
            self._clear_translation_cache()

        self.set_provider_from_key(config.get('deepl_api_key', ''))

    def set_provider_from_key(self, deepl_api_key: str):
        """Select the translation provider from the configured DeepL API key."""
        normalized_key = (deepl_api_key or '').strip()

        if normalized_key:
            if self._provider_mode != 'deepl' or self._deepl_api_key != normalized_key:
                logger.info("Switching to DeepL Provider")
                self.provider = DeepLProvider(normalized_key)
                self._provider_mode = 'deepl'
                self._deepl_api_key = normalized_key
                self._clear_translation_cache()
            return

        if self._provider_mode != 'google':
            logger.info("Switching to Google Provider")
            self.provider = GoogleTransProvider()
            self._clear_translation_cache()

        self._provider_mode = 'google'
        self._deepl_api_key = ""

    def _clear_translation_cache(self):
        self._translation_cache.clear()

    def _cache_key(self, message: str, target_lang: str, source_lang: str = None):
        normalized_source = (source_lang or 'auto').lower()
        return (
            self._provider_mode,
            (target_lang or 'en').lower(),
            normalized_source,
            message,
        )

    def _get_cached_translation(self, key):
        if key not in self._translation_cache:
            return None
        translated = self._translation_cache.pop(key)
        self._translation_cache[key] = translated
        return translated

    def _store_cached_translation(self, key, translated: str):
        self._translation_cache[key] = translated
        if len(self._translation_cache) > TRANSLATION_CACHE_SIZE:
            self._translation_cache.popitem(last=False)

    def translate_message(self, message: str, target_lang: str = 'en', source_lang: str = None) -> tuple[str, bool, str]:
        if not message.strip():
            return message, True, self.provider.name
        
        # Apply Glossary Replacement (Pre-translation)
        # Replaces known EVE terms (like '毒蜥') with English ('Gila')
        # This helps DeepL context and ensures correct terminology.
        preprocessed = self.glossary.replace_terms(message)

        cache_key = self._cache_key(preprocessed, target_lang, source_lang)
        cached = self._get_cached_translation(cache_key)
        if cached is not None:
            return cached, True, self.provider.name
        
        translated = self.provider.translate(preprocessed, target_lang, source_lang)
        if translated:
            self._store_cached_translation(cache_key, translated)
            return translated, True, self.provider.name
        else:
            # If translation fails, return preprocessed (glossary applied) at least?
            # Or original? Usually original is safer if API fails completely.
            return message, False, self.provider.name

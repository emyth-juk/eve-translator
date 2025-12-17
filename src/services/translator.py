from abc import ABC, abstractmethod
from deep_translator import GoogleTranslator
from typing import Optional

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
            # logger.error(f"Translation Error (Google): {e}") # logger not imported
            return None

class DeepLProvider(TranslationProvider):
    def __init__(self, api_key: str):
        import deepl
        self.translator = deepl.Translator(api_key)

    @property
    def name(self) -> str:
        return "DeepL"

    def translate(self, text: str, target_lang: str = 'en', source_lang: str = None) -> Optional[str]:
        # Map languages usually 'en' -> 'EN-US' for DeepL logic
        lang_map = {
            'en': 'EN-US',
            'gb': 'EN-GB',
            'pt': 'PT-PT',
            'pt-br': 'PT-BR'
        }
        target = lang_map.get(target_lang.lower(), target_lang.upper())
        
        args = {'target_lang': target}
        if source_lang and source_lang not in ['auto', 'unknown']:
            # DeepL Source Lang usually requires 2-letter code (ZH, EN, PT)
            # langdetect returns 'zh-cn', 'en', 'pt-br', etc.
            sl = source_lang.upper()
            if '-' in sl:
                 sl = sl.split('-')[0]
            args['source_lang'] = sl
            
        try:
            result = self.translator.translate_text(text, **args)
            return result.text
        except Exception as e:
            print(f"Translation Error (DeepL): {e}")
            return None

from src.core.glossary import EVEGlossary

class TranslationService:
    def __init__(self, provider: TranslationProvider = None):
        if provider is None:
            # Prepare default provider
            self.provider = GoogleTransProvider()
        else:
            self.provider = provider
        
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

    def translate_message(self, message: str, target_lang: str = 'en', source_lang: str = None) -> tuple[str, bool, str]:
        if not message.strip():
            return message, True, self.provider.name
        
        # Apply Glossary Replacement (Pre-translation)
        # Replaces known EVE terms (like '毒蜥') with English ('Gila')
        # This helps DeepL context and ensures correct terminology.
        preprocessed = self.glossary.replace_terms(message)
        
        translated = self.provider.translate(preprocessed, target_lang, source_lang)
        if translated:
            return translated, True, self.provider.name
        else:
            # If translation fails, return preprocessed (glossary applied) at least?
            # Or original? Usually original is safer if API fails completely.
            return message, False, self.provider.name

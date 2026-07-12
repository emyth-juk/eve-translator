import re
import yaml
import os
import logging
import unicodedata
from functools import lru_cache
from langdetect import DetectorFactory, detect, LangDetectException
from typing import Optional, List, Dict
try:
    from src.utils.paths import get_resource_path
except ImportError:
    # Fallback for testing
    import sys
    def get_resource_path(relative_path):
        try:
            base_path = sys._MEIPASS
        except Exception:
            base_path = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
        return os.path.join(base_path, relative_path)

logger = logging.getLogger(__name__)

LANGUAGE_CACHE_SIZE = 1024
SHORT_ASCII_ALNUM_LIMIT = 3

# langdetect otherwise samples features with a random seed. Fleet messages are
# frequently short, so the same text must not change decisions between runs.
DetectorFactory.seed = 0

LANGUAGE_ALIASES = {
    # Legacy ISO codes occasionally returned by libraries or stored in config.
    'iw': 'he',
    'in': 'id',
    'ji': 'yi',
    # langdetect reports Norwegian as "no", while providers/config may use NB/NN.
    'nb': 'no',
    'nn': 'no',
}

EVE_MARKET_PREFIX_PATTERN = re.compile(
    r'^(?:hypernet|wts|wtb|wtt|zkillboard)\b',
    re.IGNORECASE,
)
EVE_ITEM_SUFFIX_PATTERN = re.compile(
    r'(?:blueprint|skill extractor|skill injector|repairer|booster|hardener|stabilizer)'
    r'(?:\s+x?\s*\d+\w*)?$',
    re.IGNORECASE,
)
ENGLISH_FLEET_COMMAND_PATTERNS = tuple(
    re.compile(pattern, re.IGNORECASE)
    for pattern in (
        r'align(?:\s+to\s+[a-z0-9][a-z0-9 .\'/-]*)?[!.]*',
        r'jump(?:\s+jump)*[!.]*',
        r'(?:do\s+not|don\'t)\s+jump[!.]*',
        r'hold(?:\s+(?:cloak|gate|position))?[!.]*',
        r'primary\s+(?:is\s+)?[a-z0-9][a-z0-9 .\'/-]*[!.]*',
    )
)

class LanguageDetector:
    """
    Detects the language of a message.
    """
    
    def __init__(self):
        # Specific East Asian scripts. Script evidence is more reliable than
        # statistical language detection for short fleet commands.
        self.han_pattern = re.compile(r'[\u3400-\u4dbf\u4e00-\u9fff]')
        self.hangul_pattern = re.compile(r'[\uac00-\ud7af]')
        self.kana_pattern = re.compile(r'[\u3040-\u30ff]')
        self.cjk_pattern = re.compile(
            r'[\u3400-\u4dbf\u4e00-\u9fff\u3040-\u30ff\uac00-\ud7af]'
        )
        
        # Load Ignore Patterns
        self.force_ignore_patterns = []
        self.internet_slang_patterns = []
        self._load_ignore_patterns()
        self._compile_ignore_patterns()
        self._detect_language_cached = lru_cache(maxsize=LANGUAGE_CACHE_SIZE)(
            self._detect_language_uncached
        )
        self._should_translate_cached = lru_cache(maxsize=LANGUAGE_CACHE_SIZE)(
            self._should_translate_uncached
        )

    def _load_ignore_patterns(self):
        """Load ignore patterns from YAML."""
        filename = "ignored_phrases.yml"
        path = get_resource_path(os.path.join("data", "glossaries", filename))
        
        loaded = False
        if os.path.exists(path):
            try:
                with open(path, 'r', encoding='utf-8') as f:
                    data = yaml.safe_load(f)
                    
                if data:
                    self.force_ignore_patterns = data.get('force_ignore', [])
                    self.internet_slang_patterns = data.get('slang', [])
                    loaded = True
                    logger.info(f"Loaded ignore patterns from {filename}")
            except Exception as e:
                logger.error(f"Failed to load ignore patterns: {e}")

        if not loaded:
            logger.warning("Using hardcoded fallback for ignore patterns.")
            self._load_hardcoded_patterns()

    def _compile_ignore_patterns(self):
        """Compile ignore patterns once so every message does not recompile regexes."""
        self._force_ignore_regexes = self._compile_patterns(self.force_ignore_patterns)
        self._slang_ignore_regexes = self._compile_patterns(self.internet_slang_patterns)
        # Keep the combined collection for compatibility with older callers/tests.
        self._ignore_regexes = self._force_ignore_regexes + self._slang_ignore_regexes

    @staticmethod
    def _compile_patterns(patterns):
        compiled = []
        for pattern in patterns:
            try:
                compiled.append(re.compile(pattern, re.IGNORECASE))
            except re.error as exc:
                logger.error(f"Invalid ignore pattern {pattern!r}: {exc}")
        return compiled

    def _load_hardcoded_patterns(self):
        """Fallback patterns if YAML missing."""
        self.force_ignore_patterns = [
            r'HyperNet', r'contract', r'WTS', r'WTB', r'WTT', r'zkillboard',
            r'blueprint', r'skin', r'skill extractor', r'skill injector',
            r'plex', r'abyssal', r'repairer', r'armor', r'shield',
            r'booster', r'hardener', r'stabilizer', r'bpo', r'bpc'
        ]
        self.internet_slang_patterns = [
            r'^[l1]+[o0]+[l1]+$', r'^lmao+$', r'^rofl+$', r'^wtf+$',
            r'^omg+$', r'^afk$', r'^brb$', r'^o7+$', r'^gf+$', r'^gg+$'
        ]

    def is_cjk(self, text: str) -> bool:
        """Fast check if text contains Han, Kana, or Hangul characters."""
        return bool(self.cjk_pattern.search(text))

    @staticmethod
    def normalize_language_code(language: str | None) -> str:
        """Normalize locale/provider variants to a comparable language code."""
        if not language:
            return 'unknown'

        normalized = str(language).strip().lower().replace('_', '-')
        if not normalized:
            return 'unknown'
        if normalized in {'auto', 'unknown'}:
            return normalized

        base_language = normalized.split('-', 1)[0]
        return LANGUAGE_ALIASES.get(base_language, base_language)

    def _detect_script_language(self, text: str) -> str | None:
        """Return a language when East Asian script makes the decision reliable."""
        if self.kana_pattern.search(text):
            return 'ja'
        if self.hangul_pattern.search(text):
            return 'ko'
        if self.han_pattern.search(text):
            # Pure Han text is overwhelmingly Chinese in the app's EVE corpus.
            # Japanese containing Kana was handled above.
            return 'zh'
        return None

    @staticmethod
    def _has_non_latin_letters(text: str) -> bool:
        """Return True for clear foreign-script evidence such as Cyrillic or Arabic."""
        for char in text:
            if not char.isalpha():
                continue
            if 'LATIN' not in unicodedata.name(char, ''):
                return True
        return False

    @staticmethod
    def _is_short_ascii_message(text: str) -> bool:
        """Identify short ASCII calls/codes that language detectors misclassify."""
        if not text.isascii():
            return False
        alnum_count = sum(char.isalnum() for char in text)
        return alnum_count <= SHORT_ASCII_ALNUM_LIMIT

    @staticmethod
    def _looks_like_eve_only_phrase(text: str) -> bool:
        """Identify market lines and title-cased item names, not natural sentences."""
        if EVE_MARKET_PREFIX_PATTERN.search(text):
            return True

        words = re.findall(r"[A-Za-z]+(?:['-][A-Za-z]+)*", text)
        if not words:
            return False

        # EVE item names in logs are normally title-cased and end in a known
        # item noun (for example, "Capital Abyssal Armor Repairer"). Merely
        # title-casing a foreign sentence containing "Shield" is not enough.
        is_title_cased = all(
            word == 'x' or word.isupper() or word[0].isupper()
            for word in words
        )
        return is_title_cased and bool(EVE_ITEM_SUFFIX_PATTERN.search(text))

    @staticmethod
    def _is_english_fleet_command(text: str) -> bool:
        """Recognize a small set of unambiguous English FC commands."""
        if not text.isascii():
            return False
        normalized = ' '.join(text.split())
        return any(pattern.fullmatch(normalized) for pattern in ENGLISH_FLEET_COMMAND_PATTERNS)

    def detect_language(self, text: str) -> str:
        return self._detect_language_cached(text)

    def _detect_language_uncached(self, text: str) -> str:
        """
        Identify language code (ISO 639-1).
        Fixes CJK false positives (sw, etc) to 'zh'.
        Prioritizes 'zh' for pure Hanzi (detects as 'ko'/'ja' often).
        """
        stripped = text.strip()
        if not stripped:
            return 'unknown'

        script_language = self._detect_script_language(stripped)
        if script_language:
            return script_language

        # Short ASCII is normally an EVE abbreviation, count, or command token.
        # Treating it as English is deterministic and avoids results such as
        # "3x" -> Somali from the statistical detector.
        if self._is_short_ascii_message(stripped):
            return 'en'

        detected = 'unknown'
        try:
            detected = self.normalize_language_code(detect(stripped))
        except LangDetectException:
            # Fallback: If pure ASCII, assume English
            if stripped.isascii():
                return 'en'

        return self.normalize_language_code(detected)
            
    def should_translate(self, text: str, target_lang: str = 'en', ignored_langs=None) -> (bool, str):
        if ignored_langs is None:
            ignored_langs = {'en'} # Default ignore English only
        elif isinstance(ignored_langs, str):
            ignored_langs = {ignored_langs}

        normalized_target = self.normalize_language_code(target_lang or 'en')
        normalized_ignored = {
            self.normalize_language_code(language)
            for language in ignored_langs
            if language
        }
        ignored_tuple = tuple(sorted(normalized_ignored))
        return self._should_translate_cached(text, normalized_target, ignored_tuple)

    def _should_translate_uncached(self, text: str, target_lang: str, ignored_tuple: tuple) -> (bool, str):
        """
        Decides if text should be translated.
        Returns: (should_translate: bool, detected_lang: str)
        """
        s_text = text.strip()
        if not s_text:
            return False, 'unknown'

        ignored_langs = set(ignored_tuple)
        target_lang = self.normalize_language_code(target_lang)

        # 1. Strong script evidence comes before keyword rules. A broad English
        # EVE term such as "shield" must never suppress a Chinese fleet command.
        script_lang = self._detect_script_language(s_text)
        if script_lang:
            if script_lang == target_lang or script_lang in ignored_langs:
                return False, script_lang
            return True, script_lang

        # Other non-Latin scripts are also strong evidence in a mixed message.
        # Detect them before broad ignore patterns such as "kill:.*".
        if self._has_non_latin_letters(s_text):
            detected_lang = self.detect_language(s_text)
            if detected_lang == target_lang or detected_lang in ignored_langs:
                return False, detected_lang
            if detected_lang != 'unknown':
                return True, detected_lang

        # These short FC calls are routinely misidentified by langdetect
        # (for example, "jump" as Latvian). They are confidently English, but
        # still need translation when the pilot's target language is not English.
        if self._is_english_fleet_command(s_text):
            return target_lang != 'en', 'en'

        # 2. Ignore only a complete phrase match. The previous substring search
        # caused words such as "shield" or "armor" to suppress an entire mixed
        # foreign-language sentence.
        for pattern in self._ignore_regexes:
            if pattern.fullmatch(s_text):
                return False, 'ignored_keyword'

        # Preserve the old cost-saving behavior for recognisable EVE market/item
        # lines while avoiding keyword substring matches in natural sentences.
        if self._looks_like_eve_only_phrase(s_text):
            for pattern in self._force_ignore_regexes:
                if pattern.search(s_text):
                    return False, 'ignored_keyword'

        # 3. Short ASCII calls and symbol-only messages are too ambiguous to
        # spend quota on, especially in fleet chat ("x", "3x", "o7", "+++").
        if self._is_short_ascii_message(s_text) or not any(
            char.isalpha() for char in s_text
        ):
            return False, 'short_text'

        # 4. General statistical detection for messages without strong script evidence.
        lang = self.detect_language(s_text)

        if lang == target_lang or lang in ignored_langs:
            return False, lang

        if lang == 'unknown':
            # Conservative: don't translate unknown (might be just symbols)
            return False, lang

        return True, lang


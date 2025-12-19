import re
import yaml
import os
import logging
from langdetect import detect, LangDetectException
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

class LanguageDetector:
    """
    Detects the language of a message.
    """
    
    def __init__(self):
        # Regex for CJK characters (common ranges)
        # Han, Hiragana, Katakana, etc.
        self.cjk_pattern = re.compile(r'[\u4e00-\u9fff\u3040-\u309f\u30a0-\u30ff]')
        # Specific scripts
        self.hangul_pattern = re.compile(r'[\uac00-\ud7af]')
        self.kana_pattern = re.compile(r'[\u3040-\u30ff]')
        
        # Load Ignore Patterns
        self.force_ignore_patterns = []
        self.internet_slang_patterns = []
        self._load_ignore_patterns()

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
        """Fast check if text contains CJK characters."""
        return bool(self.cjk_pattern.search(text))

    def detect_language(self, text: str) -> str:
        """
        Identify language code (ISO 639-1).
        Fixes CJK false positives (sw, etc) to 'zh'.
        Prioritizes 'zh' for pure Hanzi (detects as 'ko'/'ja' often).
        """
        if not text.strip():
            return 'unknown'
        
        # Heuristic: Short ASCII usually English or Abbreviations
        # Prevents "3x" -> 'so', "x" -> 'so'
        if len(text) < 4 and text.isascii():
            return 'en'

        detected = 'unknown'
        try:
            detected = detect(text)
        except LangDetectException:
            # Fallback: If pure ASCII, assume English
            if text.isascii():
                return 'en'
            pass
            
        # Refine CJK detection
        if self.is_cjk(text):
            # 1. If detected as non-CJK ('sw', 'tr', 'en'), force 'zh'.
            if not detected.startswith('zh') and detected not in ['ja', 'ko']:
                return 'zh'
            
            # 2. If detected as 'ko' (Korean) but NO Hangul -> Force 'zh'
            if detected == 'ko' and not self.hangul_pattern.search(text):
                return 'zh'
                
            # 3. If detected as 'ja' (Japanese) but NO Kana -> Force 'zh'
            # (Pure Kanji is usually Chinese in this context)
            if detected == 'ja' and not self.kana_pattern.search(text):
                return 'zh'

        return detected
            
    def should_translate(self, text: str, target_lang: str = 'en', ignored_langs=None) -> (bool, str):
        """
        Decides if text should be translated.
        Returns: (should_translate: bool, detected_lang: str)
        """
        if ignored_langs is None:
            ignored_langs = {'en'} # Default ignore English only

        # 1a. KEYWORD FILTER (Force Ignore)
        # Combine all patterns
        all_ignore_patterns = self.force_ignore_patterns + self.internet_slang_patterns

        s_text = text.strip()
        for pattern in all_ignore_patterns:
            if re.search(pattern, s_text, re.IGNORECASE):
                return False, 'ignored_keyword'

        # 1. CJK fast check (ALWAYS translate CJK if target is not CJK)
        if self.is_cjk(text):
             # Try to identify which CJK 
             cjk_lang = self.detect_language(text) # Will return 'zh', 'ja', 'ko' (corrected)
             
             if 'zh' in ignored_langs and 'ja' in ignored_langs and 'ko' in ignored_langs:
                 return False, cjk_lang
                 
             return True, cjk_lang

        # 2. Short text filter (if not CJK)
        # E.g. "x", "o7", "??" shouldn't trigger translation costs or errors
        if len(s_text) < 2:
            return False, 'short_text'
            
        # 3. General detection
        lang = self.detect_language(text)
        
        # 3. Handle 'no' (Norwegian) false positives for English gaming terms
        # "Avatar", "Titan", "Super", "Offer" often trigger 'no' (or 'da')
        if lang in ['no', 'da', 'af', 'so']:
             pass 
             # Could add heuristics here, but relying on ignore list is safer for now.

        if lang in ignored_langs:
            return False, lang
            
        if lang == 'unknown':
            # Conservative: don't translate unknown (might be just symbols)
            return False, lang
            
        return True, lang


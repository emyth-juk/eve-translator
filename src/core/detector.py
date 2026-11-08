import re
import yaml
import os
import logging
import unicodedata
from collections.abc import Iterable
from functools import lru_cache
from langdetect import DetectorFactory, detect, LangDetectException

from src.core.combat import classify_commands
from src.core.languages import normalize_language_code
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

EVE_MARKET_PREFIX_PATTERN = re.compile(
    r'^(?:wts|wtb|wtt|zkillboard)\b',
    re.IGNORECASE,
)
HYPERNET_OFFER_MARKER_PATTERN = re.compile(
    # EVE link metadata occasionally leaks into the visible label, producing
    # shapes such as ``HyperNet512offer:`` or ``HyperNetcheap8xoffer:``.
    r'\bhypernet(?:(?:[a-z0-9]{1,16})|\s+)offer\s*:',
    re.IGNORECASE,
)
HYPERNET_BARE_MARKER_PATTERN = re.compile(
    r'^[\s\-\u2013\u2014\u2022|]*hypernet\s*:',
    re.IGNORECASE,
)
HYPERNET_MARKET_FILLER_WORDS = frozenset({
    'b', 'bil', 'cheap', 'each', 'isk', 'left', 'm', 'mil',
    'node', 'nodes', 'only', 'x',
})
EVE_ITEM_SUFFIX_PATTERN = re.compile(
    r'(?:blueprint|skill extractor|skill injector|repairer|booster|hardener|stabilizer)'
    r'(?:\s+x?\s*\d+\w*)?$',
    re.IGNORECASE,
)
ASCII_WORD_PATTERN = re.compile(r"[A-Za-z]+(?:'[A-Za-z]+)?")
TITLE_WORD_PATTERN = re.compile(r"[A-Z][a-z0-9'\-]*|[A-Z]{1,4}")
# Share of recognised English/EVE words above which ASCII chat is treated as
# English without statistical detection. langdetect reports near-certain
# results such as "GATE RED" -> German or "need dps" -> Afrikaans.
ENGLISH_WORD_SHARE_THRESHOLD = 0.5
# Common English chat words plus EVE fleet vocabulary. This is evidence for
# English, not an exhaustive dictionary: lines with too few recognised words
# still go to statistical detection, so "vamos al gate" remains Spanish. Words
# that are just as common in German, Dutch or Romance languages (a, am, an,
# in, me, no, on, so, was, will, ...) are deliberately left out.
COMMON_ENGLISH_CHAT_WORDS = frozenset("""
about above after again against ago all almost alone along already always and another any
anyone anything anyway are around as ask at away back bad be because been before behind being
best better between big bit both bring but by call came can cannot cant careful check close
coming could couldnt day did didnt does doesnt doing done dont down each early easy either
else end enough even ever every everyone everything far few find fine first for forget found
free from front full fun get gets getting give go goes going gone good got great guess guy guys
had half hang happen happy hard has hasnt have havent having hear heard hello help her here hes
hey hi him his hit hold home hope how hows id ill im into is isnt it its ive just keep kill
killed know last late later least leave left less let lets like little long look looking lost
lot lots love luck made make many may maybe mean might mine more most move much must my need
needs never new next nice night none nope not nothing now of off oh ok okay old once one only
onto open or other our out over own past people pick place play please point pretty probably
put quick quickly quite rather ready really right round run said same saw say see seen send set
she should show side since slow small some someone something soon sorry still stop stuff such
sure take taking talk tell than thank thanks that thats the their them then there theres these
they theyre thing things think this those though thought through time to today together tonight
too took top try trying turn two under until up us use used very wait want wanted wasnt watch
way we well went were what whats when where which while who why wish with without wont work
would wouldnt wrong yeah yep yes yet you youll your youre yours
afaik btw gl gz grats hf idk imo kk lmao lol np nvm ofc omg omw pls plz rofl tbh thx ty wtf ya
yup
""".split())
EVE_FLEET_WORDS = frozenset("""
afk align aligned aligning alliance alpha ammo anchor anchored anomaly armor armour assault
battleship bc beacon belt blob blue blues boosh boost booster bounce bounced bridge bubble
bubbles burn cap capital caps capsule carrier cargo celestial cepter ceptor cloak cloaked
cloaky cloaking comms contract corp cyno damage dead dictor dock docked docking dps drag dread
dreadnought drone drones dscan engage enemy escalation explo fax fight fighter fighters fit
fitting fleet flycatcher frig frigate gang gate gatecamp gates grid hac heavy heavies hic hole
holes hostile hostiles hull hunter incoming inty intel jam jammed jump jumped jumping kick kite
local lock locked logi loot lowsec missile missiles mwd neut neutral neuts nullsec orbit ore
overheat pod podded pos pilot pilots ping pings pointed primary prop pvp ratting red reds
regroup rep repair reps roam roll rolled route safe scout scram scrammed secondary sec ship
ships shield site sites skill spike spy squad station stealth structure sub subcap super supers
tackle tackled tank target targets tidi titan titans tower travel undock undocked warp warping
web webbed webs wing wormhole wreck
go gogo green greens kekw mic pap paps srp sus trap voice
abyssal afterburner amarr angel artillery autocannon autocannons blaster blasters blood
caldari capacitor charge charges control cruise disruptor ecm energy extender extenders faction
federation federal gallente guristas implant implants issue jammer large laser lasers launcher
light medium minmatar module modules navy neutralizer nosferatu painter plate plates railgun
railguns rapid republic rocket rockets sansha scrambler sensor sensors serpentis stasis
torpedo torpedoes tracking turret webifier
""".split())
ENGLISH_CHAT_WORDS = COMMON_ENGLISH_CHAT_WORDS | EVE_FLEET_WORDS
REPEATED_LETTER_PATTERN = re.compile(r"(.)\1{2,}")
REPEATED_SYLLABLE_PATTERN = re.compile(r"(.{1,3}?)\1+")


def _is_known_english_chat_word(word: str) -> bool:
    """Match chat spellings such as ``GREEEN`` or ``GOGOGO`` to the vocabulary."""
    normalized = word.casefold().replace("'", "")
    candidates = {
        normalized,
        REPEATED_LETTER_PATTERN.sub(r"\1\1", normalized),
        REPEATED_LETTER_PATTERN.sub(r"\1", normalized),
    }
    syllable = REPEATED_SYLLABLE_PATTERN.fullmatch(normalized)
    if syllable:
        candidates.add(syllable.group(1))
    return any(candidate in ENGLISH_CHAT_WORDS for candidate in candidates)
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
        return normalize_language_code(language)

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
    def _ascii_word_tokens(text: str) -> list[str]:
        """Return alphabetic word tokens, ignoring codes such as ``4-HWWF``."""
        tokens = []
        for raw_token in text.split():
            # System names, counts, and coordinates contain digits. They carry
            # no language evidence and confuse statistical detection.
            if any(character.isdigit() for character in raw_token):
                continue
            tokens.extend(ASCII_WORD_PATTERN.findall(raw_token))
        return tokens

    @classmethod
    def _ascii_english_evidence(cls, text: str) -> str | None:
        """Classify plain-ASCII chat before langdetect can mislabel it.

        Returns ``'en'`` for recognisable English/EVE chat and title-cased
        names, ``'short_text'`` when only codes remain, or ``None`` when
        statistical detection should decide.
        """
        if not text.isascii():
            return None
        words = cls._ascii_word_tokens(text)
        if not words:
            return 'short_text'
        known = sum(1 for word in words if _is_known_english_chat_word(word))
        if known / len(words) >= ENGLISH_WORD_SHARE_THRESHOLD:
            return 'en'
        # A lone title-cased word is almost always a name: a system
        # ("Otsasai"), pilot or corp. Title-cased multi-word lines can be
        # foreign prose ("Hola Mundo") and stay with statistical detection.
        if len(words) == 1 and TITLE_WORD_PATTERN.fullmatch(words[0]):
            return 'en'
        return None

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
    def _looks_like_english_hypernet_offer(text: str) -> bool:
        """Identify generated HyperNet offer lines without hiding discussion.

        The offer label can be preceded by punctuation and can contain compact
        EVE-link artifacts.  Once labels are removed, generated offers contain
        title-cased item names, acronyms and numeric market metadata.  Lowercase
        prose that is not known market filler keeps the line eligible for normal
        language detection.
        """

        has_offer_marker = bool(HYPERNET_OFFER_MARKER_PATTERN.search(text))
        has_bare_marker = bool(HYPERNET_BARE_MARKER_PATTERN.search(text))
        if not has_offer_marker and not has_bare_marker:
            return False

        remainder = HYPERNET_OFFER_MARKER_PATTERN.sub(' ', text)
        remainder = HYPERNET_BARE_MARKER_PATTERN.sub(' ', remainder)
        words = re.findall(r"[A-Za-z]+(?:['-][A-Za-z]+)*", remainder)
        if not words:
            return False

        has_item_word = False
        for word in words:
            if word.casefold() in HYPERNET_MARKET_FILLER_WORDS:
                continue
            if word.isupper() or word[0].isupper():
                has_item_word = True
                continue
            return False

        return has_item_word

    @staticmethod
    def _commands_cover_text(text: str, command_matches: Iterable[object]) -> bool:
        """Return whether command matches account for every alphanumeric token."""

        remaining = unicodedata.normalize("NFKC", text or "")
        remaining = " ".join(remaining.split())
        for match in command_matches:
            clause = getattr(match, "source_clause", "")
            if not isinstance(clause, str) or not clause.strip():
                return False
            start = remaining.casefold().find(clause.casefold())
            if start < 0:
                return False
            end = start + len(clause)
            remaining = remaining[:start] + (" " * (end - start)) + remaining[end:]
        return not any(character.isalnum() for character in remaining)

    def command_language_hint(
        self,
        text: str,
        command_matches: Iterable[object] | None = None,
        source_language: str | None = None,
    ) -> str | None:
        """Return a reliable source hint for a fully classified command line.

        ``combat.classify_commands`` is the single built-in command grammar.
        Callers that add doctrine-only phrases can also supply an explicit
        ``source_language``; without that metadata, statistical detection is
        retained for ASCII doctrine phrases instead of guessing English.
        """

        matches = tuple(
            classify_commands(text) if command_matches is None else command_matches
        )
        if not matches or not self._commands_cover_text(text, matches):
            return None

        explicit_hint = normalize_language_code(source_language, "")
        if explicit_hint and explicit_hint not in {"auto", "unknown"}:
            return explicit_hint

        match_hints = tuple(
            normalize_language_code(
                getattr(match, "source_language", ""),
                "",
            )
            for match in matches
        )
        unique_hints = set(match_hints)
        if (
            len(unique_hints) == 1
            and match_hints[0] not in {"", "auto", "unknown"}
        ):
            return match_hints[0]

        script_language = self._detect_script_language(text)
        if script_language:
            return script_language

        # ASCII commands are English only when every upstream match is also
        # produced by the built-in combat grammar. Doctrine-only phrases need
        # an explicit source-language hint to avoid a new false assumption.
        if text.isascii():
            builtins = classify_commands(text)
            builtin_keys = {
                (match.code, match.source_clause.casefold()) for match in builtins
            }
            match_keys = {
                (
                    str(getattr(match, "code", "")),
                    str(getattr(match, "source_clause", "")).casefold(),
                )
                for match in matches
            }
            if match_keys and match_keys.issubset(builtin_keys):
                # ALIGN/WARP deliberately accept arbitrary destinations. A
                # multi-word lowercase tail without an English connector can
                # therefore be foreign prose ("align con la puerta"), not an
                # English source-language guarantee.
                for match in matches:
                    code = str(getattr(match, "code", "")).upper()
                    argument = str(getattr(match, "argument", "")).strip()
                    clause = str(getattr(match, "source_clause", ""))
                    if code not in {"ALIGN", "WARP"} or " " not in argument:
                        continue
                    prefix = clause[: clause.casefold().rfind(argument.casefold())]
                    if prefix.strip().casefold() not in {
                        "align to",
                        "align on",
                        "warp to",
                        "warp on",
                    }:
                        return None
                return "en"
        return None

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

        if self._ascii_english_evidence(stripped) is not None:
            # Codes-only lines also default to English, matching short ASCII.
            return 'en'

        detected = 'unknown'
        try:
            detected = self.normalize_language_code(detect(stripped))
        except LangDetectException:
            # Fallback: If pure ASCII, assume English
            if stripped.isascii():
                return 'en'

        return self.normalize_language_code(detected)
            
    def should_translate(
        self,
        text: str,
        target_lang: str = 'en',
        ignored_langs=None,
        *,
        command_matches: Iterable[object] | None = None,
        command_source_language: str | None = None,
    ) -> tuple[bool, str]:
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
        command_hint = self.command_language_hint(
            text,
            command_matches,
            command_source_language,
        )
        return self._should_translate_cached(
            text,
            normalized_target,
            ignored_tuple,
            command_hint or "",
        )

    def _should_translate_uncached(
        self,
        text: str,
        target_lang: str,
        ignored_tuple: tuple,
        command_hint: str,
    ) -> tuple[bool, str]:
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

        # Command classification is derived from combat.py's complete grammar,
        # not a second detector-specific list. Known commands still translate
        # when the target is different even if their source is generally ignored.
        if command_hint:
            return target_lang != command_hint, command_hint

        # Generated HyperNet offers are English market data, not chat prose.
        # Check their structure before statistical detection so leading dashes
        # and leaked link metadata cannot turn ship names into false Norwegian
        # (or other short-text) detections.
        if self._looks_like_english_hypernet_offer(s_text):
            return False, 'market_offer'

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
        if self._ascii_english_evidence(s_text) == 'short_text':
            # Only system names, counts, or other codes ("4-HWWF", "15W-GC*").
            return False, 'short_text'

        # 4. General statistical detection for messages without strong script evidence.
        lang = self.detect_language(s_text)

        if lang == target_lang or lang in ignored_langs:
            return False, lang

        if lang == 'unknown':
            # Conservative: don't translate unknown (might be just symbols)
            return False, lang

        return True, lang


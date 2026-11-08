import hashlib
import logging
import re
import threading
import time
from abc import ABC, abstractmethod
from collections import OrderedDict
from dataclasses import dataclass
from html.parser import HTMLParser
from typing import Any, Callable, Mapping, Optional, Sequence

import requests

from src.app_config import (
    TRANSLATION_PROVIDER_DEEPL,
    TRANSLATION_PROVIDER_DISABLED,
    TRANSLATION_PROVIDER_GOOGLE,
    TRANSLATION_PROVIDER_MODES,
    resolve_translation_provider,
)
from src.core.glossary import EVEGlossary
from src.core.languages import (
    normalize_language_code as _base_language_code,
    normalize_locale as _normalize_language_code,
)
from src.core.lexicon import LexiconTerm, LongestLexiconMatcher
from src.core.span_matching import NOT_AFTER_NON_CJK_WORD, NOT_BEFORE_NON_CJK_WORD

logger = logging.getLogger(__name__)
TRANSLATION_CACHE_SIZE = 512
GLOSSARY_CACHE_SIZE = 16
LEXICON_MATCHER_CACHE_SIZE = 32
PROVIDER_CALL_TIMEOUT_SECONDS = 12.0
# Chat translations are only useful while the conversation is current. The
# DeepL SDK defaults to 5 retries with 10 s attempts (~90 s per call); keep one
# retry so a failing call ends after roughly 21 s.
DEEPL_MAX_NETWORK_RETRIES = 1
DEEPL_CALL_TIMEOUT_SECONDS = 25.0
GOOGLE_CALL_TIMEOUT_SECONDS = 15.0
PROVIDER_USAGE_TIMEOUT_SECONDS = 20.0
PROVIDER_ORPHAN_COOLDOWN_SECONDS = 120.0
# Consecutive provider failures before calls pause, and how long they pause.
PROVIDER_FAILURE_THRESHOLD = 3
PROVIDER_FAILURE_COOLDOWN_SECONDS = 30.0
# Quota and authentication failures will not fix themselves within seconds.
PROVIDER_HARD_FAILURE_COOLDOWN_SECONDS = 300.0
FAILURE_QUOTA = "quota exhausted"
FAILURE_AUTH = "API key rejected"
FAILURE_RATE_LIMIT = "rate limited"
FAILURE_NETWORK = "network error"
FAILURE_TIMEOUT = "timed out"
FAILURE_PROTECTED_NAMES = "provider changed protected EVE names"
HARD_PROVIDER_FAILURES = frozenset({FAILURE_QUOTA, FAILURE_AUTH})
GOOGLE_CONNECT_TIMEOUT_SECONDS = 4.0
GOOGLE_READ_TIMEOUT_SECONDS = 8.0
GOOGLE_MOBILE_TRANSLATE_URL = "https://translate.google.com/m"


class _GoogleResultParser(HTMLParser):
    """Extract the translated text from Google's small mobile response."""

    def __init__(self, result_class: str):
        super().__init__(convert_charrefs=True)
        self._result_class = result_class
        self._result_depth = 0
        self._matched_result = False
        self._parts: list[str] = []

    def handle_starttag(self, tag, attrs):
        if self._result_depth:
            if tag == "div":
                self._result_depth += 1
            return
        if self._matched_result or tag != "div":
            return
        attributes = dict(attrs)
        if self._result_class in attributes.get("class", "").split():
            self._matched_result = True
            self._result_depth = 1

    def handle_endtag(self, tag):
        if self._result_depth and tag == "div":
            self._result_depth -= 1

    def handle_data(self, data):
        if self._result_depth:
            self._parts.append(data)

    @property
    def result(self) -> Optional[str]:
        if not self._matched_result:
            return None
        # Strip each HTML text segment without inserting artificial separators.
        return "".join(part.strip() for part in self._parts if part.strip())


class _GoogleMobileWebAdapter:
    """Minimal Google mobile-web client with no scraper dependency."""

    def __init__(self, session: requests.Session):
        self._session = session

    def translate(self, text: str, *, source_lang: str, target_lang: str) -> str:
        if not isinstance(text, str):
            raise TypeError("Google translation text must be a string")
        if len(text) > 5000:
            raise ValueError("Google translation text exceeds 5000 characters")
        text = text.strip()
        if not text or source_lang == target_lang:
            return text

        response = self._session.get(
            GOOGLE_MOBILE_TRANSLATE_URL,
            params={
                "sl": source_lang,
                "tl": target_lang,
                "q": text,
            },
            timeout=(GOOGLE_CONNECT_TIMEOUT_SECONDS, GOOGLE_READ_TIMEOUT_SECONDS),
        )
        try:
            response.raise_for_status()
            # Google has used both class names. Prefer the legacy primary name,
            # matching the previous provider path, then use its current fallback.
            for result_class in ("t0", "result-container"):
                parser = _GoogleResultParser(result_class)
                parser.feed(response.text)
                parser.close()
                translated = parser.result
                if translated is not None:
                    return translated
            raise ValueError("Google response did not contain a translation")
        finally:
            response.close()


class ProviderCallTimeout(TimeoutError):
    """Raised when a provider call exceeds the app's responsiveness budget."""


class ProviderCallCancelled(ProviderCallTimeout):
    """Raised when shutdown stops waiting for an in-flight provider call."""


_PROTECTED_MARKER_PATTERN = re.compile(r"_+EVE(?:DOCTRINE|ENTITY)_\d+_+", re.IGNORECASE)


def _has_text_to_translate(request_text: str) -> bool:
    """Return whether anything but protected names, codes and symbols remains.

    Count/code tokens such as ``3x``, ``4-HWWF`` or ``J123456`` are not
    language; a CJK word next to a digit ("3个") still is.
    """
    remainder = _PROTECTED_MARKER_PATTERN.sub(" ", request_text or "")
    for token in remainder.split():
        letters = [character for character in token if character.isalpha()]
        if not letters:
            continue
        if any(character.isdigit() for character in token) and all(
            letter.isascii() for letter in letters
        ):
            continue
        return True
    return False


def describe_provider_failure(exc: BaseException) -> str:
    """Return a short, message-free reason for a provider exception."""
    if isinstance(exc, ProviderCallCancelled):
        return "shutting down"
    if isinstance(exc, (ProviderCallTimeout, requests.exceptions.Timeout)):
        return FAILURE_TIMEOUT
    if isinstance(exc, requests.exceptions.HTTPError):
        status = getattr(getattr(exc, 'response', None), 'status_code', None)
        if status == 429:
            return FAILURE_RATE_LIMIT
        return f"HTTP {status}" if status else "request failed"
    if isinstance(exc, requests.exceptions.ConnectionError):
        return FAILURE_NETWORK
    # The DeepL SDK is imported lazily, so classify its exceptions by name.
    by_name = {
        'QuotaExceededException': FAILURE_QUOTA,
        'AuthorizationException': FAILURE_AUTH,
        'TooManyRequestsException': FAILURE_RATE_LIMIT,
        'ConnectionException': FAILURE_NETWORK,
    }
    for cls in type(exc).__mro__:
        reason = by_name.get(cls.__name__)
        if reason:
            return reason
    return "request failed"


class _SortedNames(tuple):
    """Deduplicated protected names, longest first (precomputed once)."""

    @classmethod
    def of(cls, names) -> "_SortedNames":
        return cls(sorted(set(names), key=len, reverse=True))


class _ProviderCircuit:
    """Pause calls to a provider after repeated or unrecoverable failures.

    During an outage every queued chat line would otherwise wait for its own
    network timeout on the single scheduler thread. Once open, calls fail
    immediately with the recorded reason until the cooldown expires; the next
    call after that is a normal probe.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._failures = 0
        self._open_until = 0.0
        self._reason = ""

    def blocked_reason(self, now: float | None = None) -> str:
        now = time.monotonic() if now is None else now
        with self._lock:
            if now >= self._open_until:
                return ""
            remaining = max(1, int(round(self._open_until - now)))
            reason = self._reason or "repeated failures"
        return f"{reason}; retrying in {remaining}s"

    def record_success(self) -> None:
        with self._lock:
            self._failures = 0
            self._open_until = 0.0
            self._reason = ""

    def record_failure(self, reason: str, now: float | None = None) -> None:
        now = time.monotonic() if now is None else now
        with self._lock:
            self._failures += 1
            self._reason = reason or "request failed"
            if self._reason in HARD_PROVIDER_FAILURES:
                self._open_until = now + PROVIDER_HARD_FAILURE_COOLDOWN_SECONDS
            elif self._failures >= PROVIDER_FAILURE_THRESHOLD:
                self._open_until = now + PROVIDER_FAILURE_COOLDOWN_SECONDS


@dataclass
class _OrphanedCall:
    thread: threading.Thread
    quarantine_after: float
    quarantined: bool = False


class _BoundedCallRunner:
    """Run at most one provider request and stop waiting at a fixed deadline.

    Third-party SDKs do not expose consistent timeout controls. The request
    thread is a daemon so a stalled socket cannot hold the app open, and a
    second request is not started while the first is still winding down.
    """

    _orphan_lock = threading.Lock()
    _orphaned_threads: dict[
        str,
        dict[threading.Thread, _OrphanedCall],
    ] = {}

    def __init__(
        self,
        scope,
        timeout_seconds=PROVIDER_CALL_TIMEOUT_SECONDS,
        orphan_cooldown_seconds=PROVIDER_ORPHAN_COOLDOWN_SECONDS,
    ):
        self.scope = scope
        self.timeout_seconds = timeout_seconds
        self.orphan_cooldown_seconds = max(0.0, float(orphan_cooldown_seconds))
        self._state_lock = threading.Lock()
        self._active_thread = None
        self._timed_out = False
        self._cancel_requested = threading.Event()

    def request_cancel(self) -> None:
        """Stop the owner thread waiting while its daemon request winds down."""
        self._cancel_requested.set()

    @classmethod
    def _scope_orphan_state(
        cls,
        scope,
        active_thread: threading.Thread | None = None,
    ) -> tuple[bool, bool]:
        """Return ``(blocked, active_is_quarantined)`` for one provider scope.

        After its cooldown, one never-returning request is quarantined and no
        longer blocks sequential retries.  A second live timeout is never
        quarantined while the first remains alive, bounding the policy to one
        ignored orphan plus one current request instead of leaking threads on
        every retry.
        """

        with cls._orphan_lock:
            records = {
                thread: record
                for thread, record in cls._orphaned_threads.get(scope, {}).items()
                if thread.is_alive()
            }
            if not records:
                cls._orphaned_threads.pop(scope, None)
                return False, False

            quarantined = next(
                (record for record in records.values() if record.quarantined),
                None,
            )
            if quarantined is None:
                now = time.monotonic()
                eligible = sorted(
                    (
                        record
                        for record in records.values()
                        if now >= record.quarantine_after
                    ),
                    key=lambda record: record.quarantine_after,
                )
                if eligible:
                    eligible[0].quarantined = True

            cls._orphaned_threads[scope] = records
            active_record = records.get(active_thread)
            return (
                any(not record.quarantined for record in records.values()),
                bool(active_record and active_record.quarantined),
            )

    def _mark_timed_out(self):
        with self._state_lock:
            active_thread = self._active_thread
        if active_thread is None:
            return
        with self._orphan_lock:
            self._orphaned_threads.setdefault(self.scope, {}).setdefault(
                active_thread,
                _OrphanedCall(
                    thread=active_thread,
                    quarantine_after=(
                        time.monotonic() + self.orphan_cooldown_seconds
                    ),
                ),
            )
        with self._state_lock:
            if self._active_thread is active_thread:
                self._timed_out = True

    @property
    def available(self) -> bool:
        with self._state_lock:
            active_thread = self._active_thread
            timed_out = self._timed_out
        blocked, active_is_quarantined = self._scope_orphan_state(
            self.scope,
            active_thread,
        )
        with self._state_lock:
            if self._active_thread is active_thread and timed_out and (
                active_thread is None
                or not active_thread.is_alive()
                or active_is_quarantined
            ):
                self._active_thread = None
                self._timed_out = False
            own_call_available = not self._timed_out
        return own_call_available and not blocked

    @property
    def unavailable_reason(self) -> str:
        if self.available:
            return ""
        return "The previous provider request timed out and is still closing. Retry later."

    def run(self, callback):
        result = []
        errors = []
        finished = threading.Event()

        if self._cancel_requested.is_set():
            raise ProviderCallCancelled("Provider shutdown was requested")

        def invoke():
            try:
                result.append(callback())
            except Exception as exc:
                errors.append(exc)
            finally:
                finished.set()

        if not self.available:
            raise ProviderCallTimeout("A timed-out provider request is still pending")

        with self._state_lock:
            active_request = (
                self._active_thread is not None
                and self._active_thread.is_alive()
            )
        if active_request:
            raise ProviderCallTimeout("A previous provider request is still pending")

        with self._state_lock:
            self._active_thread = threading.Thread(
                target=invoke,
                name="translation-provider-request",
                daemon=True,
            )
            self._active_thread.start()

        deadline = time.monotonic() + max(0.0, float(self.timeout_seconds))
        while not finished.is_set():
            if self._cancel_requested.is_set():
                self._mark_timed_out()
                raise ProviderCallCancelled("Provider shutdown was requested")
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                self._mark_timed_out()
                raise ProviderCallTimeout(
                    "The provider request exceeded its deadline"
                )
            finished.wait(min(0.05, remaining))
        if errors:
            raise errors[0]
        return result[0] if result else None


class TranslationProvider(ABC):
    # Short reason for the most recent failed call ("quota exhausted", ...).
    # Providers swallow their own exceptions, so this carries the cause.
    last_failure_reason: str = ""

    @property
    @abstractmethod
    def name(self) -> str:
        pass

    def _note_failure(self, exc: BaseException) -> None:
        self.last_failure_reason = describe_provider_failure(exc)

    def _note_success(self) -> None:
        self.last_failure_reason = ""

    @property
    def available(self) -> bool:
        return True

    @property
    def unavailable_reason(self) -> str:
        return ""

    @property
    def supports_batch(self) -> bool:
        """Whether one provider request can translate multiple messages."""
        return False

    @property
    def supports_context(self) -> bool:
        """Whether the provider has a non-translated context parameter."""
        return False

    def get_usage(self, *, refresh: bool = False) -> dict:
        """Return a safe provider-specific usage snapshot without message data."""

        return {'available': False}

    def request_cancel(self) -> None:
        """Thread-safe shutdown hook for providers with bounded call runners."""
        return

    @abstractmethod
    def translate(
        self,
        text: str,
        target_lang: str = 'en',
        source_lang: str = None,
        context: str = None,
    ) -> Optional[str]:
        pass

    def translate_batch(
        self,
        texts: Sequence[str],
        target_lang: str = 'en',
        source_lang: str = None,
        context: str = None,
    ) -> Optional[list[Optional[str]]]:
        """Translate a batch when ``supports_batch`` is true.

        The base implementation deliberately does not emulate batching with a
        loop: callers can distinguish a real provider batch from sequential
        requests through ``supports_batch``.
        """
        return None


class DisabledTranslationProvider(TranslationProvider):
    @property
    def name(self) -> str:
        return "Disabled"

    @property
    def available(self) -> bool:
        return False

    @property
    def unavailable_reason(self) -> str:
        return "Translation provider is disabled."

    def translate(
        self,
        text: str,
        target_lang: str = 'en',
        source_lang: str = None,
        context: str = None,
    ) -> Optional[str]:
        return None


class UnavailableTranslationProvider(TranslationProvider):
    def __init__(self, name: str, reason: str):
        self._name = name
        self._reason = reason

    @property
    def name(self) -> str:
        return self._name

    @property
    def available(self) -> bool:
        return False

    @property
    def unavailable_reason(self) -> str:
        return self._reason

    def translate(
        self,
        text: str,
        target_lang: str = 'en',
        source_lang: str = None,
        context: str = None,
    ) -> Optional[str]:
        return None


class MockTranslator(TranslationProvider):
    @property
    def name(self) -> str:
        return "Mock"

    def translate(
        self,
        text: str,
        target_lang: str = 'en',
        source_lang: str = None,
        context: str = None,
    ) -> Optional[str]:
        return f"[MOCK] {text}"


class GoogleTransProvider(TranslationProvider):
    def __init__(self):
        self._http_session = requests.Session()
        self.translator = _GoogleMobileWebAdapter(self._http_session)
        # Let the transport's 4s connect + 8s read limits fire before the
        # outer guard, leaving headroom for parsing and thread scheduling.
        self._call_runner = _BoundedCallRunner(
            "google", timeout_seconds=GOOGLE_CALL_TIMEOUT_SECONDS
        )
        self._usage_lock = threading.Lock()
        self._estimated_characters = 0

    @property
    def name(self) -> str:
        return "Google"

    @property
    def available(self) -> bool:
        return self._call_runner.available

    @property
    def unavailable_reason(self) -> str:
        return self._call_runner.unavailable_reason

    @property
    def supports_batch(self) -> bool:
        # The mobile endpoint handles one source string per request.
        return False

    def request_cancel(self) -> None:
        self._call_runner.request_cancel()

    def translate(
        self,
        text: str,
        target_lang: str = 'en',
        source_lang: str = None,
        context: str = None,
    ) -> Optional[str]:
        try:
            normalized_target = _normalize_language_code(target_lang)
            normalized_source = _normalize_language_code(source_lang, default='auto')

            def request():
                request_source = (
                    normalized_source
                    if normalized_source not in {'auto', 'unknown'}
                    else 'auto'
                )
                with self._usage_lock:
                    self._estimated_characters += len(text)
                return self.translator.translate(
                    text,
                    source_lang=request_source,
                    target_lang=normalized_target,
                )

            translated = self._call_runner.run(request)
            self._note_success()
            return translated
        except Exception as exc:
            # Provider exceptions can contain request details. Keep logs useful
            # without risking credential or message disclosure.
            logger.error("Translation Error (Google): %s", type(exc).__name__)
            self._note_failure(exc)
            return None

    def get_usage(self, *, refresh: bool = False) -> dict:
        del refresh
        with self._usage_lock:
            estimated = self._estimated_characters
        return {
            'available': True,
            'estimated_characters': estimated,
        }


class DeepLProvider(TranslationProvider):
    # DeepL is strict about source language codes.
    DEEPL_SOURCE_LANGS = {
        'BG', 'CS', 'DA', 'DE', 'EL', 'EN', 'ES', 'ET', 'FI', 'FR', 'HU', 'ID', 'IT',
        'JA', 'KO', 'LT', 'LV', 'NB', 'NL', 'PL', 'PT', 'RO', 'RU', 'SK', 'SL', 'SV',
        'TR', 'UK', 'ZH',
    }

    def __init__(self, api_key: str):
        import deepl

        try:
            # Module-level SDK setting (documented): each attempt may take up
            # to 10 s, so the default 5 retries could block chat for ~90 s.
            deepl.http_client.max_network_retries = DEEPL_MAX_NETWORK_RETRIES
        except Exception as exc:
            logger.warning("Could not limit DeepL retries: %s", type(exc).__name__)
        self.translator = deepl.Translator(api_key)
        self._usage_translator = deepl.Translator(api_key)
        # The SDK applies bounded 10-second socket attempts plus one retry.
        # Keep the outer guard beyond that envelope so it does not orphan a
        # healthy SDK call that is still inside its own retry.
        self._call_runner = _BoundedCallRunner(
            "deepl", timeout_seconds=DEEPL_CALL_TIMEOUT_SECONDS
        )
        self._usage_lock = threading.Lock()
        self._usage_snapshot = {'available': False}
        self._usage_call_runner = _BoundedCallRunner(
            "deepl-usage",
            timeout_seconds=PROVIDER_USAGE_TIMEOUT_SECONDS,
        )

    @property
    def name(self) -> str:
        return "DeepL"

    @property
    def available(self) -> bool:
        return self._call_runner.available

    @property
    def unavailable_reason(self) -> str:
        return self._call_runner.unavailable_reason

    @property
    def supports_batch(self) -> bool:
        return True

    @property
    def supports_context(self) -> bool:
        return True

    def request_cancel(self) -> None:
        self._call_runner.request_cancel()
        self._usage_call_runner.request_cancel()

    @classmethod
    def _translation_args(
        cls,
        target_lang: str,
        source_lang: str = None,
        context: str = None,
    ) -> dict[str, str]:
        target_lang = _normalize_language_code(target_lang)
        # DeepL target codes differ from common locale tags; unmapped regional
        # Chinese or Norwegian codes are rejected by the API.
        lang_map = {
            'en': 'EN-US',
            'en-us': 'EN-US',
            'gb': 'EN-GB',
            'en-gb': 'EN-GB',
            'pt': 'PT-PT',
            'pt-pt': 'PT-PT',
            'pt-br': 'PT-BR',
            'zh': 'ZH-HANS',
            'zh-cn': 'ZH-HANS',
            'zh-sg': 'ZH-HANS',
            'zh-hans': 'ZH-HANS',
            'zh-tw': 'ZH-HANT',
            'zh-hk': 'ZH-HANT',
            'zh-mo': 'ZH-HANT',
            'zh-hant': 'ZH-HANT',
            'no': 'NB',
            'nb': 'NB',
        }
        target = lang_map.get(target_lang.lower())
        if target is None:
            parts = target_lang.lower().replace('_', '-').split('-')
            base, rest = parts[0], parts[1:]
            if base == 'zh':
                traditional = any(part in {'hant', 'tw', 'hk', 'mo'} for part in rest)
                target = 'ZH-HANT' if traditional else 'ZH-HANS'
            elif base in {'no', 'nb'}:
                target = 'NB'
            elif base == 'en':
                target = 'EN-GB' if any(part in {'gb', 'uk'} for part in rest) else 'EN-US'
            elif base == 'pt':
                target = 'PT-BR' if 'br' in rest else 'PT-PT'
            else:
                # Other DeepL targets have no regional variants ("de-AT" -> DE).
                target = base.upper()
        args = {'target_lang': target}

        normalized_source = _normalize_language_code(source_lang, default='auto')
        if normalized_source not in {'auto', 'unknown'}:
            source = normalized_source.upper().split('-')[0]
            if source == 'NO':
                source = 'NB'
            if source in cls.DEEPL_SOURCE_LANGS:
                args['source_lang'] = source

        if isinstance(context, str) and context.strip():
            args['context'] = context.strip()
        return args

    def translate(
        self,
        text: str,
        target_lang: str = 'en',
        source_lang: str = None,
        context: str = None,
    ) -> Optional[str]:
        args = self._translation_args(target_lang, source_lang, context)

        try:
            result = self._call_runner.run(
                lambda: self.translator.translate_text(text, **args)
            )
            self._note_success()
            return result.text
        except Exception as exc:
            logger.error("Translation Error (DeepL): %s", type(exc).__name__)
            self._note_failure(exc)
            return None

    def translate_batch(
        self,
        texts: Sequence[str],
        target_lang: str = 'en',
        source_lang: str = None,
        context: str = None,
    ) -> Optional[list[Optional[str]]]:
        batch = list(texts)
        if not batch:
            return []
        args = self._translation_args(target_lang, source_lang, context)
        try:
            results = self._call_runner.run(
                lambda: self.translator.translate_text(batch, **args)
            )
            if not isinstance(results, Sequence) or isinstance(results, (str, bytes)):
                self.last_failure_reason = "unexpected response"
                return None
            self._note_success()
            return [
                result.text if isinstance(getattr(result, 'text', None), str) else None
                for result in results
            ]
        except Exception as exc:
            logger.error("Translation Error (DeepL batch): %s", type(exc).__name__)
            self._note_failure(exc)
            return None

    def get_usage(self, *, refresh: bool = False) -> dict:
        if refresh:
            try:
                usage = self._usage_call_runner.run(
                    self._usage_translator.get_usage
                )
                character = getattr(usage, 'character', None)
                count = getattr(character, 'count', None)
                limit = getattr(character, 'limit', None)
                if (
                    isinstance(count, int)
                    and not isinstance(count, bool)
                    and isinstance(limit, int)
                    and not isinstance(limit, bool)
                    and count >= 0
                    and limit > 0
                ):
                    snapshot = {
                        'available': True,
                        'used': count,
                        'limit': limit,
                    }
                else:
                    snapshot = {'available': False}
            except Exception as exc:
                logger.error("DeepL usage request failed: %s", type(exc).__name__)
                snapshot = {'available': False, 'error': True}
            with self._usage_lock:
                self._usage_snapshot = snapshot
        with self._usage_lock:
            return dict(self._usage_snapshot)


ProviderFactory = Callable[[str], TranslationProvider]


def _normalize_api_key(value) -> str:
    return value.strip() if isinstance(value, str) else ""


@dataclass(frozen=True)
class ProviderTestResult:
    success: bool
    provider_name: str
    message: str


@dataclass(frozen=True)
class _PreparedTranslation:
    original: str
    request_text: str
    tokens: Mapping[str, str]
    cache_key: tuple


@dataclass(frozen=True)
class _CachedTranslation:
    text: str
    provider_name: str


class TranslationService:
    def __init__(
        self,
        provider: TranslationProvider = None,
        provider_factories: Mapping[str, ProviderFactory] | None = None,
        translation_memory: Any = None,
        entity_index: Any = None,
        doctrine_manager: Any = None,
        doctrine_pack: Any = None,
        glossary_factory: Callable[..., EVEGlossary] = EVEGlossary,
    ):
        self._provider_factories: dict[str, ProviderFactory] = {
            TRANSLATION_PROVIDER_DISABLED: lambda _key: DisabledTranslationProvider(),
            TRANSLATION_PROVIDER_GOOGLE: lambda _key: GoogleTransProvider(),
            TRANSLATION_PROVIDER_DEEPL: lambda key: DeepLProvider(key),
        }
        if provider_factories:
            self._provider_factories.update(provider_factories)

        self._provider_lock = threading.RLock()
        self.provider = provider or DisabledTranslationProvider()
        self._provider_mode = self.provider.name.lower()
        self._deepl_api_key = ""
        self._fallback_provider: TranslationProvider | None = None
        self._fallback_mode = TRANSLATION_PROVIDER_DISABLED
        self._fallback_enabled = False
        self._circuit_lock = threading.Lock()
        # Reason for the most recent failed translation, for HUD/compose text.
        self.last_failure_reason = ""
        self._translation_cache = OrderedDict()
        self._glossary_factory = glossary_factory
        self._glossaries: OrderedDict[
            tuple[str, str], EVEGlossary
        ] = OrderedDict()
        self._glossary_revisions: OrderedDict[
            tuple[str, str], str
        ] = OrderedDict()
        self._lexicon_matchers: OrderedDict[
            tuple, LongestLexiconMatcher
        ] = OrderedDict()
        self._target_lang = 'en'
        self.translation_memory = translation_memory
        self.entity_index = entity_index
        self.doctrine_manager = doctrine_manager
        self.doctrine_pack = doctrine_pack
        # Compatibility for callers that inspect ``service.glossary``. It
        # points to the most recently selected language-pair glossary.
        self.glossary = self._get_glossary('zh', 'en')

    @property
    def provider_mode(self) -> str:
        with self._provider_lock:
            return self._provider_mode

    @property
    def provider_name(self) -> str:
        with self._provider_lock:
            provider = self.provider
        return provider.name

    @property
    def provider_available(self) -> bool:
        with self._provider_lock:
            provider = self.provider
        return bool(getattr(provider, 'available', True))

    def _provider_circuit(self, provider) -> _ProviderCircuit:
        """Return the failure circuit owned by one provider instance.

        Storing it on the instance means a rebuilt provider (new key or mode)
        starts closed without any explicit reset bookkeeping.
        """
        with self._circuit_lock:
            circuit = getattr(provider, '_translation_circuit', None)
            if isinstance(circuit, _ProviderCircuit):
                return circuit
            circuit = _ProviderCircuit()
            try:
                provider._translation_circuit = circuit
            except (AttributeError, TypeError):
                pass
            return circuit

    def get_provider_status(self) -> dict:
        with self._provider_lock:
            mode = self._provider_mode
            provider = self.provider
            fallback_enabled = self._fallback_enabled
            fallback_mode = self._fallback_mode
            fallback_provider = self._fallback_provider
        primary_blocked = (
            self._provider_circuit(provider).blocked_reason()
            if provider is not None
            else ""
        )
        primary_available = (
            bool(getattr(provider, 'available', True)) and not primary_blocked
        )
        fallback_available = bool(
            fallback_enabled
            and fallback_provider is not None
            and getattr(fallback_provider, 'available', True)
            and not self._provider_circuit(fallback_provider).blocked_reason()
        )
        reason = getattr(provider, 'unavailable_reason', '')
        if primary_blocked:
            reason = f"{provider.name} paused: {primary_blocked}."
        if not primary_available and fallback_available:
            reason = (
                f"{provider.name} is unavailable; consented "
                f"{fallback_provider.name} fallback is ready."
            )
        status = {
            'mode': mode,
            'name': provider.name,
            'available': primary_available or fallback_available,
            'reason': reason,
        }
        if fallback_enabled:
            status['fallback'] = {
                'enabled': True,
                'mode': fallback_mode,
                'name': (
                    fallback_provider.name
                    if fallback_provider is not None
                    else 'Unavailable'
                ),
                'available': fallback_available,
                'primary_available': primary_available,
            }
        return status

    def get_provider_usage(self, *, refresh: bool = True) -> dict:
        """Return primary and consented-fallback usage snapshots.

        Refreshes run on the scheduler's dedicated usage thread.  In
        particular, a Google-primary configuration still exposes the exact
        DeepL quota consumed by its consented fallback.
        """

        with self._provider_lock:
            mode = self._provider_mode
            provider = self.provider
            fallback_enabled = self._fallback_enabled
            fallback_mode = self._fallback_mode
            fallback_provider = self._fallback_provider

        snapshot = self._provider_usage_snapshot(provider, mode, refresh=refresh)
        if fallback_enabled:
            snapshot['fallback'] = self._provider_usage_snapshot(
                fallback_provider,
                fallback_mode,
                refresh=refresh,
            )
        return snapshot

    @staticmethod
    def _provider_usage_snapshot(
        provider: TranslationProvider | None,
        mode: str,
        *,
        refresh: bool,
    ) -> dict:
        if provider is None:
            return {'provider': mode, 'available': False}
        get_usage = getattr(provider, 'get_usage', None)
        if not callable(get_usage):
            return {'provider': mode, 'available': False}
        try:
            usage = get_usage(refresh=refresh)
        except Exception as exc:
            logger.error("Provider usage failed: %s", type(exc).__name__)
            usage = {'available': False, 'error': True}
        snapshot = dict(usage) if isinstance(usage, Mapping) else {}
        snapshot['provider'] = mode
        return snapshot

    def set_config(self, config: dict):
        """Apply translation settings without enabling a cloud provider implicitly."""
        target_lang = _normalize_language_code(config.get('target_language', 'en'))
        if target_lang != self._target_lang:
            self._target_lang = target_lang
            self.glossary = self._get_glossary('zh', target_lang)
            self._clear_translation_cache()

        provider_mode = resolve_translation_provider(config)
        self._configure_providers(
            provider_mode,
            config.get('deepl_api_key', ''),
            fallback_enabled=config.get('provider_fallback_enabled') is True,
        )

    def set_translation_memory(self, translation_memory: Any) -> None:
        self.translation_memory = translation_memory
        self._clear_translation_cache()

    def set_entity_index(self, entity_index: Any) -> None:
        # Every config apply re-sends the index. Cache keys already include
        # the entity revision, so only a real change needs to drop the caches
        # (rebuilding a lexicon matcher costs 40-80 ms).
        if entity_index is getattr(self, 'entity_index', None):
            return
        self.entity_index = entity_index
        self._clear_translation_cache()

    def request_cancel_pending(self) -> None:
        """Thread-safely stop waiting for primary and fallback requests."""
        with self._provider_lock:
            providers = (self.provider, self._fallback_provider)
        seen: set[int] = set()
        for provider in providers:
            if provider is None or id(provider) in seen:
                continue
            seen.add(id(provider))
            request_cancel = getattr(provider, 'request_cancel', None)
            if callable(request_cancel):
                request_cancel()

    def set_doctrine_manager(self, doctrine_manager: Any) -> None:
        self.doctrine_manager = doctrine_manager
        self._clear_translation_cache()

    def set_doctrine_pack(self, doctrine_pack: Any) -> None:
        current = getattr(self, 'doctrine_pack', None)
        if doctrine_pack is current:
            return
        current_revision = self._doctrine_revision(current)
        self.doctrine_pack = doctrine_pack
        if current_revision and current_revision == self._doctrine_revision(doctrine_pack):
            # The same pack reloaded from disk by a config apply.
            return
        self._clear_translation_cache()

    def set_fallback_authorizer(self, authorizer: Callable[[], bool] | None) -> None:
        """Install a scheduler-owned consent-generation check for fallback."""

        self._fallback_authorizer = authorizer if callable(authorizer) else None

    def _fallback_authorized(self) -> bool:
        authorizer = getattr(self, '_fallback_authorizer', None)
        if authorizer is None:
            return True
        try:
            return bool(authorizer())
        except Exception as exc:
            logger.error("Fallback authorization check failed: %s", type(exc).__name__)
            return False

    @staticmethod
    def _normalized_provider_mode(provider_mode: str) -> str:
        normalized_mode = (
            provider_mode.strip().lower()
            if isinstance(provider_mode, str)
            else TRANSLATION_PROVIDER_DISABLED
        )
        if normalized_mode not in TRANSLATION_PROVIDER_MODES:
            normalized_mode = TRANSLATION_PROVIDER_DISABLED
        return normalized_mode

    def _build_provider(
        self,
        normalized_mode: str,
        normalized_key: str,
    ) -> TranslationProvider:
        if normalized_mode == TRANSLATION_PROVIDER_DISABLED:
            return DisabledTranslationProvider()
        if normalized_mode == TRANSLATION_PROVIDER_DEEPL and not normalized_key:
            return UnavailableTranslationProvider(
                "DeepL", "DeepL API key is required."
            )
        try:
            factory_key = (
                normalized_key
                if normalized_mode == TRANSLATION_PROVIDER_DEEPL
                else ""
            )
            return self._provider_factories[normalized_mode](factory_key)
        except Exception as exc:
            logger.error(
                "Could not initialize %s provider: %s",
                normalized_mode,
                type(exc).__name__,
            )
            display_name = (
                "DeepL"
                if normalized_mode == TRANSLATION_PROVIDER_DEEPL
                else "Google"
            )
            return UnavailableTranslationProvider(
                display_name,
                f"{display_name} could not be initialized.",
            )

    def _configure_providers(
        self,
        provider_mode: str,
        deepl_api_key: str = "",
        *,
        fallback_enabled: bool = False,
    ) -> None:
        normalized_mode = self._normalized_provider_mode(provider_mode)
        normalized_key = _normalize_api_key(deepl_api_key)
        fallback_enabled = bool(
            fallback_enabled
            and normalized_mode
            in {TRANSLATION_PROVIDER_GOOGLE, TRANSLATION_PROVIDER_DEEPL}
        )
        fallback_mode = TRANSLATION_PROVIDER_DISABLED
        if fallback_enabled:
            fallback_mode = (
                TRANSLATION_PROVIDER_DEEPL
                if normalized_mode == TRANSLATION_PROVIDER_GOOGLE
                else TRANSLATION_PROVIDER_GOOGLE
            )

        with self._provider_lock:
            current_mode = self._provider_mode
            current_key = self._deepl_api_key
            current_provider = self.provider
            current_fallback_mode = self._fallback_mode
            current_fallback = self._fallback_provider

        primary_key_changed = (
            normalized_mode == TRANSLATION_PROVIDER_DEEPL
            and normalized_key != current_key
        )
        reuse_primary = (
            normalized_mode == current_mode
            and not primary_key_changed
            and (
                normalized_mode == TRANSLATION_PROVIDER_DISABLED
                or bool(getattr(current_provider, 'available', True))
            )
        )
        provider = (
            current_provider
            if reuse_primary
            else self._build_provider(normalized_mode, normalized_key)
        )

        fallback_key_changed = (
            fallback_mode == TRANSLATION_PROVIDER_DEEPL
            and normalized_key != current_key
        )
        reuse_fallback = (
            fallback_enabled
            and fallback_mode == current_fallback_mode
            and current_fallback is not None
            and not fallback_key_changed
            and bool(getattr(current_fallback, 'available', True))
        )
        fallback_provider = None
        if fallback_enabled:
            fallback_provider = (
                current_fallback
                if reuse_fallback
                else self._build_provider(fallback_mode, normalized_key)
            )

        logger.info("Translation provider mode changed to %s", normalized_mode)
        with self._provider_lock:
            self.provider = provider
            self._provider_mode = normalized_mode
            self._deepl_api_key = normalized_key
            self._fallback_enabled = fallback_enabled
            self._fallback_mode = fallback_mode
            self._fallback_provider = fallback_provider
        self._clear_translation_cache()

    def set_provider(self, provider_mode: str, deepl_api_key: str = ""):
        self._configure_providers(provider_mode, deepl_api_key)

    def test_provider(
        self,
        provider_mode: str,
        deepl_api_key: str = "",
        target_lang: str = "en",
    ) -> ProviderTestResult:
        """Test a provider without changing the active service configuration."""
        normalized_mode = (
            provider_mode.strip().lower()
            if isinstance(provider_mode, str)
            else TRANSLATION_PROVIDER_DISABLED
        )
        if normalized_mode not in TRANSLATION_PROVIDER_MODES:
            normalized_mode = TRANSLATION_PROVIDER_DISABLED

        if normalized_mode == TRANSLATION_PROVIDER_DISABLED:
            return ProviderTestResult(
                False,
                "Disabled",
                "Select Google or DeepL before testing.",
            )

        normalized_key = _normalize_api_key(deepl_api_key)
        if normalized_mode == TRANSLATION_PROVIDER_DEEPL and not normalized_key:
            return ProviderTestResult(False, "DeepL", "Enter a DeepL API key first.")

        display_name = "DeepL" if normalized_mode == TRANSLATION_PROVIDER_DEEPL else "Google"
        try:
            factory_key = (
                normalized_key if normalized_mode == TRANSLATION_PROVIDER_DEEPL else ""
            )
            test_provider = self._provider_factories[normalized_mode](factory_key)
        except Exception as exc:
            logger.error(
                "Provider test initialization failed for %s: %s",
                normalized_mode,
                type(exc).__name__,
            )
            return ProviderTestResult(
                False,
                display_name,
                f"{display_name} could not be initialized. Check its configuration.",
            )

        normalized_target = _normalize_language_code(target_lang)
        if normalized_target.startswith('en'):
            test_text, source_lang = "Verbindungstest", "de"
        else:
            test_text, source_lang = "Connection test", "en"

        try:
            translated = test_provider.translate(test_text, normalized_target, source_lang)
        except Exception as exc:
            logger.error(
                "Provider test request failed for %s: %s",
                normalized_mode,
                type(exc).__name__,
            )
            translated = None
        if not translated:
            return ProviderTestResult(
                False,
                display_name,
                f"{display_name} did not complete the test. Check credentials and network access.",
            )
        return ProviderTestResult(
            True,
            display_name,
            f"{display_name} translated a test message successfully.",
        )

    @staticmethod
    def _glossary_source_code(source_lang: str) -> str:
        normalized = _normalize_language_code(source_lang, default='auto')
        # langdetect reports regional Chinese variants while the bundled EVE
        # glossaries intentionally use the common ``zh`` key. The same base
        # language fallback also makes importable pair glossaries predictable.
        return normalized.split('-', 1)[0]

    def _get_glossary(self, source_lang: str, target_lang: str) -> EVEGlossary:
        source = self._glossary_source_code(source_lang)
        target = _normalize_language_code(target_lang)
        pair = (source, target)
        glossary = self._glossaries.get(pair)
        if glossary is None:
            glossary = self._glossary_factory(
                source_lang=source,
                target_lang=target,
            )
            self._glossaries[pair] = glossary
            while len(self._glossaries) > GLOSSARY_CACHE_SIZE:
                expired_pair, _ = self._glossaries.popitem(last=False)
                self._glossary_revisions.pop(expired_pair, None)
                for key in tuple(self._lexicon_matchers):
                    if key and key[0] == expired_pair:
                        self._lexicon_matchers.pop(key, None)
        else:
            self._glossaries.move_to_end(pair)
            if pair in self._glossary_revisions:
                self._glossary_revisions.move_to_end(pair)
        return glossary

    @staticmethod
    def _mapping_digest(mapping: Mapping[Any, Any]) -> str:
        digest = hashlib.sha256()
        for key in sorted(mapping, key=lambda value: str(value)):
            digest.update(str(key).encode('utf-8', errors='replace'))
            digest.update(b'\0')
            digest.update(str(mapping[key]).encode('utf-8', errors='replace'))
            digest.update(b'\0')
        return digest.hexdigest()[:16]

    def _glossary_revision(
        self,
        pair: tuple[str, str],
        glossary: EVEGlossary,
    ) -> str:
        revision = self._glossary_revisions.get(pair)
        if revision is None:
            terms = getattr(glossary, 'terms', {})
            revision = self._mapping_digest(terms if isinstance(terms, Mapping) else {})
            self._glossary_revisions[pair] = revision
        else:
            self._glossary_revisions.move_to_end(pair)
        return revision

    def _active_doctrine(self) -> Any:
        if self.doctrine_pack is not None:
            return self.doctrine_pack
        manager = self.doctrine_manager
        if manager is None:
            return None
        getter = getattr(manager, 'get_active_pack', None)
        if callable(getter):
            try:
                return getter()
            except Exception as exc:
                logger.warning("Could not read active doctrine: %s", type(exc).__name__)
                return None
        for attribute in ('active_pack', 'active_doctrine', 'active'):
            try:
                active = getattr(manager, attribute, None)
            except Exception as exc:
                logger.warning("Could not read active doctrine: %s", type(exc).__name__)
                return None
            if active is not None:
                return active
        # A DoctrinePack can be supplied through the manager seam directly.
        if callable(getattr(manager, 'terms_for', None)):
            return manager
        return None

    def _doctrine_scope(self) -> str:
        doctrine = self._active_doctrine()
        if doctrine is None:
            return ""
        for attribute in ('scope', 'pack_id', 'id', 'name'):
            value = getattr(doctrine, attribute, None)
            if isinstance(value, str) and value.strip():
                return value.strip()
        return ""

    def _doctrine_revision(self, doctrine: Any) -> str:
        if doctrine is None:
            return ""
        for attribute in ('revision', 'digest', 'version'):
            value = getattr(doctrine, attribute, None)
            if value is not None and str(value).strip():
                return str(value).strip()
        return ""

    def _doctrine_memo_get(self, key, compute):
        """Memoize doctrine-derived data per doctrine revision.

        A large pack (thousands of terms/names) was re-filtered and re-sorted
        for every uncached message.
        """
        memo = self.__dict__.setdefault('_doctrine_memo', {})
        if key in memo:
            return memo[key]
        value = compute()
        if len(memo) > 64:
            memo.clear()
        memo[key] = value
        return value

    def _doctrine_terms(self, source_lang: str, target_lang: str) -> dict[str, str]:
        doctrine = self._active_doctrine()
        if doctrine is None:
            return {}
        source = _normalize_language_code(source_lang, default='auto')
        target = _normalize_language_code(target_lang)
        key = ('terms', id(doctrine), self._doctrine_revision(doctrine), source, target)
        return self._doctrine_memo_get(
            key,
            lambda: self._doctrine_terms_uncached(doctrine, source, target),
        )

    def _doctrine_terms_uncached(self, doctrine, source: str, target: str) -> dict[str, str]:
        for method_name in ('terms_for', 'get_terms', 'glossary_for'):
            method = getattr(doctrine, method_name, None)
            if not callable(method):
                continue
            candidates = [(source, target)]
            base_pair = (source.split('-', 1)[0], target.split('-', 1)[0])
            if base_pair not in candidates:
                candidates.append(base_pair)
            for pair_source, pair_target in candidates:
                try:
                    terms = method(pair_source, pair_target)
                except Exception as exc:
                    logger.warning("Could not read doctrine terms: %s", type(exc).__name__)
                    return {}
                if isinstance(terms, Mapping) and terms:
                    return {
                        key: value
                        for key, value in terms.items()
                        if isinstance(key, str) and key and isinstance(value, str) and value
                    }
            return {}

        terms = getattr(doctrine, 'terms', {})
        if not isinstance(terms, Mapping):
            return {}
        if all(isinstance(value, str) for value in terms.values()):
            return {
                key: value
                for key, value in terms.items()
                if isinstance(key, str) and key and isinstance(value, str) and value
            }
        for pair_key in (
            (source, target),
            f"{source}_{target}",
            f"{source}-{target}",
        ):
            pair_terms = terms.get(pair_key)
            if isinstance(pair_terms, Mapping):
                return {
                    key: value
                    for key, value in pair_terms.items()
                    if isinstance(key, str) and key and isinstance(value, str) and value
                }
        return {}

    def _doctrine_protected_names(self) -> tuple[str, ...]:
        doctrine = self._active_doctrine()
        if doctrine is None:
            return ()
        key = ('names', id(doctrine), self._doctrine_revision(doctrine))
        return self._doctrine_memo_get(
            key,
            lambda: _SortedNames.of(self._doctrine_protected_names_uncached(doctrine)),
        )

    @staticmethod
    def _doctrine_protected_names_uncached(doctrine) -> tuple[str, ...]:
        for attribute in ('protected_names', 'protected_entities'):
            names = getattr(doctrine, attribute, None)
            if isinstance(names, Mapping):
                names = names.keys()
            if isinstance(names, Sequence) and not isinstance(names, (str, bytes)):
                return tuple(
                    name.strip()
                    for name in names
                    if isinstance(name, str) and name.strip()
                )
        return ()

    @staticmethod
    def _protect_named_entities(
        text: str,
        names: Sequence[str],
    ) -> tuple[str, dict[str, str]]:
        protected = text
        tokens: dict[str, str] = {}
        ordered = (
            names
            if isinstance(names, _SortedNames)
            else sorted(set(names), key=len, reverse=True)
        )
        for name in ordered:
            if not name or name not in protected:
                continue
            starts_cjk = '\u3400' <= name[0] <= '\u9fff'
            ends_cjk = '\u3400' <= name[-1] <= '\u9fff'
            prefix = (
                NOT_AFTER_NON_CJK_WORD
                if not starts_cjk and (name[0].isalnum() or name[0] == '_')
                else ''
            )
            suffix = (
                NOT_BEFORE_NON_CJK_WORD
                if not ends_cjk and (name[-1].isalnum() or name[-1] == '_')
                else ''
            )
            pattern = re.compile(f"{prefix}{re.escape(name)}{suffix}")

            def replace(match):
                placeholder = f"__EVEDOCTRINE_{len(tokens) + 1}__"
                while placeholder in protected or placeholder in tokens:
                    placeholder = f"_{placeholder}_"
                tokens[placeholder] = match.group(0)
                return placeholder

            protected = pattern.sub(replace, protected)
        return protected, tokens

    def _protect_sde_entities(self, text: str) -> tuple[str, dict[str, str]]:
        index = self.entity_index
        if index is None or getattr(index, 'available', True) is False:
            return text, {}
        protect = getattr(index, 'protect', None)
        if not callable(protect):
            return text, {}
        try:
            result = protect(text)
            protected_text = getattr(result, 'text', None)
            tokens = getattr(result, 'tokens', None)
            if isinstance(protected_text, str) and isinstance(tokens, Mapping):
                return protected_text, {
                    key: value
                    for key, value in tokens.items()
                    if isinstance(key, str) and isinstance(value, str)
                }
        except Exception as exc:
            logger.warning("Could not protect SDE entities: %s", type(exc).__name__)
        return text, {}

    def _lexicon_context(
        self,
        source_lang: str,
        target_lang: str,
        glossary: EVEGlossary,
    ) -> tuple:
        pair = (
            self._glossary_source_code(source_lang),
            _normalize_language_code(target_lang),
        )
        doctrine = self._active_doctrine()
        if doctrine is not None:
            # Doctrine terms are looked up by the exact regional pair first
            # (zh-tw before zh), so the matcher key must not merge them.
            pair = (
                _normalize_language_code(source_lang, default='auto'),
                pair[1],
            )
        entity = self.entity_index
        entity_metadata = getattr(entity, 'metadata', {})
        if not isinstance(entity_metadata, Mapping):
            entity_metadata = {}
        entity_revision = str(
            entity_metadata.get('asset_sha256')
            or getattr(entity, 'revision', '')
            or getattr(entity, 'sde_version', '')
            or ''
        )
        doctrine_revision = self._doctrine_revision(doctrine)
        if doctrine is not None and not doctrine_revision:
            doctrine_revision = f"object:{id(doctrine)}"
        return (
            pair,
            self._glossary_revision(pair, glossary),
            self._doctrine_scope(),
            doctrine_revision,
            entity_revision,
            int(getattr(entity, 'entity_count', 0) or 0),
            id(entity) if entity is not None else 0,
        )

    def _lexicon_matcher(
        self,
        source_lang: str,
        target_lang: str,
        glossary: EVEGlossary,
        doctrine_terms: Mapping[str, str],
    ) -> LongestLexiconMatcher:
        entity_index = self.entity_index
        key = self._lexicon_context(source_lang, target_lang, glossary)
        matcher = self._lexicon_matchers.get(key)
        if matcher is not None:
            self._lexicon_matchers.move_to_end(key)
            return matcher

        entity_terms: Mapping[str, str] = {}
        translations_for = getattr(entity_index, 'lexicon_terms_for', None)
        if not callable(translations_for):
            translations_for = getattr(entity_index, 'translations_for', None)
        if callable(translations_for):
            try:
                candidate = translations_for(source_lang, target_lang)
                if isinstance(candidate, Mapping):
                    entity_terms = candidate
            except Exception as exc:
                logger.warning("Could not read SDE entity translations: %s", type(exc).__name__)

        glossary_terms = getattr(glossary, 'terms', {})
        if not isinstance(glossary_terms, Mapping):
            glossary_terms = {}
        matcher = LongestLexiconMatcher([
            *(
                LexiconTerm(source, target, 0)
                for source, target in entity_terms.items()
            ),
            *(
                LexiconTerm(source, target, 10)
                for source, target in glossary_terms.items()
                if isinstance(source, str) and isinstance(target, str)
            ),
            *(
                LexiconTerm(source, target, 20)
                for source, target in doctrine_terms.items()
            ),
        ])
        self._lexicon_matchers[key] = matcher
        while len(self._lexicon_matchers) > LEXICON_MATCHER_CACHE_SIZE:
            self._lexicon_matchers.popitem(last=False)
        return matcher

    def _clear_translation_cache(self):
        self._translation_cache.clear()
        self._lexicon_matchers.clear()
        self.__dict__.get('_doctrine_memo', {}).clear()

    def _cache_key(
        self,
        message: str,
        target_lang: str,
        source_lang: str = None,
        lexicon_context: tuple | None = None,
        context: str = None,
    ):
        normalized_target = _normalize_language_code(target_lang)
        normalized_source = _normalize_language_code(source_lang, default='auto')
        with self._provider_lock:
            provider_mode = self._provider_mode
            provider = self.provider
            fallback_mode = self._fallback_mode
            fallback_provider = self._fallback_provider
            fallback_enabled = self._fallback_enabled
        context_fingerprint = ""
        if (
            isinstance(context, str)
            and context.strip()
            and (
                bool(getattr(provider, 'supports_context', False))
                or bool(
                    fallback_enabled
                    and fallback_provider is not None
                    and getattr(fallback_provider, 'supports_context', False)
                )
            )
        ):
            context_fingerprint = hashlib.sha256(
                context.strip().encode('utf-8', errors='replace')
            ).hexdigest()[:16]
        return (
            provider_mode,
            fallback_mode if fallback_enabled else '',
            normalized_target,
            normalized_source,
            lexicon_context,
            context_fingerprint,
            message,
        )

    def _get_cached_translation(self, key):
        if key not in self._translation_cache:
            return None
        cached = self._translation_cache.pop(key)
        self._translation_cache[key] = cached
        if isinstance(cached, _CachedTranslation):
            return cached
        # Compatibility with pre-feature tests and any in-process cache value.
        return _CachedTranslation(str(cached), self.provider_name)

    def _store_cached_translation(
        self,
        key,
        translated: str,
        provider_name: str = "",
    ):
        self._translation_cache[key] = _CachedTranslation(
            translated,
            provider_name or self.provider_name,
        )
        if len(self._translation_cache) > TRANSLATION_CACHE_SIZE:
            self._translation_cache.popitem(last=False)

    def _lookup_correction(
        self,
        message: str,
        source_lang: str,
        target_lang: str,
    ) -> str | None:
        memory = self.translation_memory
        lookup = getattr(memory, 'lookup', None)
        scope = self._doctrine_scope()
        corrected = None
        if callable(lookup):
            try:
                corrected = lookup(message, source_lang, target_lang, scope)
            except Exception as exc:
                logger.warning(
                    "Could not read local correction memory: %s",
                    type(exc).__name__,
                )
        if isinstance(corrected, str) and corrected.strip():
            return corrected

        doctrine = self._active_doctrine()
        doctrine_lookup = getattr(doctrine, 'correction_for', None)
        if callable(doctrine_lookup):
            try:
                corrected = doctrine_lookup(
                    message,
                    source_lang,
                    target_lang,
                )
            except Exception as exc:
                logger.warning(
                    "Could not read doctrine correction: %s",
                    type(exc).__name__,
                )
                return None
            if isinstance(corrected, str) and corrected.strip():
                return corrected
        return None

    def _prepare_translation(
        self,
        message: str,
        target_lang: str,
        source_lang: str,
        context: str = None,
        cache_key: tuple | None = None,
    ) -> _PreparedTranslation:
        glossary = self._get_glossary(source_lang, target_lang)
        self.glossary = glossary
        doctrine_terms = self._doctrine_terms(source_lang, target_lang)
        doctrine_names = self._doctrine_protected_names()

        # Explicit doctrine protection wins first. Longest official SDE names
        # are canonicalized before literal CJK shorthand replacements, avoiding
        # corrupt hybrids such as ``灾难级`` -> ``Apocalypse 级``. Doctrine and
        # bundled YAML then handle only the remaining aliases/slang, after which
        # every target-locale EVE name is hidden from the provider.
        preprocessed, doctrine_tokens = self._protect_named_entities(
            message,
            doctrine_names,
        )
        preprocessed = self._lexicon_matcher(
            source_lang,
            target_lang,
            glossary,
            doctrine_terms,
        ).replace(preprocessed)
        preprocessed, sde_tokens = self._protect_sde_entities(preprocessed)
        tokens = {**doctrine_tokens, **sde_tokens}
        if cache_key is None:
            lexicon_context = self._lexicon_context(
                source_lang,
                target_lang,
                glossary,
            )
            cache_key = self._cache_key(
                message,
                target_lang,
                source_lang,
                lexicon_context,
                context,
            )
        return _PreparedTranslation(
            original=message,
            request_text=preprocessed,
            tokens=tokens,
            cache_key=cache_key,
        )

    def _message_cache_key(
        self,
        message: str,
        target_lang: str,
        source_lang: str,
        context: str = None,
    ) -> tuple:
        """Build a cheap stable key before any lexicon/entity preprocessing."""

        glossary = self._get_glossary(source_lang, target_lang)
        self.glossary = glossary
        return self._cache_key(
            message,
            target_lang,
            source_lang,
            self._lexicon_context(source_lang, target_lang, glossary),
            context,
        )

    def _local_only_translation(self, prepared: _PreparedTranslation) -> str | None:
        """Resolve lines that are only protected names, without a provider.

        SDE names were already mapped to the target locale during
        preparation, so "加达里海军雷神重型导弹*" needs no cloud request.
        """
        if _has_text_to_translate(prepared.request_text):
            return None
        return self._restore_protected_entities(
            prepared.request_text,
            prepared.tokens,
        ) if prepared.tokens else prepared.request_text

    def _restore_or_note_failure(
        self,
        translated: Any,
        tokens: Mapping[str, str],
    ) -> str | None:
        """Restore markers and record why a provider response was discarded."""
        restored = self._restore_protected_entities(translated, tokens)
        if restored is None and isinstance(translated, str) and translated.strip():
            self.last_failure_reason = FAILURE_PROTECTED_NAMES
        return restored

    @staticmethod
    def _restore_protected_entities(
        translated: Any,
        tokens: Mapping[str, str],
    ) -> str | None:
        """Restore provider-safe markers only when every identity is unique.

        Providers sometimes change marker case, wrapper underscore counts, or
        the separator before the ordinal. Those cosmetic mutations are safe to
        recover because the reserved family and ordinal still identify exactly
        one original value. Missing, duplicated, embedded, or otherwise
        ambiguous markers return ``None``. Callers then discard the complete
        provider response and show the untouched original; partial restoration
        is deliberately never accepted.
        """

        if not isinstance(translated, str) or not translated.strip():
            return None
        restored = translated
        for placeholder, original in tokens.items():
            token_match = re.fullmatch(
                r"(_+)(EVE(?:DOCTRINE|ENTITY))_(\d+)(_+)",
                placeholder,
                flags=re.IGNORECASE,
            )
            if token_match is None:
                exact_count = restored.count(placeholder)
                if exact_count != 1:
                    logger.warning("Provider changed a protected EVE entity placeholder")
                    return None
                restored = restored.replace(placeholder, original, 1)
                continue
            _leading, family, number, _trailing = token_match.groups()

            underscore = r"[_\uff3f]"
            leading_wrapper = rf"(?:{underscore}+(?:\s*{underscore}+)*\s*)?"
            ordinal_separator = rf"(?:\s*{underscore}+\s*|\s*-\s*|\s+)?"
            trailing_wrapper = rf"(?:\s*{underscore}+(?:\s*{underscore}+)*)?"

            # CJK text has no spaces, so providers glue markers to the
            # neighbouring characters ("主要目标__EVEENTITY_1__在网格上").
            # Only Latin/number neighbours make a marker embedded.
            flexible_pattern = re.compile(
                NOT_AFTER_NON_CJK_WORD
                + leading_wrapper
                + re.escape(family)
                + ordinal_separator
                + re.escape(number)
                + trailing_wrapper
                + NOT_BEFORE_NON_CJK_WORD,
                flags=re.IGNORECASE,
            )
            matches = list(flexible_pattern.finditer(restored))
            if len(matches) != 1:
                logger.warning("Provider changed a protected EVE entity placeholder")
                return None
            match = matches[0]
            restored = restored[:match.start()] + original + restored[match.end():]
        return restored

    def _translate_via_provider(
        self,
        text: str,
        target_lang: str,
        source_lang: str,
        context: str = None,
        *,
        skip_primary: bool = False,
    ) -> tuple[Optional[str], str]:
        with self._provider_lock:
            primary = self.provider
            fallback = self._fallback_provider if self._fallback_enabled else None
        providers = [fallback] if skip_primary else [primary, fallback]
        last_provider_name = primary.name
        for provider in providers:
            if provider is None:
                continue
            if provider is fallback and not self._fallback_authorized():
                logger.info(
                    "Fallback skipped because provider consent/config changed"
                )
                break
            last_provider_name = provider.name
            if not bool(getattr(provider, 'available', True)):
                # Disabled/unconfigured providers and pending timeouts are
                # not transient call failures; report them as they are.
                self.last_failure_reason = str(
                    getattr(provider, 'unavailable_reason', '') or 'unavailable'
                )
                if isinstance(provider, (DisabledTranslationProvider, UnavailableTranslationProvider)):
                    continue
            circuit = self._provider_circuit(provider)
            blocked_reason = circuit.blocked_reason()
            if blocked_reason:
                self.last_failure_reason = blocked_reason
                continue
            try:
                if (
                    isinstance(context, str)
                    and context.strip()
                    and bool(getattr(provider, 'supports_context', False))
                ):
                    translated = provider.translate(
                        text,
                        target_lang,
                        source_lang,
                        context=context.strip(),
                    )
                else:
                    translated = provider.translate(text, target_lang, source_lang)
            except Exception as exc:
                logger.error(
                    "%s translation provider failed: %s",
                    provider.name,
                    type(exc).__name__,
                )
                translated = None
                failure_reason = describe_provider_failure(exc)
            else:
                failure_reason = ""
            if isinstance(translated, str) and translated.strip():
                circuit.record_success()
                self.last_failure_reason = ""
                if provider is fallback:
                    logger.warning(
                        "Primary translation failed; explicit fallback used %s",
                        provider.name,
                    )
                return translated, provider.name
            if not failure_reason:
                provider_reason = getattr(provider, 'last_failure_reason', '')
                failure_reason = (
                    provider_reason
                    if isinstance(provider_reason, str) and provider_reason
                    else "no translation returned"
                )
            circuit.record_failure(failure_reason)
            self.last_failure_reason = failure_reason
        return None, last_provider_name

    def _translate_prepared(
        self,
        prepared: _PreparedTranslation,
        target_lang: str,
        source_lang: str,
        context: str = None,
    ) -> tuple[str, bool, str]:
        cached = self._get_cached_translation(prepared.cache_key)
        if cached is not None:
            return cached.text, True, cached.provider_name
        local = self._local_only_translation(prepared)
        if local is not None:
            return local, True, "Local"
        translated, provider_name = self._translate_via_provider(
            prepared.request_text,
            target_lang,
            source_lang,
            context,
        )
        restored = self._restore_or_note_failure(translated, prepared.tokens)
        if restored is None:
            with self._provider_lock:
                fallback_available = (
                    self._fallback_enabled and self._fallback_provider is not None
                )
                primary_name = self.provider.name
            if fallback_available and provider_name == primary_name:
                translated, provider_name = self._translate_via_provider(
                    prepared.request_text,
                    target_lang,
                    source_lang,
                    context,
                    skip_primary=True,
                )
                restored = self._restore_or_note_failure(
                    translated,
                    prepared.tokens,
                )
        if restored is not None:
            self._store_cached_translation(
                prepared.cache_key,
                restored,
                provider_name,
            )
            return restored, True, provider_name
        return prepared.original, False, provider_name

    def translate_message(
        self,
        message: str,
        target_lang: str = 'en',
        source_lang: str = None,
        context: str = None,
    ) -> tuple[str, bool, str]:
        """Translate a message while preserving the existing tuple result shape."""
        if not message.strip():
            return message, True, self.provider.name

        target_lang = _normalize_language_code(target_lang)
        source_lang = _normalize_language_code(source_lang, default='auto')
        corrected = self._lookup_correction(message, source_lang, target_lang)
        if corrected is not None:
            return corrected, True, "Local correction"
        if (
            _base_language_code(source_lang, 'auto')
            == _base_language_code(target_lang, 'unknown')
            and _base_language_code(source_lang, 'auto') not in {'auto', 'unknown'}
        ):
            return message, True, "Local"
        cache_key = self._message_cache_key(
            message,
            target_lang,
            source_lang,
            context,
        )
        cached = self._get_cached_translation(cache_key)
        if cached is not None:
            return cached.text, True, cached.provider_name
        prepared = self._prepare_translation(
            message,
            target_lang,
            source_lang,
            context,
            cache_key,
        )
        return self._translate_prepared(
            prepared,
            target_lang,
            source_lang,
            context,
        )

    def translate_batch_messages(
        self,
        messages: Sequence[str],
        target_lang: str = 'en',
        source_lang: str = None,
        context: str = None,
    ) -> list[tuple[str, bool, str]]:
        """Translate messages in order, using one real provider batch when supported.

        Cache hits and exact local corrections are resolved before the provider
        request. Invalid, partial, or mis-sized batch results fall back only to
        the affected single-message calls while preserving result order.
        """
        if isinstance(messages, str):
            message_list = [messages]
        else:
            message_list = list(messages)
        if not message_list:
            return []

        target = _normalize_language_code(target_lang)
        source = _normalize_language_code(source_lang, default='auto')
        results: list[tuple[str, bool, str] | None] = [None] * len(message_list)
        pending: list[tuple[int, _PreparedTranslation]] = []

        for index, message in enumerate(message_list):
            if not message.strip():
                results[index] = (message, True, self.provider.name)
                continue
            corrected = self._lookup_correction(message, source, target)
            if corrected is not None:
                results[index] = (corrected, True, "Local correction")
                continue
            if (
                _base_language_code(source, 'auto')
                == _base_language_code(target, 'unknown')
                and _base_language_code(source, 'auto') not in {'auto', 'unknown'}
            ):
                results[index] = (message, True, "Local")
                continue
            cache_key = self._message_cache_key(message, target, source, context)
            cached = self._get_cached_translation(cache_key)
            if cached is not None:
                results[index] = (cached.text, True, cached.provider_name)
                continue
            prepared = self._prepare_translation(
                message,
                target,
                source,
                context,
                cache_key,
            )
            local = self._local_only_translation(prepared)
            if local is not None:
                results[index] = (local, True, "Local")
                continue
            pending.append((index, prepared))

        batch_results: list[Any] | None = None
        batch_attempted = False
        # The provider itself failed (error, timeout, paused circuit), as
        # opposed to answering with a partial or mis-sized result. Resending
        # each line to the same provider would multiply the outage delay.
        batch_failed = False
        with self._provider_lock:
            primary_provider = self.provider
            fallback_configured = (
                self._fallback_enabled and self._fallback_provider is not None
            )
        if pending and bool(getattr(primary_provider, 'supports_batch', False)):
            translate_batch = getattr(primary_provider, 'translate_batch', None)
            circuit = self._provider_circuit(primary_provider)
            blocked_reason = circuit.blocked_reason()
            if blocked_reason and callable(translate_batch):
                batch_attempted = True
                batch_failed = True
                self.last_failure_reason = blocked_reason
            elif callable(translate_batch):
                batch_attempted = True
                failure_reason = ""
                try:
                    request_texts = [prepared.request_text for _, prepared in pending]
                    if (
                        isinstance(context, str)
                        and context.strip()
                        and bool(getattr(primary_provider, 'supports_context', False))
                    ):
                        response = translate_batch(
                            request_texts,
                            target,
                            source,
                            context=context.strip(),
                        )
                    else:
                        response = translate_batch(request_texts, target, source)
                    if (
                        isinstance(response, Sequence)
                        and not isinstance(response, (str, bytes))
                        and len(response) == len(pending)
                    ):
                        batch_results = list(response)
                        circuit.record_success()
                    elif response is None:
                        provider_reason = getattr(
                            primary_provider, 'last_failure_reason', ''
                        )
                        failure_reason = (
                            provider_reason
                            if isinstance(provider_reason, str) and provider_reason
                            else "no translation returned"
                        )
                    else:
                        logger.warning("Provider returned an invalid translation batch")
                except Exception as exc:
                    logger.error("Translation batch failed: %s", type(exc).__name__)
                    failure_reason = describe_provider_failure(exc)
                if failure_reason:
                    batch_failed = True
                    circuit.record_failure(failure_reason)
                    self.last_failure_reason = failure_reason

        for batch_index, (result_index, prepared) in enumerate(pending):
            translated = (
                batch_results[batch_index]
                if batch_results is not None
                else None
            )
            restored = self._restore_or_note_failure(translated, prepared.tokens)
            if restored is None:
                translated, provider_name = self._translate_via_provider(
                    prepared.request_text,
                    target,
                    source,
                    context,
                    skip_primary=batch_attempted
                    and (fallback_configured or batch_failed),
                )
                restored = self._restore_or_note_failure(translated, prepared.tokens)
            else:
                provider_name = primary_provider.name
            if restored is None:
                results[result_index] = (
                    prepared.original,
                    False,
                    provider_name,
                )
            else:
                self._store_cached_translation(
                    prepared.cache_key,
                    restored,
                    provider_name,
                )
                results[result_index] = (restored, True, provider_name)

        return [
            result
            if result is not None
            else (message_list[index], False, self.provider.name)
            for index, result in enumerate(results)
        ]

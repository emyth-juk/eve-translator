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

logger = logging.getLogger(__name__)
TRANSLATION_CACHE_SIZE = 512
GLOSSARY_CACHE_SIZE = 16
LEXICON_MATCHER_CACHE_SIZE = 32
PROVIDER_CALL_TIMEOUT_SECONDS = 12.0
DEEPL_CALL_TIMEOUT_SECONDS = 90.0
GOOGLE_CALL_TIMEOUT_SECONDS = 15.0
PROVIDER_USAGE_TIMEOUT_SECONDS = 20.0
PROVIDER_ORPHAN_COOLDOWN_SECONDS = 120.0
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
    @property
    @abstractmethod
    def name(self) -> str:
        pass

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

            return self._call_runner.run(request)
        except Exception as exc:
            # Provider exceptions can contain request details. Keep logs useful
            # without risking credential or message disclosure.
            logger.error("Translation Error (Google): %s", type(exc).__name__)
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

        self.translator = deepl.Translator(api_key)
        self._usage_translator = deepl.Translator(api_key)
        # The SDK applies bounded 10-second socket attempts plus retry backoff.
        # Keep our outer guard beyond that documented retry envelope so it does
        # not orphan a healthy SDK call after the old 12-second deadline.
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
        lang_map = {
            'en': 'EN-US',
            'gb': 'EN-GB',
            'pt': 'PT-PT',
            'pt-br': 'PT-BR',
        }
        target = lang_map.get(target_lang.lower(), target_lang.upper())
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
            return result.text
        except Exception as exc:
            logger.error("Translation Error (DeepL): %s", type(exc).__name__)
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
                return None
            return [
                result.text if isinstance(getattr(result, 'text', None), str) else None
                for result in results
            ]
        except Exception as exc:
            logger.error("Translation Error (DeepL batch): %s", type(exc).__name__)
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

    def get_provider_status(self) -> dict:
        with self._provider_lock:
            mode = self._provider_mode
            provider = self.provider
            fallback_enabled = self._fallback_enabled
            fallback_mode = self._fallback_mode
            fallback_provider = self._fallback_provider
        primary_available = bool(getattr(provider, 'available', True))
        fallback_available = bool(
            fallback_enabled
            and fallback_provider is not None
            and getattr(fallback_provider, 'available', True)
        )
        reason = getattr(provider, 'unavailable_reason', '')
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
        self.doctrine_pack = doctrine_pack
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

    def _doctrine_terms(self, source_lang: str, target_lang: str) -> dict[str, str]:
        doctrine = self._active_doctrine()
        if doctrine is None:
            return {}
        source = _normalize_language_code(source_lang, default='auto')
        target = _normalize_language_code(target_lang)
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
        for name in sorted(set(names), key=len, reverse=True):
            if not name or name not in protected:
                continue
            starts_cjk = '\u3400' <= name[0] <= '\u9fff'
            ends_cjk = '\u3400' <= name[-1] <= '\u9fff'
            prefix = (
                r'(?<!\w)'
                if not starts_cjk and (name[0].isalnum() or name[0] == '_')
                else ''
            )
            suffix = (
                r'(?!\w)'
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

            flexible_pattern = re.compile(
                r"(?<!\w)"
                + leading_wrapper
                + re.escape(family)
                + ordinal_separator
                + re.escape(number)
                + trailing_wrapper
                + r"(?!\w)",
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
            if isinstance(translated, str) and translated.strip():
                if provider is fallback:
                    logger.warning(
                        "Primary translation failed; explicit fallback used %s",
                        provider.name,
                    )
                return translated, provider.name
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
        translated, provider_name = self._translate_via_provider(
            prepared.request_text,
            target_lang,
            source_lang,
            context,
        )
        restored = self._restore_protected_entities(translated, prepared.tokens)
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
                restored = self._restore_protected_entities(
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
            pending.append((index, prepared))

        batch_results: list[Any] | None = None
        batch_attempted = False
        with self._provider_lock:
            primary_provider = self.provider
            fallback_configured = (
                self._fallback_enabled and self._fallback_provider is not None
            )
        if pending and bool(getattr(primary_provider, 'supports_batch', False)):
            translate_batch = getattr(primary_provider, 'translate_batch', None)
            if callable(translate_batch):
                batch_attempted = True
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
                    else:
                        logger.warning("Provider returned an invalid translation batch")
                except Exception as exc:
                    logger.error("Translation batch failed: %s", type(exc).__name__)

        for batch_index, (result_index, prepared) in enumerate(pending):
            translated = (
                batch_results[batch_index]
                if batch_results is not None
                else None
            )
            restored = self._restore_protected_entities(translated, prepared.tokens)
            if restored is None:
                translated, provider_name = self._translate_via_provider(
                    prepared.request_text,
                    target,
                    source,
                    context,
                    skip_primary=batch_attempted and fallback_configured,
                )
                restored = self._restore_protected_entities(translated, prepared.tokens)
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

import html
import itertools
import logging
import re
import threading
from collections import defaultdict, deque
from dataclasses import dataclass, replace

from PySide6.QtCore import QObject, QThread, QTimer, Signal, Slot, Qt

from src.app_config import TRANSLATION_PROVIDER_DISABLED, resolve_translation_provider
from src.core.combat import classify_commands, speaker_role
from src.core.languages import normalize_locale
from src.core.message import ProcessedMessage, TranslationState

logger = logging.getLogger(__name__)


class ConfigUpdatePending(RuntimeError):
    """Internal control flow used to defer work until new consent is applied."""


@dataclass(frozen=True)
class ConfigGenerationSnapshot:
    requested: int
    applied: int

    @property
    def pending(self) -> bool:
        return self.requested > self.applied


class ConfigGenerationGate:
    """Thread-safe requested/applied generation boundary for worker handoffs."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._requested = 0
        self._applied = 0

    def _snapshot_unlocked(self) -> ConfigGenerationSnapshot:
        return ConfigGenerationSnapshot(
            requested=self._requested,
            applied=self._applied,
        )

    def request_next(self) -> int:
        """Allocate the next generation for the analysis worker."""
        with self._lock:
            self._requested += 1
            return self._requested

    def request(self, generation: int) -> ConfigGenerationSnapshot:
        """Accept an externally allocated generation for the scheduler."""
        with self._lock:
            self._requested = max(self._requested, generation)
            return self._snapshot_unlocked()

    def apply(self, generation: int) -> ConfigGenerationSnapshot:
        """Mark a generation applied and return one atomic state snapshot."""
        with self._lock:
            self._applied = max(self._applied, generation)
            return self._snapshot_unlocked()

    def snapshot(self) -> ConfigGenerationSnapshot:
        with self._lock:
            return self._snapshot_unlocked()

    def pending(self) -> bool:
        return self.snapshot().pending


class WorkerSignals(QObject):
    message_ready = Signal(object)
    translation_requested = Signal(object)


@dataclass(frozen=True)
class TranslationJob:
    message_id: str
    session_id: str
    source_id: str
    source_text: str
    final_original: str
    sender: str
    timestamp: str
    detected_language: str
    target_language: str
    tokens: dict[str, str]
    commands: tuple = ()
    speaker_role: str = ""
    config_generation: int = 0
    revision: int = 1


def restore_with_highlight(text, tokens):
    safe_text = html.escape(text or "")
    for placeholder, original in tokens.items():
        safe_original = html.escape(original)
        highlighted = f"<span style='color: yellow;'>{safe_original}</span>"
        safe_text = safe_text.replace(placeholder, highlighted)
    return safe_text


class LogProcessingWorker(QObject):
    """
    Shared worker that processes lines from all sessions and emits session-routed results.
    """

    def __init__(
        self,
        parser,
        tokenizer,
        detector,
        translator_service,
        *,
        defer_translation: bool = False,
    ):
        super().__init__()
        self.parser = parser
        self.tokenizer = tokenizer
        self.detector = detector
        self.translator_service = translator_service
        self.defer_translation = bool(defer_translation)
        self.signals = WorkerSignals()
        self.config = {'ignored_languages': ['en'], 'target_language': 'en'}
        self._message_counter = itertools.count(1)
        self._config_gate = ConfigGenerationGate()
        self._deferred_batches = deque()
        self.doctrine_pack = None

    def request_config_update(self) -> int:
        """Atomically stop authorizing new provider calls.

        This method only touches a lock-protected integer, so the GUI thread may
        call it directly without mutating worker-owned provider state.
        """
        return self._config_gate.request_next()

    def _config_update_pending(self) -> bool:
        return self._config_gate.pending()

    @Slot(dict)
    def update_config(self, config):
        """Apply an initial/direct config when no queued generation is involved."""
        self._apply_config(config)

    @Slot(int, dict)
    def apply_queued_config(self, generation, config):
        self._apply_config(config)
        generation_state = self._config_gate.apply(generation)

        if generation_state.pending:
            return

        deferred_batches = list(self._deferred_batches)
        self._deferred_batches.clear()
        for session_id, source_id, lines in deferred_batches:
            self.process_lines(session_id, source_id, lines)

    def _apply_config(self, config):
        self.config = config.copy() if isinstance(config, dict) else {}
        target_lang = self.config.get('target_language', 'en')
        self.config['target_language'] = normalize_locale(target_lang, 'en')
        ignored_languages = self.config.get('ignored_languages', ['en'])
        if isinstance(ignored_languages, str):
            ignored_languages = [ignored_languages]
        elif not isinstance(ignored_languages, (list, tuple, set)):
            ignored_languages = ['en']
        self.config['ignored_languages'] = [
            normalize_locale(language)
            for language in ignored_languages
            if isinstance(language, str) and language.strip()
        ]
        doctrine_path = self.config.get('active_doctrine_pack', '')
        if doctrine_path:
            try:
                from src.core.doctrine import load_doctrine_pack
                self.doctrine_pack = load_doctrine_pack(doctrine_path)
            except Exception as exc:
                logger.warning("Ignoring invalid active doctrine pack: %s", exc)
                self.doctrine_pack = None
        else:
            self.doctrine_pack = None
        if not self.defer_translation:
            self.translator_service.set_config(self.config)

    def _defer_lines(self, session_id, source_id, lines):
        if lines:
            self._deferred_batches.append((session_id, source_id, list(lines)))

    @Slot(str, str, list)
    def process_lines(self, session_id: str, source_id: str, lines: list):
        for index, line in enumerate(lines):
            if QThread.currentThread().isInterruptionRequested():
                logger.info("[%s] Stopping queued message processing", session_id)
                break
            if self._config_update_pending():
                self._defer_lines(session_id, source_id, lines[index:])
                break
            try:
                self._process_single_line(session_id, source_id, line)
            except ConfigUpdatePending:
                self._defer_lines(session_id, source_id, lines[index:])
                break
            except Exception as exc:
                logger.error("[%s] Error processing line: %s", session_id, exc)

    def _process_single_line(self, session_id: str, source_id: str, line: str):
        msg = self.parser.parse(line, 0)
        if not msg:
            return

        tokenized = self.tokenizer.tokenize(msg.message)
        command_source = re.sub(r'__EVELINK_\d+__', '', tokenized.cleaned)
        command_source = ''.join(
            character
            for character in command_source
            if ord(character) >= 0x20 and not 0x7F <= ord(character) <= 0x9F
        )
        commands = classify_commands(
            command_source,
            doctrine_pack=self.doctrine_pack,
        )
        role = speaker_role(msg.sender, self.config.get('speaker_roles', {}))

        ignored = set(self.config.get('ignored_languages', ['en']))
        target_lang = self.config.get('target_language', 'en')
        should_translate, detected_lang = self.detector.should_translate(
            tokenized.cleaned,
            target_lang=target_lang,
            ignored_langs=ignored,
            command_matches=commands,
        )

        # Establish a linear boundary with request_config_update(): a line that
        # has not reached this point when consent changes is reprocessed later.
        if self._config_update_pending():
            raise ConfigUpdatePending

        timestamp_str = msg.timestamp.strftime("%H:%M:%S")
        message_id = f"{session_id}:{source_id}:{next(self._message_counter)}"
        final_original = restore_with_highlight(tokenized.cleaned, tokenized.tokens)

        if not should_translate:
            self.signals.message_ready.emit(
                ProcessedMessage(
                    message_id=message_id,
                    session_id=session_id,
                    text=final_original,
                    sender=msg.sender,
                    timestamp=timestamp_str,
                    source_id=source_id,
                    state=TranslationState.ORIGINAL,
                    detected_language=detected_lang,
                    translation_relevant=False,
                    speaker_role=role,
                    commands=commands,
                    lookup_text=tokenized.cleaned,
                )
            )
            return

        if self.defer_translation:
            provider_mode = resolve_translation_provider(self.config)
            if provider_mode == TRANSLATION_PROVIDER_DISABLED:
                self.signals.message_ready.emit(
                    ProcessedMessage(
                        message_id=message_id,
                        session_id=session_id,
                        text=final_original,
                        sender=msg.sender,
                        timestamp=timestamp_str,
                        source_id=source_id,
                        state=TranslationState.ORIGINAL,
                        provider="Disabled",
                        detected_language=detected_lang,
                        translation_relevant=True,
                        speaker_role=role,
                        commands=commands,
                        lookup_text=tokenized.cleaned,
                    )
                )
                return

            self.signals.message_ready.emit(
                ProcessedMessage(
                    message_id=message_id,
                    session_id=session_id,
                    text=final_original,
                    sender=msg.sender,
                    timestamp=timestamp_str,
                    source_id=source_id,
                    state=TranslationState.PENDING,
                    detected_language=detected_lang,
                    translation_relevant=True,
                    speaker_role=role,
                    commands=commands,
                    revision=1,
                    lookup_text=tokenized.cleaned,
                )
            )
            self.signals.translation_requested.emit(TranslationJob(
                message_id=message_id,
                session_id=session_id,
                source_id=source_id,
                source_text=tokenized.cleaned,
                final_original=final_original,
                sender=msg.sender,
                timestamp=timestamp_str,
                detected_language=detected_lang,
                target_language=target_lang,
                tokens=dict(tokenized.tokens),
                commands=commands,
                speaker_role=role,
                config_generation=self._config_gate.snapshot().applied,
            ))
            return

        get_provider_status = getattr(
            self.translator_service, 'get_provider_status', None
        )
        provider_status = get_provider_status() if callable(get_provider_status) else None
        if isinstance(provider_status, dict) and not provider_status.get('available', True):
            provider = str(provider_status.get('name') or 'Translation provider')
            if provider_status.get('mode') == 'disabled':
                self.signals.message_ready.emit(
                    ProcessedMessage(
                        message_id=message_id,
                        session_id=session_id,
                        text=final_original,
                        sender=msg.sender,
                        timestamp=timestamp_str,
                        source_id=source_id,
                        state=TranslationState.ORIGINAL,
                        provider=provider,
                        detected_language=detected_lang,
                        translation_relevant=True,
                        speaker_role=role,
                        commands=commands,
                        lookup_text=tokenized.cleaned,
                    )
                )
                return

            error = str(
                provider_status.get('reason')
                or f"{provider} translation unavailable"
            )
            self.signals.message_ready.emit(
                ProcessedMessage(
                    message_id=message_id,
                    session_id=session_id,
                    text=final_original,
                    sender=msg.sender,
                    timestamp=timestamp_str,
                    source_id=source_id,
                    state=TranslationState.FAILED,
                    provider=provider,
                    detected_language=detected_lang,
                    error=error,
                    translation_relevant=True,
                    speaker_role=role,
                    commands=commands,
                    lookup_text=tokenized.cleaned,
                )
            )
            return

        # Surface the source line before a provider call blocks the worker. In the
        # real application this signal crosses to the GUI thread, so pilots can
        # see the original immediately while translation is in progress.
        self.signals.message_ready.emit(
            ProcessedMessage(
                message_id=message_id,
                session_id=session_id,
                text=final_original,
                sender=msg.sender,
                timestamp=timestamp_str,
                source_id=source_id,
                state=TranslationState.PENDING,
                detected_language=detected_lang,
                translation_relevant=True,
                speaker_role=role,
                commands=commands,
                revision=1,
                lookup_text=tokenized.cleaned,
            )
        )

        try:
            translated_text, success, provider = self.translator_service.translate_message(
                tokenized.cleaned, target_lang, source_lang=detected_lang
            )
        except Exception as exc:
            provider = getattr(self.translator_service, 'provider_name', '')
            translated_text = tokenized.cleaned
            success = False
            logger.error(
                "[%s] Unexpected %s translation failure: %s",
                session_id.upper(), provider or "provider", exc,
            )

        status = "Success" if success else "Failed"
        logger.debug(
            "[%s] [%s] %s: %r -> %r",
            session_id.upper(), provider, status, tokenized.cleaned, translated_text
        )

        final_text = restore_with_highlight(translated_text, tokenized.tokens)
        state = TranslationState.TRANSLATED if success else TranslationState.FAILED
        error = ""
        if not success:
            provider_label = (provider or "Translation provider").strip()
            if provider_label.casefold() in {"disabled", "none"}:
                error = "Translation is disabled"
            else:
                error = f"{provider_label} translation unavailable"

        self.signals.message_ready.emit(
            ProcessedMessage(
                message_id=message_id,
                session_id=session_id,
                text=final_text if success else final_original,
                sender=msg.sender,
                timestamp=timestamp_str,
                source_id=source_id,
                original_text=final_original if success else "",
                state=state,
                provider=provider,
                detected_language=detected_lang,
                error=error,
                translation_relevant=True,
                speaker_role=role,
                commands=commands,
                revision=2,
                lookup_text=tokenized.cleaned,
            )
        )

    def _restore_with_highlight(self, text, tokens):
        return restore_with_highlight(text, tokens)


class TranslationScheduler(QObject):
    """Own provider calls on a dedicated thread and coalesce safe bursts."""

    message_ready = Signal(object)
    entity_index_ready = Signal(object)
    compose_translation_ready = Signal(int, str)
    compose_translation_failed = Signal(int, str)
    provider_usage_ready = Signal(dict)
    provider_status_ready = Signal(dict)

    def __init__(
        self,
        translator_service,
        parent=None,
        *,
        entity_index_loader=None,
        entity_index_path=None,
    ):
        super().__init__(parent)
        self.translator_service = translator_service
        self.config = {
            'target_language': 'en',
            'burst_optimization': True,
            'burst_batch_size': 5,
            'context_assist': False,
            'context_messages': 3,
        }
        self._jobs: deque[TranslationJob] = deque()
        self._deferred_jobs: deque[TranslationJob] = deque()
        self._context = defaultdict(deque)
        self._config_gate = ConfigGenerationGate()
        self._entity_index = getattr(translator_service, 'entity_index', None)
        self._entity_index_loader = entity_index_loader
        self._entity_index_path = entity_index_path
        self._entity_index_initialized = entity_index_loader is None
        self._entity_initialization_armed = False
        self._compose_detector = None
        self._usage_refresh_lock = threading.Lock()
        self._usage_refresh_in_flight = False
        self._stopping = False
        set_fallback_authorizer = getattr(
            translator_service,
            'set_fallback_authorizer',
            None,
        )
        if callable(set_fallback_authorizer):
            set_fallback_authorizer(
                lambda: not self._config_update_pending()
                and not self._stopping
            )
        self._doctrine_path = None
        self._flush_timer = QTimer(self)
        self._flush_timer.setSingleShot(True)
        self._flush_timer.setInterval(75)
        self._flush_timer.setTimerType(Qt.TimerType.PreciseTimer)
        self._flush_timer.timeout.connect(self.flush)

    @Slot()
    def initialize_entity_index(self) -> None:
        """Load the generated SDE index on the scheduler thread.

        Qt delivers this slot before subsequently queued translation jobs when
        it is connected to ``QThread.started``. Originals can therefore render
        immediately while provider work waits for complete entity protection.
        """

        self._entity_initialization_armed = True
        if not self.config.get('entity_protection', True):
            set_entity_index = getattr(
                self.translator_service,
                'set_entity_index',
                None,
            )
            if callable(set_entity_index):
                set_entity_index(None)
            self.entity_index_ready.emit(None)
            return
        if self._entity_index_initialized:
            return
        self._entity_index_initialized = True
        try:
            self._entity_index = self._entity_index_loader(
                self._entity_index_path or None
            )
        except Exception as exc:
            logger.warning("Could not initialize SDE entity index: %s", type(exc).__name__)
            self._entity_index = None
        set_entity_index = getattr(self.translator_service, 'set_entity_index', None)
        if callable(set_entity_index):
            set_entity_index(
                self._entity_index
                if self.config.get('entity_protection', True)
                else None
            )
        self.entity_index_ready.emit(self._entity_index)

    @Slot()
    def refresh_provider_usage(self) -> None:
        get_usage = getattr(self.translator_service, 'get_provider_usage', None)
        if not callable(get_usage):
            self.provider_usage_ready.emit({'available': False, 'error': True})
            return
        with self._usage_refresh_lock:
            if self._usage_refresh_in_flight or self._stopping:
                return
            self._usage_refresh_in_flight = True

        def run_usage_refresh():
            try:
                usage = get_usage(refresh=True)
            except Exception as exc:
                logger.error(
                    "Unexpected provider usage failure: %s",
                    type(exc).__name__,
                )
                usage = {'available': False, 'error': True}
            with self._usage_refresh_lock:
                self._usage_refresh_in_flight = False
                stopping = self._stopping
            if not stopping:
                self.provider_usage_ready.emit(
                    usage
                    if isinstance(usage, dict)
                    else {'available': False, 'error': True}
                )

        threading.Thread(
            target=run_usage_refresh,
            name="provider-usage-refresh",
            daemon=True,
        ).start()

    @Slot(int, str, str, str)
    def translate_composed_message(
        self,
        request_id: int,
        text: str,
        source_language: str,
        target_language: str,
    ) -> None:
        """Translate manual outbound text without automating EVE input."""

        if self._config_update_pending():
            self.compose_translation_failed.emit(
                request_id,
                "Provider settings are changing. Try again in a moment.",
            )
            return
        message = str(text or '').strip()
        if not message:
            self.compose_translation_failed.emit(request_id, "Enter a message first.")
            return
        if len(message) > 5_000:
            self.compose_translation_failed.emit(
                request_id,
                "Outbound messages are limited to 5,000 characters.",
            )
            return
        source = normalize_locale(source_language, 'auto')
        if source in {'auto', 'unknown'}:
            try:
                if self._compose_detector is None:
                    from src.core.detector import LanguageDetector
                    self._compose_detector = LanguageDetector()
                _translate, detected = self._compose_detector.should_translate(
                    message,
                    target_lang=target_language,
                    ignored_langs=set(),
                )
                if detected not in {'auto', 'unknown'}:
                    source = detected
            except Exception as exc:
                logger.warning(
                    "Could not detect outbound source language: %s",
                    type(exc).__name__,
                )
        try:
            translated, success, provider = self.translator_service.translate_message(
                message,
                target_language,
                source_lang=source,
            )
        except Exception as exc:
            logger.error("Unexpected outbound translation failure: %s", type(exc).__name__)
            translated, success, provider = message, False, "Translation provider"
        if self._config_update_pending():
            self.compose_translation_failed.emit(
                request_id,
                "Provider settings changed while translating. Try again.",
            )
            return
        if success and isinstance(translated, str) and translated.strip():
            self.compose_translation_ready.emit(request_id, translated)
            return
        provider_label = str(provider or "Translation provider").strip()
        self.compose_translation_failed.emit(
            request_id,
            f"{provider_label} could not translate this message.",
        )

    @Slot(dict)
    def update_config(self, config):
        self._apply_config(config)
        self._emit_provider_status()

    def request_config_update(self, generation: int) -> None:
        self._config_gate.request(generation)

    def _config_update_pending(self) -> bool:
        return self._config_gate.pending()

    @Slot(int, dict)
    def apply_queued_config(self, generation: int, config: dict) -> None:
        # A config request may arrive during the 75 ms coalescing window. Drain
        # those jobs before mutating provider/target/doctrine state so old job
        # metadata can never run against the new pipeline.
        self._flush_timer.stop()
        waiting_jobs = list(self._jobs)
        self._jobs.clear()
        waiting_jobs.extend(self._deferred_jobs)
        self._deferred_jobs.clear()
        self._apply_config(config)
        generation_state = self._config_gate.apply(generation)
        if generation_state.pending:
            self._deferred_jobs.extend(waiting_jobs)
            return
        self._emit_provider_status()
        target = self.config.get('target_language', 'en')
        for job in waiting_jobs:
            self.queue_job(replace(
                job,
                target_language=target,
                config_generation=generation_state.applied,
            ))

    def _apply_config(self, config) -> None:
        entity_protection_was_enabled = self.config.get('entity_protection', True)
        self.config = config.copy() if isinstance(config, dict) else {}
        self.translator_service.set_config(self.config)
        entity_protection_enabled = self.config.get('entity_protection', True)
        set_entity_index = getattr(self.translator_service, 'set_entity_index', None)
        if callable(set_entity_index):
            set_entity_index(
                self._entity_index
                if entity_protection_enabled and self._entity_index_initialized
                else None
            )
        if (
            entity_protection_enabled
            and not self._entity_index_initialized
            and self._entity_initialization_armed
        ):
            self.initialize_entity_index()
        elif (
            not entity_protection_enabled
            and entity_protection_was_enabled
            and self._entity_initialization_armed
        ):
            self.entity_index_ready.emit(None)
        doctrine_path = self.config.get('active_doctrine_pack', '')
        doctrine = None
        if doctrine_path:
            try:
                from src.core.doctrine import load_doctrine_pack
                doctrine = load_doctrine_pack(doctrine_path)
            except Exception as exc:
                logger.warning("Ignoring invalid active doctrine pack: %s", exc)
        set_doctrine = getattr(self.translator_service, 'set_doctrine_pack', None)
        if callable(set_doctrine):
            set_doctrine(doctrine)
        self._doctrine_path = doctrine_path
        self._context.clear()

    def _emit_provider_status(self) -> None:
        get_provider_status = getattr(
            self.translator_service,
            'get_provider_status',
            None,
        )
        if callable(get_provider_status):
            status = get_provider_status()
            self.provider_status_ready.emit(
                status if isinstance(status, dict) else {}
            )

    @Slot(object)
    def queue_job(self, job) -> None:
        if not isinstance(job, TranslationJob):
            return
        generation_state = self._config_gate.snapshot()
        if generation_state.pending or job.config_generation > generation_state.applied:
            self._deferred_jobs.append(job)
            return
        if job.config_generation < generation_state.applied:
            # Cross-thread delivery can let the scheduler apply a config before
            # the analysis worker's older job signal arrives. Retarget it now;
            # there may be no later config event to drain a deferred queue.
            job = replace(
                job,
                target_language=self.config.get('target_language', 'en'),
                config_generation=generation_state.applied,
            )
        high_priority = bool(job.commands or job.speaker_role)
        burst_enabled = bool(self.config.get('burst_optimization', True))
        if high_priority or not burst_enabled:
            self._translate_jobs([job])
            return
        self._jobs.append(job)
        if not self._flush_timer.isActive():
            self._flush_timer.start()

    @Slot()
    def flush(self) -> None:
        if self._config_update_pending():
            self._deferred_jobs.extend(self._jobs)
            self._jobs.clear()
            return
        max_items = self.config.get('burst_batch_size', 5)
        try:
            max_items = max(1, min(10, int(max_items)))
        except (TypeError, ValueError):
            max_items = 5
        while self._jobs:
            first = self._jobs.popleft()
            group = [first]
            key = self._batch_key(first)
            remainder = deque()
            while self._jobs and len(group) < max_items:
                candidate = self._jobs.popleft()
                if self._batch_key(candidate) == key:
                    group.append(candidate)
                else:
                    remainder.append(candidate)
            remainder.extend(self._jobs)
            self._jobs = remainder
            self._translate_jobs(group)

    @staticmethod
    def _batch_key(job: TranslationJob) -> tuple:
        return (
            job.session_id,
            job.source_id,
            job.detected_language,
            job.target_language,
            job.config_generation,
        )

    def _context_key(self, job: TranslationJob) -> tuple:
        return self._batch_key(job)

    def _context_for(self, job: TranslationJob) -> str | None:
        if not self.config.get('context_assist', False):
            return None
        history = self._context.get(self._context_key(job))
        if not history:
            return None
        return "\n".join(history)

    def _remember_context(self, job: TranslationJob) -> None:
        if not self.config.get('context_assist', False):
            return
        try:
            limit = max(1, min(10, int(self.config.get('context_messages', 3))))
        except (TypeError, ValueError):
            limit = 3
        history = self._context[self._context_key(job)]
        history.append(job.source_text[:1000])
        while len(history) > limit:
            history.popleft()

    def _translate_jobs(self, jobs: list[TranslationJob]) -> None:
        if not jobs:
            return
        if self._config_update_pending():
            self._deferred_jobs.extend(jobs)
            return
        context = self._context_for(jobs[0])
        messages = [job.source_text for job in jobs]
        try:
            translate_batch = getattr(
                self.translator_service, 'translate_batch_messages', None
            )
            if len(jobs) > 1 and callable(translate_batch):
                results = translate_batch(
                    messages,
                    jobs[0].target_language,
                    source_lang=jobs[0].detected_language,
                    context=context,
                )
            else:
                results = [
                    self.translator_service.translate_message(
                        job.source_text,
                        job.target_language,
                        source_lang=job.detected_language,
                        **({'context': context} if context else {}),
                    )
                    for job in jobs
                ]
        except Exception as exc:
            logger.error("Unexpected scheduled translation failure: %s", type(exc).__name__)
            provider = getattr(self.translator_service, 'provider_name', '')
            results = [(job.source_text, False, provider) for job in jobs]

        if not isinstance(results, (list, tuple)) or len(results) != len(jobs):
            logger.error("Provider batch returned an invalid result count")
            provider = getattr(self.translator_service, 'provider_name', '')
            results = [(job.source_text, False, provider) for job in jobs]

        if self._config_update_pending():
            self._deferred_jobs.extend(jobs)
            return

        for job, result in zip(jobs, results):
            try:
                translated_text, success, provider = result
            except (TypeError, ValueError):
                translated_text, success, provider = (
                    job.source_text,
                    False,
                    getattr(self.translator_service, 'provider_name', ''),
                )
            final_text = restore_with_highlight(translated_text, job.tokens)
            state = TranslationState.TRANSLATED if success else TranslationState.FAILED
            provider_label = str(provider or 'Translation provider').strip()
            error = '' if success else f"{provider_label} translation unavailable"
            self.message_ready.emit(ProcessedMessage(
                message_id=job.message_id,
                session_id=job.session_id,
                source_id=job.source_id,
                text=final_text if success else job.final_original,
                original_text=job.final_original if success else '',
                sender=job.sender,
                timestamp=job.timestamp,
                state=state,
                provider=provider_label,
                detected_language=job.detected_language,
                error=error,
                translation_relevant=True,
                speaker_role=job.speaker_role,
                commands=job.commands,
                revision=job.revision + 1,
                lookup_text=job.source_text,
            ))
            if success:
                self._remember_context(job)

    @Slot()
    def stop(self) -> None:
        with self._usage_refresh_lock:
            self._stopping = True
        self._flush_timer.stop()
        self._jobs.clear()
        self._deferred_jobs.clear()
        self._context.clear()

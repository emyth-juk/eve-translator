import html
import logging

from PySide6.QtCore import QObject, Signal, Slot

logger = logging.getLogger(__name__)


class WorkerSignals(QObject):
    message_ready = Signal(str, str, str, str, str, bool)


class LogProcessingWorker(QObject):
    """
    Shared worker that processes lines from all sessions and emits session-routed results.
    """

    def __init__(self, parser, tokenizer, detector, translator_service):
        super().__init__()
        self.parser = parser
        self.tokenizer = tokenizer
        self.detector = detector
        self.translator_service = translator_service
        self.signals = WorkerSignals()
        self.config = {'ignored_languages': ['en'], 'target_language': 'en'}

    @Slot(dict)
    def update_config(self, config):
        self.config = config.copy()
        self.translator_service.set_config(self.config)

    @Slot(str, list)
    def process_lines(self, session_id: str, lines: list):
        for line in lines:
            try:
                self._process_single_line(session_id, line)
            except Exception as exc:
                logger.error("[%s] Error processing line: %s", session_id, exc)

    def _process_single_line(self, session_id: str, line: str):
        msg = self.parser.parse(line, 0)
        if not msg:
            return

        tokenized = self.tokenizer.tokenize(msg.message)

        ignored = set(self.config.get('ignored_languages', ['en']))
        should_translate, detected_lang = self.detector.should_translate(
            tokenized.cleaned, ignored_langs=ignored
        )

        timestamp_str = msg.timestamp.strftime("%H:%M:%S")

        if not should_translate:
            final_text = self._restore_with_highlight(tokenized.cleaned, tokenized.tokens)
            self.signals.message_ready.emit(
                session_id, final_text, msg.sender,
                timestamp_str, "", False
            )
            return

        target_lang = self.config.get('target_language', 'en')
        translated_text, success, provider = self.translator_service.translate_message(
            tokenized.cleaned, target_lang, source_lang=detected_lang
        )

        status = "Success" if success else "Failed"
        logger.info(
            "[%s] [%s] %s: %r -> %r",
            session_id.upper(), provider, status, tokenized.cleaned, translated_text
        )

        final_text = self._restore_with_highlight(translated_text, tokenized.tokens)
        final_original = self._restore_with_highlight(tokenized.cleaned, tokenized.tokens)

        self.signals.message_ready.emit(
            session_id, final_text, msg.sender,
            timestamp_str, final_original, True
        )

    def _restore_with_highlight(self, text, tokens):
        safe_text = html.escape(text)
        for placeholder, original in tokens.items():
            safe_original = html.escape(original)
            highlighted = f"<span style='color: yellow;'>{safe_original}</span>"
            safe_text = safe_text.replace(placeholder, highlighted)
        return safe_text

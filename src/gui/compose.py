"""Manual outbound translation dialog.

The dialog deliberately stops at the clipboard boundary: it asks the
application's translation worker for a result and lets the pilot explicitly
copy that result.  It never sends keystrokes or text to the EVE client.
"""

from __future__ import annotations

import re

from PySide6.QtCore import Qt, Signal, Slot
from PySide6.QtGui import QCloseEvent, QKeySequence, QShortcut
from PySide6.QtWidgets import (
    QApplication,
    QComboBox,
    QDialog,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QTextEdit,
    QVBoxLayout,
    QWidget,
)

from src.version import __version__


LANGUAGE_CODE_PATTERN = re.compile(r"^[a-z]{2,3}(?:-[a-z]{2,4})?$")

# These cover the languages most often encountered in public EVE fleets while
# leaving both boxes editable for any provider-supported ISO language code.
COMMON_LANGUAGES = (
    ("English", "en"),
    ("German", "de"),
    ("French", "fr"),
    ("Spanish", "es"),
    ("Portuguese", "pt"),
    ("Russian", "ru"),
    ("Ukrainian", "uk"),
    ("Polish", "pl"),
    ("Italian", "it"),
    ("Dutch", "nl"),
    ("Swedish", "sv"),
    ("Turkish", "tr"),
    ("Chinese", "zh"),
    ("Japanese", "ja"),
    ("Korean", "ko"),
)


LANGUAGE_NAME_CODES = {name.lower(): code for name, code in COMMON_LANGUAGES}


def _normalized_language_code(combo: QComboBox, *, allow_auto: bool) -> str:
    """Return the selected item data or an editable ISO-style value."""
    item_data = combo.currentData()
    selected_text = combo.itemText(combo.currentIndex())
    if (
        isinstance(item_data, str)
        and item_data
        and combo.currentText() == selected_text
    ):
        return item_data

    value = combo.currentText().strip().lower().replace("_", "-")
    if allow_auto and value in {"", "auto", "detect", "detect automatically"}:
        return "auto"
    # Typing a language name ("Russian") is natural; map it to its code.
    return LANGUAGE_NAME_CODES.get(value, value)


class ComposeDialog(QDialog):
    """Translate text for manual copy-and-paste into EVE fleet chat.

    ``translation_requested`` is intentionally transport-agnostic.  The main
    application connects it to its existing asynchronous translation worker,
    then calls :meth:`finish_translation` or :meth:`fail_translation` on the
    GUI thread.  Request IDs suppress late results from superseded work.
    """

    translation_requested = Signal(int, str, str, str)
    settings_requested = Signal()

    def __init__(
        self,
        parent: QWidget | None = None,
        *,
        source_language: str = "auto",
        target_language: str = "en",
        provider_label: str = "configured cloud provider",
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle(f"Compose Translation - v{__version__}")
        self.setMinimumSize(540, 480)
        self.resize(640, 590)

        self._request_counter = 0
        self._translation_available = True
        self._validation_hint_shown = False
        self._active_request_id: int | None = None
        self._active_request_stale = False
        self._last_completed_request_id: int | None = None

        layout = QVBoxLayout(self)

        heading = QLabel("Compose a translation for fleet chat", self)
        heading.setStyleSheet("font-size: 16px; font-weight: bold;")
        layout.addWidget(heading)

        self.notice_label = QLabel(self)
        self.notice_label.setObjectName("composePrivacyNotice")
        self.notice_label.setWordWrap(True)
        self.notice_label.setTextFormat(Qt.TextFormat.PlainText)
        self.notice_label.setStyleSheet("color: #777;")
        layout.addWidget(self.notice_label)
        self.set_provider_label(provider_label)

        language_row = QHBoxLayout()
        source_label = QLabel("&From:", self)
        self.source_language_combo = self._make_language_combo(include_auto=True)
        self.source_language_combo.setObjectName("composeSourceLanguage")
        source_label.setBuddy(self.source_language_combo)
        language_row.addWidget(source_label)
        language_row.addWidget(self.source_language_combo, 1)

        target_label = QLabel("T&o:", self)
        self.target_language_combo = self._make_language_combo(include_auto=False)
        self.target_language_combo.setObjectName("composeTargetLanguage")
        target_label.setBuddy(self.target_language_combo)
        language_row.addWidget(target_label)
        language_row.addWidget(self.target_language_combo, 1)
        layout.addLayout(language_row)

        source_text_label = QLabel("&Message to translate:", self)
        self.source_text_edit = QTextEdit(self)
        self.source_text_edit.setObjectName("composeSourceText")
        self.source_text_edit.setAcceptRichText(False)
        self.source_text_edit.setPlaceholderText("Type or paste the fleet message here...")
        self.source_text_edit.setAccessibleName("Message to translate")
        source_text_label.setBuddy(self.source_text_edit)
        layout.addWidget(source_text_label)
        layout.addWidget(self.source_text_edit, 2)

        action_row = QHBoxLayout()
        self.settings_button = QPushButton("Open &Settings", self)
        self.settings_button.setObjectName("composeSettingsButton")
        self.settings_button.setVisible(False)
        self.settings_button.clicked.connect(self.settings_requested)
        action_row.addWidget(self.settings_button)
        self.cancel_button = QPushButton("Ca&ncel", self)
        self.cancel_button.setObjectName("composeCancelButton")
        self.cancel_button.setEnabled(False)
        self.cancel_button.clicked.connect(self.cancel_translation)
        self.translate_button = QPushButton("&Translate", self)
        self.translate_button.setObjectName("composeTranslateButton")
        self.translate_button.setDefault(True)
        self.translate_button.setEnabled(False)
        self.translate_button.clicked.connect(self.request_translation)
        action_row.addStretch()
        action_row.addWidget(self.cancel_button)
        action_row.addWidget(self.translate_button)
        layout.addLayout(action_row)

        self.status_label = QLabel("Ready", self)
        self.status_label.setObjectName("composeStatus")
        self.status_label.setTextFormat(Qt.TextFormat.PlainText)
        self.status_label.setWordWrap(True)
        self.status_label.setAccessibleName("Translation status")
        layout.addWidget(self.status_label)

        output_label = QLabel("T&ranslated message:", self)
        self.output_text_edit = QTextEdit(self)
        self.output_text_edit.setObjectName("composeOutputText")
        self.output_text_edit.setAcceptRichText(False)
        self.output_text_edit.setReadOnly(True)
        self.output_text_edit.setPlaceholderText("The translation will appear here.")
        self.output_text_edit.setAccessibleName("Translated message")
        output_label.setBuddy(self.output_text_edit)
        layout.addWidget(output_label)
        layout.addWidget(self.output_text_edit, 2)

        footer_row = QHBoxLayout()
        self.copy_button = QPushButton("&Copy translation", self)
        self.copy_button.setObjectName("composeCopyButton")
        self.copy_button.setEnabled(False)
        self.copy_button.clicked.connect(self.copy_translation)
        footer_row.addWidget(self.copy_button)
        self.copy_close_button = QPushButton("Cop&y && close", self)
        self.copy_close_button.setObjectName("composeCopyCloseButton")
        self.copy_close_button.setEnabled(False)
        self.copy_close_button.clicked.connect(self.copy_and_close)
        footer_row.addWidget(self.copy_close_button)
        footer_row.addStretch()
        self.close_button = QPushButton("C&lose", self)
        self.close_button.clicked.connect(self.reject)
        footer_row.addWidget(self.close_button)
        layout.addLayout(footer_row)

        self._set_language(self.source_language_combo, source_language, allow_auto=True)
        self._set_language(self.target_language_combo, target_language, allow_auto=False)

        self.source_text_edit.textChanged.connect(self._request_input_changed)
        self.source_language_combo.currentTextChanged.connect(self._request_input_changed)
        self.target_language_combo.currentTextChanged.connect(self._request_input_changed)

        self.translate_shortcuts = (
            QShortcut(QKeySequence("Ctrl+Return"), self),
            QShortcut(QKeySequence("Ctrl+Enter"), self),
        )
        for shortcut in self.translate_shortcuts:
            shortcut.setContext(Qt.ShortcutContext.WindowShortcut)
            shortcut.activated.connect(self.request_translation)

        self.copy_shortcut = QShortcut(QKeySequence("Ctrl+Shift+C"), self)
        self.copy_shortcut.setContext(Qt.ShortcutContext.WindowShortcut)
        self.copy_shortcut.activated.connect(self.copy_translation)

        self.setTabOrder(self.source_language_combo, self.target_language_combo)
        self.setTabOrder(self.target_language_combo, self.source_text_edit)
        self.setTabOrder(self.source_text_edit, self.translate_button)
        self.setTabOrder(self.translate_button, self.output_text_edit)
        self.setTabOrder(self.output_text_edit, self.copy_button)
        self.setTabOrder(self.copy_button, self.close_button)

        self._refresh_request_action()

    @staticmethod
    def _make_language_combo(*, include_auto: bool) -> QComboBox:
        combo = QComboBox()
        combo.setEditable(True)
        combo.setInsertPolicy(QComboBox.InsertPolicy.NoInsert)
        if include_auto:
            combo.addItem("Detect automatically", "auto")
        for name, code in COMMON_LANGUAGES:
            combo.addItem(f"{name} ({code})", code)
        combo.setAccessibleDescription(
            "Choose a language or type a provider-supported ISO language code."
        )
        return combo

    @staticmethod
    def _set_language(combo: QComboBox, value: str, *, allow_auto: bool) -> None:
        code = str(value or ("auto" if allow_auto else "en")).strip().lower()
        code = code.replace("_", "-")
        index = combo.findData(code)
        if index >= 0:
            combo.setCurrentIndex(index)
        else:
            combo.setEditText(code)

    @property
    def active_request_id(self) -> int | None:
        return self._active_request_id

    @property
    def request_in_flight(self) -> bool:
        return self._active_request_id is not None

    @property
    def last_completed_request_id(self) -> int | None:
        return self._last_completed_request_id

    @Slot(str)
    def set_provider_label(self, provider_label: str) -> None:
        provider = str(provider_label or "configured cloud provider").strip()
        self.notice_label.setText(
            f"Privacy: this text is sent to your {provider}. Nothing is posted "
            "to EVE; copying and pasting are always manual."
        )

    def set_translation_available(self, available: bool, provider_label: str = "") -> None:
        """Show whether a provider is configured; Disabled cannot translate."""
        self._translation_available = bool(available)
        self.settings_button.setVisible(not self._translation_available)
        if self._translation_available:
            if provider_label:
                self.set_provider_label(provider_label)
        else:
            self.notice_label.setText(
                "Translation is turned off. Choose Google or DeepL in Settings to "
                "translate outbound messages. Nothing is posted to EVE."
            )
        self._refresh_request_action()

    def source_language(self) -> str:
        return _normalized_language_code(self.source_language_combo, allow_auto=True)

    def target_language(self) -> str:
        return _normalized_language_code(self.target_language_combo, allow_auto=False)

    @Slot()
    def present(self) -> None:
        """Show and focus the dialog from a tray action or global hotkey."""
        if self.isMinimized():
            self.showNormal()
        else:
            self.show()
        self.raise_()
        self.activateWindow()
        self.source_text_edit.setFocus(Qt.FocusReason.ShortcutFocusReason)
        # Typing the next call replaces the previous one instead of appending.
        self.source_text_edit.selectAll()

    @Slot()
    def request_translation(self) -> int | None:
        """Emit one asynchronous translation request and return its ID."""
        # Keyboard shortcuts can invoke this slot even while the button is
        # disabled.  Keep exactly one outbound cloud request active at a time.
        if self._active_request_id is not None:
            self._refresh_request_action()
            return None
        text = self.source_text_edit.toPlainText().strip()
        source = self.source_language()
        target = self.target_language()
        if not text:
            self._set_error("Enter a message before translating.")
            self._refresh_request_action()
            return None
        if source != "auto" and not LANGUAGE_CODE_PATTERN.fullmatch(source):
            self._set_error("Enter a valid source language code, such as en or pt-BR.")
            self._refresh_request_action()
            return None
        if not LANGUAGE_CODE_PATTERN.fullmatch(target):
            self._set_error("Enter a valid target language code, such as en or pt-BR.")
            self._refresh_request_action()
            return None

        self._request_counter += 1
        request_id = self._request_counter
        self._active_request_id = request_id
        self._active_request_stale = False
        self.output_text_edit.clear()
        self.copy_button.setEnabled(False)
        self.status_label.setStyleSheet("color: #777;")
        self.status_label.setText("Translating...")
        self.translate_button.setText("Translating...")
        self.translation_requested.emit(request_id, text, source, target)
        self._refresh_request_action()
        return request_id

    @Slot(int, str)
    def finish_translation(self, request_id: int, translated_text: str) -> bool:
        """Display the current request's result; ignore superseded responses."""
        if request_id != self._active_request_id:
            return False
        if self._active_request_stale:
            self._discard_stale_response()
            return False
        result = str(translated_text or "").strip()
        if not result:
            return self.fail_translation(request_id, "The provider returned an empty translation.")

        self._active_request_id = None
        self._active_request_stale = False
        self._last_completed_request_id = request_id
        self.output_text_edit.setPlainText(result)
        self.copy_button.setEnabled(True)
        self.copy_close_button.setEnabled(True)
        self.status_label.setStyleSheet("color: #2e7d32;")
        self.status_label.setText("Translation ready - review it, then copy it manually.")
        self.translate_button.setText("&Translate")
        self._refresh_request_action()
        return True

    @Slot(int, str)
    def fail_translation(self, request_id: int, error_message: str) -> bool:
        """Display a safe current-request error; ignore superseded responses."""
        if request_id != self._active_request_id:
            return False
        if self._active_request_stale:
            self._discard_stale_response()
            return False
        self._active_request_id = None
        self._active_request_stale = False
        message = str(error_message or "Translation failed.").strip()
        self._set_error(message[:500])
        self.translate_button.setText("&Translate")
        self._refresh_request_action()
        return True

    @Slot()
    def copy_translation(self) -> bool:
        """Copy only after the pilot explicitly invokes this action."""
        translated_text = self.output_text_edit.toPlainText()
        if not translated_text:
            return False
        QApplication.clipboard().setText(translated_text)
        self.status_label.setStyleSheet("color: #2e7d32;")
        self.status_label.setText("Copied. Paste it into EVE when you are ready.")
        return True

    @Slot()
    def copy_and_close(self) -> bool:
        """Copy the translation and get out of the way of the game."""
        if not self.copy_translation():
            return False
        self.close()
        return True

    @Slot()
    def cancel_translation(self) -> None:
        """Stop waiting for a slow provider; its late result is ignored."""
        if self._active_request_id is None:
            return
        self._active_request_id = None
        self._active_request_stale = False
        self.status_label.setStyleSheet("color: #777;")
        self.status_label.setText("Translation cancelled.")
        self.translate_button.setText("&Translate")
        self._refresh_request_action()

    @Slot()
    def _request_input_changed(self) -> None:
        # Editing any request field makes an in-flight response stale. Keep the
        # request marker until its callback arrives so edits cannot be used to
        # queue overlapping provider calls.
        request_was_active = self._active_request_id is not None
        result_was_visible = bool(self.output_text_edit.toPlainText())
        if request_was_active:
            self._active_request_stale = True
        if request_was_active or result_was_visible:
            self.output_text_edit.clear()
            self.copy_button.setEnabled(False)
            self.copy_close_button.setEnabled(False)
            self.status_label.setStyleSheet("color: #777;")
            self.status_label.setText(
                "Message changed - waiting for the current translation to finish."
                if request_was_active
                else "Message changed - translate again when ready."
            )
            self.translate_button.setText("&Translate")
        elif self.status_label.text() != "Ready":
            self.status_label.setStyleSheet("")
            self.status_label.setText("Ready")
        self._refresh_request_action()

    def _refresh_request_action(self) -> None:
        text_present = bool(self.source_text_edit.toPlainText().strip())
        source = self.source_language()
        target = self.target_language()
        source_valid = source == "auto" or bool(LANGUAGE_CODE_PATTERN.fullmatch(source))
        target_valid = bool(LANGUAGE_CODE_PATTERN.fullmatch(target))
        self.translate_button.setEnabled(
            text_present
            and source_valid
            and target_valid
            and self._translation_available
            and self._active_request_id is None
        )
        self.cancel_button.setEnabled(self._active_request_id is not None)
        # Say why Translate is disabled instead of leaving "Ready" on screen.
        hint = ""
        if self._active_request_id is None:
            if not self._translation_available:
                hint = "Translation is turned off. Use Open Settings to choose a provider."
            elif not source_valid:
                hint = "Enter a valid source language code or name, such as ru, Russian or pt-BR."
            elif not target_valid:
                hint = "Enter a valid target language code or name, such as ru, Russian or pt-BR."
        if hint:
            self.status_label.setStyleSheet("color: #b45309;")
            self.status_label.setText(hint)
            self._validation_hint_shown = True
        elif getattr(self, '_validation_hint_shown', False):
            self._validation_hint_shown = False
            self.status_label.setStyleSheet("")
            self.status_label.setText("Ready")

    def _set_error(self, message: str) -> None:
        self.status_label.setStyleSheet("color: #c0392b;")
        self.status_label.setText(message)

    def _discard_stale_response(self) -> None:
        self._active_request_id = None
        self._active_request_stale = False
        self.output_text_edit.clear()
        self.copy_button.setEnabled(False)
        self.copy_close_button.setEnabled(False)
        self.status_label.setStyleSheet("color: #777;")
        self.status_label.setText(
            "Previous translation discarded - translate the updated message when ready."
        )
        self.translate_button.setText("&Translate")
        self._refresh_request_action()

    def _invalidate_pending_request(self) -> None:
        if self._active_request_id is not None:
            self._active_request_stale = True

    def reject(self) -> None:
        # Keep an in-flight request: reopening with the same text shows its
        # result. Editing the text still marks it stale.
        super().reject()

    def closeEvent(self, event: QCloseEvent) -> None:
        super().closeEvent(event)

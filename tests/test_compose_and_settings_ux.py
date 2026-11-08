"""Compose and Settings usability fixes."""

import sys
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest
from PySide6.QtCore import QObject
from PySide6.QtWidgets import QApplication

from src.app_config import get_default_config
from src.gui.compose import ComposeDialog
from src.gui.settings import SettingsDialog


@pytest.fixture(scope="module", autouse=True)
def qt_app():
    app = QApplication.instance() or QApplication(sys.argv)
    yield app


def test_compose_disabled_provider_offers_settings_instead_of_translate():
    dialog = ComposeDialog(target_language="ru")
    requested = []
    dialog.settings_requested.connect(lambda: requested.append(True))
    dialog.source_text_edit.setPlainText("align to gate")

    dialog.set_translation_available(False)

    assert dialog.translate_button.isEnabled() is False
    assert dialog.settings_button.isVisibleTo(dialog) is True
    assert "turned off" in dialog.notice_label.text()
    assert "turned off" in dialog.status_label.text()
    dialog.settings_button.click()
    assert requested == [True]

    dialog.set_translation_available(True, "DeepL provider")
    assert dialog.translate_button.isEnabled() is True
    assert dialog.settings_button.isVisibleTo(dialog) is False
    dialog.close()


def test_compose_accepts_language_names_and_has_unique_mnemonics():
    dialog = ComposeDialog()
    dialog.source_text_edit.setPlainText("align to gate")
    dialog.target_language_combo.setEditText("Russian")

    assert dialog.target_language() == "ru"
    assert dialog.translate_button.isEnabled() is True
    def mnemonic(text):
        # "&&" is a literal ampersand; a single "&" marks the Alt shortcut.
        single = text.replace("&&", "")
        return single[single.index("&") + 1].lower() if "&" in single else None

    labels = [
        str(widget.text()) for widget in dialog.findChildren(QObject)
        if hasattr(widget, "text") and callable(widget.text)
    ]
    mnemonics = [key for key in map(mnemonic, labels) if key]
    assert len(mnemonics) == len(set(mnemonics)), labels
    dialog.close()


def test_compose_copy_and_close_and_cancel():
    dialog = ComposeDialog(target_language="de")
    dialog.source_text_edit.setPlainText("align to gate")
    dialog.show()
    request_id = dialog.request_translation()

    assert dialog.cancel_button.isEnabled() is True
    dialog.cancel_translation()
    assert dialog.request_in_flight is False
    assert dialog.finish_translation(request_id, "late") is False

    second = dialog.request_translation()
    dialog.finish_translation(second, "am Tor ausrichten")
    clipboard = QApplication.clipboard()
    previous = clipboard.text()
    try:
        assert dialog.copy_and_close() is True
        assert clipboard.text() == "am Tor ausrichten"
        assert dialog.isVisible() is False
    finally:
        clipboard.setText(previous)


def test_compose_result_survives_closing_and_reopening():
    dialog = ComposeDialog(target_language="de")
    dialog.source_text_edit.setPlainText("align to gate")
    dialog.show()
    request_id = dialog.request_translation()

    dialog.close()
    dialog.present()

    assert dialog.finish_translation(request_id, "am Tor ausrichten") is True
    assert dialog.output_text_edit.toPlainText() == "am Tor ausrichten"
    dialog.close()


def test_settings_validates_shortcuts_and_ignored_language_names():
    dialog = SettingsDialog(get_default_config())

    dialog.edit_ignored.setText("English, German")
    assert dialog.btn_save.isEnabled() is True
    assert dialog.get_settings()["shared"]["ignored_languages"] == ["en", "de"]

    dialog.edit_ignored.setText("English, Klingon")
    assert dialog.btn_save.isEnabled() is False
    assert "Klingon" in dialog.lbl_provider_validation.text()
    dialog.edit_ignored.setText("en")

    dialog.edit_compose_hotkey.setText("Ctrl+Shift+L")
    assert dialog.btn_save.isEnabled() is False
    assert "different" in dialog.lbl_provider_validation.text()

    dialog.edit_compose_hotkey.setText("ctrl+alt+f5")
    assert dialog.btn_save.isEnabled() is True
    assert dialog.get_settings()["shared"]["compose_hotkey"] == "Ctrl+Alt+F5"


def test_refresh_usage_refuses_unsaved_provider_or_key():
    dialog = SettingsDialog(get_default_config())
    requested = []
    dialog.provider_usage_requested.connect(lambda: requested.append(True))
    dialog.combo_provider.setCurrentIndex(dialog.combo_provider.findData("deepl"))
    dialog.edit_deepl.setText("new-key:fx")

    dialog._request_provider_usage()

    assert requested == []
    assert "Save first" in dialog.lbl_provider_usage.text()


def test_provider_test_also_checks_the_fallback_key():
    calls = []

    class FakeService:
        translation_memory = None
        entity_index = None

        def test_provider(self, mode, key, target):
            calls.append((mode, key))
            return SimpleNamespace(success=True, message=f"{mode} ok.")

    config = get_default_config()
    config["shared"].update({
        "translation_provider": "google",
        "provider_fallback_enabled": True,
        "deepl_api_key": "fallback:fx",
    })
    dialog = SettingsDialog(config, translation_service=FakeService())
    results = []
    dialog._provider_test_signals.finished.connect(
        lambda token, result, key: results.append(result)
    )

    with patch("src.gui.settings.threading.Thread") as thread_cls:
        dialog._test_provider()
        target = thread_cls.call_args.kwargs["target"]
    target()

    assert calls == [("google", ""), ("deepl", "fallback:fx")]
    assert "Fallback: deepl ok." in results[0].message


def test_compose_defaults_to_the_fleets_recent_language():
    with patch("src.main.TranslatorManager.__init__", lambda self: None):
        from src.main import TranslatorManager

        manager = TranslatorManager()
    manager.config = get_default_config()
    history = [
        {"translation_relevant": True, "detected_language": "zh"} for _ in range(4)
    ] + [{"translation_relevant": True, "detected_language": "ru"}]
    manager.sessions = {"fleet": SimpleNamespace(overlay=SimpleNamespace(chat_history=history))}

    assert manager._compose_default_target() == "zh"

    manager.sessions = {}
    manager.config["shared"]["compose_target_language"] = "ru"
    assert manager._compose_default_target() == "ru"

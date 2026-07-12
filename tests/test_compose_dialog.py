import os
import sys

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtGui import QKeySequence
from PySide6.QtWidgets import QApplication

from src.gui.compose import ComposeDialog


app = QApplication.instance() or QApplication(sys.argv)


def make_dialog(**kwargs):
    dialog = ComposeDialog(**kwargs)
    dialog.source_text_edit.setPlainText("align to the gate")
    return dialog


def test_request_signal_carries_id_text_languages_and_blocks_repeat_submission():
    dialog = make_dialog(source_language="auto", target_language="de")
    requests = []
    dialog.translation_requested.connect(lambda *request: requests.append(request))

    first_id = dialog.request_translation()
    second_id = dialog.request_translation()

    assert first_id == 1
    assert second_id is None
    assert requests == [(1, "align to the gate", "auto", "de")]
    assert dialog.active_request_id == 1
    assert dialog.request_in_flight is True
    assert dialog.translate_button.isEnabled() is False
    assert dialog.status_label.text() == "Translating..."
    dialog.close()


def test_only_latest_out_of_order_result_can_update_output():
    dialog = make_dialog()
    first_id = dialog.request_translation()
    dialog.source_text_edit.setPlainText("jump on contact")

    assert dialog.request_translation() is None
    assert dialog.finish_translation(first_id, "stale result") is False
    assert dialog.output_text_edit.toPlainText() == ""
    second_id = dialog.request_translation()
    assert second_id == 2
    assert dialog.finish_translation(first_id, "late stale result") is False
    assert dialog.finish_translation(second_id, "am Tor ausrichten") is True

    assert dialog.output_text_edit.toPlainText() == "am Tor ausrichten"
    assert dialog.last_completed_request_id == second_id
    assert dialog.request_in_flight is False
    assert dialog.copy_button.isEnabled() is True
    dialog.close()


def test_editing_request_invalidates_in_flight_result():
    dialog = make_dialog()
    request_id = dialog.request_translation()

    dialog.source_text_edit.setPlainText("jump on contact")

    assert dialog.active_request_id == request_id
    assert dialog.request_in_flight is True
    assert dialog.translate_button.isEnabled() is False
    assert "waiting" in dialog.status_label.text()
    assert dialog.request_translation() is None
    assert dialog.finish_translation(request_id, "late translation") is False
    assert dialog.active_request_id is None
    assert dialog.translate_button.isEnabled() is True
    assert dialog.fail_translation(request_id, "late error") is False
    assert dialog.output_text_edit.toPlainText() == ""
    dialog.close()


def test_editing_during_failed_request_waits_for_failure_before_retry():
    dialog = make_dialog()
    request_id = dialog.request_translation()

    dialog.target_language_combo.setCurrentIndex(
        dialog.target_language_combo.findData("fr")
    )

    assert dialog.request_translation() is None
    assert dialog.fail_translation(request_id, "old provider error") is False
    assert "old provider error" not in dialog.status_label.text()
    assert dialog.request_translation() == 2
    dialog.close()


def test_editing_after_completion_clears_mismatched_result_and_copy_action():
    dialog = make_dialog()
    request_id = dialog.request_translation()
    dialog.finish_translation(request_id, "am Tor ausrichten")

    dialog.target_language_combo.setCurrentIndex(
        dialog.target_language_combo.findData("fr")
    )

    assert dialog.output_text_edit.toPlainText() == ""
    assert dialog.copy_button.isEnabled() is False
    assert "Message changed" in dialog.status_label.text()
    dialog.close()


def test_current_error_leaves_dialog_ready_for_retry():
    dialog = make_dialog()
    request_id = dialog.request_translation()

    assert dialog.fail_translation(request_id, "Provider unavailable") is True

    assert dialog.status_label.text() == "Provider unavailable"
    assert "#c0392b" in dialog.status_label.styleSheet()
    assert dialog.translate_button.isEnabled() is True
    assert dialog.copy_button.isEnabled() is False
    assert dialog.request_in_flight is False
    dialog.close()


def test_result_is_only_copied_after_explicit_action():
    dialog = make_dialog()
    clipboard = QApplication.clipboard()
    previous = clipboard.text()
    try:
        clipboard.setText("leave this alone")
        request_id = dialog.request_translation()
        dialog.finish_translation(request_id, "am Tor ausrichten")

        assert clipboard.text() == "leave this alone"
        assert dialog.copy_translation() is True
        assert clipboard.text() == "am Tor ausrichten"
        assert dialog.status_label.text().startswith("Copied.")
    finally:
        clipboard.setText(previous)
        dialog.close()


def test_language_boxes_accept_manual_iso_codes_and_validate_them():
    dialog = make_dialog()
    emitted = []
    dialog.translation_requested.connect(lambda *request: emitted.append(request))
    dialog.source_language_combo.setEditText("pt_BR")
    dialog.target_language_combo.setEditText("ZH-hant")

    request_id = dialog.request_translation()

    assert request_id == 1
    assert emitted == [(1, "align to the gate", "pt-br", "zh-hant")]

    dialog.target_language_combo.setEditText("not a language")
    assert dialog.translate_button.isEnabled() is False
    assert dialog.request_translation() is None
    assert "waiting" in dialog.status_label.text()
    assert dialog.finish_translation(request_id, "stale translation") is False
    assert dialog.request_translation() is None
    assert "valid target language" in dialog.status_label.text()
    dialog.close()


def test_accessibility_shortcuts_and_privacy_boundary_are_explicit():
    dialog = ComposeDialog(provider_label="DeepL cloud provider")

    keys = {shortcut.key().toString() for shortcut in dialog.translate_shortcuts}
    assert keys == {
        QKeySequence("Ctrl+Return").toString(),
        QKeySequence("Ctrl+Enter").toString(),
    }
    assert dialog.copy_shortcut.key() == QKeySequence("Ctrl+Shift+C")
    assert dialog.source_text_edit.accessibleName() == "Message to translate"
    assert "sent to your DeepL cloud provider" in dialog.notice_label.text()
    assert "Nothing is posted to EVE" in dialog.notice_label.text()
    assert "manual" in dialog.notice_label.text()
    dialog.close()


def test_present_is_a_tray_and_hotkey_safe_entrypoint():
    dialog = ComposeDialog()

    dialog.present()
    app.processEvents()

    assert dialog.isVisible() is True
    assert dialog.source_text_edit.hasFocus() is True
    dialog.close()

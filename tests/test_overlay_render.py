"""Translation updates redraw one message instead of the whole overlay."""

import sys
import time

import pytest
from PySide6.QtWidgets import QApplication

from src.app_config import get_default_config
from src.core.combat import classify_commands
from src.core.message import ProcessedMessage, TranslationState
from src.gui.overlay import OverlayWindow


@pytest.fixture(scope="module", autouse=True)
def qt_app():
    app = QApplication.instance() or QApplication(sys.argv)
    yield app


def _overlay(**overrides):
    config = get_default_config()
    merged = {**config["shared"], **config["sessions"]["fleet"]}
    merged.update({"x": 40, "y": 40, "w": 460, "h": 360})
    merged.update(overrides)
    overlay = OverlayWindow("fleet", merged)
    overlay.show()
    QApplication.processEvents()
    return overlay


def _flush(overlay):
    deadline = time.monotonic() + 1.0
    while time.monotonic() < deadline:
        QApplication.processEvents()
        if not overlay._flush_timer.isActive() and not overlay._pending_messages:
            break
        time.sleep(0.01)
    QApplication.processEvents()


def _message(index, state, *, text="bonjour", commands=(), revision=1):
    translated = state == TranslationState.TRANSLATED
    return ProcessedMessage(
        message_id=f"fleet:s:{index}",
        session_id="fleet",
        source_id="s",
        text=f"hello {index}" if translated else f"{text} {index}",
        original_text=f"{text} {index}" if translated else "",
        sender="Pilot",
        timestamp=f"12:00:{index:02d}",
        state=state,
        provider="DeepL" if translated else "",
        detected_language="fr",
        translation_relevant=True,
        commands=tuple(commands),
        revision=revision,
        lookup_text=f"{text} {index}",
    )


def test_translation_update_redraws_only_that_message():
    overlay = _overlay()
    for index in range(20):
        overlay.upsert_processed_message(_message(index, TranslationState.PENDING))
    _flush(overlay)
    refreshes = []
    original_refresh = overlay.refresh_ui
    overlay.refresh_ui = lambda: (refreshes.append(True), original_refresh())

    overlay.upsert_processed_message(
        _message(7, TranslationState.TRANSLATED, revision=2)
    )
    _flush(overlay)
    in_place_text = overlay.text_browser.toPlainText()
    original_refresh()
    rebuilt_text = overlay.text_browser.toPlainText()

    assert refreshes == []
    assert "hello 7" in in_place_text
    assert in_place_text == rebuilt_text


def test_update_of_a_line_hidden_in_combat_mode_does_not_redraw():
    overlay = _overlay(display_mode="combat")
    overlay.upsert_processed_message(_message(1, TranslationState.PENDING))
    _flush(overlay)
    refreshes = []
    overlay.refresh_ui = lambda: refreshes.append(True)

    overlay.upsert_processed_message(
        _message(1, TranslationState.TRANSLATED, revision=2)
    )
    _flush(overlay)

    assert refreshes == []
    assert not overlay._flush_timer.isActive()


def test_command_card_update_falls_back_to_a_full_refresh():
    overlay = _overlay()
    commands = classify_commands("align to gate")
    overlay.upsert_processed_message(
        _message(1, TranslationState.PENDING, text="align to gate", commands=commands)
    )
    _flush(overlay)
    refreshes = []
    original_refresh = overlay.refresh_ui
    overlay.refresh_ui = lambda: (refreshes.append(True), original_refresh())

    overlay.upsert_processed_message(
        _message(1, TranslationState.FAILED, text="align to gate", commands=commands, revision=2)
    )
    _flush(overlay)

    assert refreshes == [True]

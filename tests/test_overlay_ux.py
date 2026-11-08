"""Overlay UX: reading position, unread pill, header chip, menus and caching."""

import sys
import time
from unittest.mock import patch

import pytest
from PySide6.QtCore import QEvent, QPoint, QPointF, Qt
from PySide6.QtGui import QMouseEvent
from PySide6.QtWidgets import QApplication

from src.app_config import (
    DEFAULT_COLOR_HIGHLIGHT,
    DEFAULT_COLOR_TRANSLATED,
    OVERLAY_PALETTE_VERSION,
    get_default_config,
    normalize_config,
)
from src.core.combat import CommandMatch
from src.core.message import ProcessedMessage, TranslationState
from src.gui.overlay import MAX_VISIBLE_MESSAGES, OverlayWindow, _dispose_menu


@pytest.fixture(scope="module", autouse=True)
def qt_app():
    app = QApplication.instance() or QApplication(sys.argv)
    yield app


def _overlay(**overrides):
    config = get_default_config()
    merged = {**config["shared"], **config["sessions"]["fleet"]}
    merged.update({"x": 60, "y": 60, "w": 360, "h": 260})
    merged.update(overrides)
    overlay = OverlayWindow("fleet", merged)
    overlay.show()
    QApplication.processEvents()
    return overlay


def _settle(overlay, timeout=1.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        QApplication.processEvents()
        if not overlay._flush_timer.isActive() and not overlay._pending_messages:
            break
        time.sleep(0.01)
    QApplication.processEvents()


def _message(index, *, state=TranslationState.ORIGINAL, text=None, sender="Pilot",
             commands=(), detected="fr"):
    translated = state == TranslationState.TRANSLATED
    return ProcessedMessage(
        message_id=f"fleet:ux:{index}",
        session_id="fleet",
        source_id="ux",
        text=text or (
            f"hello {index}" if translated else f"line {index} with enough words to wrap around"
        ),
        original_text=f"bonjour {index}" if translated else "",
        sender=sender,
        timestamp=f"12:{index // 60 % 60:02d}:{index % 60:02d}",
        state=state,
        detected_language=detected,
        translation_relevant=translated,
        commands=tuple(commands),
        revision=1,
    )


def _fill(overlay, count, start=0):
    for index in range(start, start + count):
        overlay.upsert_processed_message(_message(index))
    _settle(overlay)


def test_every_message_is_one_block_with_its_decoration_state():
    overlay = _overlay(speaker_roles={"fleet boss": "fc"})
    overlay.clear_messages()
    jump = CommandMatch("JUMP", "MOVE", source_clause="jump")
    overlay.upsert_processed_message(_message(1, state=TranslationState.TRANSLATED))
    overlay.upsert_processed_message(
        _message(2, text="jump", sender="Fleet Boss", commands=(jump,))
    )
    _settle(overlay)

    document = overlay.text_browser.document()
    line, card = document.firstBlock(), document.lastBlock()
    assert document.blockCount() == 2
    assert line.userState() & 0xF == 1
    assert card.userState() & 0xF == 3
    assert card.userState() & 0x100  # FC-verified card
    # Wrapped lines hang under the speaker, keeping a clean time column.
    assert line.blockFormat().textIndent() == -line.blockFormat().leftMargin() < 0
    overlay.close()


def test_new_lines_follow_a_reader_at_the_bottom():
    overlay = _overlay()
    _fill(overlay, 40)
    bar = overlay.text_browser.verticalScrollBar()
    assert bar.maximum() > 0
    assert bar.value() == bar.maximum()

    _fill(overlay, 3, start=100)

    assert bar.value() == bar.maximum()
    assert not overlay.text_browser.pill.isVisible()
    overlay.close()


def test_new_lines_do_not_yank_a_reader_who_scrolled_up():
    overlay = _overlay()
    _fill(overlay, 40)
    bar = overlay.text_browser.verticalScrollBar()
    bar.setValue(0)
    QApplication.processEvents()

    _fill(overlay, 3, start=100)

    pill = overlay.text_browser.pill
    assert bar.value() == 0
    assert overlay._unread == 3
    assert pill.isVisible()

    pill.clicked.emit()
    QApplication.processEvents()
    assert bar.value() == bar.maximum()
    assert not pill.isVisible()
    overlay.close()


def test_scrolling_back_down_clears_the_unread_pill():
    overlay = _overlay()
    _fill(overlay, 40)
    bar = overlay.text_browser.verticalScrollBar()
    bar.setValue(0)
    _fill(overlay, 2, start=100)
    assert overlay.text_browser.pill.isVisible()

    bar.setValue(bar.maximum())
    QApplication.processEvents()

    assert overlay._unread == 0
    assert not overlay.text_browser.pill.isVisible()
    overlay.close()


def test_locked_overlay_hides_the_scroll_bar_and_follows_the_latest_line():
    overlay = _overlay()
    _fill(overlay, 40)
    view = overlay.text_browser
    view.verticalScrollBar().setValue(0)

    overlay.set_interaction_locked(True)
    QApplication.processEvents()

    bar = view.verticalScrollBar()
    assert view.verticalScrollBarPolicy() == Qt.ScrollBarPolicy.ScrollBarAlwaysOff
    assert bar.value() == bar.maximum()
    assert not view.pill.isVisible()

    overlay.set_interaction_locked(False)
    assert view.verticalScrollBarPolicy() == Qt.ScrollBarPolicy.ScrollBarAsNeeded
    overlay.close()


def test_clicking_the_mode_chip_cycles_the_display_mode():
    overlay = _overlay()
    saved = []
    overlay.config_updated.connect(lambda config: saved.append(config["display_mode"]))
    header = overlay.status_header

    def click_chip():
        point = header._layout().chip.center()
        global_point = QPointF(header.mapToGlobal(point.toPoint()))
        for event_type, buttons in (
            (QEvent.Type.MouseButtonPress, Qt.MouseButton.LeftButton),
            (QEvent.Type.MouseButtonRelease, Qt.MouseButton.NoButton),
        ):
            overlay.eventFilter(header, QMouseEvent(
                event_type, point, global_point, Qt.MouseButton.LeftButton,
                buttons, Qt.KeyboardModifier.NoModifier,
            ))

    assert header.region_at(header._layout().chip.center()) == 'mode'
    click_chip()
    assert overlay.display_mode == "bilingual"
    assert "Bilingual" in header.text()
    click_chip()
    assert overlay.display_mode == "combat"
    assert saved[-1] == "combat"
    overlay.close()


def test_right_clicked_translation_gets_its_own_menu_actions():
    overlay = _overlay()
    overlay.clear_messages()
    overlay.upsert_processed_message(
        _message(7, state=TranslationState.TRANSLATED, sender="Wang Lei")
    )
    _settle(overlay)
    view = overlay.text_browser
    rect = view.document().documentLayout().blockBoundingRect(view.document().firstBlock())
    point = view.viewport().mapToGlobal(QPoint(int(rect.center().x()), int(rect.center().y())))

    target = overlay._message_at_global(point)
    assert target is not None and target["message_id"] == "fleet:ux:7"

    menu = overlay._build_context_menu(target)
    actions = {action.text(): action for action in menu.actions()}
    assert {"Copy translation", "Copy original", "Improve this translation…"} <= set(actions)
    assert actions["Speaker role"].menu() is not None
    with patch.object(QApplication, "clipboard") as clipboard:
        actions["Copy translation"].trigger()
    clipboard.return_value.setText.assert_called_once_with("hello 7")
    _dispose_menu(menu)
    overlay.close()


def test_menu_outside_a_line_offers_latest_message_shortcuts_and_lock():
    overlay = _overlay()
    overlay.clear_messages()
    overlay.upsert_processed_message(
        _message(3, state=TranslationState.TRANSLATED, sender="Li Na")
    )
    _settle(overlay)
    viewport = overlay.text_browser.viewport()
    below = viewport.mapToGlobal(QPoint(viewport.width() // 2, viewport.height() - 3))
    assert overlay._message_at_global(below) is None

    menu = overlay._build_context_menu(None)
    actions = {action.text(): action for action in menu.actions()}
    assert "Speaker: Li Na" in actions
    assert "Improve last translation…" in actions
    requested = []
    overlay.lock_requested.connect(requested.append)
    next(action for text, action in actions.items() if text.startswith("Lock overlays")).trigger()
    assert requested == [True]
    _dispose_menu(menu)
    overlay.close()


def test_mode_switch_rebuilds_reuse_parsed_messages():
    overlay = _overlay()
    _fill(overlay, 20)
    with patch.object(overlay, "_render_message", wraps=overlay._render_message) as render:
        overlay.set_display_mode("bilingual", save=False)
        overlay.set_display_mode("mirror", save=False)
    assert render.call_count == 0

    # A style change re-renders every visible line once.
    with patch.object(overlay, "_render_message", wraps=overlay._render_message) as render:
        overlay.config["color_translated"] = "#123456"
        overlay.apply_config()
    assert render.call_count == len(overlay.chat_history)
    overlay.close()


def test_parsed_message_cache_stays_bounded():
    overlay = _overlay()
    for start in range(0, MAX_VISIBLE_MESSAGES + 60, 10):
        _fill(overlay, 10, start=start)
    assert len(overlay._fragment_cache) <= len(overlay.chat_history) + 16
    overlay.close()


def test_translation_identical_to_its_original_takes_one_line():
    overlay = _overlay()
    rendered = overlay._format_message_html({
        "message_id": "same", "text": "GF", "original_text": "gf", "sender": "Pilot",
        "timestamp": "12:00:00", "state": "translated", "commands": (),
    })
    assert "<br>" not in rendered
    overlay.close()


def test_unverified_command_card_mutes_its_target():
    primary = CommandMatch(
        "PRIMARY", "TARGET", argument="Vexor", source_clause="primary vexor"
    )
    trusted = _overlay(speaker_roles={"pilot": "fc"})
    untrusted = _overlay()
    message = _message(9, text="primary vexor", commands=(primary,), detected="en")
    trusted.upsert_processed_message(message)
    untrusted.upsert_processed_message(message)

    vivid = f"color:{DEFAULT_COLOR_HIGHLIGHT};'>Vexor"
    assert vivid in trusted._format_message_html(trusted.chat_history[-1])
    assert vivid not in untrusted._format_message_html(untrusted.chat_history[-1])
    assert "UNVERIFIED" in untrusted._format_message_html(untrusted.chat_history[-1])
    trusted.close()
    untrusted.close()


def test_original_neon_defaults_migrate_once_to_the_refined_palette():
    config = get_default_config()
    shared = config["shared"]
    shared.pop("overlay_palette")
    shared["color_translated"] = "#00FFFF"
    shared["color_highlight"] = "yellow"

    normalize_config(config)

    assert shared["color_translated"] == DEFAULT_COLOR_TRANSLATED
    assert shared["color_highlight"] == DEFAULT_COLOR_HIGHLIGHT
    assert shared["overlay_palette"] == OVERLAY_PALETTE_VERSION

    # A colour chosen deliberately after the migration is kept.
    shared["color_translated"] = "#00ffff"
    normalize_config(config)
    assert shared["color_translated"] == "#00ffff"


def test_custom_overlay_colours_are_never_migrated():
    config = get_default_config()
    shared = config["shared"]
    shared.pop("overlay_palette")
    shared["color_translated"] = "#33ff99"
    shared["color_highlight"] = "#ff00ff"

    normalize_config(config)

    assert (shared["color_translated"], shared["color_highlight"]) == ("#33ff99", "#ff00ff")

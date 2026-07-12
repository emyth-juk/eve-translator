import sys
from unittest.mock import MagicMock, patch

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QApplication

from src.core.combat import CommandMatch
from src.core.message import ProcessedMessage, TranslationState
from src.gui.overlay import OverlayStatus, OverlayWindow


app = QApplication.instance() or QApplication(sys.argv)


def make_overlay(**config):
    base = {
        'x': 100, 'y': 100, 'w': 400, 'h': 260,
        'opacity': 0.8, 'font_size': 10,
        'color_default': '#e0e0e0', 'color_translated': '#00ffff',
        'color_highlight': 'yellow', 'auto_scroll': True,
        'background_color': '#33001a', 'display_mode': 'mirror',
        'compact_header': True, 'command_cards': True, 'speaker_roles': {},
    }
    base.update(config)
    overlay = OverlayWindow('fleet', base)
    overlay.clear_messages()
    return overlay


def message(identifier, text, *, relevant=False, role='', commands=(), state=None):
    return ProcessedMessage(
        message_id=identifier,
        session_id='fleet',
        source_id='source',
        text=text,
        original_text='原文' if state == TranslationState.TRANSLATED else '',
        sender='Pilot',
        timestamp='12:00:00',
        state=state or TranslationState.ORIGINAL,
        translation_relevant=relevant,
        speaker_role=role,
        commands=commands,
    )


def test_mode_filtering_keeps_full_history_and_never_reprocesses():
    overlay = make_overlay()
    try:
        overlay.upsert_processed_message(message('ordinary', 'ordinary'))
        overlay.upsert_processed_message(message('translated', 'translated', relevant=True))
        overlay.upsert_processed_message(message('fc', 'fc chatter', role='fc'))

        overlay.set_display_mode('bilingual', save=False)
        plain = overlay.text_browser.toPlainText()
        assert 'ordinary' not in plain
        assert 'translated' in plain
        assert 'fc chatter' in plain

        overlay.set_display_mode('combat', save=False)
        plain = overlay.text_browser.toPlainText()
        assert 'translated' not in plain
        assert 'fc chatter' in plain
        assert len(overlay.chat_history) == 3

        overlay.set_display_mode('mirror', save=False)
        assert 'ordinary' in overlay.text_browser.toPlainText()
    finally:
        overlay.close()


def test_command_card_is_trusted_only_for_explicit_fc_and_shows_original():
    command = CommandMatch('JUMP', 'MOVE', source_clause='jump')
    overlay = make_overlay(speaker_roles={'pilot': 'fc'})
    try:
        rendered = overlay._format_message_html(
            overlay_message_dict(overlay, message('jump', 'jump', commands=(command,)))
        )
        assert 'FC VERIFIED' in rendered
        assert 'JUMP' in rendered
        assert 'jump' in rendered

        overlay.config['speaker_roles'] = {}
        rendered = overlay._format_message_html(
            overlay_message_dict(overlay, message('jump2', 'jump', commands=(command,)))
        )
        assert 'UNVERIFIED' in rendered
    finally:
        overlay.close()


def overlay_message_dict(overlay, processed):
    overlay.upsert_processed_message(processed)
    return overlay.chat_history[-1]


def test_late_pending_update_cannot_replace_final_translation():
    overlay = make_overlay()
    try:
        final = message('same', 'hello', relevant=True, state=TranslationState.TRANSLATED)
        pending = message('same', 'bonjour', relevant=True, state=TranslationState.PENDING)
        overlay.upsert_processed_message(final)
        overlay.upsert_processed_message(pending)
        assert overlay.chat_history[0]['state'] == 'translated'
        assert overlay.chat_history[0]['text'] == 'hello'
    finally:
        overlay.close()


def test_interaction_lock_preserves_core_flags_geometry_and_visibility():
    overlay = make_overlay()
    try:
        overlay.show()
        app.processEvents()
        geometry = overlay.geometry()
        overlay.set_interaction_locked(True)
        flags = overlay.windowFlags()
        assert flags & Qt.WindowType.WindowTransparentForInput
        assert flags & Qt.WindowType.WindowDoesNotAcceptFocus
        assert flags & Qt.WindowType.FramelessWindowHint
        assert flags & Qt.WindowType.WindowStaysOnTopHint
        assert overlay.geometry() == geometry
        assert overlay.isVisible()

        overlay.set_interaction_locked(False)
        flags = overlay.windowFlags()
        assert not flags & Qt.WindowType.WindowTransparentForInput
        assert not flags & Qt.WindowType.WindowDoesNotAcceptFocus
        assert flags & Qt.WindowType.FramelessWindowHint
    finally:
        overlay.close()


def test_structured_status_header_exposes_mode_lock_and_health():
    overlay = make_overlay()
    try:
        overlay.update_status(OverlayStatus(
            channel='Fleet', state='Active', context='Alice', provider='DeepL',
            provider_available=False, display_mode='combat', interaction_locked=True,
            detail='Provider unavailable',
        ))
        text = overlay.status_header.text()
        assert 'FLEET' in text
        assert 'Active' in text
        assert 'Combat' in text
        assert overlay.status_header.toolTip() == 'Provider unavailable'
    finally:
        overlay.close()


def test_correction_uses_canonical_provider_source_for_linked_messages():
    overlay = make_overlay()
    try:
        processed = ProcessedMessage(
            message_id='linked', session_id='fleet', source_id='source',
            text='Align Jita', original_text='align Jita', sender='FC',
            timestamp='12:00:00', state=TranslationState.TRANSLATED,
            detected_language='fr', translation_relevant=True,
            lookup_text='align __EVELINK_1__Jita__EVELINK_2__',
        )
        overlay.upsert_processed_message(processed)
        received = MagicMock()
        overlay.correction_submitted.connect(received)

        with patch(
            'src.gui.overlay.QInputDialog.getMultiLineText',
            return_value=('Align to Jita', True),
        ):
            overlay._request_correction(overlay.chat_history[0])

        assert received.call_args.args[0]['source_text'] == (
            'align __EVELINK_1__Jita__EVELINK_2__'
        )
    finally:
        overlay.close()

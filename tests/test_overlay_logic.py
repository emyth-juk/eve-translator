import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from PySide6.QtWidgets import QApplication
from PySide6.QtTest import QTest

from src.core.character_info import CharacterInfo
from src.core.channel_info import ChatChannelInfo
from src.core.fleet_info import FleetInfo
from src.core.message import ProcessedMessage, TranslationState


class TestOverlayNonUILogic(unittest.TestCase):
    """Tests for OverlayWindow non-UI methods: HTML stripping, message management."""

    @classmethod
    def setUpClass(cls):
        if not QApplication.instance():
            cls.app = QApplication(sys.argv)

    def _make_overlay(self, session_id='fleet'):
        from src.gui.overlay import OverlayWindow
        config = {
            'x': 100, 'y': 100, 'w': 300, 'h': 200,
            'opacity': 0.8, 'font_size': 10,
            'color_default': '#e0e0e0',
            'color_translated': '#00ffff',
            'color_highlight': 'yellow',
            'auto_scroll': True,
            'background_color': '#33001a',
        }
        overlay = OverlayWindow(session_id=session_id, initial_config=config)
        self.addCleanup(overlay.close)
        return overlay

    def _wait_for_overlay_flush(self):
        from src.gui.overlay import UI_FLUSH_INTERVAL_MS

        QTest.qWait(UI_FLUSH_INTERVAL_MS + 50)
        QApplication.processEvents()

    # --- _strip_html ---

    def test_strip_html_removes_tags(self):
        overlay = self._make_overlay()
        result = overlay._strip_html("<span style='color: red;'>Hello</span> world")
        self.assertEqual(result, "Hello world")

    def test_strip_html_unescapes_entities(self):
        overlay = self._make_overlay()
        result = overlay._strip_html("&lt;test&gt; &amp; value")
        self.assertEqual(result, "<test> & value")

    def test_strip_html_empty_string(self):
        overlay = self._make_overlay()
        self.assertEqual(overlay._strip_html(""), "")
        self.assertEqual(overlay._strip_html(None), "")

    def test_strip_html_nested_tags(self):
        overlay = self._make_overlay()
        result = overlay._strip_html("<div><span>nested</span> text</div>")
        self.assertEqual(result, "nested text")

    # --- add_message ---

    def test_add_message_stores_in_history(self):
        overlay = self._make_overlay()
        initial_count = len(overlay.chat_history)
        overlay.add_message("Test", "Player", "08:00:00", "", False)
        self.assertEqual(len(overlay.chat_history), initial_count + 1)

        msg = overlay.chat_history[-1]
        self.assertEqual(msg['text'], "Test")
        self.assertEqual(msg['sender'], "Player")
        self.assertEqual(msg['timestamp'], "08:00:00")
        self.assertFalse(msg['is_translated'])

    def test_add_message_translated(self):
        overlay = self._make_overlay()
        overlay.add_message("Translated text", "Player", "08:00:00", "Original text", True)
        msg = overlay.chat_history[-1]
        self.assertTrue(msg['is_translated'])
        self.assertEqual(msg['original_text'], "Original text")

    def test_structured_pending_message_is_replaced_in_place(self):
        overlay = self._make_overlay()
        overlay.clear_messages()
        pending = ProcessedMessage(
            message_id="fleet:1",
            session_id="fleet",
            text="bonjour",
            sender="Pilot",
            timestamp="08:00:00",
            state=TranslationState.PENDING,
            detected_language="fr",
        )
        translated = ProcessedMessage(
            message_id="fleet:1",
            session_id="fleet",
            text="hello",
            sender="Pilot",
            timestamp="08:00:00",
            original_text="bonjour",
            state=TranslationState.TRANSLATED,
            provider="DeepL",
            detected_language="fr",
        )

        overlay.upsert_processed_message(pending)
        overlay.upsert_processed_message(translated)

        self.assertEqual(len(overlay.chat_history), 1)
        self.assertEqual(overlay.chat_history[0]['text'], "hello")
        self.assertEqual(overlay.chat_history[0]['state'], "translated")

    def test_failed_message_html_shows_original_and_warning(self):
        overlay = self._make_overlay()
        message = ProcessedMessage(
            message_id="fleet:2",
            session_id="fleet",
            text="bonjour",
            sender="Pilot",
            timestamp="08:00:00",
            state=TranslationState.FAILED,
            provider="DeepL",
            detected_language="fr",
            error="DeepL translation unavailable",
        )

        overlay.upsert_processed_message(message)
        rendered = overlay._format_message_html(overlay.chat_history[-1])

        self.assertIn("bonjour", rendered)
        self.assertIn("DeepL translation unavailable", rendered)
        self.assertIn("#ffbf69", rendered)

    def test_translation_update_preserves_manual_scroll_position(self):
        overlay = self._make_overlay()
        overlay.config['auto_scroll'] = False
        overlay.show()
        QApplication.processEvents()
        overlay.clear_messages()

        for index in range(80):
            overlay.add_message(
                f"Message {index} with enough text to make the overlay scroll",
                "Pilot",
                "08:00:00",
                "",
                False,
            )
        pending = ProcessedMessage(
            message_id="fleet:scroll",
            session_id="fleet",
            source_id="source-1",
            text="bonjour",
            sender="Pilot",
            timestamp="08:00:01",
            state=TranslationState.PENDING,
        )
        overlay.upsert_processed_message(pending)
        self._wait_for_overlay_flush()

        scroll_bar = overlay.text_browser.verticalScrollBar()
        scroll_bar.setValue(scroll_bar.maximum() // 2)
        before = scroll_bar.value()
        translated = ProcessedMessage(
            message_id="fleet:scroll",
            session_id="fleet",
            source_id="source-1",
            text="hello",
            sender="Pilot",
            timestamp="08:00:01",
            original_text="bonjour",
            state=TranslationState.TRANSLATED,
            provider="DeepL",
        )

        overlay.upsert_processed_message(translated)
        self._wait_for_overlay_flush()

        self.assertGreater(before, 0)
        self.assertEqual(scroll_bar.value(), before)

    def test_add_message_trims_at_100(self):
        overlay = self._make_overlay()
        # Clear welcome message
        overlay.clear_messages()

        # Add 101 messages
        for i in range(101):
            overlay.add_message(f"Msg {i}", "Player", "08:00:00", "", False)

        # Should trim to 100
        self.assertEqual(len(overlay.chat_history), 100)

    def test_hidden_overlay_history_remains_capped(self):
        from src.gui.overlay import MAX_VISIBLE_MESSAGES

        overlay = self._make_overlay()
        overlay.clear_messages()

        for i in range(MAX_VISIBLE_MESSAGES + 50):
            overlay.add_message(f"Msg {i}", "Player", "08:00:00", "", False)

        self.assertEqual(len(overlay.chat_history), MAX_VISIBLE_MESSAGES)
        self.assertEqual(overlay.chat_history[0]['text'], "Msg 50")
        self.assertTrue(overlay._needs_full_refresh)

    def test_visible_overlay_document_block_count_remains_capped(self):
        from src.gui.overlay import MAX_VISIBLE_MESSAGES

        overlay = self._make_overlay()
        overlay.show()
        QApplication.processEvents()
        overlay.clear_messages()

        for i in range(MAX_VISIBLE_MESSAGES + 50):
            overlay.add_message(f"Msg {i}", "Player", "08:00:00", "", False)

        self._wait_for_overlay_flush()

        self.assertEqual(len(overlay.chat_history), MAX_VISIBLE_MESSAGES)
        self.assertLessEqual(overlay.text_browser.document().blockCount(), MAX_VISIBLE_MESSAGES)

    def test_style_change_rebuilds_once_and_preserves_messages(self):
        overlay = self._make_overlay()
        overlay.show()
        QApplication.processEvents()
        overlay.clear_messages()
        overlay.add_message(
            "Move to <span style='color: yellow;'>Jita</span>",
            "FC",
            "08:00:00",
            "",
            False,
        )
        self._wait_for_overlay_flush()

        with patch.object(overlay, "refresh_ui", wraps=overlay.refresh_ui) as refresh_spy:
            overlay.config['color_highlight'] = '#123456'
            overlay.apply_config()

        rendered_html = overlay.text_browser.toHtml()
        self.assertEqual(refresh_spy.call_count, 1)
        self.assertIn("Move to", rendered_html)
        self.assertIn("#123456", rendered_html)

    def test_batched_message_flush_scrolls_once(self):
        overlay = self._make_overlay()
        overlay.show()
        QApplication.processEvents()
        overlay.clear_messages()

        original_move_cursor = overlay.text_browser.moveCursor
        overlay.text_browser.moveCursor = MagicMock(side_effect=original_move_cursor)

        for i in range(10):
            overlay.add_message(f"Msg {i}", "Player", "08:00:00", "", False)

        self._wait_for_overlay_flush()

        self.assertEqual(overlay.text_browser.moveCursor.call_count, 1)

    def test_export_includes_bounded_current_history(self):
        overlay = self._make_overlay()
        overlay.clear_messages()
        for i in range(150):
            overlay.add_message(f"Msg {i}", "Player", "08:00:00", "", False)

        with tempfile.TemporaryDirectory() as temp_dir:
            export_path = Path(temp_dir) / "overlay-export.txt"
            with patch(
                'src.gui.overlay.QFileDialog.getSaveFileName',
                return_value=(str(export_path), "Text Files (*.txt)"),
            ), patch('src.gui.overlay.QMessageBox.information'):
                overlay.export_chat()

            exported_lines = export_path.read_text(encoding='utf-8').splitlines()

        self.assertEqual(len(exported_lines), 100)
        self.assertIn("Msg 50", exported_lines[0])
        self.assertIn("Msg 149", exported_lines[-1])

    def test_clear_messages_clears_history_and_text_browser(self):
        overlay = self._make_overlay()
        overlay.add_message("Test", "Player", "08:00:00", "", False)
        self.assertTrue(overlay.chat_history)

        overlay.clear_messages()

        self.assertEqual(list(overlay.chat_history), [])
        self.assertEqual(overlay.text_browser.toPlainText(), "")

    def test_exit_requested_signal(self):
        overlay = self._make_overlay()
        signal_spy = MagicMock()
        overlay.exit_requested.connect(signal_spy)

        overlay.exit_requested.emit()

        signal_spy.assert_called_once()

    # --- update_session_states ---

    def test_update_session_states(self):
        overlay = self._make_overlay()
        overlay.update_session_states({'fleet': True, 'local': False})
        self.assertEqual(overlay.all_session_states, {'fleet': True, 'local': False})

    # --- update_character_list ---

    def test_update_character_list(self):
        overlay = self._make_overlay('local')
        chars = {
            '123': CharacterInfo('123', 'TestPilot', '/path', 1000, 'Jita', True),
        }
        overlay.update_character_list(chars)
        self.assertEqual(len(overlay.available_characters), 1)
        self.assertIn('123', overlay.available_characters)

    # --- update_fleet_list ---

    def test_update_fleet_list(self):
        overlay = self._make_overlay('fleet')
        fleets = {
            'f1': FleetInfo('f1', 'Pilot1', '/path1', 1000, 100.0, True),
            'f2': FleetInfo('f2', 'Pilot2', '/path2', 2000, 200.0, True),
        }
        overlay.update_fleet_list(fleets, 'f1')
        self.assertEqual(len(overlay.available_fleets), 2)
        self.assertEqual(overlay.selected_fleet_id, 'f1')

    def test_update_any_chat_list_tracks_stable_selection(self):
        overlay = self._make_overlay('any')
        channel = ChatChannelInfo(
            selection_id='player_intel::123',
            channel_id='player_intel',
            channel_name='Intel',
            character_id='123',
            listener_name='TestPilot',
            log_path='/path/intel.txt',
            log_mtime=1000,
            started_time=900,
            is_active=True,
        )

        overlay.update_channel_list(
            {channel.selection_id: channel},
            channel.selection_id,
        )

        self.assertEqual(overlay.available_channels[channel.selection_id], channel)
        self.assertEqual(overlay.selected_channel_id, channel.selection_id)
        self.assertEqual(
            overlay.config['channel_selection'],
            channel.selection_id,
        )

    def test_any_chat_overlay_uses_explicit_window_label(self):
        overlay = self._make_overlay('any')

        self.assertIn('Any Chat', overlay.windowTitle())

    # --- _format_message_html ---

    def test_format_untranslated_message(self):
        overlay = self._make_overlay()
        msg = {
            'text': 'Hello fleet',
            'sender': 'Player1',
            'timestamp': '08:00:00',
            'original_text': '',
            'is_translated': False,
        }
        html = overlay._format_message_html(msg)
        self.assertIn('Player1', html)
        self.assertIn('Hello fleet', html)
        self.assertIn('08:00:00', html)
        self.assertIn(overlay.config['color_default'], html)

    def test_format_translated_message(self):
        overlay = self._make_overlay()
        msg = {
            'text': 'Translated text',
            'sender': 'Player1',
            'timestamp': '08:00:00',
            'original_text': 'Original text',
            'is_translated': True,
        }
        html = overlay._format_message_html(msg)
        self.assertIn('Translated text', html)
        self.assertIn('Original text', html)
        self.assertIn(overlay.config['color_translated'], html)

    def test_format_translated_message_none_original(self):
        overlay = self._make_overlay()
        msg = {
            'text': 'Translated text',
            'sender': 'Player1',
            'timestamp': '08:00:00',
            'original_text': None,
            'is_translated': True,
        }
        # Should not crash
        html = overlay._format_message_html(msg)
        self.assertIn('Translated text', html)

    def test_format_applies_custom_colors(self):
        overlay = self._make_overlay()
        overlay.config['color_default'] = '#ff0000'
        overlay.config['color_translated'] = '#00ff00'
        overlay.config['color_highlight'] = '#0000ff'

        msg = {
            'text': "Has <span style='color: yellow;'>highlight</span>",
            'sender': 'Player',
            'timestamp': '08:00:00',
            'original_text': '',
            'is_translated': False,
        }
        html = overlay._format_message_html(msg)
        self.assertIn('#ff0000', html)
        self.assertIn('color: #0000ff', html)


class TestDataclassStr(unittest.TestCase):
    """Tests for CharacterInfo and FleetInfo __str__ methods."""

    def test_character_info_str_with_system(self):
        char = CharacterInfo('123', 'TestPilot', '/path', 1000, 'Jita', True)
        self.assertEqual(str(char), "TestPilot (Jita)")

    def test_character_info_str_without_system(self):
        char = CharacterInfo('123', 'TestPilot', '/path', 1000, None, True)
        self.assertEqual(str(char), "TestPilot")

    def test_fleet_info_str(self):
        fleet = FleetInfo('f1', 'Pilot1', '/path', 1000, 100.0, True)
        self.assertEqual(str(fleet), "Fleet - Pilot1")


if __name__ == '__main__':
    unittest.main()

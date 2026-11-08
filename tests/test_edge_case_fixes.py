"""Edge cases: tail handoff, folder choice, DeepL codes, watcher recovery."""

from unittest.mock import MagicMock, patch

import pytest
from PySide6.QtCore import QObject

from src.core.tailer import FleetLogTailer
from src.services.log_discovery import folder_has_eve_logs, resolve_chatlogs_dir
from src.services.translator import DeepLProvider

HEADER = "\ufeff\r\n  Channel Name:    Fleet\r\n\r\n"


def _append(path, text):
    with open(path, "ab") as log_file:
        log_file.write(text.encode("utf-16-le"))


def test_line_written_between_history_and_tail_start_is_not_lost(tmp_path):
    path = tmp_path / "Fleet_20261108_100000_2110000001.txt"
    path.write_bytes(HEADER.encode("utf-16-le"))
    _append(path, "[ 2026.11.08 10:00:01 ] FC > align to gate\r\n")
    tailer = FleetLogTailer(str(path))

    history = tailer.read_last_n_lines(5)
    _append(path, "[ 2026.11.08 10:00:02 ] FC > jump\r\n")  # races the session start
    tailer.seek_to_end()

    assert history == ["[ 2026.11.08 10:00:01 ] FC > align to gate"]
    assert tailer.read_new_lines() == ["[ 2026.11.08 10:00:02 ] FC > jump"]
    tailer.close()


def test_partial_last_line_is_read_once_complete(tmp_path):
    path = tmp_path / "Fleet_20261108_100000_2110000001.txt"
    path.write_bytes(HEADER.encode("utf-16-le"))
    _append(path, "[ 2026.11.08 10:00:01 ] FC > align to")
    tailer = FleetLogTailer(str(path))

    assert tailer.read_last_n_lines(5) == []
    tailer.seek_to_end()
    _append(path, " gate\r\n")

    assert tailer.read_new_lines() == ["[ 2026.11.08 10:00:01 ] FC > align to gate"]
    tailer.close()


def test_choosing_the_eve_logs_folder_uses_its_chatlogs_subfolder(tmp_path):
    chatlogs = tmp_path / "logs" / "Chatlogs"
    chatlogs.mkdir(parents=True)
    (tmp_path / "logs" / "Gamelogs").mkdir()

    assert resolve_chatlogs_dir(str(tmp_path / "logs")) == str(chatlogs)
    assert resolve_chatlogs_dir(str(chatlogs)) == str(chatlogs)
    assert folder_has_eve_logs(str(chatlogs)) is False
    (chatlogs / "Local_20261108_100000_2110000001.txt").write_bytes(b"")
    assert folder_has_eve_logs(str(chatlogs)) is True


@pytest.mark.parametrize(
    ("code", "expected"),
    [
        ("zh", "ZH-HANS"),
        ("zh-CN", "ZH-HANS"),
        ("zh-TW", "ZH-HANT"),
        ("zh-Hant-TW", "ZH-HANT"),
        ("no", "NB"),
        ("nb-NO", "NB"),
        ("de-AT", "DE"),
        ("pt-BR", "PT-BR"),
        ("en", "EN-US"),
    ],
)
def test_deepl_target_codes_use_supported_variants(code, expected):
    assert DeepLProvider._translation_args(code)["target_lang"] == expected


def test_successful_discovery_rearms_a_lost_folder_watch():
    with patch("src.main.TranslatorManager.__init__", lambda self: None):
        from src.main import TranslatorManager

        manager = TranslatorManager()
        QObject.__init__(manager)
    from src.services.chat_discovery import ChatDiscoveryResult

    manager.config = {
        "shared": {"log_dir": "C:/logs"},
        "sessions": {"fleet": {"enabled": True}, "local": {"enabled": False}, "any": {"enabled": False}},
    }
    manager.sessions = {}
    manager._shutting_down = False
    manager._discovery_inflight_generation = 3
    manager._discovery_inflight_signature = None
    manager._discovery_request_invalidated = False
    manager.log_dir_watcher = MagicMock()
    manager._log_dir_watch_active = False
    manager._configure_log_directory_watcher = MagicMock()
    manager._scan_characters = MagicMock()
    manager._scan_channels = MagicMock()
    manager._scan_fleets = MagicMock()
    manager._recover_waiting_sessions = MagicMock()
    manager._update_tray_menu = MagicMock()

    manager._apply_chat_discovery_result(
        ChatDiscoveryResult(generation=3, log_dir="C:/logs")
    )

    manager._configure_log_directory_watcher.assert_called_once_with()

import stat
from types import SimpleNamespace
from unittest.mock import MagicMock, call, patch

import pytest
from PySide6.QtCore import QObject

from src.core.character_info import CharacterInfo
from src.main import SessionRuntimeState, TranslatorManager
from src.services.chat_discovery import (
    ChatDiscoveryError,
    ChatDiscoveryResult,
    ChatDiscoveryWorker,
    active_character_starts,
)
from src.services.log_discovery import (
    normalized_log_path,
    parse_eve_timestamp,
    scan_eve_log_entries,
)


def _character(
    character_id: str,
    path: str,
    *,
    active: bool = True,
) -> CharacterInfo:
    return CharacterInfo(
        character_id=character_id,
        character_name=f"Pilot {character_id}",
        latest_log_path=path,
        log_mtime=1_000.0,
        system_name="Jita",
        is_active=active,
    )


def _manager(*, fleet=True, local=True, any_chat=False) -> TranslatorManager:
    with patch.object(TranslatorManager, "__init__", lambda self: None):
        manager = TranslatorManager()
        QObject.__init__(manager)

    enabled = {"fleet": fleet, "local": local, "any": any_chat}
    manager.config = {
        "shared": {
            "log_dir": "C:/logs",
            "fleet_inactive_threshold": 1_800,
            "translation_provider": "disabled",
        },
        "sessions": {
            session_id: {"enabled": is_enabled}
            for session_id, is_enabled in enabled.items()
        },
    }
    manager.sessions = {session_id: None for session_id in enabled}
    manager._session_runtime_states = {
        session_id: (
            SessionRuntimeState.WAITING
            if is_enabled
            else SessionRuntimeState.DISABLED
        )
        for session_id, is_enabled in enabled.items()
    }
    manager._session_runtime_errors = {}
    manager._discovery_error_sessions = set()
    manager._discovery_failure_count = 0
    manager._discovery_retry_signature = None
    manager._discovery_rescan_requested = False
    manager._discovery_request_invalidated = False
    manager._sessions_initialized = True
    manager.session_actions = {
        session_id: MagicMock() for session_id in enabled
    }
    manager.tray_icon = MagicMock()
    manager.discovery_retry_timer = MagicMock()
    manager._broadcast_character_list = MagicMock()
    manager._broadcast_channel_list = MagicMock()
    manager._broadcast_fleet_list = MagicMock()
    return manager


def _prime_generation(manager: TranslatorManager, generation: int) -> None:
    manager._discovery_generation = generation
    manager._discovery_inflight_generation = generation
    manager._discovery_inflight_signature = (
        normalized_log_path("C:/logs"),
        1_800,
    )
    manager._discovery_scan_pending = True
    manager._discovery_request_invalidated = False


def _failure(generation: int, message: str = "Access is denied"):
    return ChatDiscoveryResult(
        generation=generation,
        log_dir="C:/logs",
        error=ChatDiscoveryError(
            stage="snapshot",
            exception_type="PermissionError",
            message=message,
        ),
    )


def test_log_snapshot_distinguishes_empty_from_inaccessible(tmp_path):
    assert scan_eve_log_entries(tmp_path) == ()

    with patch(
        "src.services.log_discovery.os.scandir",
        side_effect=PermissionError("Access is denied"),
    ):
        with pytest.raises(PermissionError, match="Access is denied"):
            scan_eve_log_entries(tmp_path)


def test_log_snapshot_keeps_native_scan_order_without_global_sort(tmp_path):
    class FakeEntry:
        def __init__(self, name):
            self.name = name
            self.path = str(tmp_path / name)

        def stat(self):
            return SimpleNamespace(
                st_mode=stat.S_IFREG,
                st_size=1,
                st_mtime=1.0,
                st_mtime_ns=1_000_000_000,
                st_ctime=1.0,
            )

    class FakeScandir:
        def __enter__(self):
            return iter(
                (
                    FakeEntry("Zulu_20260712_120000_7.txt"),
                    FakeEntry("Alpha_20260712_120000_7.txt"),
                )
            )

        def __exit__(self, *_args):
            return False

    with patch(
        "src.services.log_discovery.os.scandir",
        return_value=FakeScandir(),
    ):
        entries = scan_eve_log_entries(tmp_path)

    assert [entry.name for entry in entries] == [
        "Zulu_20260712_120000_7.txt",
        "Alpha_20260712_120000_7.txt",
    ]


def test_snapshot_access_failure_becomes_worker_error_not_empty_success():
    local = MagicMock()
    channel = MagicMock()
    fleet = MagicMock()
    worker = ChatDiscoveryWorker(
        local_detector=local,
        channel_detector=channel,
        fleet_detector=fleet,
        entry_scanner=MagicMock(
            side_effect=PermissionError("Access is denied")
        ),
    )
    results = []
    worker.result_ready.connect(results.append)

    worker.scan(7, "C:/unavailable", 1_800)

    assert len(results) == 1
    result = results[0]
    assert result.succeeded is False
    assert result.error == ChatDiscoveryError(
        stage="snapshot",
        exception_type="PermissionError",
        message="Access is denied",
    )
    local.scan_active_characters.assert_not_called()
    channel.scan_channels.assert_not_called()
    fleet.scan_active_fleets.assert_not_called()


def test_active_character_start_mapping_is_shared_with_sync_fallback():
    characters = {
        "7": _character("7", "C:/logs/Local_20260712_120000_7.txt"),
        "8": _character(
            "8",
            "C:/logs/Local_20260712_120001_8.txt",
            active=False,
        ),
        "9": _character("9", "C:/logs/not-an-eve-log.txt"),
    }
    expected = {
        "7": parse_eve_timestamp("20260712120000"),
        "8": False,
        "9": None,
    }
    assert active_character_starts(characters) == expected

    manager = _manager()
    manager.character_registry = characters
    manager.channel_registry = {}
    manager.selected_channel_id = None
    manager.any_chat_detector = MagicMock()
    manager.any_chat_detector.scan_channels.return_value = {}

    with patch("src.main.os.path.exists", return_value=True):
        manager._scan_channels()

    manager.any_chat_detector.scan_channels.assert_called_once_with(
        "C:/logs",
        active_characters=expected,
    )


def test_persistent_failure_preserves_registries_and_surfaces_then_recovers():
    manager = _manager()
    fleet_session = MagicMock()
    fleet_session.overlay.display_mode = "combat"
    manager.sessions["fleet"] = fleet_session
    old_characters = {"7": object()}
    old_channels = {"intel": object()}
    old_fleets = {"fleet": object()}
    manager.character_registry = old_characters
    manager.channel_registry = old_channels
    manager.fleet_registry = old_fleets
    manager._scan_characters = MagicMock()
    manager._scan_channels = MagicMock()
    manager._scan_fleets = MagicMock()
    manager._recover_waiting_sessions = MagicMock()

    for generation in range(1, 4):
        _prime_generation(manager, generation)
        manager._discovery_rescan_requested = True
        with patch("src.main.QTimer.singleShot") as single_shot:
            manager._apply_chat_discovery_result(_failure(generation))
        single_shot.assert_not_called()

    assert manager.discovery_retry_timer.start.call_args_list == [
        call(1_000),
        call(2_000),
        call(4_000),
    ]
    assert manager.character_registry is old_characters
    assert manager.channel_registry is old_channels
    assert manager.fleet_registry is old_fleets
    manager._scan_characters.assert_not_called()
    manager._scan_channels.assert_not_called()
    manager._scan_fleets.assert_not_called()
    assert manager._session_runtime_states == {
        "fleet": SessionRuntimeState.ERROR,
        "local": SessionRuntimeState.ERROR,
        "any": SessionRuntimeState.DISABLED,
    }
    assert set(manager._session_runtime_errors) == {"fleet", "local"}
    assert 'Cannot read EVE chat log folder "C:/logs"' in (
        manager._session_runtime_errors["fleet"]
    )
    assert "PermissionError: Access is denied" in (
        manager._session_runtime_errors["fleet"]
    )
    assert "Retrying automatically in 4s" in (
        manager._session_runtime_errors["fleet"]
    )
    tray_tooltip = manager.tray_icon.setToolTip.call_args.args[0]
    assert "Fleet: Error" in tray_tooltip
    assert len(tray_tooltip) <= 127
    assert "Access is denied" in tray_tooltip
    overlay_status = fleet_session.overlay.update_status.call_args.args[0]
    assert overlay_status.state == "Error"
    assert "Access is denied" in overlay_status.detail

    _prime_generation(manager, 4)
    manager._apply_chat_discovery_result(
        ChatDiscoveryResult(generation=4, log_dir="C:/logs")
    )

    assert manager._discovery_failure_count == 0
    assert manager._discovery_retry_signature is None
    assert manager._session_runtime_errors == {}
    assert manager._session_runtime_states == {
        "fleet": SessionRuntimeState.WAITING,
        "local": SessionRuntimeState.WAITING,
        "any": SessionRuntimeState.DISABLED,
    }
    manager._scan_characters.assert_called_once_with(new_registry={})
    manager._scan_channels.assert_called_once_with(new_registry={})
    manager._scan_fleets.assert_called_once_with(new_registry={})
    manager._recover_waiting_sessions.assert_called_once_with()


def test_active_retry_backoff_blocks_routine_rescan_requests():
    manager = _manager()
    manager.discovery_thread = MagicMock()
    manager.discovery_thread.isRunning.return_value = True
    manager.discovery_retry_timer.isActive.return_value = True
    manager._discovery_retry_signature = (
        normalized_log_path("C:/logs"),
        1_800,
    )
    manager._discovery_scan_pending = False
    emitted = []
    manager.chat_discovery_requested.connect(
        lambda generation, log_dir, threshold: emitted.append(
            (generation, log_dir, threshold)
        )
    )

    assert manager._request_chat_discovery() is True
    assert manager._request_chat_discovery() is True

    assert emitted == []
    assert manager._discovery_scan_pending is False
    manager.discovery_retry_timer.stop.assert_not_called()

    manager.discovery_retry_timer.isActive.return_value = False
    assert manager._request_chat_discovery() is True
    assert emitted == [(1, "C:/logs", 1_800)]


def test_nonempty_missing_path_is_dispatched_to_worker_for_error_reporting():
    manager = _manager()
    manager.discovery_thread = MagicMock()
    manager.discovery_thread.isRunning.return_value = True
    manager.discovery_retry_timer.isActive.return_value = False
    manager._discovery_scan_pending = False
    emitted = []
    manager.chat_discovery_requested.connect(
        lambda generation, log_dir, threshold: emitted.append(
            (generation, log_dir, threshold)
        )
    )

    with patch(
        "src.main.os.path.isdir",
        side_effect=AssertionError("GUI path preflight must not hide errors"),
    ):
        assert manager._request_chat_discovery() is True

    assert emitted == [(1, "C:/logs", 1_800)]

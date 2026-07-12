from unittest.mock import MagicMock, patch

from PySide6.QtCore import QObject

from src.app_config import get_default_config
from src.core.channel_info import ChatChannelInfo
from src.core.character_info import CharacterInfo
from src.main import SessionRuntimeState, TranslatorManager
from src.services.log_discovery import parse_eve_log_filename
from src.services.scan_state import ChatScanState


def _channel(
    selection_id: str,
    *,
    path: str,
    name: str = "Intel",
    listener: str = "Pilot",
    character_id: str = "123",
    started: float = 1000.0,
    active: bool = True,
) -> ChatChannelInfo:
    return ChatChannelInfo(
        selection_id=selection_id,
        channel_id=f"player_{selection_id}",
        channel_name=name,
        character_id=character_id,
        listener_name=listener,
        log_path=path,
        log_mtime=started,
        started_time=started,
        is_active=active,
    )


def _manager(*, any_enabled: bool = True) -> TranslatorManager:
    with patch.object(TranslatorManager, "__init__", lambda self: None):
        manager = TranslatorManager()
        QObject.__init__(manager)

    manager.config = get_default_config()
    manager.config["sessions"]["any"]["enabled"] = any_enabled
    manager.sessions = {"fleet": None, "local": None, "any": None}
    manager._scan_state = ChatScanState.from_config(manager.config)
    manager._session_runtime_states = {
        "fleet": SessionRuntimeState.DISABLED,
        "local": SessionRuntimeState.DISABLED,
        "any": (
            SessionRuntimeState.WAITING
            if any_enabled
            else SessionRuntimeState.DISABLED
        ),
    }
    manager._session_runtime_errors = {}
    manager._save_config = MagicMock()
    manager._broadcast_channel_list = MagicMock()
    manager._update_any_overlay_title = MagicMock()
    manager.fleet_action = MagicMock()
    manager.local_action = MagicMock()
    manager.any_action = MagicMock()
    manager.tray_icon = MagicMock()
    manager.translation_service = MagicMock()
    manager.translation_service.get_provider_status.return_value = {
        "mode": "disabled",
        "name": "Disabled",
        "available": True,
    }
    return manager


def test_first_enable_selects_newest_active_channel():
    manager = _manager()
    old = _channel("old", path="C:/logs/old.txt", started=1000)
    new = _channel("new", path="C:/logs/new.txt", started=2000)
    manager.channel_registry = {old.selection_id: old, new.selection_id: new}

    assert manager._find_latest_any_log() == new.log_path
    assert manager.selected_channel_id == new.selection_id
    assert manager.config["sessions"]["any"]["channel_name"] == "Intel"
    assert manager.config["sessions"]["any"]["character_id"] == "123"


def test_pinned_unavailable_channel_does_not_jump_to_another_source():
    manager = _manager()
    pinned = _channel("pinned", path="C:/logs/pinned.txt", active=False)
    busy = _channel("busy", path="C:/logs/busy.txt", started=3000)
    manager.channel_registry = {
        pinned.selection_id: pinned,
        busy.selection_id: busy,
    }
    manager.selected_channel_id = pinned.selection_id

    assert manager._find_latest_any_log() is None
    assert manager.selected_channel_id == pinned.selection_id


def test_picker_falls_back_to_only_the_newest_login_when_none_are_active():
    manager = _manager(any_enabled=False)
    old = _channel("old", path="C:/logs/old.txt", started=1000, active=False)
    recent_a = _channel(
        "recent-a",
        path="C:/logs/recent-a.txt",
        started=2000,
        active=False,
    )
    recent_b = _channel(
        "recent-b",
        path="C:/logs/recent-b.txt",
        started=2000,
        active=False,
    )
    manager.channel_registry = {
        item.selection_id: item for item in (old, recent_a, recent_b)
    }

    choices = manager._any_channel_choices()

    assert {item.selection_id for item in choices} == {
        recent_a.selection_id,
        recent_b.selection_id,
    }


def test_scan_publishes_rotated_source_before_switching_session(tmp_path):
    manager = _manager()
    manager.config["shared"]["log_dir"] = str(tmp_path)
    rotated = _channel("intel", path=str(tmp_path / "new.txt"), started=2000)
    manager.selected_channel_id = rotated.selection_id
    session = MagicMock()
    session.log_path = tmp_path / "old.txt"
    manager.sessions["any"] = session
    manager._set_session_runtime_state = MagicMock()
    detector = MagicMock()
    detector.scan_channels.return_value = {rotated.selection_id: rotated}
    manager.any_chat_detector = detector

    def assert_registry_published(path):
        assert manager.channel_registry[rotated.selection_id].log_path == path

    session.switch_log.side_effect = assert_registry_published

    manager._scan_channels()

    detector.scan_channels.assert_called_once_with(
        str(tmp_path),
        active_characters={},
    )
    session.switch_log.assert_called_once_with(rotated.log_path)
    manager._set_session_runtime_state.assert_called_once_with(
        "any",
        SessionRuntimeState.ACTIVE,
    )


def test_sync_scan_passes_known_inactive_characters_explicitly(tmp_path):
    manager = _manager()
    manager.config["shared"]["log_dir"] = str(tmp_path)
    manager.character_registry = {
        "active": CharacterInfo(
            "active",
            "Online Pilot",
            str(tmp_path / "Local_20260712_120000_1.txt"),
            1000.0,
            "Jita",
            True,
        ),
        "inactive": CharacterInfo(
            "inactive",
            "Offline Pilot",
            str(tmp_path / "Local_20260712_110000_2.txt"),
            900.0,
            "Amarr",
            False,
        ),
    }
    detector = MagicMock()
    detector.scan_channels.return_value = {}
    manager.any_chat_detector = detector

    manager._scan_channels()

    parsed = parse_eve_log_filename("Local_20260712_120000_1.txt")
    assert parsed is not None
    detector.scan_channels.assert_called_once_with(
        str(tmp_path),
        active_characters={
            "active": parsed.started_time,
            "inactive": False,
        },
    )


def test_manual_switch_updates_metadata_and_invalidates_old_source_path():
    manager = _manager()
    channel = _channel(
        "corp",
        path="C:/logs/corp.txt",
        name="Corp",
        listener="Second Pilot",
        character_id="456",
    )
    manager.channel_registry = {channel.selection_id: channel}
    session = MagicMock()
    session.log_path = "C:/logs/old.txt"
    manager.sessions["any"] = session
    manager._set_session_runtime_state = MagicMock()

    manager.switch_channel(channel.selection_id)

    session.switch_log.assert_called_once_with(channel.log_path)
    assert manager.selected_channel_id == channel.selection_id
    any_config = manager.config["sessions"]["any"]
    assert any_config["channel_name"] == "Corp"
    assert any_config["listener_name"] == "Second Pilot"
    assert any_config["character_id"] == "456"
    manager._save_config.assert_called_once_with()


def test_save_config_preserves_manager_source_identity_and_overlay_geometry():
    manager = _manager()
    manager._save_config = TranslatorManager._save_config.__get__(manager)
    manager.config_store = MagicMock()
    manager.config_store.save.return_value = True

    authoritative = {
        "fleet": {"fleet_id": "new-fleet"},
        "local": {
            "character_id": "new-character",
            "character_name": "New Pilot",
            "system_name": "Jita",
        },
        "any": {
            "channel_selection": "new-channel",
            "channel_id": "player_new",
            "channel_name": "New Intel",
            "character_id": "new-character",
            "listener_name": "New Pilot",
        },
    }
    stale = {
        "fleet": {"fleet_id": "old-fleet"},
        "local": {
            "character_id": "old-character",
            "character_name": "Old Pilot",
            "system_name": "Amarr",
        },
        "any": {
            "channel_selection": "old-channel",
            "channel_id": "player_old",
            "channel_name": "Old Intel",
            "character_id": "old-character",
            "listener_name": "Old Pilot",
        },
    }

    for index, session_id in enumerate(("fleet", "local", "any"), start=1):
        manager.config["sessions"][session_id].update(authoritative[session_id])
        session = MagicMock()
        session.get_config.return_value = {
            **stale[session_id],
            "x": 100 * index,
            "y": 200 * index,
            "display_mode": "bilingual",
        }
        manager.sessions[session_id] = session

    assert manager._save_config() is True

    for index, session_id in enumerate(("fleet", "local", "any"), start=1):
        saved_session = manager.config["sessions"][session_id]
        for key, value in authoritative[session_id].items():
            assert saved_session[key] == value
        assert saved_session["x"] == 100 * index
        assert saved_session["y"] == 200 * index
        assert saved_session["display_mode"] == "bilingual"

    manager.config_store.save.assert_called_once_with(manager.config)


def test_failed_any_start_preserves_error_state_and_detail():
    manager = _manager()
    channel = _channel("corp", path="C:/logs/corp.txt", name="Corp")
    manager.channel_registry = {channel.selection_id: channel}

    def fail_start(_session_id):
        manager._set_session_runtime_state(
            "any",
            SessionRuntimeState.ERROR,
            error="overlay construction failed",
        )
        return False

    manager.start_session = MagicMock(side_effect=fail_start)

    manager.switch_channel(channel.selection_id)

    assert manager.sessions["any"] is None
    assert manager._get_session_runtime_state("any") == SessionRuntimeState.ERROR
    assert manager._session_runtime_errors["any"] == "overlay construction failed"
    manager.start_session.assert_called_once_with("any")


def test_unavailable_any_start_preserves_waiting_state():
    manager = _manager()
    channel = _channel("corp", path="C:/logs/corp.txt", name="Corp")
    manager.channel_registry = {channel.selection_id: channel}
    manager.start_session = MagicMock(return_value=False)

    manager.switch_channel(channel.selection_id)

    assert manager.sessions["any"] is None
    assert manager._get_session_runtime_state("any") == SessionRuntimeState.WAITING
    manager.start_session.assert_called_once_with("any")


def test_selecting_inactive_source_captures_live_any_geometry():
    manager = _manager()
    channel = _channel(
        "corp",
        path="C:/logs/corp.txt",
        name="Corp",
        active=False,
    )
    manager.channel_registry = {channel.selection_id: channel}
    session = MagicMock()
    session.get_config.return_value = {
        "x": 432,
        "y": 234,
        "w": 640,
        "h": 180,
        "channel_selection": "stale-source",
    }
    manager.sessions["any"] = session
    manager._disconnect_session_signals = MagicMock()

    manager.switch_channel(channel.selection_id)

    saved = manager.config["sessions"]["any"]
    assert {key: saved[key] for key in ("x", "y", "w", "h")} == {
        "x": 432,
        "y": 234,
        "w": 640,
        "h": 180,
    }
    assert saved["channel_selection"] == channel.selection_id
    assert manager.sessions["any"] is None
    session.stop.assert_called_once_with()
    manager._disconnect_session_signals.assert_called_once_with(session)


def test_successful_any_start_keeps_active_state():
    manager = _manager()
    channel = _channel("corp", path="C:/logs/corp.txt", name="Corp")
    manager.channel_registry = {channel.selection_id: channel}

    def successful_start(_session_id):
        manager.sessions["any"] = MagicMock()
        manager._set_session_runtime_state("any", SessionRuntimeState.ACTIVE)
        return True

    manager.start_session = MagicMock(side_effect=successful_start)

    manager.switch_channel(channel.selection_id)

    assert manager.sessions["any"] is not None
    assert manager._get_session_runtime_state("any") == SessionRuntimeState.ACTIVE
    manager.start_session.assert_called_once_with("any")


def test_selecting_source_while_any_chat_is_off_only_persists_choice():
    manager = _manager(any_enabled=False)
    channel = _channel("alliance", path="C:/logs/alliance.txt")
    manager.channel_registry = {channel.selection_id: channel}
    manager.start_session = MagicMock()
    manager._set_session_runtime_state = MagicMock()

    manager.switch_channel(channel.selection_id)

    assert manager.selected_channel_id == channel.selection_id
    manager.start_session.assert_not_called()
    manager._set_session_runtime_state.assert_not_called()
    manager._save_config.assert_called_once_with()


def test_any_overlay_status_uses_channel_and_listener_not_fleet_context():
    manager = _manager()
    overlay = MagicMock()
    manager.sessions["any"] = MagicMock(overlay=overlay)
    manager.config["sessions"]["any"].update({
        "channel_name": "Alliance",
        "listener_name": "Pilot One",
    })
    manager._session_runtime_states["any"] = SessionRuntimeState.ACTIVE

    manager._update_overlay_status("any")

    status = overlay.update_status.call_args.args[0]
    assert status.channel == "Any Chat"
    assert status.context == "Alliance · Pilot One"
    assert status.state == "Active"

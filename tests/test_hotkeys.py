import ctypes
from ctypes import wintypes
from unittest.mock import MagicMock, patch

import pytest

from src.services.hotkeys import (
    COMPOSE_HOTKEY_ID,
    ERROR_HOTKEY_ALREADY_REGISTERED,
    HOTKEY_ID,
    MOD_CONTROL,
    MOD_NOREPEAT,
    MOD_SHIFT,
    WM_HOTKEY,
    WindowsGlobalHotkey,
    format_hotkey_registration_error,
    parse_hotkey,
)


def test_parse_hotkey_uses_mod_norepeat_and_supported_key():
    modifiers, key = parse_hotkey("Ctrl+Shift+L")
    assert modifiers & MOD_CONTROL
    assert modifiers & MOD_SHIFT
    assert modifiers & MOD_NOREPEAT
    assert key == ord("L")


@pytest.mark.parametrize("value", ["L", "Ctrl+Space", "Super+L"])
def test_parse_hotkey_rejects_unsafe_or_unsupported_chords(value):
    with pytest.raises(ValueError):
        parse_hotkey(value)


def test_registration_native_event_and_idempotent_unregister():
    app = MagicMock()
    callback = MagicMock()
    user32 = MagicMock()
    user32.RegisterHotKey.return_value = True
    hotkey = WindowsGlobalHotkey(app, callback, user32=user32)

    assert hotkey.register() is True
    app.installNativeEventFilter.assert_called_once_with(hotkey)
    user32.RegisterHotKey.assert_called_once()

    message = wintypes.MSG()
    message.message = WM_HOTKEY
    message.wParam = HOTKEY_ID
    with patch('src.services.hotkeys.QTimer.singleShot', side_effect=lambda _ms, fn: fn()):
        assert hotkey.nativeEventFilter(
            b"windows_dispatcher_MSG",
            ctypes.addressof(message),
        ) is False
    callback.assert_called_once_with()

    hotkey.unregister()
    hotkey.unregister()
    app.removeNativeEventFilter.assert_called_once_with(hotkey)
    user32.UnregisterHotKey.assert_called_once_with(None, HOTKEY_ID)


def test_registration_conflict_preserves_code_and_names_the_chord():
    user32 = MagicMock()
    user32.RegisterHotKey.return_value = False
    hotkey = WindowsGlobalHotkey(
        MagicMock(),
        MagicMock(),
        chord="Ctrl+Shift+L",
        user32=user32,
    )

    with patch(
        "src.services.hotkeys.ctypes.get_last_error",
        return_value=ERROR_HOTKEY_ALREADY_REGISTERED,
    ):
        assert hotkey.register() is False

    assert hotkey.error_code == ERROR_HOTKEY_ALREADY_REGISTERED
    assert hotkey.error == (
        "Ctrl+Shift+L is already in use by another application."
    )


def test_unknown_registration_failure_keeps_windows_error_code():
    message = format_hotkey_registration_error("Ctrl+Shift+T", 5)

    assert "Ctrl+Shift+T" in message
    assert "error 5" in message


def test_wrong_native_message_does_nothing():
    callback = MagicMock()
    hotkey = WindowsGlobalHotkey(MagicMock(), callback, user32=MagicMock())
    message = wintypes.MSG()
    message.message = WM_HOTKEY
    message.wParam = HOTKEY_ID + 1

    hotkey.nativeEventFilter(b"windows_dispatcher_MSG", ctypes.addressof(message))

    callback.assert_not_called()


def test_distinct_valid_hotkey_ids_route_only_their_own_event():
    lock_callback = MagicMock()
    compose_callback = MagicMock()
    lock = WindowsGlobalHotkey(
        MagicMock(), lock_callback, user32=MagicMock(), hotkey_id=HOTKEY_ID
    )
    compose = WindowsGlobalHotkey(
        MagicMock(), compose_callback, user32=MagicMock(),
        hotkey_id=COMPOSE_HOTKEY_ID,
    )
    message = wintypes.MSG()
    message.message = WM_HOTKEY
    message.wParam = COMPOSE_HOTKEY_ID

    with patch('src.services.hotkeys.QTimer.singleShot', side_effect=lambda _ms, fn: fn()):
        lock.nativeEventFilter(
            b"windows_dispatcher_MSG", ctypes.addressof(message)
        )
        compose.nativeEventFilter(
            b"windows_dispatcher_MSG", ctypes.addressof(message)
        )

    lock_callback.assert_not_called()
    compose_callback.assert_called_once_with()


@pytest.mark.parametrize("hotkey_id", [0, -1, 0xC000, True, "4557"])
def test_hotkey_id_must_fit_win32_application_range(hotkey_id):
    with pytest.raises(ValueError):
        WindowsGlobalHotkey(MagicMock(), MagicMock(), hotkey_id=hotkey_id)

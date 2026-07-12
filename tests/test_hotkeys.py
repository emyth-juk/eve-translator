import ctypes
from ctypes import wintypes
from unittest.mock import MagicMock, patch

import pytest

from src.services.hotkeys import (
    HOTKEY_ID,
    MOD_CONTROL,
    MOD_NOREPEAT,
    MOD_SHIFT,
    WM_HOTKEY,
    WindowsGlobalHotkey,
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


def test_wrong_native_message_does_nothing():
    callback = MagicMock()
    hotkey = WindowsGlobalHotkey(MagicMock(), callback, user32=MagicMock())
    message = wintypes.MSG()
    message.message = WM_HOTKEY
    message.wParam = HOTKEY_ID + 1

    hotkey.nativeEventFilter(b"windows_dispatcher_MSG", ctypes.addressof(message))

    callback.assert_not_called()

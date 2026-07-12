"""Minimal Windows global-hotkey support without keyboard hooks."""

from __future__ import annotations

import ctypes
import os
from ctypes import wintypes
from typing import Callable

from PySide6.QtCore import QAbstractNativeEventFilter, QTimer


WM_HOTKEY = 0x0312
MOD_ALT = 0x0001
MOD_CONTROL = 0x0002
MOD_SHIFT = 0x0004
MOD_WIN = 0x0008
MOD_NOREPEAT = 0x4000
# RegisterHotKey IDs for a NULL window handle must stay in 0x0000-0xBFFF.
LOCK_HOTKEY_ID = 0x4556
COMPOSE_HOTKEY_ID = 0x4557
HOTKEY_ID = LOCK_HOTKEY_ID  # Backward-compatible public name.

_MODIFIERS = {
    'alt': MOD_ALT,
    'ctrl': MOD_CONTROL,
    'control': MOD_CONTROL,
    'shift': MOD_SHIFT,
    'win': MOD_WIN,
    'windows': MOD_WIN,
}


def parse_hotkey(chord: str) -> tuple[int, int]:
    parts = [part.strip().lower() for part in str(chord or '').split('+') if part.strip()]
    if len(parts) < 2:
        raise ValueError("A global hotkey needs a modifier and a key")
    modifiers = MOD_NOREPEAT
    for part in parts[:-1]:
        if part not in _MODIFIERS:
            raise ValueError(f"Unsupported hotkey modifier: {part}")
        modifiers |= _MODIFIERS[part]
    key = parts[-1]
    if len(key) == 1 and key.isalnum():
        virtual_key = ord(key.upper())
    elif key.startswith('f') and key[1:].isdigit() and 1 <= int(key[1:]) <= 24:
        virtual_key = 0x70 + int(key[1:]) - 1
    else:
        raise ValueError(f"Unsupported hotkey key: {key}")
    return modifiers, virtual_key


class WindowsGlobalHotkey(QAbstractNativeEventFilter):
    """Observe one declared key chord using RegisterHotKey, never a key hook."""

    def __init__(
        self,
        app,
        callback: Callable[[], None],
        chord='Ctrl+Shift+L',
        user32=None,
        *,
        hotkey_id: int = HOTKEY_ID,
    ):
        super().__init__()
        if (
            isinstance(hotkey_id, bool)
            or not isinstance(hotkey_id, int)
            or not 0 < hotkey_id <= 0xBFFF
        ):
            raise ValueError("hotkey_id must be between 1 and 0xBFFF")
        self.app = app
        self.callback = callback
        self.chord = chord
        self.hotkey_id = hotkey_id
        self._user32 = user32
        self.registered = False
        self.installed = False
        self.error = ''

    def register(self) -> bool:
        if self.registered:
            return True
        if os.name != 'nt' and self._user32 is None:
            self.error = 'Global shortcuts are only supported on Windows.'
            return False
        try:
            modifiers, virtual_key = parse_hotkey(self.chord)
            if self._user32 is None:
                self._user32 = ctypes.WinDLL('user32', use_last_error=True)
            register_hotkey = self._user32.RegisterHotKey
            register_hotkey.argtypes = [wintypes.HWND, ctypes.c_int, wintypes.UINT, wintypes.UINT]
            register_hotkey.restype = wintypes.BOOL
            if not register_hotkey(None, self.hotkey_id, modifiers, virtual_key):
                error_code = ctypes.get_last_error()
                self.error = f"Windows could not register {self.chord} (error {error_code})."
                return False
            self.app.installNativeEventFilter(self)
            self.installed = True
            self.registered = True
            self.error = ''
            return True
        except (AttributeError, OSError, ValueError) as exc:
            self.error = str(exc)
            return False

    def nativeEventFilter(self, event_type, message):
        try:
            event_name = bytes(event_type)
        except (TypeError, ValueError):
            event_name = b''
        if not event_name.startswith(b'windows_'):
            return False
        try:
            msg = ctypes.cast(
                int(message),
                ctypes.POINTER(wintypes.MSG),
            ).contents
        except (TypeError, ValueError, ctypes.ArgumentError):
            return False
        if msg.message == WM_HOTKEY and int(msg.wParam) == self.hotkey_id:
            QTimer.singleShot(0, self.callback)
        return False

    def unregister(self) -> None:
        if self.installed:
            self.app.removeNativeEventFilter(self)
            self.installed = False
        if self.registered and self._user32 is not None:
            try:
                unregister = self._user32.UnregisterHotKey
                unregister.argtypes = [wintypes.HWND, ctypes.c_int]
                unregister.restype = wintypes.BOOL
                unregister(None, self.hotkey_id)
            finally:
                self.registered = False

    def __del__(self):
        # Normal shutdown calls unregister on the GUI thread. Avoid Win32 calls
        # during interpreter teardown; this is just state hygiene.
        self.registered = False

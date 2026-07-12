"""Prevent overlapping translator processes from competing for global state."""

from __future__ import annotations

import ctypes
import os
from ctypes import wintypes
from typing import Callable

ERROR_ALREADY_EXISTS = 183
INSTANCE_MUTEX_NAME = r"Local\EVEFleetChatTranslator.SingleInstance"

MB_OK = 0x00000000
MB_ICONINFORMATION = 0x00000040
MB_SETFOREGROUND = 0x00010000


class SingleInstanceGuard:
    """Own one per-Windows-session named mutex for the process lifetime."""

    def __init__(
        self,
        name: str = INSTANCE_MUTEX_NAME,
        *,
        kernel32=None,
        last_error_getter: Callable[[], int] | None = None,
    ) -> None:
        self.name = name
        self._kernel32 = kernel32
        self._last_error_getter = last_error_getter or getattr(
            ctypes,
            "get_last_error",
            lambda: 0,
        )
        self._handle = None
        self._portable_noop = False
        self.already_running = False
        self.error = ""

    def acquire(self) -> bool:
        if self._handle is not None or self._portable_noop:
            return True
        if os.name != "nt" and self._kernel32 is None:
            # The packaged application is Windows-only. Keep source/test runs
            # on other platforms usable without introducing a lock-file policy.
            self._portable_noop = True
            return True

        try:
            if self._kernel32 is None:
                self._kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
            create_mutex = self._kernel32.CreateMutexW
            create_mutex.argtypes = [
                wintypes.LPVOID,
                wintypes.BOOL,
                wintypes.LPCWSTR,
            ]
            create_mutex.restype = wintypes.HANDLE
            set_last_error = getattr(ctypes, "set_last_error", None)
            if callable(set_last_error):
                set_last_error(0)
            handle = create_mutex(None, False, self.name)
            error_code = int(self._last_error_getter() or 0)
            if not handle:
                self.error = (
                    "Windows could not create the single-instance guard"
                    f" (error {error_code})."
                )
                return False
            if error_code == ERROR_ALREADY_EXISTS:
                self._close_handle(handle)
                self.already_running = True
                self.error = "EVE Translator is already running."
                return False
            self._handle = handle
            self.already_running = False
            self.error = ""
            return True
        except (AttributeError, OSError, ValueError) as exc:
            self.error = str(exc)
            return False

    def _close_handle(self, handle) -> None:
        if self._kernel32 is None or not handle:
            return
        close_handle = self._kernel32.CloseHandle
        close_handle.argtypes = [wintypes.HANDLE]
        close_handle.restype = wintypes.BOOL
        close_handle(handle)

    def release(self) -> None:
        if self._handle is not None:
            self._close_handle(self._handle)
            self._handle = None
        self._portable_noop = False


def show_already_running_message(*, user32=None) -> None:
    """Explain a blocked second launch without initializing another Qt app."""
    if os.name != "nt" and user32 is None:
        return
    try:
        if user32 is None:
            user32 = ctypes.WinDLL("user32", use_last_error=True)
        message_box = user32.MessageBoxW
        message_box.argtypes = [
            wintypes.HWND,
            wintypes.LPCWSTR,
            wintypes.LPCWSTR,
            wintypes.UINT,
        ]
        message_box.restype = ctypes.c_int
        message_box(
            None,
            "EVE Translator is already running in the system tray.",
            "EVE Translator",
            MB_OK | MB_ICONINFORMATION | MB_SETFOREGROUND,
        )
    except (AttributeError, OSError, ValueError):
        # The guard still did its job; notification failure must not start a
        # second translator process.
        return


__all__ = [
    "ERROR_ALREADY_EXISTS",
    "INSTANCE_MUTEX_NAME",
    "SingleInstanceGuard",
    "show_already_running_message",
]

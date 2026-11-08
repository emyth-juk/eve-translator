from unittest.mock import MagicMock, patch

from src.services.single_instance import (
    ERROR_ALREADY_EXISTS,
    MB_ICONINFORMATION,
    MB_SETFOREGROUND,
    SingleInstanceGuard,
    show_already_running_message,
)


def test_guard_acquires_idempotently_and_releases_exact_handle():
    kernel32 = MagicMock()
    kernel32.CreateMutexW.return_value = 123
    guard = SingleInstanceGuard(
        kernel32=kernel32,
        last_error_getter=lambda: 0,
    )

    assert guard.acquire() is True
    assert guard.acquire() is True
    kernel32.CreateMutexW.assert_called_once()

    guard.release()
    guard.release()
    kernel32.CloseHandle.assert_called_once_with(123)


def test_guard_rejects_existing_instance_and_closes_duplicate_handle():
    kernel32 = MagicMock()
    kernel32.CreateMutexW.return_value = 456
    guard = SingleInstanceGuard(
        kernel32=kernel32,
        last_error_getter=lambda: ERROR_ALREADY_EXISTS,
    )

    assert guard.acquire() is False
    assert guard.already_running is True
    assert "already running" in guard.error
    kernel32.CloseHandle.assert_called_once_with(456)


def test_guard_reports_mutex_creation_failure():
    kernel32 = MagicMock()
    kernel32.CreateMutexW.return_value = 0
    guard = SingleInstanceGuard(
        kernel32=kernel32,
        last_error_getter=lambda: 8,
    )

    assert guard.acquire() is False
    assert guard.already_running is False
    assert "error 8" in guard.error


def test_access_denied_mutex_means_an_elevated_copy_is_running():
    kernel32 = MagicMock()
    kernel32.CreateMutexW.return_value = 0
    guard = SingleInstanceGuard(
        kernel32=kernel32,
        last_error_getter=lambda: 5,
    )

    assert guard.acquire() is False
    assert guard.already_running is True
    assert "already running" in guard.error


def test_non_windows_source_run_is_a_noop_without_injected_kernel():
    with patch("src.services.single_instance.os.name", "posix"):
        guard = SingleInstanceGuard()
        assert guard.acquire() is True
        guard.release()


def test_already_running_message_is_explicit_and_informational():
    user32 = MagicMock()

    show_already_running_message(user32=user32)

    args = user32.MessageBoxW.call_args.args
    assert "already running in the system tray" in args[1]
    assert args[2] == "EVE Translator"
    assert args[3] & MB_ICONINFORMATION
    assert args[3] & MB_SETFOREGROUND

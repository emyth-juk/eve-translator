from unittest.mock import MagicMock, patch

import pytest

from src.app_config import get_default_config
from src.main import TranslatorManager, run_application


def test_second_instance_exits_before_logging_or_manager_construction():
    guard = MagicMock(already_running=True)
    guard.acquire.return_value = False
    manager_factory = MagicMock()
    notifier = MagicMock()
    logging_setup = MagicMock()

    assert run_application(
        guard=guard,
        manager_factory=manager_factory,
        already_running_notifier=notifier,
        logging_setup=logging_setup,
    ) == 0

    notifier.assert_called_once_with()
    logging_setup.assert_not_called()
    manager_factory.assert_not_called()


def test_primary_instance_shuts_down_and_releases_guard():
    guard = MagicMock(already_running=False)
    guard.acquire.return_value = True
    manager = MagicMock(_shutting_down=False)
    manager.app.exec.return_value = 7
    logging_setup = MagicMock()

    result = run_application(
        guard=guard,
        manager_factory=MagicMock(return_value=manager),
        logging_setup=logging_setup,
    )

    assert result == 7
    logging_setup.assert_called_once_with()
    manager.shutdown.assert_called_once_with()
    guard.release.assert_called_once_with()


def test_primary_instance_releases_guard_when_event_loop_fails():
    guard = MagicMock(already_running=False)
    guard.acquire.return_value = True
    manager = MagicMock(_shutting_down=False)
    manager.app.exec.side_effect = RuntimeError("event loop failed")

    with pytest.raises(RuntimeError, match="event loop failed"):
        run_application(
            guard=guard,
            manager_factory=MagicMock(return_value=manager),
            logging_setup=MagicMock(),
        )

    manager.shutdown.assert_called_once_with()
    guard.release.assert_called_once_with()


def test_cancelled_first_run_exits_before_worker_threads_are_created():
    app = MagicMock()
    with (
        patch("src.main.QApplication", return_value=app),
        patch("src.main.QIcon"),
        patch("src.main.ConfigStore"),
        patch.object(
            TranslatorManager,
            "_load_config",
            return_value=get_default_config(),
        ),
        patch.object(
            TranslatorManager,
            "_ensure_log_directory",
            return_value=False,
        ),
        patch("src.main.QThread") as thread_class,
    ):
        manager = TranslatorManager()

    assert manager.startup_cancelled is True
    assert manager._shutting_down is True
    thread_class.assert_not_called()


def test_run_application_treats_first_run_cancel_as_clean_exit():
    guard = MagicMock(already_running=False)
    guard.acquire.return_value = True
    logging_setup = MagicMock()
    manager = MagicMock(startup_cancelled=True, _shutting_down=True)

    assert run_application(
        guard=guard,
        manager_factory=MagicMock(return_value=manager),
        logging_setup=logging_setup,
    ) == 0

    logging_setup.assert_called_once_with()
    manager.app.exec.assert_not_called()
    manager.shutdown.assert_not_called()
    guard.release.assert_called_once_with()


def test_log_directory_prompt_returns_false_after_confirmed_exit():
    class FakeFileDialog:
        @staticmethod
        def getExistingDirectory(*_args):
            return ""

    class FakeMessageBox:
        class StandardButton:
            Yes = 1
            No = 2

        def setWindowTitle(self, _title):
            pass

        def setText(self, _text):
            pass

        def setInformativeText(self, _text):
            pass

        def exec(self):
            pass

        @staticmethod
        def question(*_args):
            return FakeMessageBox.StandardButton.Yes

    with patch.object(TranslatorManager, "__init__", lambda self: None):
        manager = TranslatorManager()
    manager.config = get_default_config()
    manager.config["shared"]["log_dir"] = "C:/missing/chatlogs"
    manager.sessions = {}
    manager._save_config = MagicMock()

    with (
        patch("src.main.os.path.isdir", return_value=False),
        patch("PySide6.QtWidgets.QFileDialog", FakeFileDialog),
        patch("PySide6.QtWidgets.QMessageBox", FakeMessageBox),
    ):
        assert manager._ensure_log_directory() is False

    manager._save_config.assert_not_called()

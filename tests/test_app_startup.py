from unittest.mock import MagicMock

import pytest

from src.main import run_application


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

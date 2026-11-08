"""Settings dialog usability and Save/Cancel merge behaviour."""

import copy
import sys
from unittest.mock import MagicMock, patch

import pytest
from PySide6.QtCore import QObject, Qt
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication

from src.app_config import get_default_config
from src.core.session_specs import get_session_spec
from src.gui import settings as settings_module
from src.gui.settings import SettingsDialog


@pytest.fixture(scope="module", autouse=True)
def qt_app():
    app = QApplication.instance() or QApplication(sys.argv)
    yield app


def test_enter_in_a_text_field_saves_instead_of_opening_the_folder_picker(monkeypatch):
    picker_calls = []
    monkeypatch.setattr(
        settings_module.QFileDialog,
        "getExistingDirectory",
        staticmethod(lambda *args, **kwargs: picker_calls.append(args) or ""),
    )
    dialog = SettingsDialog(get_default_config())
    accepted = []
    dialog.accepted.connect(lambda: accepted.append(True))
    dialog.show()
    QApplication.processEvents()

    dialog.edit_target.setFocus()
    QTest.keyClick(dialog.edit_target, Qt.Key.Key_Return)
    QApplication.processEvents()

    assert picker_calls == []
    assert accepted == [True]
    assert dialog.btn_save.isDefault()


def test_dialog_can_shrink_to_fit_small_laptop_screens():
    dialog = SettingsDialog(get_default_config())

    # Tab contents scroll, so the dialog no longer needs ~826 px of height.
    assert dialog.minimumSizeHint().height() < 600
    assert dialog.height() <= 760


def test_reset_position_uses_session_default_size_and_previews():
    dialog = SettingsDialog(get_default_config())
    previews = []
    dialog.settings_changed.connect(previews.append)

    dialog._reset_position("fleet")

    spec = get_session_spec("fleet")
    session = dialog.config["sessions"]["fleet"]
    assert (session["x"], session["y"]) == (-1, -1)
    assert (session["w"], session["h"]) == (spec.default_width, spec.default_height)
    assert previews, "reset should preview immediately"
    assert "default position" in dialog._reset_labels["fleet"].text()


@pytest.mark.parametrize("opacity", [0.29, 0.57, 0.58, 0.8])
def test_opacity_survives_open_and_save_without_drift(opacity):
    config = get_default_config()
    config["shared"]["opacity"] = opacity

    assert SettingsDialog(config).get_settings()["shared"]["opacity"] == opacity


def _manager():
    with patch("src.main.TranslatorManager.__init__", lambda self: None):
        from src.main import TranslatorManager

        manager = TranslatorManager()
        QObject.__init__(manager)
    manager.config = get_default_config()
    manager.config["shared"]["provider_choice_confirmed"] = True
    manager.config["sessions"]["local"].update({
        "enabled": False,
        "system_name": "B-9C24",
    })
    manager.sessions = {"fleet": None, "local": None, "any": None}
    manager.worker = MagicMock()
    manager.worker.request_config_update.return_value = 1
    manager.tray_icon = MagicMock()
    manager._save_config = MagicMock()
    manager._apply_config_update = MagicMock()
    manager._apply_scanner_config = MagicMock()
    manager._queue_worker_config = MagicMock()
    return manager


def _dialog_returning(manager, *, accept, edit=None, during_exec=None):
    """Patch SettingsDialog with one that snapshots config like the real one."""
    dialog_config = copy.deepcopy(manager.config)

    def exec_dialog():
        if during_exec is not None:
            during_exec()
        if edit is not None:
            edit(dialog_config)
        return accept

    dialog = MagicMock()
    dialog.get_settings.side_effect = lambda: dialog_config
    dialog.exec.side_effect = exec_dialog
    dialog.pending_doctrine_source = None
    return dialog


def _runtime_changes(manager):
    def apply():
        # Discovery reports a jump and the pilot enables Local from the tray
        # while the modal dialog is still open.
        manager.config["sessions"]["local"]["system_name"] = "Jita"
        manager.config["sessions"]["local"]["enabled"] = True
        manager.config["shared"]["speaker_roles"] = {"fleet boss": "fc"}
    return apply


def test_save_applies_only_dialog_edits_and_keeps_runtime_state():
    manager = _manager()
    dialog = _dialog_returning(
        manager,
        accept=True,
        edit=lambda config: config["shared"].update({"font_size": 14}),
        during_exec=_runtime_changes(manager),
    )

    with patch("src.main.SettingsDialog", return_value=dialog):
        manager.open_settings_dialog()

    local = manager.config["sessions"]["local"]
    assert manager.config["shared"]["font_size"] == 14
    assert local["system_name"] == "Jita"
    assert local["enabled"] is True
    assert manager.config["shared"]["speaker_roles"] == {"fleet boss": "fc"}
    # A visual-only edit does not reset the provider pipeline or its caches.
    manager._queue_worker_config.assert_not_called()
    manager._save_config.assert_called_once_with()


def test_cancel_reverts_preview_but_keeps_runtime_state():
    manager = _manager()
    original_opacity = manager.config["shared"]["opacity"]

    def preview_and_runtime_changes():
        manager.preview_settings({"shared": {"opacity": 0.4}, "sessions": {}})
        _runtime_changes(manager)()

    dialog = _dialog_returning(
        manager,
        accept=False,
        during_exec=preview_and_runtime_changes,
    )

    with patch("src.main.SettingsDialog", return_value=dialog):
        manager.open_settings_dialog()

    local = manager.config["sessions"]["local"]
    assert manager.config["shared"]["opacity"] == original_opacity
    assert local["system_name"] == "Jita"
    assert local["enabled"] is True
    manager._save_config.assert_called_once_with()


def test_opening_settings_twice_raises_the_open_dialog():
    manager = _manager()
    existing = MagicMock()
    manager.settings_dialog = existing

    with patch("src.main.SettingsDialog") as dialog_cls:
        manager.open_settings_dialog()

    dialog_cls.assert_not_called()
    existing.raise_.assert_called_once_with()
    existing.activateWindow.assert_called_once_with()

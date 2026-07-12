import os
import sys
import threading
import time
from types import SimpleNamespace
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication, QLineEdit

from src.app_config import get_default_config
from src.gui.settings import SettingsDialog
from src.services.translator import ProviderTestResult


app = QApplication.instance() or QApplication(sys.argv)


def wait_for_provider_test(dialog, timeout_seconds=1.0):
    deadline = time.monotonic() + timeout_seconds
    while dialog._provider_test_running and time.monotonic() < deadline:
        app.processEvents()
        time.sleep(0.005)
    app.processEvents()
    assert dialog._provider_test_running is False


class FakeTranslationService:
    def __init__(self, result):
        self.result = result
        self.calls = []

    def test_provider(self, provider_mode, deepl_api_key, target_lang):
        self.calls.append((provider_mode, deepl_api_key, target_lang))
        return self.result


def test_settings_default_to_disabled_and_mask_api_key():
    dialog = SettingsDialog(get_default_config())

    assert dialog.combo_provider.currentData() == 'disabled'
    assert dialog.combo_provider.currentText() == 'Disabled (no translation)'
    assert dialog.btn_test_provider.isEnabled() is False
    assert dialog.edit_deepl.echoMode() == QLineEdit.EchoMode.Password
    assert dialog.edit_deepl.isEnabled() is False

    dialog.close()


def test_doctrine_import_is_validated_but_not_installed_until_save():
    dialog = SettingsDialog(get_default_config())
    validated = SimpleNamespace(name='Armor HACs', version='1.0')

    with patch(
        'src.gui.settings.QFileDialog.getOpenFileName',
        return_value=('C:/packs/armor.zip', 'Doctrine packs (*.zip)'),
    ), patch(
        'src.core.doctrine.load_doctrine_pack',
        return_value=validated,
    ), patch('src.core.doctrine.import_doctrine_pack') as install:
        dialog._import_doctrine_pack()

    assert dialog.pending_doctrine_source == 'C:/packs/armor.zip'
    assert dialog.config['shared']['active_doctrine_pack'] == ''
    assert 'ready to import on Save' in dialog.lbl_doctrine.text()
    install.assert_not_called()
    dialog.close()


def test_settings_migrate_legacy_deepl_key_and_reveal_on_request():
    config = get_default_config()
    config['shared'].pop('translation_provider')
    config['shared']['deepl_api_key'] = 'legacy-secret'
    dialog = SettingsDialog(config)

    assert dialog.combo_provider.currentData() == 'deepl'
    assert dialog.edit_deepl.text() == 'legacy-secret'
    assert dialog.edit_deepl.echoMode() == QLineEdit.EchoMode.Password

    dialog.chk_reveal_deepl.setChecked(True)
    assert dialog.edit_deepl.echoMode() == QLineEdit.EchoMode.Normal

    dialog.close()


def test_switching_away_from_deepl_hides_revealed_key():
    config = get_default_config()
    config['shared']['translation_provider'] = 'deepl'
    config['shared']['deepl_api_key'] = 'stored-secret'
    dialog = SettingsDialog(config)
    dialog.chk_reveal_deepl.setChecked(True)

    dialog.combo_provider.setCurrentIndex(dialog.combo_provider.findData('google'))

    assert dialog.chk_reveal_deepl.isChecked() is False
    assert dialog.edit_deepl.echoMode() == QLineEdit.EchoMode.Password
    assert dialog.edit_deepl.isEnabled() is False

    dialog.close()


def test_deepl_requires_key_before_settings_can_be_saved():
    config = get_default_config()
    dialog = SettingsDialog(config)

    dialog.combo_provider.setCurrentIndex(dialog.combo_provider.findData('deepl'))

    assert dialog.btn_save.isEnabled() is False
    assert 'API key is required' in dialog.lbl_provider_validation.text()

    dialog.edit_deepl.setText('configured-key')

    assert dialog.btn_save.isEnabled() is True
    assert dialog.lbl_provider_validation.text() == ''
    dialog.close()


def test_cloud_provider_requires_iso_style_target_language():
    config = get_default_config()
    dialog = SettingsDialog(config)
    dialog.combo_provider.setCurrentIndex(dialog.combo_provider.findData('google'))

    dialog.edit_target.setText('not a language')

    assert dialog.btn_save.isEnabled() is False
    assert 'target language' in dialog.lbl_provider_validation.text()

    dialog.edit_target.setText('pt-BR')

    assert dialog.btn_save.isEnabled() is True
    assert dialog.lbl_provider_validation.text() == ''
    dialog.close()


def test_provider_test_uses_injected_service_and_shows_success():
    fake_service = FakeTranslationService(
        ProviderTestResult(True, 'Google', 'Google test succeeded.')
    )
    config = get_default_config()
    config['shared']['deepl_api_key'] = 'stored-deepl-secret'
    dialog = SettingsDialog(config, translation_service=fake_service)
    dialog.combo_provider.setCurrentIndex(dialog.combo_provider.findData('google'))

    dialog.btn_test_provider.click()
    wait_for_provider_test(dialog)

    assert fake_service.calls == [('google', '', 'en')]
    assert dialog.lbl_provider_test.text() == 'Google test succeeded.'
    assert dialog.get_settings()['shared']['translation_provider'] == 'google'

    dialog.close()


def test_provider_test_redacts_key_from_failure_message():
    secret = 'never-display-this-key'
    fake_service = FakeTranslationService(
        ProviderTestResult(False, 'DeepL', f'DeepL rejected {secret}')
    )
    config = get_default_config()
    config['shared']['translation_provider'] = 'deepl'
    config['shared']['deepl_api_key'] = secret
    dialog = SettingsDialog(config, translation_service=fake_service)

    dialog.btn_test_provider.click()
    wait_for_provider_test(dialog)

    assert fake_service.calls == [('deepl', secret, 'en')]
    assert secret not in dialog.lbl_provider_test.text()
    assert '<redacted>' in dialog.lbl_provider_test.text()

    dialog.close()


def test_malformed_provider_values_do_not_prevent_settings_from_opening():
    config = get_default_config()
    config['shared']['translation_provider'] = 'deepl'
    config['shared']['target_language'] = 123
    config['shared']['deepl_api_key'] = 456

    dialog = SettingsDialog(config)

    assert dialog.edit_target.text() == 'en'
    assert dialog.edit_deepl.text() == ''
    assert dialog.get_settings()['shared']['target_language'] == 'en'

    dialog.close()


def test_empty_target_is_saved_as_english_and_invalidates_provider_test():
    config = get_default_config()
    dialog = SettingsDialog(config)
    dialog.lbl_provider_test.setText('Google test succeeded.')

    dialog.edit_target.setText('')

    assert dialog.lbl_provider_test.text() == ''
    assert dialog.get_settings()['shared']['target_language'] == 'en'

    dialog.close()


def test_closing_dialog_invalidates_an_in_flight_provider_test():
    started = threading.Event()
    release = threading.Event()

    class BlockingTranslationService:
        def test_provider(self, provider_mode, deepl_api_key, target_lang):
            started.set()
            release.wait(1.0)
            return ProviderTestResult(True, 'Google', 'late result')

    config = get_default_config()
    dialog = SettingsDialog(config, translation_service=BlockingTranslationService())
    dialog.combo_provider.setCurrentIndex(dialog.combo_provider.findData('google'))

    dialog.btn_test_provider.click()
    assert started.wait(0.5)
    assert dialog._provider_test_running is True

    dialog.reject()
    assert dialog._provider_test_running is False
    release.set()

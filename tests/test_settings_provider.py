import os
import sys
import threading
import time
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

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
    assert dialog.chk_provider_fallback.isChecked() is False
    assert dialog.chk_provider_fallback.isEnabled() is False
    assert dialog.btn_refresh_provider_usage.isEnabled() is False

    dialog.close()


def test_google_fallback_requires_deepl_key_and_records_explicit_consent():
    dialog = SettingsDialog(get_default_config())
    dialog.combo_provider.setCurrentIndex(dialog.combo_provider.findData('google'))

    assert dialog.chk_provider_fallback.isEnabled() is True
    assert dialog.edit_deepl.isEnabled() is False

    dialog.chk_provider_fallback.setChecked(True)

    assert dialog.edit_deepl.isEnabled() is True
    assert dialog.btn_save.isEnabled() is False
    assert 'DeepL API key is required' in dialog.lbl_provider_validation.text()
    assert 'same message will be sent to the second cloud provider' in (
        dialog.lbl_provider_fallback.text()
    )

    dialog.edit_deepl.setText('fallback-key')

    assert dialog.btn_save.isEnabled() is True
    settings = dialog.get_settings()['shared']
    assert settings['translation_provider'] == 'google'
    assert settings['provider_fallback_enabled'] is True
    assert settings['deepl_api_key'] == 'fallback-key'
    dialog.close()


def test_disabled_provider_cannot_retain_fallback_consent():
    config = get_default_config()
    config['shared']['translation_provider'] = 'google'
    config['shared']['provider_fallback_enabled'] = True
    config['shared']['deepl_api_key'] = 'fallback-key'
    dialog = SettingsDialog(config)

    dialog.combo_provider.setCurrentIndex(dialog.combo_provider.findData('disabled'))

    assert dialog.chk_provider_fallback.isChecked() is False
    assert dialog.chk_provider_fallback.isEnabled() is False
    assert dialog.get_settings()['shared']['provider_fallback_enabled'] is False
    dialog.close()


def test_deepl_fallback_is_google_and_needs_no_additional_credential():
    config = get_default_config()
    config['shared']['translation_provider'] = 'deepl'
    config['shared']['deepl_api_key'] = 'primary-key'
    dialog = SettingsDialog(config)

    dialog.chk_provider_fallback.setChecked(True)

    assert dialog.chk_provider_fallback.text() == (
        'Fallback to Google after DeepL fails'
    )
    assert dialog.btn_save.isEnabled() is True
    assert dialog.get_settings()['shared']['provider_fallback_enabled'] is True
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


def test_refresh_usage_emits_request_and_shows_loading_state():
    config = get_default_config()
    config['shared']['translation_provider'] = 'google'
    dialog = SettingsDialog(config)
    requests = []
    dialog.provider_usage_requested.connect(lambda: requests.append(True))

    dialog.btn_refresh_provider_usage.click()

    assert requests == [True]
    assert dialog.lbl_provider_usage.text() == 'Refreshing provider usage...'
    dialog.close()


def test_deepl_usage_shows_exact_counts_percent_and_threshold_warning():
    config = get_default_config()
    config['shared']['translation_provider'] = 'deepl'
    config['shared']['deepl_api_key'] = 'primary-key'
    dialog = SettingsDialog(config)

    dialog.set_provider_usage({
        'provider': 'deepl',
        'used': 800_000,
        'limit': 1_000_000,
    })

    usage_text = dialog.lbl_provider_usage.text()
    assert '800,000 / 1,000,000 characters used' in usage_text
    assert '(80.0%)' in usage_text
    assert 'Warning' in usage_text
    dialog.close()


def test_deepl_usage_accepts_sdk_field_names_without_warning_below_threshold():
    config = get_default_config()
    config['shared']['translation_provider'] = 'deepl'
    config['shared']['deepl_api_key'] = 'primary-key'
    dialog = SettingsDialog(config)

    dialog.set_provider_usage({
        'provider': 'deepl',
        'character_count': 799,
        'character_limit': 1_000,
    })

    usage_text = dialog.lbl_provider_usage.text()
    assert '799 / 1,000 characters used' in usage_text
    assert '(79.9%)' in usage_text
    assert 'Warning' not in usage_text
    dialog.close()


def test_google_usage_is_clearly_a_local_estimate_without_provider_limit():
    config = get_default_config()
    config['shared']['translation_provider'] = 'google'
    dialog = SettingsDialog(config)

    dialog.set_provider_usage({
        'provider': 'google',
        'estimated_characters': 12_345,
    })

    usage_text = dialog.lbl_provider_usage.text()
    assert 'local runtime estimate: 12,345 characters' in usage_text
    assert 'Estimate only' in usage_text
    assert 'no provider usage limit' in usage_text
    dialog.close()


def test_usage_error_is_generic_and_stale_provider_response_is_ignored():
    config = get_default_config()
    config['shared']['translation_provider'] = 'google'
    dialog = SettingsDialog(config)

    dialog.set_provider_usage({'provider': 'google', 'error': 'secret-key leaked'})

    assert 'secret-key' not in dialog.lbl_provider_usage.text()
    assert 'unavailable' in dialog.lbl_provider_usage.text()
    previous = dialog.lbl_provider_usage.text()

    dialog.set_provider_usage({
        'provider': 'deepl',
        'used': 1,
        'limit': 100,
    })

    assert dialog.lbl_provider_usage.text() == previous
    dialog.close()


def test_correction_export_creates_doctrine_zip_without_glossary_conversion():
    service = MagicMock()
    service.entity_index = None
    service.translation_memory.count.return_value = 2
    dialog = SettingsDialog(get_default_config(), translation_service=service)
    exported = SimpleNamespace(corrections=(object(), object()))

    with patch(
        'src.gui.settings.QFileDialog.getSaveFileName',
        return_value=('C:/tmp/My Fleet Corrections.zip', 'Doctrine packs (*.zip)'),
    ), patch(
        'src.core.doctrine.export_correction_pack',
        return_value=exported,
    ) as export, patch('src.gui.settings.QMessageBox.information') as information:
        dialog.btn_export_corrections.click()

    export.assert_called_once_with(
        service.translation_memory,
        Path('C:/tmp/My Fleet Corrections.zip'),
        pack_id='my-fleet-corrections',
        name='EVE Fleet Corrections',
        doctrine_scope='',
        overwrite=True,
    )
    assert '2 exact corrections' in information.call_args.args[2]
    dialog.close()

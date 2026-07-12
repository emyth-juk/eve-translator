import copy
import os
import re
import threading
import sqlite3
from pathlib import Path

from PySide6.QtWidgets import (QDialog, QVBoxLayout, QHBoxLayout, QLabel,
                               QSlider, QSpinBox, QCheckBox, QPushButton,
                               QColorDialog, QFormLayout, QGroupBox, QWidget, QTabWidget,
                               QFileDialog, QLineEdit, QComboBox, QMessageBox)
from PySide6.QtCore import QObject, Qt, QTimer, Signal, Slot
from PySide6.QtGui import QColor
from src.app_config import (
    DISPLAY_MODE_BILINGUAL,
    DISPLAY_MODE_COMBAT,
    DISPLAY_MODE_MIRROR,
    TRANSLATION_PROVIDER_DEEPL,
    TRANSLATION_PROVIDER_DISABLED,
    TRANSLATION_PROVIDER_GOOGLE,
    normalize_translation_provider_config,
)
from src.services.translator import TranslationService
from src.version import __version__


PROVIDER_TEST_TIMEOUT_MS = 15_000
LANGUAGE_CODE_PATTERN = re.compile(r"^[A-Za-z]{2,3}(?:[-_][A-Za-z]{2,4})?$")


class ProviderTestSignals(QObject):
    finished = Signal(int, object, str)


class SettingsDialog(QDialog):
    # Signal carrying the updated config dict (full structure)
    settings_changed = Signal(dict)
    provider_usage_requested = Signal()

    def __init__(self, full_config, parent=None, translation_service=None):
        super().__init__(parent)
        self.setWindowTitle(f"Global Settings - v{__version__}")
        self.resize(520, 700)
        # Deep copy config to allow reversion/modification
        self.config = copy.deepcopy(full_config)
        normalize_translation_provider_config(self.config.setdefault('shared', {}))
        self._translation_service = translation_service
        self.pending_doctrine_source = None
        self._pending_doctrine_pack = None
        self._provider_test_signals = ProviderTestSignals(self)
        self._provider_test_signals.finished.connect(self._provider_test_finished)
        self._provider_test_token = 0
        self._provider_test_running = False
        self.btn_save = QPushButton("Save")
        self.btn_save.clicked.connect(self.accept)
        
        self.main_layout = QVBoxLayout(self)
        
        self.tabs = QTabWidget()
        self.main_layout.addWidget(self.tabs)
        
        # 1. Shared (General) Tab
        self.tab_shared = QWidget()
        self._init_shared_tab()
        self.tabs.addTab(self.tab_shared, "General")
        
        # 2. Fleet Tab
        self.tab_fleet = QWidget()
        self._init_session_tab(self.tab_fleet, 'fleet')
        self.tabs.addTab(self.tab_fleet, "Fleet Overlay")
        
        # 3. Local Tab
        self.tab_local = QWidget()
        self._init_session_tab(self.tab_local, 'local')
        self.tabs.addTab(self.tab_local, "Local Overlay")

        # 4. EVE-aware translation controls stay out of the dense General tab.
        self.tab_intelligence = QWidget()
        self._init_intelligence_tab()
        self.tabs.addTab(self.tab_intelligence, "Translation Intelligence")
        
        # Buttons
        btn_box = QHBoxLayout()
        btn_cancel = QPushButton("Cancel")
        btn_cancel.clicked.connect(self.reject)
        
        # Optional: Reset Layouts button?
        
        btn_box.addWidget(self.btn_save)
        btn_box.addWidget(btn_cancel)
        self.main_layout.addLayout(btn_box)

    def _init_shared_tab(self):
        layout = QVBoxLayout(self.tab_shared)
        
        # Paths Group
        grp_paths = QGroupBox("Paths")
        form_paths = QFormLayout(grp_paths)
        
        log_dir = self._config_text(self.config['shared'].get('log_dir', ''))
        self.edit_log_dir = QLineEdit(log_dir)
        self.edit_log_dir.setReadOnly(True)
        
        btn_browse = QPushButton("Browse...")
        btn_browse.clicked.connect(self._browse_log_dir)
        
        # Horizontal layout for line edit + button
        h_path = QHBoxLayout()
        h_path.addWidget(self.edit_log_dir)
        h_path.addWidget(btn_browse)
        
        form_paths.addRow("EVE Logs:", h_path)
        layout.addWidget(grp_paths)

        # Appearance Group
        grp_app = QGroupBox("Shared Appearance")
        form = QFormLayout(grp_app)
        
        # Opacity
        self.slider_opacity = QSlider(Qt.Orientation.Horizontal)
        self.slider_opacity.setRange(20, 100)
        self.slider_opacity.setValue(int(self.config['shared'].get('opacity', 0.8) * 100))
        self.slider_opacity.valueChanged.connect(self._notify_change)
        form.addRow("Opacity:", self.slider_opacity)
        
        # Font Size
        self.spin_font = QSpinBox()
        self.spin_font.setRange(8, 36)
        self.spin_font.setValue(self.config['shared'].get('font_size', 10))
        self.spin_font.valueChanged.connect(self._notify_change)
        form.addRow("Font Size:", self.spin_font)
        
        layout.addWidget(grp_app)
        
        # Colors (Shared)
        grp_colors = QGroupBox("Text Colors")
        form_col = QFormLayout(grp_colors)
        
        self.btn_col_def = self._make_color_btn('color_default', self.config['shared'].get('color_default', '#e0e0e0'))
        form_col.addRow("Default Text:", self.btn_col_def)
        
        self.btn_col_trans = self._make_color_btn('color_translated', self.config['shared'].get('color_translated', '#00ffff'))
        form_col.addRow("Translated:", self.btn_col_trans)
        
        self.btn_col_high = self._make_color_btn('color_highlight', self.config['shared'].get('color_highlight', 'yellow'))
        form_col.addRow("Highlights:", self.btn_col_high)
        
        layout.addWidget(grp_colors)
        
        # Behavior / Translation
        grp_beh = QGroupBox("Translation & Behavior")
        form_beh = QFormLayout(grp_beh)
        
        self.chk_autoscroll = QCheckBox("Auto-scroll")
        self.chk_autoscroll.setChecked(self.config['shared'].get('auto_scroll', True))
        self.chk_autoscroll.stateChanged.connect(self._notify_change)
        form_beh.addRow(self.chk_autoscroll)

        self.combo_provider = QComboBox()
        self.combo_provider.addItem("Disabled (no translation)", TRANSLATION_PROVIDER_DISABLED)
        self.combo_provider.addItem("Google (cloud)", TRANSLATION_PROVIDER_GOOGLE)
        self.combo_provider.addItem("DeepL (cloud)", TRANSLATION_PROVIDER_DEEPL)
        provider_mode = self.config['shared'].get(
            'translation_provider', TRANSLATION_PROVIDER_DISABLED
        )
        provider_index = self.combo_provider.findData(provider_mode)
        self.combo_provider.setCurrentIndex(max(provider_index, 0))
        form_beh.addRow("Provider:", self.combo_provider)

        self.lbl_provider_hint = QLabel()
        self.lbl_provider_hint.setWordWrap(True)
        self.lbl_provider_hint.setStyleSheet("color: gray; font-style: italic;")
        form_beh.addRow("", self.lbl_provider_hint)

        self.chk_provider_fallback = QCheckBox()
        self.chk_provider_fallback.setChecked(
            self.config['shared'].get('provider_fallback_enabled') is True
        )
        form_beh.addRow(self.chk_provider_fallback)

        self.lbl_provider_fallback = QLabel(
            "If enabled, the same message will be sent to the second cloud "
            "provider only after the primary provider fails. If Context assist "
            "is enabled, its same-channel context lines follow that request."
        )
        self.lbl_provider_fallback.setWordWrap(True)
        self.lbl_provider_fallback.setStyleSheet(
            "color: gray; font-style: italic;"
        )
        form_beh.addRow("", self.lbl_provider_fallback)

        target_language = self._config_text(
            self.config['shared'].get('target_language', 'en'),
            default='en',
        ) or 'en'
        self.edit_target = self._make_line_edit(target_language)
        form_beh.addRow("Target Lang (ISO):", self.edit_target)
        
        # Ignored
        ign = self.config['shared'].get('ignored_languages', [])
        ign_str = ", ".join(ign) if isinstance(ign, list) else str(ign)
        self.edit_ignored = self._make_line_edit(ign_str)
        form_beh.addRow("Ignored Langs:", self.edit_ignored)
        
        # API key is masked by default and preserved when another provider is selected.
        deepl_api_key = self._config_text(self.config['shared'].get('deepl_api_key', ''))
        self.edit_deepl = self._make_line_edit(deepl_api_key)
        self.edit_deepl.setEchoMode(QLineEdit.EchoMode.Password)
        self.chk_reveal_deepl = QCheckBox("Show")
        self.chk_reveal_deepl.toggled.connect(self._set_api_key_revealed)

        api_key_row = QHBoxLayout()
        api_key_row.addWidget(self.edit_deepl)
        api_key_row.addWidget(self.chk_reveal_deepl)
        form_beh.addRow("DeepL API Key:", api_key_row)

        self.btn_test_provider = QPushButton("Test Provider")
        self.btn_test_provider.clicked.connect(self._test_provider)
        self.lbl_provider_test = QLabel("")
        self.lbl_provider_test.setWordWrap(True)

        provider_test_row = QHBoxLayout()
        provider_test_row.addWidget(self.btn_test_provider)
        provider_test_row.addWidget(self.lbl_provider_test, 1)
        form_beh.addRow("", provider_test_row)

        self.btn_refresh_provider_usage = QPushButton("Refresh Usage")
        self.btn_refresh_provider_usage.clicked.connect(
            self._request_provider_usage
        )
        self.lbl_provider_usage = QLabel("")
        self.lbl_provider_usage.setWordWrap(True)

        provider_usage_row = QHBoxLayout()
        provider_usage_row.addWidget(self.btn_refresh_provider_usage)
        provider_usage_row.addWidget(self.lbl_provider_usage, 1)
        form_beh.addRow("Usage:", provider_usage_row)

        self.lbl_provider_validation = QLabel("")
        self.lbl_provider_validation.setWordWrap(True)
        self.lbl_provider_validation.setStyleSheet("color: #c0392b;")
        form_beh.addRow("", self.lbl_provider_validation)

        self.combo_provider.currentIndexChanged.connect(self._provider_mode_changed)
        self.chk_provider_fallback.toggled.connect(
            self._provider_fallback_changed
        )
        self.edit_deepl.textChanged.connect(self._provider_input_changed)
        self.edit_target.textChanged.connect(self._provider_input_changed)
        self._update_provider_controls()
        self._show_provider_usage_idle()
        
        layout.addWidget(grp_beh)
        
        # Performance
        grp_perf = QGroupBox("Performance")
        form_perf = QFormLayout(grp_perf)
        
        # Need to import QDoubleSpinBox inside init or at top. 
        # It's not imported at top: QSpinBox is, but not QDoubleSpinBox.
        # Let's import it locally to be safe or rely on existing imports if any.
        from PySide6.QtWidgets import QDoubleSpinBox
        
        self.spin_poll = QDoubleSpinBox()
        self.spin_poll.setRange(0.1, 10.0)
        self.spin_poll.setSingleStep(0.1)
        self.spin_poll.setValue(float(self.config['shared'].get('polling_interval', 1.0)))
        self.spin_poll.setSuffix(" sec")
        self.spin_poll.valueChanged.connect(self._notify_change)
        
        lbl_hint = QLabel("Setting this to 2.0s might improve performance.")
        lbl_hint.setStyleSheet("color: gray; font-style: italic;")
        
        form_perf.addRow("Log Poll Interval:", self.spin_poll)
        form_perf.addRow("", lbl_hint)
        
        layout.addWidget(grp_perf)
        
        layout.addStretch()

    def _init_session_tab(self, tab_widget, session_id):
        layout = QVBoxLayout(tab_widget)
        
        session_cfg = self.config['sessions'][session_id]

        grp_hud = QGroupBox("Combat HUD")
        form_hud = QFormLayout(grp_hud)
        mode_combo = QComboBox()
        mode_combo.addItem("Combat — trusted calls and starred speakers", DISPLAY_MODE_COMBAT)
        mode_combo.addItem("Bilingual — translated and starred messages", DISPLAY_MODE_BILINGUAL)
        mode_combo.addItem("Mirror — complete chat feed", DISPLAY_MODE_MIRROR)
        selected_mode = session_cfg.get('display_mode', DISPLAY_MODE_MIRROR)
        mode_combo.setCurrentIndex(max(0, mode_combo.findData(selected_mode)))
        mode_combo.currentIndexChanged.connect(self._notify_change)
        form_hud.addRow("Display mode:", mode_combo)

        compact_header = QCheckBox("Show compact channel/status header")
        compact_header.setChecked(session_cfg.get('compact_header', True))
        compact_header.stateChanged.connect(self._notify_change)
        form_hud.addRow(compact_header)

        command_cards = QCheckBox("Render exact command cards")
        command_cards.setChecked(session_cfg.get('command_cards', True))
        command_cards.stateChanged.connect(self._notify_change)
        form_hud.addRow(command_cards)

        hint = QLabel(
            "Star speakers or mark FC/scout roles from an unlocked overlay. "
            "Only explicit FC roles create verified execution cards."
        )
        hint.setWordWrap(True)
        hint.setStyleSheet("color: gray; font-style: italic;")
        form_hud.addRow("", hint)
        layout.addWidget(grp_hud)
        if not hasattr(self, '_session_hud_controls'):
            self._session_hud_controls = {}
        self._session_hud_controls[session_id] = (
            mode_combo,
            compact_header,
            command_cards,
        )
        
        # Visibility check? (Just implicit via enable/disable in tray)
        
        if session_id == 'fleet':
            grp_fleet = QGroupBox("Fleet Behavior")
            form_fleet = QFormLayout(grp_fleet)

            # Fleet inactivity threshold (minutes)
            self.spin_fleet_threshold = QSpinBox()
            self.spin_fleet_threshold.setRange(1, 120)  # 1-120 minutes
            threshold_seconds = self.config['shared'].get('fleet_inactive_threshold', 1800)
            self.spin_fleet_threshold.setValue(threshold_seconds // 60)  # Convert to minutes
            self.spin_fleet_threshold.setSuffix(" min")
            self.spin_fleet_threshold.valueChanged.connect(self._notify_change)
            form_fleet.addRow("Inactive Threshold:", self.spin_fleet_threshold)

            # Auto-switch on inactive
            self.chk_fleet_autoswitch = QCheckBox("Auto-switch when current fleet becomes inactive")
            self.chk_fleet_autoswitch.setChecked(self.config['shared'].get('fleet_auto_switch', True))
            self.chk_fleet_autoswitch.stateChanged.connect(self._notify_change)
            form_fleet.addRow(self.chk_fleet_autoswitch)

            # Fleet Scan Interval (seconds)
            self.spin_fleet_interval = QSpinBox()
            self.spin_fleet_interval.setRange(5, 300)  # 5s to 5 minutes
            self.spin_fleet_interval.setValue(self.config['shared'].get('fleet_scan_interval', 10))
            self.spin_fleet_interval.setSuffix(" sec")
            self.spin_fleet_interval.valueChanged.connect(self._notify_change)
            form_fleet.addRow("Scan Interval:", self.spin_fleet_interval)

            # Backfill History Lines
            self.spin_fleet_history = QSpinBox()
            self.spin_fleet_history.setRange(0, 50) # 0 to 50 lines
            self.spin_fleet_history.setValue(self.config['shared'].get('fleet_history_lines', 5))
            self.spin_fleet_history.setSuffix(" lines")
            self.spin_fleet_history.valueChanged.connect(self._notify_change)
            form_fleet.addRow("History Lines:", self.spin_fleet_history)

            layout.addWidget(grp_fleet)
        
        grp_layout = QGroupBox("Layout")
        form = QFormLayout(grp_layout)
        
        # Background Color (Each session can have distinct BG)
        bg_col = session_cfg.get('background_color', '#000000')
        btn_bg = QPushButton()
        btn_bg.setStyleSheet(f"background-color: {bg_col};")
        # Need to store ref to retrieve value later? 
        # Or cleaner: update config immediately on pick?
        # But we want 'Cancel' to revert.
        # So we store transient value in 'self.config' via lambda
        btn_bg.clicked.connect(lambda: self._pick_color_session(btn_bg, session_id, 'background_color'))
        form.addRow("Background Color:", btn_bg)
        
        layout.addWidget(grp_layout)
        
        # Position Reset
        btn_reset = QPushButton("Reset Position to Default")
        btn_reset.clicked.connect(lambda: self._reset_position(session_id))
        layout.addWidget(btn_reset)
        
        layout.addStretch()

    def _init_intelligence_tab(self):
        layout = QVBoxLayout(self.tab_intelligence)
        shared = self.config['shared']

        grp_runtime = QGroupBox("Translation pipeline")
        form_runtime = QFormLayout(grp_runtime)
        self.chk_burst_optimization = QCheckBox(
            "Burst optimization (recommended; up to five compatible messages)"
        )
        self.chk_burst_optimization.setChecked(shared.get('burst_optimization', True))
        self.chk_burst_optimization.stateChanged.connect(self._notify_change)
        form_runtime.addRow(self.chk_burst_optimization)

        self.chk_context_assist = QCheckBox(
            "Context assist (sends recent same-channel lines to supported cloud providers)"
        )
        self.chk_context_assist.setChecked(shared.get('context_assist', False))
        self.chk_context_assist.stateChanged.connect(self._notify_change)
        form_runtime.addRow(self.chk_context_assist)

        context_note = QLabel(
            "Context is off by default, never crosses Fleet/Local or log sources, "
            "and is used only through a provider-native context API."
        )
        context_note.setWordWrap(True)
        context_note.setStyleSheet("color: gray; font-style: italic;")
        form_runtime.addRow("", context_note)
        layout.addWidget(grp_runtime)

        grp_entities = QGroupBox("EVE name protection")
        form_entities = QFormLayout(grp_entities)
        self.chk_entity_protection = QCheckBox("Protect SDE and doctrine entity names")
        self.chk_entity_protection.setChecked(shared.get('entity_protection', True))
        self.chk_entity_protection.stateChanged.connect(self._notify_change)
        form_entities.addRow(self.chk_entity_protection)
        index = getattr(self._translation_service, 'entity_index', None)
        if index is not None and getattr(index, 'available', False):
            index_text = (
                f"Installed · SDE {getattr(index, 'sde_version', '') or 'unknown'} "
                f"· {getattr(index, 'entity_count', 0):,} names"
            )
        else:
            index_text = "No generated index installed; translation continues normally"
        self.lbl_entity_index = QLabel(index_text)
        self.lbl_entity_index.setWordWrap(True)
        form_entities.addRow("Entity index:", self.lbl_entity_index)
        layout.addWidget(grp_entities)

        grp_local = QGroupBox("Local intelligence")
        form_local = QFormLayout(grp_local)
        memory = getattr(self._translation_service, 'translation_memory', None)
        try:
            correction_count = memory.count() if memory is not None else 0
        except (OSError, sqlite3.Error):
            correction_count = 0
        self.lbl_correction_count = QLabel(f"{correction_count:,} exact local corrections")
        form_local.addRow("Correction memory:", self.lbl_correction_count)
        self.btn_export_corrections = QPushButton("Export corrections ZIPâ€¦")
        self.btn_export_corrections.setEnabled(memory is not None and correction_count > 0)
        self.btn_export_corrections.clicked.connect(self._export_correction_pack)
        form_local.addRow("Share corrections:", self.btn_export_corrections)

        self.lbl_doctrine = QLabel()
        self.lbl_doctrine.setWordWrap(True)
        doctrine_row = QHBoxLayout()
        doctrine_row.addWidget(self.lbl_doctrine, 1)
        btn_import = QPushButton("Import ZIP…")
        btn_import.clicked.connect(self._import_doctrine_pack)
        doctrine_row.addWidget(btn_import)
        btn_deactivate = QPushButton("Deactivate")
        btn_deactivate.clicked.connect(self._deactivate_doctrine_pack)
        doctrine_row.addWidget(btn_deactivate)
        form_local.addRow("Doctrine pack:", doctrine_row)
        self._refresh_doctrine_label()
        layout.addWidget(grp_local)
        layout.addStretch()

    def _refresh_doctrine_label(self):
        if self._pending_doctrine_pack is not None:
            self.lbl_doctrine.setText(
                f"{self._pending_doctrine_pack.name} "
                f"v{self._pending_doctrine_pack.version} (ready to import on Save)"
            )
            return
        active = self.config['shared'].get('active_doctrine_pack', '')
        self.lbl_doctrine.setText(Path(active).name if active else "Built-in only")

    def _import_doctrine_pack(self):
        filename, _ = QFileDialog.getOpenFileName(
            self,
            "Import doctrine pack",
            "",
            "Doctrine packs (*.zip);;All files (*)",
        )
        if not filename:
            return
        try:
            from src.core.doctrine import load_doctrine_pack
            validated = load_doctrine_pack(filename)
        except Exception as exc:
            QMessageBox.warning(self, "Doctrine pack rejected", str(exc))
            return
        self.pending_doctrine_source = filename
        self._pending_doctrine_pack = validated
        self._refresh_doctrine_label()
        self._notify_change()

    def _deactivate_doctrine_pack(self):
        self.pending_doctrine_source = None
        self._pending_doctrine_pack = None
        self.config['shared']['active_doctrine_pack'] = ''
        self._refresh_doctrine_label()
        self._notify_change()

    def _export_correction_pack(self):
        memory = getattr(self._translation_service, 'translation_memory', None)
        if memory is None:
            QMessageBox.warning(
                self,
                "Corrections unavailable",
                "No local correction memory is available to export.",
            )
            return
        filename, _ = QFileDialog.getSaveFileName(
            self,
            "Export exact corrections",
            "eve-fleet-corrections.zip",
            "Doctrine packs (*.zip)",
        )
        if not filename:
            return
        destination = Path(filename)
        if destination.suffix.casefold() != '.zip':
            destination = destination.with_suffix('.zip')
        pack_id = re.sub(r'[^a-z0-9._-]+', '-', destination.stem.casefold())
        pack_id = pack_id.strip('._-')[:64] or 'fleet-corrections'
        doctrine_scope = ''
        active = self.config['shared'].get('active_doctrine_pack', '')
        if active:
            try:
                from src.core.doctrine import load_doctrine_pack
                doctrine_scope = load_doctrine_pack(active).scope
            except Exception:
                doctrine_scope = ''
        try:
            from src.core.doctrine import export_correction_pack
            exported = export_correction_pack(
                memory,
                destination,
                pack_id=pack_id,
                name="EVE Fleet Corrections",
                doctrine_scope=doctrine_scope,
                overwrite=True,
            )
        except Exception as exc:
            QMessageBox.warning(self, "Correction export failed", str(exc))
            return
        QMessageBox.information(
            self,
            "Corrections exported",
            f"Exported {len(exported.corrections):,} exact corrections as a safe doctrine pack.",
        )

    def _make_color_btn(self, shared_key, initial_hex):
        btn = QPushButton()
        btn.setStyleSheet(f"background-color: {initial_hex};")
        btn.clicked.connect(lambda: self._pick_color_shared(btn, shared_key))
        return btn
        
    def _make_line_edit(self, text):
        from PySide6.QtWidgets import QLineEdit
        le = QLineEdit(text)
        return le

    @staticmethod
    def _config_text(value, default=''):
        """Return safe line-edit text for values loaded from user-editable JSON."""
        return value.strip() if isinstance(value, str) else default

    def _selected_provider_mode(self):
        return self.combo_provider.currentData() or TRANSLATION_PROVIDER_DISABLED

    def _set_api_key_revealed(self, revealed):
        echo_mode = (
            QLineEdit.EchoMode.Normal if revealed else QLineEdit.EchoMode.Password
        )
        self.edit_deepl.setEchoMode(echo_mode)

    def _provider_mode_changed(self, *args):
        self._cancel_provider_test()
        self.lbl_provider_test.clear()
        self._update_provider_controls()
        self._show_provider_usage_idle()
        self._notify_change()

    def _provider_fallback_changed(self, *args):
        self._cancel_provider_test()
        self.lbl_provider_test.clear()
        self._update_provider_controls()
        self._notify_change()

    def _provider_input_changed(self, *args):
        self._cancel_provider_test()
        self.lbl_provider_test.clear()
        self._update_provider_controls()
        self._notify_change()

    def _cancel_provider_test(self):
        if self._provider_test_running:
            self._provider_test_token += 1
            self._provider_test_running = False
            self._update_provider_controls()

    def _update_provider_controls(self):
        provider_mode = self._selected_provider_mode()
        if provider_mode == TRANSLATION_PROVIDER_DISABLED:
            if self.chk_provider_fallback.isChecked():
                self.chk_provider_fallback.setChecked(False)
            self.chk_provider_fallback.setText(
                "Provider fallback unavailable while translation is disabled"
            )
            self.chk_provider_fallback.setEnabled(False)
        elif provider_mode == TRANSLATION_PROVIDER_GOOGLE:
            self.chk_provider_fallback.setText(
                "Fallback to DeepL after Google fails"
            )
            self.chk_provider_fallback.setEnabled(True)
        else:
            self.chk_provider_fallback.setText(
                "Fallback to Google after DeepL fails"
            )
            self.chk_provider_fallback.setEnabled(True)

        fallback_enabled = self.chk_provider_fallback.isChecked()
        uses_deepl = (
            provider_mode == TRANSLATION_PROVIDER_DEEPL
            or (
                provider_mode == TRANSLATION_PROVIDER_GOOGLE
                and fallback_enabled
            )
        )
        if not uses_deepl and self.chk_reveal_deepl.isChecked():
            # Never leave a stored DeepL key visible after switching providers.
            self.chk_reveal_deepl.setChecked(False)
        self.edit_deepl.setEnabled(uses_deepl)
        self.chk_reveal_deepl.setEnabled(uses_deepl)
        self.btn_test_provider.setEnabled(
            provider_mode != TRANSLATION_PROVIDER_DISABLED
            and not self._provider_test_running
        )
        self.btn_refresh_provider_usage.setEnabled(
            provider_mode != TRANSLATION_PROVIDER_DISABLED
        )

        if provider_mode == TRANSLATION_PROVIDER_DISABLED:
            hint = "Translation is disabled; no chat text is sent to a cloud provider."
            placeholder = "Select DeepL to configure an API key"
        elif provider_mode == TRANSLATION_PROVIDER_GOOGLE:
            hint = (
                "Google translation sends message text to a cloud service. "
                "The provider test sends only a built-in test phrase."
            )
            placeholder = (
                "Required for DeepL fallback"
                if fallback_enabled
                else "Stored for DeepL; not used by Google"
            )
        else:
            hint = (
                "DeepL translation sends message text to DeepL. "
                "The provider test sends only a built-in test phrase."
            )
            placeholder = "Required for DeepL"

        self.lbl_provider_hint.setText(hint)
        self.edit_deepl.setPlaceholderText(placeholder)
        validation_error = self._provider_validation_error()
        self.lbl_provider_validation.setText(validation_error)
        self.btn_save.setEnabled(not validation_error)
        if validation_error:
            self.btn_test_provider.setEnabled(False)

    def _provider_validation_error(self):
        provider_mode = self._selected_provider_mode()
        target_lang = self.edit_target.text().strip() or 'en'
        if (
            provider_mode != TRANSLATION_PROVIDER_DISABLED
            and not LANGUAGE_CODE_PATTERN.fullmatch(target_lang)
        ):
            return "Enter a target language such as en, de, zh, or pt-BR."
        if (
            (
                provider_mode == TRANSLATION_PROVIDER_DEEPL
                or (
                    provider_mode == TRANSLATION_PROVIDER_GOOGLE
                    and self.chk_provider_fallback.isChecked()
                )
            )
            and not self.edit_deepl.text().strip()
        ):
            if provider_mode == TRANSLATION_PROVIDER_GOOGLE:
                return "A DeepL API key is required for Google fallback."
            return "A DeepL API key is required before saving DeepL."
        return ""

    def _show_provider_usage_idle(self):
        provider_mode = self._selected_provider_mode()
        self.lbl_provider_usage.setStyleSheet("color: gray;")
        if provider_mode == TRANSLATION_PROVIDER_DISABLED:
            message = "Usage unavailable while translation is disabled."
        elif provider_mode == TRANSLATION_PROVIDER_GOOGLE:
            message = (
                "Local runtime estimate not refreshed. Google provides no "
                "provider usage limit."
            )
        else:
            message = "DeepL usage not refreshed."
        self.lbl_provider_usage.setText(message)

    def _request_provider_usage(self):
        if self._selected_provider_mode() == TRANSLATION_PROVIDER_DISABLED:
            self._show_provider_usage_idle()
            return
        self.lbl_provider_usage.setStyleSheet("color: gray;")
        self.lbl_provider_usage.setText("Refreshing provider usage...")
        self.provider_usage_requested.emit()

    @staticmethod
    def _usage_count(value):
        if isinstance(value, bool):
            return None
        try:
            count = int(value)
        except (TypeError, ValueError, OverflowError):
            return None
        return count if count >= 0 else None

    @Slot(dict)
    def set_provider_usage(self, usage):
        """Render an asynchronous provider-usage snapshot safely.

        DeepL accepts ``used``/``limit`` (or its SDK-style
        ``character_count``/``character_limit`` aliases). Google accepts
        ``estimated_characters`` and is always labelled as a local estimate.
        """
        if not isinstance(usage, dict):
            usage = {}

        selected_provider = self._selected_provider_mode()
        provider = usage.get('provider', selected_provider)
        if provider not in {
            TRANSLATION_PROVIDER_DISABLED,
            TRANSLATION_PROVIDER_GOOGLE,
            TRANSLATION_PROVIDER_DEEPL,
        }:
            provider = selected_provider

        # Do not let a slow response overwrite the status for a newly selected
        # provider.
        if provider != selected_provider:
            return

        if usage.get('error') or usage.get('unavailable') or usage.get('available') is False:
            self.lbl_provider_usage.setStyleSheet("color: #c0392b;")
            self.lbl_provider_usage.setText(
                "Provider usage is unavailable. Check the connection and try again."
            )
            return

        if provider == TRANSLATION_PROVIDER_DEEPL:
            used = self._usage_count(
                usage.get('used', usage.get('character_count'))
            )
            limit = self._usage_count(
                usage.get('limit', usage.get('character_limit'))
            )
            if used is None or limit is None or limit == 0:
                self.lbl_provider_usage.setStyleSheet("color: #c0392b;")
                self.lbl_provider_usage.setText(
                    "DeepL usage is unavailable. Check the connection and try again."
                )
                return

            percent = used / limit * 100
            warning = percent >= 80
            message = (
                f"DeepL: {used:,} / {limit:,} characters used "
                f"({percent:.1f}%)."
            )
            if warning:
                message += " Warning: quota usage is at or above 80%."
            self.lbl_provider_usage.setStyleSheet(
                "color: #c0392b;" if warning else "color: #2e8b57;"
            )
            self.lbl_provider_usage.setText(message)
            return

        if provider == TRANSLATION_PROVIDER_GOOGLE:
            estimated = self._usage_count(
                usage.get(
                    'estimated_characters',
                    usage.get('character_count', usage.get('used')),
                )
            )
            if estimated is None:
                self.lbl_provider_usage.setStyleSheet("color: #c0392b;")
                self.lbl_provider_usage.setText(
                    "Google usage estimate is unavailable. Try again."
                )
                return
            self.lbl_provider_usage.setStyleSheet("color: gray;")
            self.lbl_provider_usage.setText(
                f"Google local runtime estimate: {estimated:,} characters. "
                "Estimate only; Google provides no provider usage limit."
            )
            return

        self._show_provider_usage_idle()

    def _test_provider(self):
        provider_mode = self._selected_provider_mode()
        api_key = self.edit_deepl.text().strip()
        provider_api_key = api_key if provider_mode == TRANSLATION_PROVIDER_DEEPL else ""
        target_lang = self.edit_target.text().strip() or 'en'

        if self._translation_service is None:
            self._translation_service = TranslationService()

        self._provider_test_token += 1
        token = self._provider_test_token
        self._provider_test_running = True
        self._update_provider_controls()
        self.lbl_provider_test.setStyleSheet("color: gray;")
        self.lbl_provider_test.setText("Testing...")

        def run_test():
            try:
                result = self._translation_service.test_provider(
                    provider_mode,
                    provider_api_key,
                    target_lang,
                )
            except Exception:
                # Provider libraries may include request metadata in exceptions;
                # pass no exception details back to the UI.
                result = None
            self._provider_test_signals.finished.emit(token, result, api_key)

        threading.Thread(
            target=run_test,
            name="provider-connection-test",
            daemon=True,
        ).start()
        QTimer.singleShot(
            PROVIDER_TEST_TIMEOUT_MS,
            lambda: self._provider_test_timed_out(token),
        )

    def _provider_test_finished(self, token, result, api_key):
        if token != self._provider_test_token or not self._provider_test_running:
            return

        self._provider_test_running = False
        if result is None:
            message = "Provider test failed unexpectedly. Check the configuration and try again."
            success = False
        else:
            message = str(result.message)
            success = bool(result.success)
        if api_key:
            message = message.replace(api_key, "<redacted>")

        color = "#2e8b57" if success else "#c0392b"
        self.lbl_provider_test.setStyleSheet(f"color: {color};")
        self.lbl_provider_test.setText(message)
        self._update_provider_controls()

    def _provider_test_timed_out(self, token):
        if token != self._provider_test_token or not self._provider_test_running:
            return

        self._provider_test_running = False
        self.lbl_provider_test.setStyleSheet("color: #c0392b;")
        self.lbl_provider_test.setText(
            "Provider test timed out. Check your connection and try again."
        )
        self._update_provider_controls()

    def _notify_change(self, *args):
        self.settings_changed.emit(self.get_settings())

    def _pick_color_shared(self, btn, key):
        curr = self.config['shared'].get(key, '#ffffff')
        c = QColorDialog.getColor(QColor(curr), self, "Pick Color")
        if c.isValid():
            h = c.name()
            self.config['shared'][key] = h
            btn.setStyleSheet(f"background-color: {h};")
            self._notify_change()
            
    def _pick_color_session(self, btn, session_id, key):
        curr = self.config['sessions'][session_id].get(key, '#000000')
        c = QColorDialog.getColor(QColor(curr), self, "Pick Color")
        if c.isValid():
            h = c.name()
            self.config['sessions'][session_id][key] = h
            btn.setStyleSheet(f"background-color: {h};")
            self._notify_change()

    def _reset_position(self, session_id):
        # Set specific negative coords to trigger default placement logic
        self.config['sessions'][session_id]['x'] = -1
        self.config['sessions'][session_id]['y'] = -1
        self.config['sessions'][session_id]['w'] = 600
        self.config['sessions'][session_id]['h'] = 400
        # Feedback? 
        lbl = QLabel("Position reset pending save.")
        self.sender().parent().layout().addWidget(lbl) # Hacky feedback

    def get_settings(self):
        # Collect values from widgets that aren't auto-updated

        # Shared
        self.config['shared']['opacity'] = self.slider_opacity.value() / 100.0
        self.config['shared']['font_size'] = self.spin_font.value()
        self.config['shared']['auto_scroll'] = self.chk_autoscroll.isChecked()
        self.config['shared']['target_language'] = self.edit_target.text().strip() or 'en'
        self.config['shared']['translation_provider'] = self._selected_provider_mode()
        self.config['shared']['provider_fallback_enabled'] = (
            self._selected_provider_mode() != TRANSLATION_PROVIDER_DISABLED
            and self.chk_provider_fallback.isChecked()
        )
        self.config['shared']['deepl_api_key'] = self.edit_deepl.text().strip()

        ign_raw = self.edit_ignored.text()
        self.config['shared']['ignored_languages'] = [x.strip() for x in ign_raw.split(',') if x.strip()]

        self.config['shared']['log_dir'] = self.edit_log_dir.text()

        # Fleet Chat Settings (Always available as Global Settings)
        self.config['shared']['fleet_inactive_threshold'] = self.spin_fleet_threshold.value() * 60
        self.config['shared']['fleet_auto_switch'] = self.chk_fleet_autoswitch.isChecked()
        self.config['shared']['fleet_scan_interval'] = self.spin_fleet_interval.value()
        self.config['shared']['fleet_history_lines'] = self.spin_fleet_history.value()
             
        # Save Performance Settings
        self.config['shared']['polling_interval'] = self.spin_poll.value()

        self.config['shared']['burst_optimization'] = (
            self.chk_burst_optimization.isChecked()
        )
        self.config['shared']['context_assist'] = self.chk_context_assist.isChecked()
        self.config['shared']['entity_protection'] = (
            self.chk_entity_protection.isChecked()
        )

        for session_id, controls in self._session_hud_controls.items():
            mode_combo, compact_header, command_cards = controls
            self.config['sessions'][session_id]['display_mode'] = (
                mode_combo.currentData() or DISPLAY_MODE_MIRROR
            )
            self.config['sessions'][session_id]['compact_header'] = (
                compact_header.isChecked()
            )
            self.config['sessions'][session_id]['command_cards'] = (
                command_cards.isChecked()
            )

        return self.config

    def closeEvent(self, event):
        self._cancel_provider_test()
        super().closeEvent(event)

    def done(self, result):
        # Save/Cancel hide the dialog without necessarily sending closeEvent.
        self._cancel_provider_test()
        super().done(result)

    def accept(self):
        self._update_provider_controls()
        if self._provider_validation_error():
            return
        super().accept()

    def _browse_log_dir(self):
        current = self.edit_log_dir.text()
        if not current:
            current = os.path.expanduser("~/Documents")
            
        d = QFileDialog.getExistingDirectory(self, "Select EVE Chatlogs Folder", current)
        if d:
            self.edit_log_dir.setText(d)
            self.config['shared']['log_dir'] = d
            self._notify_change()

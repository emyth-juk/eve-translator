from PySide6.QtWidgets import (QDialog, QVBoxLayout, QHBoxLayout, QLabel, 
                               QSlider, QSpinBox, QCheckBox, QPushButton, 
                               QColorDialog, QFormLayout, QGroupBox, QWidget, QTabWidget,
                               QFileDialog, QLineEdit)
import os
from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QColor
from src.version import __version__

class SettingsDialog(QDialog):
    # Signal carrying the updated config dict (full structure)
    settings_changed = Signal(dict)

    def __init__(self, full_config, parent=None):
        super().__init__(parent)
        self.setWindowTitle(f"Global Settings - v{__version__}")
        self.resize(400, 500)
        # Deep copy config to allow reversion/modification
        # But for list/dict we need deep copy.
        import copy
        self.config = copy.deepcopy(full_config)
        
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
        
        # Buttons
        btn_box = QHBoxLayout()
        btn_save = QPushButton("Save")
        btn_save.clicked.connect(self.accept)
        btn_cancel = QPushButton("Cancel")
        btn_cancel.clicked.connect(self.reject)
        
        # Optional: Reset Layouts button?
        
        btn_box.addWidget(btn_save)
        btn_box.addWidget(btn_cancel)
        self.main_layout.addLayout(btn_box)

    def _init_shared_tab(self):
        layout = QVBoxLayout(self.tab_shared)
        
        # Paths Group
        grp_paths = QGroupBox("Paths")
        form_paths = QFormLayout(grp_paths)
        
        self.edit_log_dir = QLineEdit(self.config['shared'].get('log_dir', ''))
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
        
        self.edit_target = self._make_line_edit(self.config['shared'].get('target_language', 'en'))
        form_beh.addRow("Target Lang (ISO):", self.edit_target)
        
        # Ignored
        ign = self.config['shared'].get('ignored_languages', [])
        ign_str = ", ".join(ign) if isinstance(ign, list) else str(ign)
        self.edit_ignored = self._make_line_edit(ign_str)
        form_beh.addRow("Ignored Langs:", self.edit_ignored)
        
        # API Key
        self.edit_deepl = self._make_line_edit(self.config['shared'].get('deepl_api_key', ''))
        self.edit_deepl.setPlaceholderText("Leave empty for Google")
        form_beh.addRow("DeepL API Key:", self.edit_deepl)
        
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

    def _make_color_btn(self, shared_key, initial_hex):
        btn = QPushButton()
        btn.setStyleSheet(f"background-color: {initial_hex};")
        btn.clicked.connect(lambda: self._pick_color_shared(btn, shared_key))
        return btn
        
    def _make_line_edit(self, text):
        from PySide6.QtWidgets import QLineEdit
        le = QLineEdit(text)
        return le

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
        self.config['shared']['target_language'] = self.edit_target.text().strip()
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
             
        return self.config

    def _browse_log_dir(self):
        current = self.edit_log_dir.text()
        if not current:
            current = os.path.expanduser("~/Documents")
            
        d = QFileDialog.getExistingDirectory(self, "Select EVE Chatlogs Folder", current)
        if d:
            self.edit_log_dir.setText(d)
            self.config['shared']['log_dir'] = d
            self._notify_change()

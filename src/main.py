import sys
import os
import logging
import logging.handlers
import glob
from pathlib import Path
from datetime import datetime
import json
import html
import traceback
from src.utils.paths import get_resource_path

# Setup Logging
def setup_logging():
    log_dir = Path.home() / ".eve_translator" / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    log_file = log_dir / "translator.log"

    logger = logging.getLogger()
    logger.setLevel(logging.INFO)

    # File Handler (Rotating)
    file_handler = logging.handlers.RotatingFileHandler(
        log_file, maxBytes=1024*1024, backupCount=5, encoding='utf-8'
    )
    file_formatter = logging.Formatter('%(asctime)s - %(name)s - %(levelname)s - %(message)s')
    file_handler.setFormatter(file_formatter)
    logger.addHandler(file_handler)

    # Console Handler
    console_handler = logging.StreamHandler()
    console_formatter = logging.Formatter('%(levelname)s: %(message)s')
    console_handler.setFormatter(console_formatter)
    logger.addHandler(console_handler)

    logging.info(f"Logging initialized. Log file: {log_file}")

# setup_logging() # Moved to __main__ block to prevent side effects on import
logger = logging.getLogger(__name__)

from PySide6.QtWidgets import QApplication, QSystemTrayIcon, QMenu
from PySide6.QtCore import QObject, Signal, Slot, QThread, Qt, QTimer
from PySide6.QtGui import QIcon, QAction

from src.core.session import ChatSession
from src.core.parser import LineParser
from src.core.tokenizer import EVELinkTokenizer
from src.core.detector import LanguageDetector
from src.services.translator import TranslationService, GoogleTransProvider, DeepLProvider
from src.services.local_detector import LocalChatDetector
from src.gui.settings import SettingsDialog
from src.version import __version__


from src.core.character_info import CharacterInfo
from src.core.fleet_info import FleetInfo
from typing import Dict, Optional

# Configuration
# Configuration
# LOG_DIR is now dynamic

class WorkerSignals(QObject):
    # session_id, text, sender, timestamp, original_text, is_translated
    message_ready = Signal(str, str, str, str, str, bool)

class LogProcessingWorker(QObject):
    """
    Shared worker that processes lines from ALL sessions.
    Receives session_id with each batch to route output correctly.
    """
    def __init__(self, parser, tokenizer, detector, translator_service):
        super().__init__()
        self.parser = parser
        self.tokenizer = tokenizer
        self.detector = detector
        self.translator_service = translator_service
        self.signals = WorkerSignals()
        self.config = {'ignored_languages': ['en'], 'target_language': 'en'}

    @Slot(dict)
    def update_config(self, config):
        """Update shared configuration (target lang, API keys, etc.)."""
        self.config = config.copy()

        # Update Translator Config (Glossary reloading)
        self.translator_service.set_config(self.config)

        # Switch translator provider if needed
        deepl_key = self.config.get('deepl_api_key', '').strip()
        current_provider = self.translator_service.provider

        if deepl_key:
            if not isinstance(current_provider, DeepLProvider):
                print("Switching to DeepL Provider")
                self.translator_service.provider = DeepLProvider(deepl_key)
        else:
            if not isinstance(current_provider, GoogleTransProvider):
                print("Switching to Google Provider")
                self.translator_service.provider = GoogleTransProvider()

    @Slot(str, list)
    def process_lines(self, session_id: str, lines: list):
        """
        Process batch of lines from a specific session.
        """
        for line in lines:
            try:
                self._process_single_line(session_id, line)
            except Exception as e:
                print(f"[{session_id}] Error processing line: {e}")

    def _process_single_line(self, session_id: str, line: str):
        """Process single line and emit result with session_id."""
        msg = self.parser.parse(line, 0)
        if not msg:
            return

        # Tokenize
        tokenized = self.tokenizer.tokenize(msg.message)

        # Detect
        ignored = set(self.config.get('ignored_languages', ['en']))
        should_translate, detected_lang = self.detector.should_translate(
            tokenized.cleaned, ignored_langs=ignored
        )

        timestamp_str = msg.timestamp.strftime("%H:%M:%S")

        if not should_translate:
            final_text = self._restore_with_highlight(tokenized.cleaned, tokenized.tokens)
            self.signals.message_ready.emit(
                session_id, final_text, msg.sender,
                timestamp_str, "", False
            )
            return

        # Translate
        target_lang = self.config.get('target_language', 'en')
        translated_text, success, provider = self.translator_service.translate_message(
            tokenized.cleaned, target_lang, source_lang=detected_lang
        )
        
        # Log
        status = "Success" if success else "Failed"
        print(f"[{session_id.upper()}] [{provider}] {status}: '{tokenized.cleaned}' -> '{translated_text}'")

        # Restore
        final_text = self._restore_with_highlight(translated_text, tokenized.tokens)
        final_original = self._restore_with_highlight(tokenized.cleaned, tokenized.tokens)

        # Emit
        self.signals.message_ready.emit(
            session_id, final_text, msg.sender,
            timestamp_str, final_original, True
        )

    def _restore_with_highlight(self, text, tokens):
        safe_text = html.escape(text)
        for placeholder, original in tokens.items():
            safe_original = html.escape(original)
            highlighted = f"<span style='color: yellow;'>{safe_original}</span>"
            safe_text = safe_text.replace(placeholder, highlighted)
        return safe_text


class TranslatorManager(QObject):
    """
    Main application manager.
    Coordinates multiple chat sessions and shared processing pipeline.
    """

    def __init__(self):
        super().__init__()

        # Qt Application
        self.app = QApplication(sys.argv)
        # Prevent app from quitting when last window closed (since we use tray)
        self.app.setQuitOnLastWindowClosed(False)
        # Set App Icon
        # In Dev: src/assets/icon.png relative to root
        # In Frozen: src/assets/icon.png relative to _MEIPASS
        icon_path = get_resource_path(os.path.join('src', 'assets', 'icon.png'))
        if os.path.exists(icon_path):
            self.app.setWindowIcon(QIcon(icon_path))
        else:
            logger.warning(f"Icon not found at {icon_path}")

        # Configuration
        self.config = self._load_config()

        # Shared Processing Components
        parser = LineParser()
        tokenizer = EVELinkTokenizer()
        detector = LanguageDetector()
        translator_service = TranslationService(provider=GoogleTransProvider())

        # Shared Worker Thread
        self.thread = QThread()
        self.worker = LogProcessingWorker(parser, tokenizer, detector, translator_service)
        self.worker.moveToThread(self.thread)
        self.worker.signals.message_ready.connect(self._route_message)
        self.thread.start()

        # Initial config sync
        self.worker.update_config(self.config['shared'])

        # Sessions
        self.sessions = {}

        # Character tracking (for Local chat)
        self.character_registry: Dict[str, CharacterInfo] = {}
        # Try to load selected character from config
        self.selected_character_id: Optional[str] = self.config['sessions']['local'].get('character_id')

        # Fleet tracking
        self.fleet_registry: Dict[str, FleetInfo] = {}
        # Try to load selected fleet from config
        self.selected_fleet_id: Optional[str] = self.config['sessions']['fleet'].get('fleet_id')

        # Periodic scanner timer (check every X seconds for characters and fleets)
        # Scan interval is configurable, defaulting to 10s as per user request (legacy default was 30s)
        self.scanner_timer = QTimer()
        self.scanner_timer.timeout.connect(self._periodic_scan)
        
        scan_interval_ms = self.config['shared'].get('fleet_scan_interval', 10) * 1000
        self.scanner_timer.start(scan_interval_ms)

        # Initial scan
        self._periodic_scan()

        # System Tray
        self._setup_system_tray()

        # Validate Log Directory FIRST (before initializing sessions)
        self._ensure_log_directory()

        # Initialize sessions (after log directory is confirmed valid)
        self._initialize_sessions()

        logger.info("Translator Manager started. Check System Tray.")

    def _get_configured_log_dir(self):
        return self.config['shared'].get('log_dir', '')

    def _ensure_log_directory(self):
        """Check if log dir is valid, if not, ask user."""
        current_path = self.config['shared'].get('log_dir', '')
        
        # Try default if empty
        if not current_path:
            default_path = os.path.expanduser("~/Documents/EVE/logs/Chatlogs")
            if os.path.exists(default_path):
                current_path = default_path
        
        # Validate existence
        if not current_path or not os.path.exists(current_path):
            from PySide6.QtWidgets import QFileDialog, QMessageBox
            
            # Show message
            msg = QMessageBox()
            msg.setWindowTitle("EVE Translator - Setup")
            msg.setText("EVE Online Chat Logs folder not found.\nPlease select the folder where EVE Online saves chat logs.")
            msg.setInformativeText("Usually: Documents\\EVE\\logs\\Chatlogs")
            msg.exec()
            
            # Ask user
            while True:
                selected_dir = QFileDialog.getExistingDirectory(None, "Select EVE Chatlogs Folder", os.path.expanduser("~/Documents"))
                if selected_dir:
                    current_path = selected_dir
                    break
                else:
                    # Cancelled?
                    ret = QMessageBox.question(None, "Exit?", "Log folder is required. Exit application?", QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No)
                    if ret == QMessageBox.StandardButton.Yes:
                        sys.exit(0)
                        
        # Save validated path
        self.config['shared']['log_dir'] = current_path
        self._save_config()
        logger.info(f"Using Log Directory: {current_path}")

    def _load_config(self) -> dict:
        config_path = Path.cwd() / 'translator_config.json'
        
        # Legacy migration check
        legacy_path = Path.cwd() / 'overlay_config.json'
        legacy_data = {}
        if legacy_path.exists():
             try:
                 with open(legacy_path, 'r') as f:
                     legacy_data = json.load(f)
             except: pass

        default_config = {
            'shared': {
                'opacity': legacy_data.get('opacity', 0.8),
                'font_size': legacy_data.get('font_size', 10),
                'auto_scroll': legacy_data.get('auto_scroll', True),
                'ignored_languages': legacy_data.get('ignored_languages', ['en']),
                'target_language': legacy_data.get('target_language', 'en'),
                'deepl_api_key': legacy_data.get('deepl_api_key', ''),
                'color_default': legacy_data.get('color_default', '#e0e0e0'),
                'color_translated': legacy_data.get('color_translated', '#00ffff'),
                'color_highlight': legacy_data.get('color_highlight', 'yellow'),
                'log_dir': os.path.expanduser("~/Documents/EVE/logs/Chatlogs"),
                
                # Fleet chat settings
                'fleet_inactive_threshold': 1800,  # seconds (30 minutes)
                'fleet_auto_switch': True,  # Auto-switch when current fleet becomes inactive
                'fleet_scan_interval': 10,  # seconds
                'fleet_history_lines': 5,   # Number of past messages to load on start
                'polling_interval': 1.0     # Log check frequency (seconds)
            },
            'sessions': {
                'fleet': {
                    'enabled': True,
                    'x': legacy_data.get('x', 100),
                    'y': legacy_data.get('y', 100),
                    'w': legacy_data.get('w', 300),
                    'h': legacy_data.get('h', 200),
                    'background_color': '#33001a',
                    'title_prefix': '[FLEET]'
                },
                'local': {
                    'enabled': False,
                    'x': 410, 'y': 100, 'w': 300, 'h': 200,
                    'background_color': '#001a33',
                    'title_prefix': '[LOCAL]'
                }
            }
        }

        if config_path.exists():
            try:
                with open(config_path, 'r') as f:
                    loaded = json.load(f)
                    # Merge
                    if 'shared' in loaded:
                        default_config['shared'].update(loaded['shared'])
                    if 'sessions' in loaded:
                         for s in ['fleet', 'local']:
                             if s in loaded['sessions']:
                                 default_config['sessions'][s].update(loaded['sessions'][s])
            except Exception as e:
                logger.error(f"Error loading config: {e}")

        return default_config

    def _save_config(self):
        config_path = Path.cwd() / 'translator_config.json'
        # Update session configs
        for session_id, session in self.sessions.items():
            if session:
                current_overlay_config = session.get_config()
                # Only update session-specific properties.
                # Remove Shared Keys (handled by Shared config)
                # 'enabled' MUST be allowed so it persists to JSON.
                excluded_keys = {'opacity', 'font_size', 'auto_scroll',
                                'ignored_languages', 'target_language', 'deepl_api_key',
                                'color_default', 'color_translated', 'color_highlight', 'log_dir',
                                'fleet_inactive_threshold', 'fleet_auto_switch', 'fleet_scan_interval',
                                'fleet_history_lines', 'polling_interval'}
                
                filtered_config = {k: v for k, v in current_overlay_config.items() if k not in excluded_keys}
                
                # Update with filtered config
                self.config['sessions'][session_id].update(filtered_config)
                
                # Explicitly cleanup any shared keys that might exist in the session config (legacy/dirty state)
                for key in excluded_keys:
                    if key in self.config['sessions'][session_id]:
                        del self.config['sessions'][session_id][key]
        
        try:
            with open(config_path, 'w') as f:
                json.dump(self.config, f, indent=2)
        except Exception as e:
            logger.error(f"Error saving config: {e}")

    def _setup_system_tray(self):
        self.tray_icon = QSystemTrayIcon(self.app)
        # Try to use application icon
        icon_path = get_resource_path(os.path.join('src', 'assets', 'icon.png'))
        if os.path.exists(icon_path):
            self.tray_icon.setIcon(QIcon(icon_path))
        else:
            # Fallback: Create a simple pixmap
            from PySide6.QtGui import QPixmap, QPainter, QColor
            pixmap = QPixmap(16, 16)
            pixmap.fill(Qt.GlobalColor.transparent)
            painter = QPainter(pixmap)
            painter.setBrush(QColor("cyan"))
            painter.setPen(Qt.PenStyle.NoPen)
            painter.drawEllipse(1, 1, 14, 14)
            painter.end()
            self.tray_icon.setIcon(QIcon(pixmap))
        
        self.tray_menu = QMenu()

        self.fleet_action = QAction("Fleet Chat", self.tray_menu, checkable=True)
        self.fleet_action.setChecked(self.config['sessions']['fleet']['enabled'])
        self.fleet_action.triggered.connect(lambda: self.toggle_session('fleet'))
        self.tray_menu.addAction(self.fleet_action)

        self.local_action = QAction("Local Chat", self.tray_menu, checkable=True)
        self.local_action.setChecked(self.config['sessions']['local']['enabled'])
        self.local_action.triggered.connect(lambda: self.toggle_session('local'))
        self.tray_menu.addAction(self.local_action)

        self.tray_menu.addSeparator()
        exit_action = QAction("Exit", self.tray_menu)
        exit_action.triggered.connect(self.shutdown)
        self.tray_menu.addAction(exit_action)

        self.tray_icon.setContextMenu(self.tray_menu)
        self.tray_icon.show()

    def _initialize_sessions(self):
        # Start enabled sessions
        if self.config['sessions']['fleet']['enabled']:
            self.start_session('fleet')
        if self.config['sessions']['local']['enabled']:
            self.start_session('local')
        
        # Initial broadcast of state to any active sessions (including those just started)
        self._broadcast_session_states()

    def start_session(self, session_id):
        if session_id in self.sessions and self.sessions[session_id]:
             return # Already running

        # FIX: Set enabled=True IMMEDIATELY (before log check)
        # This captures user intent even if log file is temporarily unavailable
        self.config['sessions'][session_id]['enabled'] = True
        self._update_tray_menu()

        log_path = self._get_log_path(session_id)
        if not log_path:
            logger.warning(f"[{session_id}] No log file found - will retry on next startup")
            # enabled is already True, so user's preference is saved
            return

        session_config = self._build_session_config(session_id)
        session = ChatSession(session_id, log_path, session_config, parent=self)
        
        session.lines_ready.connect(self.worker.process_lines)
        session.request_toggle.connect(self.toggle_session) # Handle context menu requests
        session.request_settings.connect(self.open_settings_dialog)
        session.config_changed.connect(self._handle_session_config_change)
        session.character_selected.connect(self.switch_local_character)

        # Connect fleet selection signal (for fleet sessions)
        if hasattr(session.overlay, 'fleet_selected'):
            session.overlay.fleet_selected.connect(self.switch_fleet)

        self.sessions[session_id] = session
        session.start()
        
        # enabled already set above
        # self.config['sessions'][session_id]['enabled'] = True 
        # self._update_tray_menu() 
        self._broadcast_session_states()
        self._broadcast_character_list()

    def open_settings_dialog(self):
        """Open the global settings dialog."""
        try:
            # Backup current config for restore on Cancel
            import copy
            original_config = copy.deepcopy(self.config)

            dlg = SettingsDialog(self.config, parent=None)
            
            # Connect live preview
            dlg.settings_changed.connect(self.preview_settings)

            if dlg.exec():
                # Saved: Final update (redundant if previewed, but safe)
                new_config = dlg.get_settings()
                self.config.update(new_config)
                
                logger.info(f"[Settings] New Shared Config: {self.config['shared']}")
                
                # Apply changes to worker (Shared)
                self.worker.update_config(self.config['shared'])
                
                # Apply changes to running sessions
                self._apply_config_update()
                
                self._save_config()
            else:
                # Cancelled: Restore original config
                logger.info("Settings cancelled. Restoring original config.")
                self.config = original_config
                # Re-apply original config to revert any previews
                self.worker.update_config(self.config['shared'])
                self._apply_config_update()

        except Exception as e:
            logger.error(f"Error opening settings: {e}")
            import traceback
            traceback.print_exc()

    @Slot(dict)
    def preview_settings(self, new_config):
        """Apply temporary settings for live preview."""
        self.config.update(new_config)
        # Apply visual changes to sessions immediately
        self._apply_config_update()

    def _apply_config_update(self):
        """Propagate config changes to all active sessions."""
        for session_id, session in self.sessions.items():
            if session:
                # Re-build session config (merges shared + specific)
                new_session_config = self._build_session_config(session_id)
                logger.info(f"[{session_id}] Applying Config Update. Font: {new_session_config.get('font_size')}, Opacity: {new_session_config.get('opacity')}")
                session.update_config(new_session_config)

    def stop_session(self, session_id):
        if session_id not in self.sessions or not self.sessions[session_id]:
            return
            
        session = self.sessions[session_id]
        session.lines_ready.disconnect(self.worker.process_lines)
        session.stop()
        
        self.sessions[session_id] = None
        self.config['sessions'][session_id]['enabled'] = False
        self._update_tray_menu()
        self._broadcast_session_states()

    def toggle_session(self, session_id):
        # We need to toggle based on current STATE, not the checked signal from tray (which sends bool).
        # We ignore the bool and just check if we have a session.
        if session_id in self.sessions and self.sessions[session_id]:
            self.stop_session(session_id)
        else:
            self.start_session(session_id)
        
        # Save config immediately so it persists
        self._save_config()

    def _handle_session_config_change(self, session_id: str, updated_config: dict):
        """
        Handle config changes from a session's overlay.
        Updates shared config and notifies worker.
        """
        # Extract shared settings (not position/size which are per-session)
        shared_keys = [
            'opacity', 'font_size', 'auto_scroll',
            'ignored_languages', 'target_language', 'deepl_api_key',
            'color_default', 'color_translated', 'color_highlight', 'log_dir',
            'fleet_inactive_threshold', 'fleet_auto_switch', 'fleet_scan_interval',
            'fleet_history_lines',
            'polling_interval'
        ]

        for key in shared_keys:
            if key in updated_config:
                self.config['shared'][key] = updated_config[key]

        # Update session-specific (position, background)
        session_keys = ['x', 'y', 'w', 'h', 'background_color']
        for key in session_keys:
            if key in updated_config:
                self.config['sessions'][session_id][key] = updated_config[key]

        # Notify worker of shared config changes
        self.worker.update_config(self.config['shared'])

        # Save to disk
        self._save_config()

    def _broadcast_session_states(self):
        """Notify all active sessions of the current global enabled states."""
        states = self.config['sessions']
        for sid, session in self.sessions.items():
            if session:
                session.update_session_states(states)

    def _update_tray_menu(self):
        self.fleet_action.setChecked(self.config['sessions']['fleet'].get('enabled', False))
        self.local_action.setChecked(self.config['sessions']['local'].get('enabled', False))

    def _get_log_path(self, session_id):
        if session_id == 'fleet':
            return self._find_latest_fleet_log()
        elif session_id == 'local':
            return self._find_latest_local_log()
        return None

    def _find_latest_fleet_log(self):
        """
        Get the fleet log path to use.
        Priority: 1) Selected fleet from config, 2) Most recent active fleet
        """
        log_dir = self._get_configured_log_dir()
        if not log_dir or not os.path.exists(log_dir):
            return None

        # Try selected fleet first
        if self.selected_fleet_id and self.selected_fleet_id in self.fleet_registry:
            fleet_info = self.fleet_registry[self.selected_fleet_id]
            logger.info(f"[FLEET] Using selected fleet: {fleet_info.listener_name}")
            return fleet_info.log_path

        # No selected fleet or it's not active - find most recent
        if self.fleet_registry:
            from src.services.fleet_detector import FleetDetector
            detector = FleetDetector()
            most_recent = detector.get_most_recent_fleet(self.fleet_registry)
            if most_recent:
                logger.info(f"[FLEET] Auto-selecting most recent fleet: {most_recent.listener_name}")
                self.selected_fleet_id = most_recent.fleet_id
                self.config['sessions']['fleet']['fleet_id'] = most_recent.fleet_id
                return most_recent.log_path

        # Fallback to old behavior (scan directory directly)
        pattern = os.path.join(log_dir, "Fleet_*.txt")
        files = glob.glob(pattern)
        if not files:
            return None
        return max(files, key=os.path.getmtime)

    def _find_latest_local_log(self):
        """
        Get log path for Local session.
        Uses selected character if set, otherwise most recent.
        """
        log_dir = self._get_configured_log_dir()
        if not log_dir or not os.path.exists(log_dir):
            return None
            
        detector = LocalChatDetector()

        # If character selected, get their latest log
        if self.selected_character_id:
            logger.info(f"[LOCAL] Searching log for selected CharID: {self.selected_character_id}")
            log_path = detector.get_latest_log_for_character(log_dir, self.selected_character_id)

            if log_path:
                # Update character info
                if self.selected_character_id in self.character_registry:
                    char_info = self.character_registry[self.selected_character_id]
                    logger.info(f"[LOCAL] Tracking: {char_info.character_name}")
                    
                    # Store in config for overlay
                    self.config['sessions']['local']['character_name'] = char_info.character_name
                    self.config['sessions']['local']['character_id'] = char_info.character_id
                    self.config['sessions']['local']['system_name'] = char_info.system_name

                return log_path
            else:
                logger.warning(f"[LOCAL] No log found for selected character ID: {self.selected_character_id}")
                # Fall through to default behavior

        # Default: most recent log (existing behavior)
        log_path = detector.get_most_recent_local(log_dir)

        if log_path:
            # Try to determine character from filename
            path_obj = Path(log_path)
            char_id = detector.parse_character_id_from_filename(path_obj.name)
            
            # If we found an ID and it's in our registry, auto-select if none selected
            if char_id and char_id in self.character_registry:
                char_info = self.character_registry[char_id]
                logger.info(f"[LOCAL] Auto-selected: {char_info.character_name}")

                if not self.selected_character_id:
                    self.selected_character_id = char_id
                    self.config['sessions']['local']['character_id'] = char_id
                    self.config['sessions']['local']['character_name'] = char_info.character_name
                    self.config['sessions']['local']['system_name'] = char_info.system_name

        return log_path

    def _periodic_scan(self):
        """
        Periodic scanner that runs every 30 seconds.
        Scans for both active characters (Local) and active fleets (Fleet).
        """
        self._scan_characters()
        self._scan_fleets()

    def _scan_fleets(self):
        """
        Periodically scan for active fleet chat logs.
        Updates fleet registry and handles:
        - Initial auto-selection (when no fleet selected)
        - Auto-switch when current fleet becomes inactive (if enabled)
        - Does NOT auto-switch when newer fleets appear
        """
        log_dir = self._get_configured_log_dir()
        if not log_dir or not os.path.exists(log_dir):
            return

        from src.services.fleet_detector import FleetDetector
        detector = FleetDetector()

        # Get user-configured threshold
        threshold = self.config['shared'].get('fleet_inactive_threshold', 1800)
        new_registry = detector.scan_active_fleets(log_dir, active_threshold_seconds=threshold)

        # Get auto-switch setting
        auto_switch_enabled = self.config['shared'].get('fleet_auto_switch', True)

        # Check if selected fleet's log changed or became inactive
        if self.selected_fleet_id:
            if self.selected_fleet_id in new_registry:
                # Current fleet is still active - keep it (don't switch to newer fleets)
                fleet_info = new_registry[self.selected_fleet_id]
                old_info = self.fleet_registry.get(self.selected_fleet_id)

                # Log file changed? (shouldn't normally happen for fleet, but handle it)
                if old_info and fleet_info.log_mtime > old_info.log_mtime + 1:
                    logger.info(f"[FLEET] Detected change in fleet log: {fleet_info.listener_name}")
                    # Fleet log updated, session will automatically pick up new lines

                # BACK-TO-BACK FLEET CHECK:
                # If the same character starts a NEW fleet, switch to it even if the old log is technically still "active" (within 30m).
                if auto_switch_enabled:
                    most_recent = detector.get_most_recent_fleet(new_registry)
                    if most_recent and most_recent.fleet_id != self.selected_fleet_id:
                        # Check if it's the SAME character but a NEWER file
                        if most_recent.listener_name == fleet_info.listener_name and most_recent.created_time > fleet_info.created_time:
                            logger.info(f"[FLEET] Detected newer fleet log for same character. Switching: {most_recent.fleet_id}")
                            self.switch_fleet(most_recent.fleet_id)

            else:
                # Selected fleet is no longer active
                logger.warning(f"[FLEET] Selected fleet log is no longer active")

                if auto_switch_enabled and new_registry:
                    # Auto-switch to most recent active fleet
                    most_recent = detector.get_most_recent_fleet(new_registry)
                    if most_recent and most_recent.fleet_id != self.selected_fleet_id:
                        logger.info(f"[FLEET] Auto-switching to most recent active fleet: {most_recent.listener_name}")
                        self.switch_fleet(most_recent.fleet_id)
                else:
                    # Auto-switch disabled or no fleets available
                    logger.info(f"[FLEET] Current fleet inactive. Auto-switch disabled - user must manually select a fleet.")
                    self.selected_fleet_id = None
        else:
            # No fleet selected - initial auto-selection
            if new_registry:
                # Check if fleet session is enabled and running
                if self.config['sessions']['fleet'].get('enabled') and 'fleet' in self.sessions and self.sessions['fleet']:
                    # Session is running but no fleet selected - select most recent
                    most_recent = detector.get_most_recent_fleet(new_registry)
                    if most_recent:
                        logger.info(f"[FLEET] Initial auto-selection: {most_recent.listener_name}")
                        # FIX: Actually switch the session to this fleet!
                        # Previously we only set the property but didn't tell the session to update.
                        self.switch_fleet(most_recent.fleet_id)

        # Update registry
        self.fleet_registry = new_registry

        # Broadcast to overlays (for context menu)
        self._broadcast_fleet_list()

    def _scan_characters(self):
        """
        Periodically scan for active EVE characters.
        Updates character registry and handles:
        - New characters logging in
        - Characters logging out (stale logs)
        - Log file rotations (client restarts)
        """
        log_dir = self._get_configured_log_dir()
        if not log_dir or not os.path.exists(log_dir):
            return

        detector = LocalChatDetector()
        new_registry = detector.scan_active_characters(log_dir)

        # Check if selected character's log changed
        if self.selected_character_id:
            if self.selected_character_id in new_registry:
                char_info = new_registry[self.selected_character_id]
                old_info = self.character_registry.get(self.selected_character_id)

                # Log file changed (client restarted)?
                if old_info and char_info.latest_log_path != old_info.latest_log_path:
                    logger.info(f"[LOCAL] Detected new log file for {char_info.character_name}")
                    # Restart Local session with new log
                    self._switch_local_character(self.selected_character_id)

                # System changed?
                if old_info and char_info.system_name != old_info.system_name:
                    logger.info(f"[LOCAL] System changed: {old_info.system_name} -> {char_info.system_name}")
                    # Update config
                    self.config['sessions']['local']['system_name'] = char_info.system_name
                    # Update overlay title
                    self._update_local_overlay_title()

        # Update registry
        self.character_registry = new_registry

        # Broadcast to overlays (for context menu)
        self._broadcast_character_list()

    def switch_local_character(self, char_id: str):
        """
        Switch Local chat tracking to different character.
        Args:
            char_id: Target character ID
        """
        if char_id not in self.character_registry:
            logger.warning(f"[LOCAL] Character ID not found: {char_id}")
            return

        logger.info(f"[LOCAL] Switching to character: {self.character_registry[char_id].character_name}")

        # Update selection
        self.selected_character_id = char_id
        self.config['sessions']['local']['character_id'] = char_id
        self.config['sessions']['local']['character_name'] = self.character_registry[char_id].character_name
        self.config['sessions']['local']['system_name'] = self.character_registry[char_id].system_name

        # Restart Local session
        self._switch_local_character(char_id)

    def _switch_local_character(self, char_id: str):
        """Internal: Restart Local session with new character."""
        # Stop current session
        if 'local' in self.sessions and self.sessions['local']:
            self.stop_session('local')

        # Start with new log
        self.start_session('local')

        # Update overlay title
        self._update_local_overlay_title()

        # Save config
        self._save_config()

    def _update_local_overlay_title(self):
        """Update Local overlay window title with character and system info."""
        if 'local' not in self.sessions or not self.sessions['local']:
            return

        char_name = self.config['sessions']['local'].get('character_name', 'Unknown')
        system_name = self.config['sessions']['local'].get('system_name', '')

        title = f"[LOCAL] {char_name}"
        if system_name:
            title += f" - {system_name}"

        # Update overlay title
        self.sessions['local'].overlay.setWindowTitle(title)

    def switch_fleet(self, fleet_id: str):
        """
        Switch Fleet chat tracking to different fleet log.
        Args:
            fleet_id: Target fleet ID (log path)
        """
        if fleet_id not in self.fleet_registry:
            logger.warning(f"[FLEET] Fleet ID not found: {fleet_id}")
            return

        fleet_info = self.fleet_registry[fleet_id]
        logger.info(f"[FLEET] Switching to fleet: {fleet_info.listener_name}")

        # Update selection
        self.selected_fleet_id = fleet_id
        self.config['sessions']['fleet']['fleet_id'] = fleet_id

        # Restart Fleet session with new log (using switch_fleet_log in session)
        if 'fleet' in self.sessions and self.sessions['fleet']:
            self.sessions['fleet'].switch_fleet_log(fleet_info.log_path, fleet_info.listener_name)

        # Update overlay title
        self._update_fleet_overlay_title()

        # Save config
        self._save_config()

    def _update_fleet_overlay_title(self):
        """Update Fleet overlay window title with listener info."""
        if 'fleet' not in self.sessions or not self.sessions['fleet']:
            return

        if self.selected_fleet_id and self.selected_fleet_id in self.fleet_registry:
            fleet_info = self.fleet_registry[self.selected_fleet_id]
            title = f"Fleet - {fleet_info.listener_name}"
        else:
            title = "[FLEET]"

        # Update overlay title
        self.sessions['fleet'].overlay.setWindowTitle(title)

    def _broadcast_character_list(self):
        """Send available characters to overlays for context menu."""
        for session in self.sessions.values():
            if session and hasattr(session.overlay, 'update_character_list'):
                session.overlay.update_character_list(self.character_registry)

    def _broadcast_fleet_list(self):
        """Send available fleets to fleet overlay for context menu."""
        if 'fleet' in self.sessions and self.sessions['fleet']:
            if hasattr(self.sessions['fleet'].overlay, 'update_fleet_list'):
                self.sessions['fleet'].overlay.update_fleet_list(
                    self.fleet_registry,
                    self.selected_fleet_id
                )

    def _build_session_config(self, session_id):
        shared = self.config['shared'].copy()
        specific = self.config['sessions'][session_id].copy()
        return {**shared, **specific}

    @Slot(str, str, str, str, str, bool)
    def _route_message(self, session_id, text, sender, timestamp, original, is_trans):
        if session_id in self.sessions and self.sessions[session_id]:
            self.sessions[session_id].add_message(text, sender, timestamp, original, is_trans)

    def shutdown(self):
        logger.info("Shutting down...")
        # Stop sessions WITHOUT changing enabled state
        for sid in list(self.sessions.keys()):
            session = self.sessions.get(sid)
            if session:
                # Disconnect signals to prevent memory leaks
                try:
                    session.lines_ready.disconnect(self.worker.process_lines)
                except:
                    pass

                # Stop the session (cleanup resources)
                session.stop()
                
                # DON'T call stop_session() - it sets enabled=False
                # We want to preserve user's preference for next startup

        self._save_config()
        self.thread.quit()
        self.thread.wait()
        self.app.quit()

    def run(self):
        sys.exit(self.app.exec())

if __name__ == "__main__":
    setup_logging()
    manager = TranslatorManager()
    manager.run()

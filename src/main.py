import sys
import os
import logging
import logging.handlers
import glob
import time
from pathlib import Path
import copy
import traceback
import html
import sqlite3
from enum import Enum
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
from PySide6.QtCore import (
    QObject,
    QCoreApplication,
    QFileSystemWatcher,
    Signal,
    Slot,
    QThread,
    Qt,
    QTimer,
    QMetaObject,
)
from PySide6.QtGui import QIcon, QAction

from src.core.session import ChatSession
from src.core.parser import LineParser
from src.core.tokenizer import EVELinkTokenizer
from src.core.detector import LanguageDetector
from src.core.entity_protection import ProtectedEntityIndex
from src.core.message import ProcessedMessage, TranslationState
from src.core.translation_memory import TranslationMemory
from src.app_config import (
    ConfigStore,
    SHARED_CONFIG_KEYS,
    filter_session_config,
    remove_shared_keys_from_sessions,
    sanitize_config_for_log,
)
from src.services.processing import (
    LogProcessingWorker,
    TranslationScheduler,
    WorkerSignals,
)
from src.services.scan_state import ChatScanState
from src.services.translator import TranslationService
from src.services.hotkeys import (
    COMPOSE_HOTKEY_ID,
    LOCK_HOTKEY_ID,
    WindowsGlobalHotkey,
)
from src.services.local_detector import LocalChatDetector
from src.gui.settings import SettingsDialog
from src.gui.compose import ComposeDialog
from src.version import __version__

DIR_SCAN_DEBOUNCE_MS = 500
WATCHED_DIR_FALLBACK_SECONDS = 60
WORKER_SHUTDOWN_TIMEOUT_MS = 15_000


class SessionRuntimeState(str, Enum):
    """Transient state for a configured chat session."""

    DISABLED = 'disabled'
    WAITING = 'waiting'
    ACTIVE = 'active'
    ERROR = 'error'


class TranslatorManager(QObject):
    """
    Main application manager.
    Coordinates multiple chat sessions and shared processing pipeline.
    """

    worker_config_changed = Signal(int, dict)
    compose_translation_requested = Signal(int, str, str, str)
    provider_usage_requested = Signal()

    def _get_scan_state(self):
        if not hasattr(self, '_scan_state'):
            self._scan_state = ChatScanState.from_config(getattr(self, 'config', {}))
        return self._scan_state

    @property
    def character_registry(self):
        return self._get_scan_state().character_registry

    @character_registry.setter
    def character_registry(self, value):
        self._get_scan_state().character_registry = value

    @property
    def fleet_registry(self):
        return self._get_scan_state().fleet_registry

    @fleet_registry.setter
    def fleet_registry(self, value):
        self._get_scan_state().fleet_registry = value

    @property
    def selected_character_id(self):
        return self._get_scan_state().selected_character_id

    @selected_character_id.setter
    def selected_character_id(self, value):
        self._get_scan_state().selected_character_id = value

    @property
    def selected_fleet_id(self):
        return self._get_scan_state().selected_fleet_id

    @selected_fleet_id.setter
    def selected_fleet_id(self, value):
        self._get_scan_state().selected_fleet_id = value

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
        self.config_store = ConfigStore()
        self.config = self._load_config()
        # Interaction lock is deliberately runtime-only so a stale config can
        # never relaunch an unrecoverable click-through overlay.
        self.overlay_locked = False
        self.hotkey = None
        self.compose_hotkey = None
        self.compose_dialog = None
        self.settings_dialog = None
        self._provider_status_snapshot = {}

        # Shared Processing Components
        parser = LineParser()
        tokenizer = EVELinkTokenizer()
        detector = LanguageDetector()
        self.translation_memory = TranslationMemory()
        # The generated SDE artifact is parsed on the translation thread. Keep
        # an empty index until then so app/tray construction is never delayed,
        # while queued provider jobs still wait behind complete initialization.
        self.entity_index = ProtectedEntityIndex.empty()
        translator_service = TranslationService()
        if hasattr(translator_service, 'set_translation_memory'):
            translator_service.set_translation_memory(self.translation_memory)
        if hasattr(translator_service, 'set_entity_index'):
            translator_service.set_entity_index(self.entity_index)
        self.translation_service = translator_service

        # Analysis and provider work intentionally live on different threads:
        # every original can reach the HUD before a cloud request blocks.
        self.thread = QThread()
        self.worker = LogProcessingWorker(
            parser,
            tokenizer,
            detector,
            translator_service,
            defer_translation=True,
        )
        self.translation_thread = QThread()
        self.translation_scheduler = TranslationScheduler(
            translator_service,
            entity_index_loader=ProtectedEntityIndex.load,
            entity_index_path=(
                self.config.get('shared', {}).get('entity_index_path') or None
            ),
        )
        self.translation_scheduler.provider_status_ready.connect(
            self._provider_status_applied
        )
        # Apply the first snapshot before moving the worker. Later updates use
        # the queued signal so provider state is only mutated by its owner thread.
        self.worker.update_config(copy.deepcopy(self.config['shared']))
        self.translation_scheduler.update_config(copy.deepcopy(self.config['shared']))
        self.worker.moveToThread(self.thread)
        self.translation_scheduler.moveToThread(self.translation_thread)
        self.translation_thread.started.connect(
            self.translation_scheduler.initialize_entity_index
        )
        self.translation_scheduler.entity_index_ready.connect(
            self._entity_index_initialized
        )
        self.worker_config_changed.connect(self.worker.apply_queued_config)
        self.worker_config_changed.connect(
            self.translation_scheduler.apply_queued_config
        )
        self.worker.signals.message_ready.connect(self._route_message)
        self.worker.signals.translation_requested.connect(
            self.translation_scheduler.queue_job
        )
        self.translation_scheduler.message_ready.connect(self._route_message)
        self.compose_translation_requested.connect(
            self.translation_scheduler.translate_composed_message
        )
        self.provider_usage_requested.connect(
            self.translation_scheduler.refresh_provider_usage
        )
        self.translation_scheduler.compose_translation_ready.connect(
            self._finish_compose_translation
        )
        self.translation_scheduler.compose_translation_failed.connect(
            self._fail_compose_translation
        )
        self.translation_scheduler.provider_usage_ready.connect(
            self._provider_usage_ready
        )
        self.thread.start()
        self.translation_thread.start()

        # Sessions
        self.sessions = {}
        self._session_runtime_states = {
            session_id: (
                SessionRuntimeState.WAITING
                if session_config.get('enabled', False)
                else SessionRuntimeState.DISABLED
            )
            for session_id, session_config in self.config['sessions'].items()
        }
        self._session_runtime_errors = {}
        self._sessions_initialized = False

        # Chat log scan state
        self._scan_state = ChatScanState.from_config(self.config)

        # Periodic scanner timer (check every X seconds for characters and fleets)
        # Scan interval is configurable, defaulting to 10s as per user request (legacy default was 30s)
        self._log_dir_watch_active = False
        self.scanner_timer = QTimer()
        self.scanner_timer.setTimerType(Qt.TimerType.CoarseTimer)
        self.scanner_timer.timeout.connect(self._periodic_scan)
        self.scan_debounce_timer = QTimer()
        self.scan_debounce_timer.setSingleShot(True)
        self.scan_debounce_timer.setInterval(DIR_SCAN_DEBOUNCE_MS)
        self.scan_debounce_timer.setTimerType(Qt.TimerType.CoarseTimer)
        self.scan_debounce_timer.timeout.connect(self._handle_debounced_directory_scan)
        self.log_dir_watcher = None
        if QCoreApplication.instance() is not None:
            self.log_dir_watcher = QFileSystemWatcher(self)
            self.log_dir_watcher.directoryChanged.connect(self._handle_log_directory_changed)
        
        self.scanner_timer.start(self._scanner_interval_ms())

        # System Tray
        self._setup_system_tray()

        # Validate Log Directory FIRST (before initializing sessions)
        self._ensure_log_directory()
        self._configure_log_directory_watcher()
        # Scan only after first-run folder selection has finalized the path, so
        # session initialization sees the current registry immediately.
        self._periodic_scan()
        # Show provider guidance after modal log setup so the notification is
        # not hidden behind the first-run folder picker.
        self._show_provider_setup_notice()

        # Initialize sessions (after log directory is confirmed valid)
        self._initialize_sessions()

        logger.info("Translator Manager started. Check System Tray.")

    def _get_configured_log_dir(self):
        return self.config['shared'].get('log_dir', '')

    @Slot(object)
    def _entity_index_initialized(self, index):
        self.entity_index = index or ProtectedEntityIndex.empty()
        logger.info(
            "SDE entity protection ready (%s names)",
            getattr(self.entity_index, 'entity_count', 0),
        )

    def _configured_scan_interval_ms(self):
        return max(1000, int(self.config['shared'].get('fleet_scan_interval', 10) * 1000))

    def _scanner_interval_ms(self):
        configured_ms = self._configured_scan_interval_ms()
        if getattr(self, '_log_dir_watch_active', False):
            return max(configured_ms, WATCHED_DIR_FALLBACK_SECONDS * 1000)
        return configured_ms

    def _restart_scanner_timer(self):
        if hasattr(self, 'scanner_timer') and self.scanner_timer:
            self.scanner_timer.start(self._scanner_interval_ms())

    def _configure_log_directory_watcher(self):
        self._log_dir_watch_active = False
        watcher = getattr(self, 'log_dir_watcher', None)
        if watcher is None:
            self._restart_scanner_timer()
            return False

        for watched in list(watcher.directories()):
            watcher.removePath(watched)

        log_dir = self._get_configured_log_dir()
        if log_dir and os.path.isdir(log_dir):
            self._log_dir_watch_active = watcher.addPath(log_dir)

        self._restart_scanner_timer()
        return self._log_dir_watch_active

    def _handle_log_directory_changed(self, changed_path: str):
        if changed_path != self._get_configured_log_dir():
            return
        if not self.scan_debounce_timer.isActive():
            self.scan_debounce_timer.start()

    def _handle_debounced_directory_scan(self):
        self._configure_log_directory_watcher()
        self._periodic_scan()

    def _apply_scanner_config(self):
        self._configure_log_directory_watcher()

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
        if hasattr(self, 'log_dir_watcher'):
            self._configure_log_directory_watcher()
        logger.info(f"Using Log Directory: {current_path}")

    def _load_config(self) -> dict:
        config_store = getattr(self, 'config_store', ConfigStore())
        return config_store.load()

    def _save_config(self):
        # Update session configs
        for session_id, session in self.sessions.items():
            if session:
                current_overlay_config = session.get_config()
                # Only update session-specific properties.
                # Remove Shared Keys (handled by Shared config)
                # 'enabled' MUST be allowed so it persists to JSON.
                filtered_config = filter_session_config(current_overlay_config)
                
                # Update with filtered config
                self.config.setdefault('sessions', {}).setdefault(session_id, {})
                self.config['sessions'][session_id].update(filtered_config)

        remove_shared_keys_from_sessions(self.config)
        config_store = getattr(self, 'config_store', ConfigStore())
        saved = config_store.save(self.config)
        if saved is False:
            tray_icon = getattr(self, 'tray_icon', None)
            if tray_icon is not None:
                newer_schema = getattr(
                    config_store,
                    'unsupported_schema_version',
                    None,
                )
                if isinstance(newer_schema, int) and not isinstance(
                    newer_schema,
                    bool,
                ):
                    title = "Settings are read-only"
                    detail = (
                        f"This settings file uses newer schema {newer_schema}. "
                        "EVE Translator 1.4 will not overwrite it."
                    )
                else:
                    title = "EVE Translator settings were not saved"
                    detail = (
                        "Check disk access. Provider and session changes may "
                        "revert after restart."
                    )
                tray_icon.showMessage(
                    title,
                    detail,
                    QSystemTrayIcon.MessageIcon.Warning,
                    10000,
                )
            return False
        return True

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

        chord = self.config.get('shared', {}).get('global_hotkey', 'Ctrl+Shift+L')
        self.lock_overlays_action = QAction(
            f"Lock overlays — {chord}",
            self.tray_menu,
            checkable=True,
        )
        self.lock_overlays_action.setChecked(False)
        self.lock_overlays_action.triggered.connect(self.set_overlay_locked)
        self.tray_menu.addAction(self.lock_overlays_action)

        compose_chord = self.config.get('shared', {}).get(
            'compose_hotkey', 'Ctrl+Shift+T'
        )
        self.compose_action = QAction(
            f"Compose translation — {compose_chord}",
            self.tray_menu,
        )
        self.compose_action.triggered.connect(self.open_compose_dialog)
        self.tray_menu.addAction(self.compose_action)

        self.tray_menu.addSeparator()

        self.settings_action = QAction("Settings", self.tray_menu)
        self.settings_action.triggered.connect(self.open_settings_dialog)
        self.tray_menu.addAction(self.settings_action)

        self.tray_menu.addSeparator()
        exit_action = QAction("Exit", self.tray_menu)
        exit_action.triggered.connect(self.shutdown)
        self.tray_menu.addAction(exit_action)

        self.tray_icon.setContextMenu(self.tray_menu)
        self.tray_icon.messageClicked.connect(self.open_settings_dialog)
        self._update_tray_menu()
        self.tray_icon.show()
        self.hotkey = WindowsGlobalHotkey(
            self.app,
            self.toggle_overlay_lock,
            chord=chord,
            hotkey_id=LOCK_HOTKEY_ID,
        )
        if not self.hotkey.register():
            self.tray_icon.showMessage(
                "Global overlay shortcut unavailable",
                f"{self.hotkey.error or 'The key combination is already in use.'} "
                "Use Lock overlays from the tray menu.",
                QSystemTrayIcon.MessageIcon.Warning,
                12000,
            )
        self.compose_hotkey = WindowsGlobalHotkey(
            self.app,
            self.open_compose_dialog,
            chord=compose_chord,
            hotkey_id=COMPOSE_HOTKEY_ID,
        )
        if not self.compose_hotkey.register():
            self.tray_icon.showMessage(
                "Compose shortcut unavailable",
                f"{self.compose_hotkey.error or 'The key combination is already in use.'} "
                "Use Compose translation from the tray menu.",
                QSystemTrayIcon.MessageIcon.Warning,
                12000,
            )

    def _show_provider_setup_notice(self):
        config_store = getattr(self, 'config_store', None)
        newer_schema = getattr(config_store, 'unsupported_schema_version', None)
        if isinstance(newer_schema, int) and not isinstance(newer_schema, bool):
            self.tray_icon.showMessage(
                "Newer settings file detected",
                f"Schema {newer_schema} is newer than this app supports. "
                "Defaults are running read-only; the file will not be overwritten.",
                QSystemTrayIcon.MessageIcon.Warning,
                15000,
            )
            return
        if not getattr(config_store, 'provider_selection_required', False):
            return

        if getattr(config_store, 'migrated_from_implicit_google', False):
            detail = (
                "v1.4 disabled the previous automatic Google fallback. "
                "Open Settings and explicitly choose Google or DeepL."
            )
        else:
            detail = (
                "Translation is off until you explicitly choose Google or DeepL in Settings."
            )
        self.tray_icon.showMessage(
            "Choose a translation provider",
            detail,
            QSystemTrayIcon.MessageIcon.Information,
            12000,
        )

    def _initialize_sessions(self):
        # Start enabled sessions
        if self.config['sessions']['fleet']['enabled']:
            self.start_session('fleet')
        if self.config['sessions']['local']['enabled']:
            self.start_session('local')
        
        # Initial broadcast of state to any active sessions (including those just started)
        self._broadcast_session_states()
        self._sessions_initialized = True

    def _compose_provider_label(self, status: dict | None = None) -> str:
        if not isinstance(status, dict):
            status = getattr(self, '_provider_status_snapshot', {})
        primary = str(status.get('name') or 'configured cloud provider')
        fallback = status.get('fallback')
        if isinstance(fallback, dict) and fallback.get('enabled'):
            return f"{primary} provider with consented {fallback.get('name')} fallback"
        return f"{primary} provider"

    @Slot(dict)
    def _provider_status_applied(self, status: dict) -> None:
        """Publish only provider state applied by the scheduler thread."""
        self._provider_status_snapshot = dict(status) if isinstance(status, dict) else {}
        dialog = getattr(self, 'compose_dialog', None)
        if dialog is not None:
            dialog.set_provider_label(
                self._compose_provider_label(self._provider_status_snapshot)
            )

    @Slot()
    def open_compose_dialog(self) -> None:
        # As with the lock shortcut, defer native hotkey actions while another
        # modal or popup owns input.
        if QApplication.activeModalWidget() or QApplication.activePopupWidget():
            return
        dialog = getattr(self, 'compose_dialog', None)
        if dialog is None:
            dialog = ComposeDialog(
                source_language='auto',
                target_language=self.config.get('shared', {}).get(
                    'target_language', 'en'
                ),
                provider_label=self._compose_provider_label(),
            )
            dialog.translation_requested.connect(
                self.compose_translation_requested.emit
            )
            self.compose_dialog = dialog
        else:
            dialog.set_provider_label(self._compose_provider_label())
        dialog.present()

    @Slot(int, str)
    def _finish_compose_translation(self, request_id: int, text: str) -> None:
        dialog = getattr(self, 'compose_dialog', None)
        if dialog is not None:
            dialog.finish_translation(request_id, text)

    @Slot(int, str)
    def _fail_compose_translation(self, request_id: int, error: str) -> None:
        dialog = getattr(self, 'compose_dialog', None)
        if dialog is not None:
            dialog.fail_translation(request_id, error)

    @Slot(dict)
    def _provider_usage_ready(self, usage: dict) -> None:
        dialog = getattr(self, 'settings_dialog', None)
        if dialog is not None:
            dialog.set_provider_usage(usage)
        deepl_usage = usage if isinstance(usage, dict) else {}
        if deepl_usage.get('provider') == 'google':
            fallback = deepl_usage.get('fallback')
            deepl_usage = (
                fallback
                if isinstance(fallback, dict) and fallback.get('provider') == 'deepl'
                else {}
            )
        used = deepl_usage.get('used')
        limit = deepl_usage.get('limit')
        if (
            isinstance(used, int)
            and not isinstance(used, bool)
            and isinstance(limit, int)
            and not isinstance(limit, bool)
            and limit > 0
            and used / limit >= 0.8
        ):
            tray = getattr(self, 'tray_icon', None)
            if tray is not None:
                tray.showMessage(
                    "DeepL quota warning",
                    f"{used:,} of {limit:,} characters used ({used / limit:.1%}).",
                    QSystemTrayIcon.MessageIcon.Warning,
                    10000,
                )
        self._update_tray_menu()

    def start_session(self, session_id):
        if session_id in self.sessions and self.sessions[session_id]:
            self.config['sessions'][session_id]['enabled'] = True
            self._set_session_runtime_state(session_id, SessionRuntimeState.ACTIVE)
            return True # Already running

        # Keep the user's desired state enabled while waiting for EVE to create a log.
        self.config['sessions'][session_id]['enabled'] = True
        self._set_session_runtime_state(session_id, SessionRuntimeState.WAITING)

        try:
            log_path = self._get_log_path(session_id)
        except Exception as exc:
            logger.exception("[%s] Error locating chat log", session_id)
            self._set_session_runtime_state(
                session_id,
                SessionRuntimeState.ERROR,
                error=str(exc),
            )
            self._broadcast_session_states()
            return False

        if not log_path:
            logger.info(f"[{session_id}] No log file found - waiting for an active chat log")
            self.sessions[session_id] = None
            self._broadcast_session_states()
            return False

        session_config = self._build_session_config(session_id)
        session = None
        try:
            session = ChatSession(session_id, log_path, session_config, parent=self)

            session.lines_ready.connect(self.worker.process_lines)
            session.request_toggle.connect(self.toggle_session) # Handle context menu requests
            session.request_settings.connect(self.open_settings_dialog)
            session.config_changed.connect(self._handle_session_config_change)
            session.character_selected.connect(self.switch_local_character)
            session.exit_requested.connect(self.shutdown)
            session.correction_submitted.connect(self._handle_correction_submitted)

            # Connect fleet selection signal (for fleet sessions)
            if hasattr(session.overlay, 'fleet_selected'):
                session.overlay.fleet_selected.connect(self.switch_fleet)

            self.sessions[session_id] = session
            session.overlay.set_interaction_locked(
                bool(getattr(self, 'overlay_locked', False))
            )
            session.start()
        except Exception as exc:
            logger.exception("[%s] Error starting chat session", session_id)
            if session is not None:
                self._disconnect_session_signals(session)
                try:
                    session.stop()
                except Exception:
                    logger.debug("[%s] Error cleaning up failed session", session_id, exc_info=True)
            self.sessions[session_id] = None
            self._set_session_runtime_state(
                session_id,
                SessionRuntimeState.ERROR,
                error=str(exc),
            )
            self._broadcast_session_states()
            return False

        self._set_session_runtime_state(session_id, SessionRuntimeState.ACTIVE)
        self._broadcast_session_states()
        self._broadcast_character_list()
        return True

    def open_settings_dialog(self):
        """Open the global settings dialog."""
        try:
            # Backup current config for restore on Cancel
            original_config = copy.deepcopy(self.config)

            dlg = SettingsDialog(
                self.config,
                parent=None,
                translation_service=getattr(self, 'translation_service', None),
            )
            self.settings_dialog = dlg
            dlg.provider_usage_requested.connect(
                self.provider_usage_requested.emit
            )
            
            # Connect live preview
            dlg.settings_changed.connect(self.preview_settings)

            if dlg.exec():
                # Saved: Final update (redundant if previewed, but safe)
                new_config = dlg.get_settings()
                pending_doctrine = getattr(dlg, 'pending_doctrine_source', None)
                if isinstance(pending_doctrine, (str, os.PathLike)) and str(
                    pending_doctrine
                ).strip():
                    try:
                        installed = self._install_or_reuse_doctrine_pack(
                            pending_doctrine
                        )
                        new_config['shared']['active_doctrine_pack'] = str(
                            installed.source_path
                        )
                    except Exception as exc:
                        logger.warning("Doctrine pack was not installed: %s", exc)
                        new_config['shared']['active_doctrine_pack'] = (
                            original_config.get('shared', {}).get(
                                'active_doctrine_pack', ''
                            )
                        )
                        self.tray_icon.showMessage(
                            "Doctrine pack was not installed",
                            str(exc),
                            QSystemTrayIcon.MessageIcon.Warning,
                            10000,
                        )
                self.config.update(new_config)
                self.config['shared']['provider_choice_confirmed'] = True
                
                logger.info(
                    "[Settings] New Shared Config: %s",
                    sanitize_config_for_log(self.config['shared']),
                )
                
                # Apply changes to worker (Shared)
                self._queue_worker_config()
                
                # Apply changes to running sessions
                self._apply_config_update()
                self._apply_scanner_config()

                self._save_config()
            else:
                # Cancelled: Restore original config
                logger.info("Settings cancelled. Restoring original config.")
                self.config = original_config
                # Re-apply original config to revert any previews
                self._queue_worker_config()
                self._apply_config_update()
                self._apply_scanner_config()
                # Timers keep running while the modal dialog is open and may
                # persist preview state. Write the restored config back so a
                # cancelled cloud-provider choice cannot return after restart.
                self._save_config()

            self.settings_dialog = None

        except Exception as e:
            self.settings_dialog = None
            logger.error(f"Error opening settings: {e}")
            traceback.print_exc()

    @staticmethod
    def _install_or_reuse_doctrine_pack(source):
        from src.core.doctrine import (
            DoctrinePackError,
            import_doctrine_pack,
            load_doctrine_pack,
        )

        candidate = load_doctrine_pack(source)
        base_dir = Path.home() / '.eve_translator' / 'doctrines'
        installed_path = base_dir / candidate.pack_id
        if installed_path.exists():
            installed = load_doctrine_pack(installed_path)
            if installed.digest != candidate.digest:
                raise DoctrinePackError(
                    f"A different pack with ID {candidate.pack_id!r} is already installed."
                )
            return installed
        return import_doctrine_pack(source, base_dir)

    @Slot(dict)
    def preview_settings(self, new_config):
        """Preview visual settings without activating or persisting cloud choices."""
        preview_shared_keys = {
            'opacity', 'font_size', 'auto_scroll',
            'color_default', 'color_translated', 'color_highlight',
        }
        preview_session_keys = {
            'x', 'y', 'w', 'h', 'background_color',
            'display_mode', 'compact_header', 'command_cards',
        }

        incoming_shared = new_config.get('shared', {})
        current_shared = self.config.setdefault('shared', {})
        for key in preview_shared_keys:
            if key in incoming_shared:
                current_shared[key] = incoming_shared[key]

        incoming_sessions = new_config.get('sessions', {})
        current_sessions = self.config.setdefault('sessions', {})
        for session_id, incoming_session in incoming_sessions.items():
            current_session = current_sessions.setdefault(session_id, {})
            for key in preview_session_keys:
                if key in incoming_session:
                    current_session[key] = incoming_session[key]

        self._apply_config_update()

    def _apply_config_update(self):
        """Propagate config changes to all active sessions."""
        for session_id, session in self.sessions.items():
            if session:
                # Re-build session config (merges shared + specific)
                new_session_config = self._build_session_config(session_id)
                logger.info(f"[{session_id}] Applying Config Update. Font: {new_session_config.get('font_size')}, Opacity: {new_session_config.get('opacity')}")
                session.update_config(new_session_config)
                self._update_overlay_status(session_id)

    def _queue_worker_config(self):
        """Send an isolated config snapshot to the worker's owning thread."""
        generation = self.worker.request_config_update()
        scheduler = getattr(self, 'translation_scheduler', None)
        if scheduler is not None:
            scheduler.request_config_update(generation)
        self.worker_config_changed.emit(
            generation,
            copy.deepcopy(self.config['shared']),
        )

    def _disconnect_session_signals(self, session):
        """Safely disconnect all signals from a session."""
        for signal, slot in [
            (session.lines_ready, self.worker.process_lines),
            (session.request_toggle, self.toggle_session),
            (session.request_settings, self.open_settings_dialog),
            (session.config_changed, self._handle_session_config_change),
            (session.character_selected, self.switch_local_character),
            (session.exit_requested, self.shutdown),
            (session.correction_submitted, self._handle_correction_submitted),
        ]:
            try:
                signal.disconnect(slot)
            except RuntimeError:
                pass
        if hasattr(session.overlay, 'fleet_selected'):
            try:
                session.overlay.fleet_selected.disconnect(self.switch_fleet)
            except RuntimeError:
                pass

    def stop_session(self, session_id):
        session = self.sessions.get(session_id)
        if session:
            self._disconnect_session_signals(session)
            session.stop()

        self.sessions[session_id] = None
        self.config['sessions'][session_id]['enabled'] = False
        self._set_session_runtime_state(session_id, SessionRuntimeState.DISABLED)
        self._broadcast_session_states()

    def toggle_session(self, session_id):
        # Toggle the persisted user intent. A waiting/error session has no live
        # ChatSession object, but clicking it must still disable the session.
        if self.config['sessions'][session_id].get('enabled', False):
            self.stop_session(session_id)
        else:
            self.start_session(session_id)
        
        # Save config immediately so it persists
        self._save_config()

    @Slot(bool)
    def set_overlay_locked(self, locked: bool) -> None:
        self.overlay_locked = bool(locked)
        for session in getattr(self, 'sessions', {}).values():
            if session and getattr(session, 'overlay', None):
                session.overlay.set_interaction_locked(self.overlay_locked)
        action = getattr(self, 'lock_overlays_action', None)
        if action is not None:
            action.blockSignals(True)
            action.setChecked(self.overlay_locked)
            action.blockSignals(False)
        self._update_tray_menu()
        for session_id in getattr(self, 'sessions', {}):
            self._update_overlay_status(session_id)

    @Slot()
    def toggle_overlay_lock(self) -> None:
        # Native callbacks are deferred by the hotkey service. Avoid changing
        # top-level flags under an open modal dialog or popup.
        if QApplication.activeModalWidget() or QApplication.activePopupWidget():
            return
        self.set_overlay_locked(not self.overlay_locked)

    def _handle_session_config_change(self, session_id: str, updated_config: dict):
        """
        Handle config changes from a session's overlay.
        Updates shared config and notifies worker.
        """
        # Extract shared settings (not position/size which are per-session)
        shared_changed = False
        for key in SHARED_CONFIG_KEYS:
            if key in updated_config:
                shared_changed = (
                    shared_changed
                    or self.config['shared'].get(key) != updated_config[key]
                )
                self.config['shared'][key] = updated_config[key]

        # Update session-specific (position, background)
        session_keys = [
            'x', 'y', 'w', 'h', 'background_color',
            'display_mode', 'compact_header', 'command_cards',
        ]
        for key in session_keys:
            if key in updated_config:
                self.config['sessions'][session_id][key] = updated_config[key]

        if shared_changed:
            # Provider and translation state belongs to worker threads; do not
            # enqueue it for display-only position or HUD changes.
            self._queue_worker_config()
            self._apply_config_update()
        self._apply_scanner_config()

        # Save to disk
        self._save_config()

    @Slot(str, dict)
    def _handle_correction_submitted(self, session_id: str, payload: dict) -> None:
        if not isinstance(payload, dict):
            return
        try:
            doctrine_scope = ''
            doctrine_path = self.config['shared'].get('active_doctrine_pack', '')
            if doctrine_path:
                try:
                    from src.core.doctrine import load_doctrine_pack
                    doctrine_scope = load_doctrine_pack(doctrine_path).scope
                except Exception:
                    doctrine_scope = ''
            self.translation_memory.remember(
                payload.get('source_text', ''),
                payload.get('translated_text', ''),
                payload.get('source_language', 'unknown'),
                payload.get('target_language', self.config['shared'].get('target_language', 'en')),
                doctrine_scope,
            )
        except (OSError, RuntimeError, ValueError, sqlite3.Error) as exc:
            logger.warning("Could not save local correction: %s", exc)
            tray_icon = getattr(self, 'tray_icon', None)
            if tray_icon is not None:
                tray_icon.showMessage(
                    "Correction was not saved",
                    "Check access to the EVE Translator data folder.",
                    QSystemTrayIcon.MessageIcon.Warning,
                    8000,
                )
            return

        session = self.sessions.get(session_id)
        if not session or not getattr(session, 'overlay', None):
            return
        message_id = payload.get('message_id')
        existing = next(
            (
                item for item in session.overlay.chat_history
                if item.get('message_id') == message_id
            ),
            None,
        )
        if not existing:
            return
        session.handle_processed_message(ProcessedMessage(
            message_id=str(message_id),
            session_id=session_id,
            source_id=session.source_id,
            text=html.escape(str(payload.get('translated_text', ''))),
            original_text=existing.get('original_text') or html.escape(
                str(payload.get('source_text', ''))
            ),
            sender=existing.get('sender', ''),
            timestamp=existing.get('timestamp', ''),
            state=TranslationState.TRANSLATED,
            provider='Local correction',
            detected_language=existing.get('detected_language', 'unknown'),
            translation_relevant=True,
            speaker_role=existing.get('speaker_role', ''),
            commands=tuple(existing.get('commands') or ()),
            revision=int(existing.get('revision') or 0) + 1,
            lookup_text=str(payload.get('source_text') or ''),
        ))

    def _broadcast_session_states(self):
        """Notify all active sessions of the current global enabled states."""
        states = self.config['sessions']
        for sid, session in self.sessions.items():
            if session:
                session.update_session_states(states)

    def _get_session_runtime_state(self, session_id: str) -> SessionRuntimeState:
        """Return runtime state, inferring it for lightweight/test managers."""
        states = getattr(self, '_session_runtime_states', None)
        if states is None:
            states = {}
            self._session_runtime_states = states

        if session_id not in states:
            session = getattr(self, 'sessions', {}).get(session_id)
            enabled = (
                getattr(self, 'config', {})
                .get('sessions', {})
                .get(session_id, {})
                .get('enabled', False)
            )
            if session:
                states[session_id] = SessionRuntimeState.ACTIVE
            elif enabled:
                states[session_id] = SessionRuntimeState.WAITING
            else:
                states[session_id] = SessionRuntimeState.DISABLED

        return states[session_id]

    def _set_session_runtime_state(
        self,
        session_id: str,
        state: SessionRuntimeState,
        error: str | None = None,
    ) -> None:
        if not hasattr(self, '_session_runtime_states'):
            self._session_runtime_states = {}
        if not hasattr(self, '_session_runtime_errors'):
            self._session_runtime_errors = {}

        self._session_runtime_states[session_id] = state
        if error:
            self._session_runtime_errors[session_id] = error
        else:
            self._session_runtime_errors.pop(session_id, None)
        self._update_tray_menu()
        self._update_overlay_status(session_id)

    def _update_overlay_status(self, session_id: str) -> None:
        session = getattr(self, 'sessions', {}).get(session_id)
        if not session or not getattr(session, 'overlay', None):
            return
        from src.gui.overlay import OverlayStatus

        state_labels = {
            SessionRuntimeState.DISABLED: 'Off',
            SessionRuntimeState.WAITING: 'Waiting',
            SessionRuntimeState.ACTIVE: 'Active',
            SessionRuntimeState.ERROR: 'Error',
        }
        state = self._get_session_runtime_state(session_id)
        session_config = self.config.get('sessions', {}).get(session_id, {})
        if session_id == 'local':
            character = session_config.get('character_name', '')
            system = session_config.get('system_name', '')
            context = ' · '.join(value for value in (character, system) if value)
        else:
            context = ''
            selected = getattr(self, 'selected_fleet_id', None)
            fleet = getattr(self, 'fleet_registry', {}).get(selected) if selected else None
            if fleet is not None:
                context = getattr(fleet, 'listener_name', '')

        service = getattr(self, 'translation_service', None)
        provider_status = (
            service.get_provider_status()
            if service is not None and hasattr(service, 'get_provider_status')
            else {}
        )
        configured_mode = self.config.get('shared', {}).get(
            'translation_provider', 'disabled'
        )
        configured_names = {
            'disabled': 'Disabled',
            'google': 'Google',
            'deepl': 'DeepL',
        }
        if configured_mode == 'disabled':
            provider_status = {
                'mode': 'disabled',
                'name': 'Disabled',
                'available': True,
                'reason': 'Translation is disabled by choice.',
            }
        elif provider_status.get('mode') != configured_mode:
            provider_status = {
                'mode': configured_mode,
                'name': configured_names.get(configured_mode, 'Invalid'),
                'available': configured_mode in {'disabled', 'google'} or bool(
                    self.config.get('shared', {}).get('deepl_api_key', '')
                ),
                'reason': '',
            }
        error = getattr(self, '_session_runtime_errors', {}).get(session_id, '')
        provider_reason = str(provider_status.get('reason') or '')
        detail = error or provider_reason or (
            f"Translation provider: {provider_status.get('name', 'Disabled')}"
        )
        session.overlay.update_status(OverlayStatus(
            channel=session_id.capitalize(),
            state=state_labels[state],
            context=context,
            provider=str(provider_status.get('name') or 'Disabled'),
            provider_available=bool(provider_status.get('available', False)),
            detail=detail,
            display_mode=session.overlay.display_mode,
            interaction_locked=bool(getattr(self, 'overlay_locked', False)),
        ))

    def _update_tray_menu(self):
        state_labels = {
            SessionRuntimeState.DISABLED: 'Off',
            SessionRuntimeState.WAITING: 'Waiting',
            SessionRuntimeState.ACTIVE: 'Active',
            SessionRuntimeState.ERROR: 'Error',
        }
        action_specs = (
            ('fleet', 'Fleet Chat', 'fleet_action'),
            ('local', 'Local Chat', 'local_action'),
        )
        provider_mode = (
            self.config.get('shared', {})
            .get('translation_provider', 'disabled')
        )
        provider_labels = {
            'disabled': 'Translation: Off',
            'google': 'Translation: Google',
            'deepl': 'Translation: DeepL',
        }
        deepl_api_key = self.config.get('shared', {}).get('deepl_api_key', '')
        if (
            provider_mode == 'deepl'
            and not (isinstance(deepl_api_key, str) and deepl_api_key.strip())
        ):
            provider_labels['deepl'] = 'Translation: DeepL (key required)'
        tooltip_parts = [
            provider_labels.get(provider_mode, 'Translation: configuration error')
        ]
        service = getattr(self, 'translation_service', None)
        get_usage = getattr(service, 'get_provider_usage', None)
        if callable(get_usage):
            usage = get_usage(refresh=False)
            if provider_mode == 'deepl':
                used = usage.get('used') if isinstance(usage, dict) else None
                limit = usage.get('limit') if isinstance(usage, dict) else None
                if (
                    isinstance(used, int)
                    and not isinstance(used, bool)
                    and isinstance(limit, int)
                    and not isinstance(limit, bool)
                    and limit > 0
                ):
                    tooltip_parts.append(
                        f"DeepL quota: {used:,}/{limit:,} ({used / limit:.0%})"
                    )
            elif provider_mode == 'google':
                estimated = (
                    usage.get('estimated_characters')
                    if isinstance(usage, dict)
                    else None
                )
                if isinstance(estimated, int) and not isinstance(estimated, bool):
                    tooltip_parts.append(
                        f"Google local estimate: {estimated:,} characters"
                    )
                fallback = usage.get('fallback') if isinstance(usage, dict) else None
                if (
                    isinstance(fallback, dict)
                    and fallback.get('provider') == 'deepl'
                ):
                    used = fallback.get('used')
                    limit = fallback.get('limit')
                    if (
                        isinstance(used, int)
                        and not isinstance(used, bool)
                        and isinstance(limit, int)
                        and not isinstance(limit, bool)
                        and limit > 0
                    ):
                        tooltip_parts.append(
                            f"DeepL fallback quota: {used:,}/{limit:,} "
                            f"({used / limit:.0%})"
                        )
        tooltip_parts.append(
            f"Overlays: {'Locked' if getattr(self, 'overlay_locked', False) else 'Interactive'}"
        )

        for session_id, label, action_name in action_specs:
            state = self._get_session_runtime_state(session_id)
            enabled = (
                self.config.get('sessions', {})
                .get(session_id, {})
                .get('enabled', False)
            )
            state_label = state_labels[state]
            tooltip_parts.append(f"{label}: {state_label}")

            action = getattr(self, action_name, None)
            if action is not None:
                action.setChecked(enabled)
                action.setText(f"{label} - {state_label}")
                error = getattr(self, '_session_runtime_errors', {}).get(session_id)
                action.setToolTip(error or f"{label}: {state_label}")

        tray_icon = getattr(self, 'tray_icon', None)
        if tray_icon is not None:
            tray_icon.setToolTip("EVE Translator\n" + "\n".join(tooltip_parts))
        lock_action = getattr(self, 'lock_overlays_action', None)
        if lock_action is not None:
            lock_action.setChecked(bool(getattr(self, 'overlay_locked', False)))

    def _recover_waiting_sessions(self):
        """Start enabled sessions once their EVE chat log becomes available."""
        for session_id in ('fleet', 'local'):
            session_config = self.config['sessions'].get(session_id, {})
            if not session_config.get('enabled', False):
                if (
                    not self.sessions.get(session_id)
                    and self._get_session_runtime_state(session_id)
                    != SessionRuntimeState.DISABLED
                ):
                    self._set_session_runtime_state(session_id, SessionRuntimeState.DISABLED)
                continue

            if self.sessions.get(session_id):
                # A live overlay can intentionally remain open while its old log
                # is inactive, so preserve the Waiting state set by the scanner.
                continue

            if self._get_session_runtime_state(session_id) == SessionRuntimeState.WAITING:
                self.start_session(session_id)

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

        # Fallback for startup before the detector registry is populated. Never
        # claim an old, inactive fleet log is Active.
        pattern = os.path.join(log_dir, "Fleet_*.txt")
        files = glob.glob(pattern)
        threshold = self.config['shared'].get('fleet_inactive_threshold', 1800)
        now = time.time()
        active_files = []
        for filepath in files:
            try:
                if now - os.path.getmtime(filepath) < threshold:
                    active_files.append(filepath)
            except OSError:
                continue
        if not active_files:
            return None
        return max(active_files, key=os.path.getmtime)

    def _find_latest_local_log(self):
        """
        Get log path for Local session.
        Uses selected character if set, otherwise most recent.
        """
        log_dir = self._get_configured_log_dir()
        if not log_dir or not os.path.exists(log_dir):
            return None
            
        # Character activity is established by the periodic detector using both
        # recent writes and the presence of an open EVE client window. Historical
        # Local logs remain selectable metadata, but must not make readiness Active.
        selected_character = None
        if self.selected_character_id:
            logger.info(f"[LOCAL] Searching log for selected CharID: {self.selected_character_id}")
            selected_character = self.character_registry.get(
                self.selected_character_id
            )
            if selected_character is not None:
                if not selected_character.is_active:
                    logger.info(
                        "[LOCAL] Selected character %s is inactive; checking for another active character",
                        selected_character.character_name,
                    )
                else:
                    logger.info(
                        f"[LOCAL] Tracking: {selected_character.character_name}"
                    )
                    self._get_scan_state().select_character(
                        selected_character.character_id,
                        self.config,
                    )
                    return selected_character.latest_log_path

        active_characters = [
            char_info
            for char_info in self.character_registry.values()
            if char_info.is_active
        ]
        if not active_characters:
            return None

        char_info = max(active_characters, key=lambda item: item.log_mtime)
        if selected_character is not None:
            logger.info(
                "[LOCAL] Auto-switching from inactive %s to active %s",
                selected_character.character_name,
                char_info.character_name,
            )
        else:
            logger.info(f"[LOCAL] Auto-selected: {char_info.character_name}")
        self._get_scan_state().select_character(char_info.character_id, self.config)
        return char_info.latest_log_path

    def _periodic_scan(self):
        """
        Periodic scanner that runs every 30 seconds.
        Scans for both active characters (Local) and active fleets (Fleet).
        """
        self._scan_characters()
        self._scan_fleets()
        if getattr(self, '_sessions_initialized', True):
            self._recover_waiting_sessions()

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
        fleet_to_switch = None

        # Check if selected fleet's log changed or became inactive
        if self.selected_fleet_id:
            if self.selected_fleet_id in new_registry:
                # Current fleet is still active - keep it (don't switch to newer fleets)
                fleet_info = new_registry[self.selected_fleet_id]
                old_info = self.fleet_registry.get(self.selected_fleet_id)
                if (
                    self.sessions.get('fleet')
                    and self._get_session_runtime_state('fleet')
                    != SessionRuntimeState.ACTIVE
                ):
                    self._set_session_runtime_state('fleet', SessionRuntimeState.ACTIVE)

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
                            fleet_to_switch = most_recent.fleet_id

            else:
                # Selected fleet is no longer active
                logger.warning(f"[FLEET] Selected fleet log is no longer active")

                if auto_switch_enabled and new_registry:
                    # Auto-switch to most recent active fleet
                    most_recent = detector.get_most_recent_fleet(new_registry)
                    if most_recent and most_recent.fleet_id != self.selected_fleet_id:
                        logger.info(f"[FLEET] Auto-switching to most recent active fleet: {most_recent.listener_name}")
                        fleet_to_switch = most_recent.fleet_id
                else:
                    # Auto-switch disabled or no fleets available
                    logger.info(
                        "[FLEET] Current fleet inactive; waiting for a replacement log"
                    )
                    if self.sessions.get('fleet'):
                        # Keep the overlay open so its fleet picker remains
                        # available, but stop claiming that the stale log is live.
                        self._set_session_runtime_state(
                            'fleet', SessionRuntimeState.WAITING
                        )
        else:
            # No fleet selected - initial auto-selection
            if new_registry:
                # Check if fleet session is enabled and running
                if self.config['sessions']['fleet'].get('enabled') and 'fleet' in self.sessions and self.sessions['fleet']:
                    # Session is running but no fleet selected - select most recent
                    most_recent = detector.get_most_recent_fleet(new_registry)
                    if most_recent:
                        logger.info(f"[FLEET] Initial auto-selection: {most_recent.listener_name}")
                        fleet_to_switch = most_recent.fleet_id

        # Update registry
        self.fleet_registry = new_registry

        # Publish the new registry before switching so switch_fleet can resolve
        # a fleet discovered during this scan.
        if fleet_to_switch:
            self.switch_fleet(fleet_to_switch)

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
        restart_selected_local = False
        fallback_character_id = None

        # Check if selected character's log changed
        if self.selected_character_id:
            if self.selected_character_id in new_registry:
                char_info = new_registry[self.selected_character_id]
                old_info = self.character_registry.get(self.selected_character_id)

                if self.sessions.get('local'):
                    next_state = (
                        SessionRuntimeState.ACTIVE
                        if char_info.is_active
                        else SessionRuntimeState.WAITING
                    )
                    if self._get_session_runtime_state('local') != next_state:
                        self._set_session_runtime_state('local', next_state)

                # Log file changed (client restarted)?
                if old_info and char_info.latest_log_path != old_info.latest_log_path:
                    logger.info(f"[LOCAL] Detected new log file for {char_info.character_name}")
                    # Restart only when Local is intentionally enabled. Scanning
                    # metadata must never turn an Off session back on.
                    if (
                        char_info.is_active
                        and self.config['sessions']['local'].get('enabled', False)
                    ):
                        restart_selected_local = True

                # System changed?
                if old_info and char_info.system_name != old_info.system_name:
                    logger.info(f"[LOCAL] System changed: {old_info.system_name} -> {char_info.system_name}")
                    # Update config
                    self.config['sessions']['local']['system_name'] = char_info.system_name
                    # Update overlay title
                    self._update_local_overlay_title()
            elif self.sessions.get('local'):
                self._set_session_runtime_state('local', SessionRuntimeState.WAITING)

        selected_info = new_registry.get(self.selected_character_id)
        active_characters = [
            char_info for char_info in new_registry.values() if char_info.is_active
        ]
        if (
            self.config['sessions']['local'].get('enabled', False)
            and getattr(self, '_sessions_initialized', True)
            and active_characters
            and (selected_info is None or not selected_info.is_active)
        ):
            fallback = max(active_characters, key=lambda item: item.log_mtime)
            if fallback.character_id != self.selected_character_id:
                fallback_character_id = fallback.character_id

        # Update registry
        self.character_registry = new_registry

        # Restart after publishing the new registry so log lookup cannot reopen
        # the previous character log.
        if fallback_character_id:
            self.switch_local_character(fallback_character_id)
        elif restart_selected_local:
            self._switch_local_character(self.selected_character_id)

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
        self._get_scan_state().select_character(char_id, self.config)

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
        self._update_overlay_status('local')

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
        self._get_scan_state().select_fleet(fleet_id, self.config)

        # Restart Fleet session with new log (using switch_fleet_log in session)
        if 'fleet' in self.sessions and self.sessions['fleet']:
            self.sessions['fleet'].switch_fleet_log(fleet_info.log_path, fleet_info.listener_name)
            self._set_session_runtime_state('fleet', SessionRuntimeState.ACTIVE)

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
        self._update_overlay_status('fleet')

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

    @Slot(object)
    def _route_message(self, message):
        session = self.sessions.get(message.session_id)
        if session and message.source_id == session.source_id:
            session.handle_processed_message(message)
        elif session:
            logger.debug(
                "[%s] Dropping translation from a previous chat log",
                message.session_id,
            )

    def shutdown(self):
        logger.info("Shutting down...")
        hotkey = getattr(self, 'hotkey', None)
        if hotkey is not None:
            hotkey.unregister()
        compose_hotkey = getattr(self, 'compose_hotkey', None)
        if compose_hotkey is not None:
            compose_hotkey.unregister()
        compose_dialog = getattr(self, 'compose_dialog', None)
        if compose_dialog is not None:
            compose_dialog.close()
        # Stop sessions WITHOUT changing enabled state
        for sid in list(self.sessions.keys()):
            session = self.sessions.get(sid)
            if session:
                # Disconnect all signals to prevent memory leaks
                self._disconnect_session_signals(session)

                # Stop the session (cleanup resources)
                session.stop()

                # DON'T call stop_session() - it sets enabled=False
                # We want to preserve user's preference for next startup

        self._save_config()
        self.thread.requestInterruption()
        self.thread.quit()
        if not self.thread.wait(WORKER_SHUTDOWN_TIMEOUT_MS):
            logger.error("Translation worker did not stop before the shutdown deadline")
            # Provider work is bounded, so reaching this branch means an
            # unexpected third-party or parser deadlock during process exit.
            self.thread.terminate()
            self.thread.wait(1_000)
        scheduler = getattr(self, 'translation_scheduler', None)
        translation_thread = getattr(self, 'translation_thread', None)
        if translation_thread is not None:
            if scheduler is not None and translation_thread.isRunning():
                QMetaObject.invokeMethod(
                    scheduler,
                    'stop',
                    Qt.ConnectionType.QueuedConnection,
                )
            translation_thread.requestInterruption()
            translation_thread.quit()
            if not translation_thread.wait(WORKER_SHUTDOWN_TIMEOUT_MS):
                logger.error("Translation scheduler did not stop before the shutdown deadline")
                translation_thread.terminate()
                translation_thread.wait(1_000)
        memory = getattr(self, 'translation_memory', None)
        close_memory = getattr(memory, 'close', None)
        if callable(close_memory):
            close_memory()
        self.app.quit()

    def run(self):
        sys.exit(self.app.exec())

if __name__ == "__main__":
    setup_logging()
    manager = TranslatorManager()
    manager.run()

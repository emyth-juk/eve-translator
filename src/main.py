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
from src.core.session_specs import SESSION_IDS, SESSION_SPECS, SESSION_SPECS_BY_ID
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
from src.services.single_instance import (
    SingleInstanceGuard,
    show_already_running_message,
)
from src.services.local_detector import LocalChatDetector
from src.services.channel_detector import AnyChatDetector
from src.services.chat_discovery import (
    ChatDiscoveryWorker,
    active_character_starts,
)
from src.services.log_discovery import log_recency_key, normalized_log_path
from src.gui.settings import SettingsDialog
from src.gui.compose import ComposeDialog
from src.version import __version__

DIR_SCAN_DEBOUNCE_MS = 500
WATCHED_DIR_FALLBACK_SECONDS = 60
WORKER_SHUTDOWN_TIMEOUT_MS = 15_000
# Discovery normally completes in well under a second. Do not make app exit
# inherit the much longer cloud/provider worker deadline if filesystem or
# native window discovery becomes stuck.
DISCOVERY_SHUTDOWN_TIMEOUT_MS = 2_000
DISCOVERY_RETRY_BASE_MS = 1_000
DISCOVERY_RETRY_MAX_MS = 10_000
DISCOVERY_FAILURE_ERROR_THRESHOLD = 3
SCANNER_CONFIG_KEYS = frozenset({
    'log_dir',
    'fleet_inactive_threshold',
    'fleet_scan_interval',
})


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
    chat_discovery_requested = Signal(int, str, int)

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

    @property
    def channel_registry(self):
        return self._get_scan_state().channel_registry

    @channel_registry.setter
    def channel_registry(self, value):
        self._get_scan_state().channel_registry = value

    @property
    def selected_channel_id(self):
        return self._get_scan_state().selected_channel_id

    @selected_channel_id.setter
    def selected_channel_id(self, value):
        self._get_scan_state().selected_channel_id = value

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
        self._shutting_down = False
        self.app.aboutToQuit.connect(self.shutdown)

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
        self._setup_chat_discovery_worker()

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
        # Queue discovery only after first-run folder selection finalizes the
        # path. Enabled sessions begin truthfully in Waiting and recover as soon
        # as the background snapshot reaches the GUI thread.
        self._periodic_scan()
        # Show provider guidance after modal log setup so the notification is
        # not hidden behind the first-run folder picker.
        self._show_provider_setup_notice()

        # Initialize sessions (after log directory is confirmed valid)
        self._initialize_sessions()

        logger.info("Translator Manager started. Check System Tray.")

    def _setup_chat_discovery_worker(self) -> None:
        """Keep filesystem discovery and window probing off the GUI thread."""
        self._discovery_generation = 0
        self._discovery_scan_pending = False
        self._discovery_rescan_requested = False
        self._discovery_inflight_generation = None
        self._discovery_inflight_signature = None
        self._discovery_request_invalidated = False
        self._discovery_failure_count = 0
        self._discovery_retry_signature = None
        self._discovery_error_sessions = set()
        self.discovery_retry_timer = QTimer(self)
        self.discovery_retry_timer.setSingleShot(True)
        self.discovery_retry_timer.setTimerType(Qt.TimerType.CoarseTimer)
        self.discovery_retry_timer.timeout.connect(self._periodic_scan)
        # Keep the thread lifetime explicit, matching the processing workers.
        # aboutToQuit is wired to shutdown so Qt never owns/destroys it while
        # filesystem work is still in flight.
        self.discovery_thread = QThread()
        self.discovery_worker = ChatDiscoveryWorker()
        self.discovery_worker.moveToThread(self.discovery_thread)
        self.chat_discovery_requested.connect(
            self.discovery_worker.scan,
            Qt.ConnectionType.QueuedConnection,
        )
        self.discovery_worker.result_ready.connect(
            self._apply_chat_discovery_result
        )
        self.discovery_thread.finished.connect(
            self.discovery_worker.deleteLater
        )
        self.discovery_thread.start()

    def _get_configured_log_dir(self):
        return self.config['shared'].get('log_dir', '')

    def _request_chat_discovery(self) -> bool:
        """Queue at most one scan and remember if another pass is needed."""
        if getattr(self, '_shutting_down', False):
            return True

        discovery_thread = getattr(self, 'discovery_thread', None)
        if discovery_thread is None or not discovery_thread.isRunning():
            return False

        log_dir = self._get_configured_log_dir()
        if not log_dir:
            return False

        threshold = int(
            self.config['shared'].get('fleet_inactive_threshold', 1800)
        )
        signature = (normalized_log_path(log_dir), threshold)

        retry_timer = getattr(self, 'discovery_retry_timer', None)
        retry_signature = getattr(self, '_discovery_retry_signature', None)
        if (
            retry_signature is not None
            and retry_timer is not None
            and retry_timer.isActive()
        ):
            if signature == retry_signature:
                # The scheduled retry will see all filesystem changes that
                # accumulated during the quiet period. Routine timer/watcher
                # activity must not punch through the failure backoff.
                return True
            # A deliberate scanner configuration change targets different
            # work, so it does not need to wait on the old path's backoff.
            retry_timer.stop()
            self._discovery_failure_count = 0
            self._discovery_retry_signature = None
            self._clear_discovery_session_errors()

        if getattr(self, '_discovery_scan_pending', False):
            self._discovery_rescan_requested = True
            if signature != getattr(
                self,
                '_discovery_inflight_signature',
                None,
            ):
                self._discovery_request_invalidated = True
                self._discovery_failure_count = 0
                self._discovery_retry_signature = None
                self._clear_discovery_session_errors()
            return True

        if retry_timer is not None:
            retry_timer.stop()
        self._discovery_generation = (
            getattr(self, '_discovery_generation', 0) + 1
        )
        self._discovery_scan_pending = True
        self._discovery_inflight_generation = self._discovery_generation
        self._discovery_inflight_signature = signature
        self._discovery_request_invalidated = False
        self.chat_discovery_requested.emit(
            self._discovery_generation,
            log_dir,
            int(threshold),
        )
        return True

    @Slot(object)
    def _apply_chat_discovery_result(self, result) -> None:
        """Publish one complete discovery generation on the GUI thread."""
        if getattr(self, '_shutting_down', False):
            return

        generation = getattr(result, 'generation', -1)
        inflight = getattr(self, '_discovery_inflight_generation', None)
        if generation != inflight:
            logger.debug(
                "Ignoring stale chat discovery generation %s (in flight %s)",
                generation,
                inflight,
            )
            return

        self._discovery_scan_pending = False
        self._discovery_inflight_generation = None
        completed_signature = getattr(
            self,
            '_discovery_inflight_signature',
            None,
        )
        self._discovery_inflight_signature = None
        invalidated = getattr(
            self,
            '_discovery_request_invalidated',
            False,
        )
        self._discovery_request_invalidated = False
        if invalidated:
            self._discovery_rescan_requested = False
            QTimer.singleShot(0, self._periodic_scan)
            return

        current_log_dir = self._get_configured_log_dir()
        result_log_dir = getattr(result, 'log_dir', '')
        same_directory = bool(current_log_dir and result_log_dir) and (
            normalized_log_path(current_log_dir)
            == normalized_log_path(result_log_dir)
        )
        if getattr(result, 'succeeded', False) and same_directory:
            self._discovery_failure_count = 0
            self._discovery_retry_signature = None
            self._clear_discovery_session_errors()
            self._scan_characters(
                new_registry=dict(result.character_registry)
            )
            self._scan_channels(
                new_registry=dict(result.channel_registry)
            )
            self._scan_fleets(new_registry=dict(result.fleet_registry))
            if getattr(self, '_sessions_initialized', True):
                self._recover_waiting_sessions()
        elif getattr(result, 'error', None) is not None:
            self._discovery_failure_count = min(
                getattr(self, '_discovery_failure_count', 0) + 1,
                32,
            )
            error = result.error
            logger.error(
                "Chat discovery failed during %s (%s): %s",
                getattr(error, 'stage', 'unknown'),
                getattr(error, 'exception_type', 'Error'),
                getattr(error, 'message', ''),
            )
            exponent = min(self._discovery_failure_count - 1, 4)
            delay_ms = min(
                DISCOVERY_RETRY_BASE_MS * (2 ** exponent),
                DISCOVERY_RETRY_MAX_MS,
            )
            self._discovery_retry_signature = completed_signature
            self._discovery_rescan_requested = False
            if (
                self._discovery_failure_count
                >= DISCOVERY_FAILURE_ERROR_THRESHOLD
            ):
                self._surface_discovery_failure(error, delay_ms)

            if not same_directory:
                # The configured target changed without passing through the
                # normal invalidation path. Retry that new target immediately.
                self._discovery_retry_signature = None
                QTimer.singleShot(0, self._periodic_scan)
                return

            retry_timer = getattr(self, 'discovery_retry_timer', None)
            if retry_timer is not None:
                retry_timer.start(delay_ms)
            return

        immediate_rescan = (
            getattr(self, '_discovery_rescan_requested', False)
            or not same_directory
        )
        if immediate_rescan:
            self._discovery_rescan_requested = False
            QTimer.singleShot(0, self._periodic_scan)

    def _surface_discovery_failure(self, error, retry_delay_ms: int) -> None:
        """Expose a persistent discovery outage on every enabled session."""
        stage = str(getattr(error, 'stage', 'unknown') or 'unknown')
        exception_type = str(
            getattr(error, 'exception_type', 'Error') or 'Error'
        )
        message = str(getattr(error, 'message', '') or '').strip()
        log_dir = str(self._get_configured_log_dir() or '').strip()
        stage_labels = {
            'snapshot': (
                f'Cannot read EVE chat log folder "{log_dir}"'
                if log_dir
                else 'Cannot read the EVE chat log folder'
            ),
            'local': 'Could not detect active Local chats',
            'local-session-starts': 'Could not identify active EVE sessions',
            'any': 'Could not detect active chat channels',
            'fleet': 'Could not detect active Fleet chats',
        }
        summary = stage_labels.get(stage, 'Chat log discovery failed')
        reason = exception_type
        if message:
            reason = f"{exception_type}: {message}"
        retry_seconds = max(1, int(round(retry_delay_ms / 1_000)))
        detail = (
            f"{summary} ({reason}). "
            f"Retrying automatically in {retry_seconds}s."
        )

        error_sessions = getattr(self, '_discovery_error_sessions', None)
        if error_sessions is None:
            error_sessions = set()
            self._discovery_error_sessions = error_sessions
        for session_id in SESSION_IDS:
            enabled = (
                self.config.get('sessions', {})
                .get(session_id, {})
                .get('enabled', False)
            )
            if not enabled:
                continue
            error_sessions.add(session_id)
            self._set_session_runtime_state(
                session_id,
                SessionRuntimeState.ERROR,
                detail,
            )

    def _clear_discovery_session_errors(self) -> None:
        """Return discovery-owned ERROR sessions to normal recovery flow."""
        error_sessions = set(
            getattr(self, '_discovery_error_sessions', set())
        )
        self._discovery_error_sessions = set()
        for session_id in error_sessions:
            enabled = (
                self.config.get('sessions', {})
                .get(session_id, {})
                .get('enabled', False)
            )
            state = (
                SessionRuntimeState.WAITING
                if enabled
                else SessionRuntimeState.DISABLED
            )
            self._set_session_runtime_state(session_id, state)

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
        if getattr(self, '_shutting_down', False):
            return
        if changed_path != self._get_configured_log_dir():
            return
        if not self.scan_debounce_timer.isActive():
            self.scan_debounce_timer.start()

    def _handle_debounced_directory_scan(self):
        if getattr(self, '_shutting_down', False):
            return
        self._configure_log_directory_watcher()
        self._periodic_scan()

    def _apply_scanner_config(self):
        if getattr(self, '_shutting_down', False):
            return
        self._configure_log_directory_watcher()
        self._periodic_scan()

    @staticmethod
    def _scanner_config_signature(config: dict) -> tuple:
        shared = config.get('shared', {})
        return tuple(shared.get(key) for key in sorted(SCANNER_CONFIG_KEYS))

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
                # Log-source identity is selected by the manager/scanners. An
                # overlay holds a point-in-time copy, so merging those fields
                # back after a character/fleet/channel switch can roll the
                # authoritative selection back to stale metadata.
                spec = SESSION_SPECS_BY_ID.get(session_id)
                identity_keys = (
                    spec.source_identity_keys if spec is not None else ()
                )
                for key in identity_keys:
                    filtered_config.pop(key, None)

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

        self.session_actions = {}
        for spec in SESSION_SPECS:
            action = QAction(spec.label, self.tray_menu, checkable=True)
            action.setChecked(
                self.config['sessions'][spec.session_id]['enabled']
            )
            action.triggered.connect(
                lambda _checked=False, session_id=spec.session_id: (
                    self.toggle_session(session_id)
                )
            )
            self.session_actions[spec.session_id] = action
            # Retain the public attributes used by existing UI tests and
            # integrations while deriving every action from the descriptor.
            setattr(self, f"{spec.session_id}_action", action)
            self.tray_menu.addAction(action)

        self.any_source_menu = self.tray_menu.addMenu("Any Chat Source")
        self.any_source_menu.aboutToShow.connect(self._populate_any_source_menu)

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
        hotkey_failures = []
        self.hotkey = WindowsGlobalHotkey(
            self.app,
            self.toggle_overlay_lock,
            chord=chord,
            hotkey_id=LOCK_HOTKEY_ID,
        )
        if not self.hotkey.register():
            hotkey_failures.append((
                "Overlay lock",
                chord,
                self.hotkey,
                self.lock_overlays_action,
            ))
        self.compose_hotkey = WindowsGlobalHotkey(
            self.app,
            self.open_compose_dialog,
            chord=compose_chord,
            hotkey_id=COMPOSE_HOTKEY_ID,
        )
        if not self.compose_hotkey.register():
            hotkey_failures.append((
                "Compose translation",
                compose_chord,
                self.compose_hotkey,
                self.compose_action,
            ))

        if hotkey_failures:
            details = []
            for feature, failed_chord, hotkey, action in hotkey_failures:
                error = hotkey.error or (
                    f"{failed_chord} is already in use by another application."
                )
                details.append(f"{feature}: {error}")
                action.setToolTip(
                    f"{error} Use this tray action instead."
                )
                logger.warning(
                    "%s global shortcut unavailable "
                    "(chord=%s, error_code=%s): %s",
                    feature,
                    failed_chord,
                    getattr(hotkey, 'error_code', None),
                    error,
                )
            title = (
                "Global shortcut unavailable"
                if len(hotkey_failures) == 1
                else "Global shortcuts unavailable"
            )
            self.tray_icon.showMessage(
                title,
                "\n".join(details)
                + "\nThe corresponding tray actions remain available.",
                QSystemTrayIcon.MessageIcon.Warning,
                12000,
            )

    def _populate_any_source_menu(self) -> None:
        """Rebuild the arbitrary-chat picker from the latest log scan."""
        menu = getattr(self, 'any_source_menu', None)
        if menu is None:
            return
        menu.clear()

        selected = getattr(self, 'selected_channel_id', None)
        channels = self._any_channel_choices()
        if not channels:
            action = QAction("No chat logs detected", menu)
            action.setEnabled(False)
            menu.addAction(action)
            return

        channels.sort(key=lambda item: (
            not bool(item.is_active),
            item.channel_name.casefold(),
            item.listener_name.casefold(),
            -float(item.started_time),
        ))
        for channel in channels:
            label = f"{channel.channel_name} - {channel.listener_name}"
            if not channel.is_active:
                label += " [Offline]"
            action = QAction(label, menu, checkable=True)
            action.setChecked(channel.selection_id == selected)
            action.triggered.connect(
                lambda _checked=False, selection=channel.selection_id: (
                    self.switch_channel(selection)
                )
            )
            menu.addAction(action)

    def _any_channel_choices(self):
        """Keep pickers useful without flooding them with every old rotation."""
        registry = getattr(self, 'channel_registry', {})
        channels = list(registry.values())
        if not channels:
            return []

        selected = getattr(self, 'selected_channel_id', None)
        active = [channel for channel in channels if channel.is_active]
        if active:
            choices = active
        else:
            newest_start = max(channel.started_time for channel in channels)
            choices = [
                channel
                for channel in channels
                if abs(channel.started_time - newest_start) < 1.0
            ]

        selected_channel = registry.get(selected) if selected else None
        if selected_channel is not None and selected_channel not in choices:
            choices.append(selected_channel)
        return choices

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
        for session_id in SESSION_IDS:
            if self.config['sessions'][session_id]['enabled']:
                self.start_session(session_id)
        
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
            if hasattr(session.overlay, 'channel_selected'):
                session.overlay.channel_selected.connect(self.switch_channel)

            self.sessions[session_id] = session
            session.overlay.set_interaction_locked(
                bool(getattr(self, 'overlay_locked', False))
            )
            session.start()
            if session_id == 'any':
                self._update_any_overlay_title()
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
        self._broadcast_channel_list()
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
                if (
                    self._scanner_config_signature(original_config)
                    != self._scanner_config_signature(self.config)
                ):
                    self._apply_scanner_config()

                self._save_config()
            else:
                # Cancelled: Restore original config
                logger.info("Settings cancelled. Restoring original config.")
                preview_config = self.config
                self.config = original_config
                # Re-apply original config to revert any previews
                self._queue_worker_config()
                self._apply_config_update()
                if (
                    self._scanner_config_signature(preview_config)
                    != self._scanner_config_signature(self.config)
                ):
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
        if hasattr(session.overlay, 'channel_selected'):
            try:
                session.overlay.channel_selected.disconnect(self.switch_channel)
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
        scanner_changed = False
        for key in SHARED_CONFIG_KEYS:
            if key in updated_config:
                value_changed = (
                    self.config['shared'].get(key) != updated_config[key]
                )
                shared_changed = (
                    shared_changed
                    or value_changed
                )
                scanner_changed = (
                    scanner_changed
                    or (value_changed and key in SCANNER_CONFIG_KEYS)
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
        if scanner_changed:
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
        elif session_id == 'fleet':
            context = ''
            selected = getattr(self, 'selected_fleet_id', None)
            fleet = getattr(self, 'fleet_registry', {}).get(selected) if selected else None
            if fleet is not None:
                context = getattr(fleet, 'listener_name', '')
        else:
            channel = session_config.get('channel_name', '')
            listener = session_config.get('listener_name', '')
            context = ' · '.join(value for value in (channel, listener) if value)

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
        spec = SESSION_SPECS_BY_ID.get(session_id)
        session.overlay.update_status(OverlayStatus(
            channel=spec.status_label if spec is not None else session_id,
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

        for spec in SESSION_SPECS:
            session_id = spec.session_id
            label = spec.label
            state = self._get_session_runtime_state(session_id)
            enabled = (
                self.config.get('sessions', {})
                .get(session_id, {})
                .get('enabled', False)
            )
            state_label = state_labels[state]
            error = getattr(self, '_session_runtime_errors', {}).get(session_id)
            tooltip_line = f"{label}: {state_label}"
            if error:
                tooltip_line = f"{tooltip_line} - {error}"
            tooltip_parts.append(tooltip_line)

            action = getattr(self, 'session_actions', {}).get(session_id)
            if action is None:
                action = getattr(self, f"{session_id}_action", None)
            if action is not None:
                action.setChecked(enabled)
                action.setText(f"{label} - {state_label}")
                action.setToolTip(error or f"{label}: {state_label}")

        tray_icon = getattr(self, 'tray_icon', None)
        if tray_icon is not None:
            tray_icon.setToolTip("EVE Translator\n" + "\n".join(tooltip_parts))
        lock_action = getattr(self, 'lock_overlays_action', None)
        if lock_action is not None:
            lock_action.setChecked(bool(getattr(self, 'overlay_locked', False)))

    def _recover_waiting_sessions(self):
        """Start enabled sessions once their EVE chat log becomes available."""
        for session_id in SESSION_IDS:
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
        spec = SESSION_SPECS_BY_ID.get(session_id)
        finder = getattr(self, spec.log_finder, None) if spec is not None else None
        return finder() if callable(finder) else None

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
            most_recent = FleetDetector.get_most_recent_fleet(
                self.fleet_registry
            )
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

    def _find_latest_any_log(self):
        """Return the active arbitrary-channel log selected by the pilot."""
        selected = getattr(self, 'selected_channel_id', None)
        if selected:
            channel = getattr(self, 'channel_registry', {}).get(selected)
            if channel is None or not channel.is_active:
                return None
            self._get_scan_state().select_channel(selected, self.config)
            return channel.log_path

        active_channels = [
            channel
            for channel in getattr(self, 'channel_registry', {}).values()
            if channel.is_active
        ]
        if not active_channels:
            return None

        channel = max(
            active_channels,
            key=lambda item: log_recency_key(
                item.started_time,
                item.log_mtime,
                item.log_path,
            ),
        )
        self._get_scan_state().select_channel(channel.selection_id, self.config)
        logger.info(
            "[ANY] Auto-selected chat: %s - %s",
            channel.channel_name,
            channel.listener_name,
        )
        return channel.log_path

    def _scan_channels(self, *, new_registry=None):
        """Publish arbitrary chats and follow only the selected source."""
        if new_registry is None:
            log_dir = self._get_configured_log_dir()
            if not log_dir or not os.path.exists(log_dir):
                return

            detector = getattr(self, 'any_chat_detector', None)
            if detector is None:
                detector = AnyChatDetector()
                self.any_chat_detector = detector
            active_characters = active_character_starts(
                getattr(self, 'character_registry', {})
            )
            new_registry = detector.scan_channels(
                log_dir,
                active_characters=active_characters,
            )
        else:
            new_registry = dict(new_registry)
        self.channel_registry = new_registry

        selected = getattr(self, 'selected_channel_id', None)
        if (
            not selected
            and self.config['sessions']['any'].get('enabled', False)
        ):
            active_channels = [
                channel for channel in new_registry.values() if channel.is_active
            ]
            if active_channels:
                channel = max(
                    active_channels,
                    key=lambda item: log_recency_key(
                        item.started_time,
                        item.log_mtime,
                        item.log_path,
                    ),
                )
                self._get_scan_state().select_channel(
                    channel.selection_id,
                    self.config,
                )
                selected = channel.selection_id

        channel = new_registry.get(selected) if selected else None
        session = getattr(self, 'sessions', {}).get('any')
        if channel is not None and channel.is_active:
            self._get_scan_state().select_channel(channel.selection_id, self.config)
            current_path = getattr(session, 'log_path', '') if session else ''
            path_changed = bool(session) and (
                normalized_log_path(current_path)
                != normalized_log_path(channel.log_path)
            )
            if session and path_changed:
                session.switch_log(channel.log_path)
                self._update_any_overlay_title()
            if session:
                self._set_session_runtime_state(
                    'any', SessionRuntimeState.ACTIVE
                )
        elif session:
            self._set_session_runtime_state('any', SessionRuntimeState.WAITING)

        self._broadcast_channel_list()

    def _periodic_scan(self):
        """Request event-driven discovery, with a synchronous test fallback."""
        if getattr(self, '_shutting_down', False):
            return
        if self._request_chat_discovery():
            return

        self._scan_characters()
        self._scan_channels()
        self._scan_fleets()
        if getattr(self, '_sessions_initialized', True):
            self._recover_waiting_sessions()

    def _scan_fleets(self, *, new_registry=None):
        """
        Periodically scan for active fleet chat logs.
        Updates fleet registry and handles:
        - Initial auto-selection (when no fleet selected)
        - Auto-switch when current fleet becomes inactive (if enabled)
        - Does NOT auto-switch when newer fleets appear
        """
        from src.services.fleet_detector import FleetDetector
        if new_registry is None:
            log_dir = self._get_configured_log_dir()
            if not log_dir or not os.path.exists(log_dir):
                return

            # Get user-configured threshold
            threshold = self.config['shared'].get(
                'fleet_inactive_threshold',
                1800,
            )
            new_registry = FleetDetector().scan_active_fleets(
                log_dir,
                active_threshold_seconds=threshold,
            )
        else:
            new_registry = dict(new_registry)

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
                    most_recent = FleetDetector.get_most_recent_fleet(
                        new_registry
                    )
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
                    most_recent = FleetDetector.get_most_recent_fleet(
                        new_registry
                    )
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
                    most_recent = FleetDetector.get_most_recent_fleet(
                        new_registry
                    )
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

    def _scan_characters(self, *, new_registry=None):
        """
        Periodically scan for active EVE characters.
        Updates character registry and handles:
        - New characters logging in
        - Characters logging out (stale logs)
        - Log file rotations (client restarts)
        """
        if new_registry is None:
            log_dir = self._get_configured_log_dir()
            if not log_dir or not os.path.exists(log_dir):
                return

            detector = LocalChatDetector()
            new_registry = detector.scan_active_characters(log_dir)
        else:
            new_registry = dict(new_registry)
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

    def switch_channel(self, selection_id: str):
        """Select one arbitrary EVE chat without aggregating duplicate logs."""
        channel = getattr(self, 'channel_registry', {}).get(selection_id)
        if channel is None:
            logger.warning("[ANY] Chat source not found: %s", selection_id)
            return

        logger.info(
            "[ANY] Selecting chat: %s - %s",
            channel.channel_name,
            channel.listener_name,
        )
        self._get_scan_state().select_channel(selection_id, self.config)

        enabled = self.config['sessions']['any'].get('enabled', False)
        session = getattr(self, 'sessions', {}).get('any')
        if enabled and channel.is_active:
            if session:
                current = normalized_log_path(session.log_path)
                selected = normalized_log_path(channel.log_path)
                if current != selected:
                    session.switch_log(channel.log_path)
                self._set_session_runtime_state(
                    'any', SessionRuntimeState.ACTIVE
                )
            else:
                # start_session owns the resulting ACTIVE/WAITING/ERROR state.
                # In particular, do not mask a constructor/log lookup failure
                # as Active when no ChatSession was created.
                self.start_session('any')
        elif enabled:
            if session:
                self._disconnect_session_signals(session)
                session.stop()
                self.sessions['any'] = None
            self._set_session_runtime_state('any', SessionRuntimeState.WAITING)

        self._update_any_overlay_title()
        self._broadcast_channel_list()
        self._save_config()

    def _update_any_overlay_title(self):
        """Show the selected arbitrary channel and listening character."""
        session = getattr(self, 'sessions', {}).get('any')
        if not session:
            return

        session_config = self.config['sessions']['any']
        channel_name = session_config.get('channel_name') or 'Any Chat'
        listener = session_config.get('listener_name', '')
        title = f"Any Chat - {channel_name}"
        if listener:
            title += f" - {listener}"
        session.overlay.setWindowTitle(title)
        self._update_overlay_status('any')

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

    def _broadcast_channel_list(self):
        """Send arbitrary chat choices to the Any Chat overlay."""
        session = getattr(self, 'sessions', {}).get('any')
        if session and hasattr(session.overlay, 'update_channel_list'):
            choices = self._any_channel_choices()
            session.overlay.update_channel_list(
                {channel.selection_id: channel for channel in choices},
                self.selected_channel_id,
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
        if getattr(self, '_shutting_down', False):
            return
        self._shutting_down = True
        logger.info("Shutting down...")
        for timer_name in (
            'scanner_timer',
            'scan_debounce_timer',
            'discovery_retry_timer',
        ):
            timer = getattr(self, timer_name, None)
            if timer is not None:
                timer.stop()
        watcher = getattr(self, 'log_dir_watcher', None)
        if watcher is not None:
            watcher.blockSignals(True)
            for watched in list(watcher.directories()):
                watcher.removePath(watched)
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
        discovery_worker = getattr(self, 'discovery_worker', None)
        discovery_thread = getattr(self, 'discovery_thread', None)
        if discovery_worker is not None:
            discovery_worker.request_stop()
        if discovery_thread is not None:
            discovery_thread.requestInterruption()
            discovery_thread.quit()
            if not discovery_thread.wait(DISCOVERY_SHUTDOWN_TIMEOUT_MS):
                logger.error(
                    "Chat discovery did not stop within %d ms; forcing shutdown",
                    DISCOVERY_SHUTDOWN_TIMEOUT_MS,
                )
                # A blocked directory/native-window call cannot observe the
                # cooperative stop flag. Termination remains a last resort so
                # a wedged discovery pass cannot hold the whole app open.
                discovery_thread.terminate()
                discovery_thread.wait(1_000)
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


def run_application(
    *,
    guard=None,
    manager_factory=TranslatorManager,
    already_running_notifier=show_already_running_message,
    logging_setup=setup_logging,
) -> int:
    """Run one translator process and release all process-wide resources."""
    instance_guard = guard if guard is not None else SingleInstanceGuard()
    acquired = instance_guard.acquire()
    if not acquired and instance_guard.already_running:
        already_running_notifier()
        return 0

    logging_setup()
    if not acquired:
        logging.warning(
            "Single-instance guard unavailable; continuing without it: %s",
            instance_guard.error or "unknown error",
        )

    manager = None
    try:
        manager = manager_factory()
        return int(manager.app.exec())
    finally:
        if manager is not None and not getattr(
            manager,
            '_shutting_down',
            False,
        ):
            manager.shutdown()
        instance_guard.release()

if __name__ == "__main__":
    raise SystemExit(run_application())

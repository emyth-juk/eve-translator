import logging
import time
import uuid
from pathlib import Path
from PySide6.QtCore import QObject, QCoreApplication, QFileSystemWatcher, Qt, QTimer, Signal

logger = logging.getLogger(__name__)
LOG_WATCH_DEBOUNCE_MS = 100
GEOMETRY_KEYS = ('x', 'y', 'w', 'h')


def _geometry_of(config: dict) -> tuple:
    return tuple(config.get(key) for key in GEOMETRY_KEYS)

class ChatSession(QObject):
    """
    Encapsulates one Fleet, Local, or arbitrary EVE chat session.
    Manages tailer, overlay, and polling lifecycle.
    """

    # Signal emitted when new lines are ready for processing
    # session_id, source_id, lines[]
    lines_ready = Signal(str, str, list)
    # Signal emitted when config changes in overlay
    config_changed = Signal(str, dict)
    # Signal to request session toggle
    request_toggle = Signal(str)
    # Signal to request settings dialog
    request_settings = Signal()
    # Signal to request character switch
    character_selected = Signal(str)
    # Signal to request application shutdown
    exit_requested = Signal()
    # Exact local correction request, routed to the translation service.
    correction_submitted = Signal(str, dict)

    def __init__(self, session_id: str, log_path: str,
                 overlay_config: dict, parent=None):
        """
        Initialize chat session.

        Args:
            session_id: Unique ID ('fleet', 'local', or 'any')
            log_path: Path to log file to tail
            overlay_config: Session-specific overlay configuration
            parent: Parent QObject
        """
        super().__init__(parent)

        self.session_id = session_id
        self.source_id = uuid.uuid4().hex
        self.log_path = Path(log_path)
        self.config = overlay_config
        # The overlay owns its live geometry. The manager's copy can lag
        # behind a resize, so only apply pushed geometry that actually changed
        # on the manager side (for example "Reset position").
        self._last_pushed_geometry = _geometry_of(overlay_config)

        # Components
        # Import inside to avoid potential top-level circular issues if any
        from src.core.tailer import FleetLogTailer
        from src.gui.overlay import OverlayWindow

        self.tailer = FleetLogTailer(str(self.log_path))
        self.overlay = OverlayWindow(
            session_id=session_id,
            initial_config=self.config
        )
        
        # Connect overlay signals
        self.overlay.config_updated.connect(self._handle_config_update)
        # We also need to Bubble up the session toggle signal
        self.overlay.session_toggled.connect(self._handle_session_toggle)
        self.overlay.request_settings.connect(self.request_settings)
        self.overlay.character_selected.connect(self.character_selected)
        self.overlay.exit_requested.connect(self.exit_requested)
        self.overlay.speaker_role_changed.connect(self._handle_speaker_role_changed)
        self.overlay.correction_submitted.connect(
            lambda payload: self.correction_submitted.emit(self.session_id, payload)
        )

        # Polling timer
        self.poll_timer = QTimer()
        self.poll_timer.setTimerType(Qt.TimerType.CoarseTimer)
        self.poll_timer.timeout.connect(self._poll_log)
        self.file_watch_debounce = QTimer(self)
        self.file_watch_debounce.setSingleShot(True)
        self.file_watch_debounce.setInterval(LOG_WATCH_DEBOUNCE_MS)
        self.file_watch_debounce.setTimerType(Qt.TimerType.CoarseTimer)
        self.file_watch_debounce.timeout.connect(self._handle_debounced_file_change)
        self.file_watcher = None
        self._file_watch_active = False
        if QCoreApplication.instance() is not None:
            self.file_watcher = QFileSystemWatcher(self)
            self.file_watcher.fileChanged.connect(self._handle_log_file_changed)

        self.is_running = False

    def start(self):
        """Start session: begin tailing, show overlay, start polling."""
        if self.is_running:
            return

        # FIX: Don't just skip everything! Load last N messages for context.
        # This fixes the issue where a newly detected log shows nothing until a new message arrives.
        history_lines = self.config.get('fleet_history_lines', 5)
        
        if history_lines > 0:
            # CHECK: Is this log file stale?
            # If the file hasn't been modified in > 30 minutes, don't show old history.
            try:
                mtime = self.log_path.stat().st_mtime
                threshold = self.config.get('fleet_inactive_threshold', 1800)
                if time.time() - mtime < threshold:
                    last_lines = self.tailer.read_last_n_lines(history_lines)
                    if last_lines:
                        logger.info(f"[{self.session_id}] Loaded {len(last_lines)} existing messages")
                        self.lines_ready.emit(self.session_id, self.source_id, last_lines)
                else:
                    logger.info(f"[{self.session_id}] Log is stale (>30m old). Skipping history backfill.")
            except OSError:
                pass

        self.tailer.seek_to_end()

        # Show overlay
        self.overlay.show()

        # Start polling
        self._start_log_watcher()
        self.poll_timer.start(self._poll_interval_ms())

        self.is_running = True
        logger.info(f"[{self.session_id}] Session started: {self.log_path}")

    def stop(self):
        """Stop session: close tailer, hide overlay, stop polling."""
        if not self.is_running:
            return

        # Stop polling
        self.poll_timer.stop()
        self.file_watch_debounce.stop()
        self._stop_log_watcher()

        # Close tailer
        if self.tailer:
            self.tailer.close()

        # Hide and save overlay config
        if self.overlay:
            # Although manager handles save, we ensure current state is captured
            self.overlay.save_config() 
            self.overlay.close()

        self.is_running = False
        logger.info(f"[{self.session_id}] Session stopped")

    def switch_fleet_log(self, new_log_path: str, listener_name: str):
        """Compatibility wrapper for existing Fleet callers."""
        self.switch_log(new_log_path)

    def switch_log(self, new_log_path: str):
        """Switch this session to another concrete EVE chat log source."""
        if not self.is_running:
            logger.warning(f"[{self.session_id}] Cannot switch log - session not running")
            return

        logger.info(f"[{self.session_id}] Switching to new chat log: {new_log_path}")
        # Invalidate any provider result still in flight for the previous log.
        self.source_id = uuid.uuid4().hex

        # Stop polling temporarily
        self.poll_timer.stop()
        self.file_watch_debounce.stop()
        self._stop_log_watcher()

        # Close old tailer
        if self.tailer:
            self.tailer.close()

        # Update log path
        self.log_path = Path(new_log_path)

        # Create new tailer
        from src.core.tailer import FleetLogTailer
        self.tailer = FleetLogTailer(str(self.log_path))

        # Clear existing messages in overlay
        if self.overlay:
            self.overlay.clear_messages()

        # Load last N lines from new log
        history_lines = self.config.get('fleet_history_lines', 5)
        if history_lines > 0:
            # CHECK: Is this log file stale?
            try:
                mtime = self.log_path.stat().st_mtime
                threshold = self.config.get('fleet_inactive_threshold', 1800)
                if time.time() - mtime < threshold:
                    last_lines = self.tailer.read_last_n_lines(history_lines)
                    if last_lines:
                        logger.info(f"[{self.session_id}] Loaded {len(last_lines)} messages from new fleet log")
                        # Emit these lines for processing
                        self.lines_ready.emit(self.session_id, self.source_id, last_lines)
                else:
                    logger.info(f"[{self.session_id}] Log is stale (>30m old). Skipping history backfill.")
            except OSError:
                pass

        # Seek to end for future messages
        self.tailer.seek_to_end()

        # Resume polling
        self._start_log_watcher()
        self.poll_timer.start(self._poll_interval_ms())

        logger.info(f"[{self.session_id}] Chat log switch complete")

    def _configured_poll_interval_ms(self):
        interval = self.config.get('polling_interval', 0.5)
        return max(100, int(interval * 1000))

    def _poll_interval_ms(self):
        # QFileSystemWatcher accelerates delivery when Windows reports an
        # append, but EVE's UTF-16 log writes do not reliably produce one event
        # per chat line. Keep the configured poll as the hard latency fallback
        # even while the watcher is registered.
        return self._configured_poll_interval_ms()

    def _start_log_watcher(self):
        self._file_watch_active = False
        if self.file_watcher is None:
            return False

        path = str(self.log_path)
        for watched in list(self.file_watcher.files()):
            if watched != path:
                self.file_watcher.removePath(watched)

        if not self.log_path.exists():
            if path in self.file_watcher.files():
                self.file_watcher.removePath(path)
            return False

        if path not in self.file_watcher.files():
            self._file_watch_active = self.file_watcher.addPath(path)
        else:
            self._file_watch_active = True
        return self._file_watch_active

    def _stop_log_watcher(self):
        if self.file_watcher is None:
            self._file_watch_active = False
            return
        for watched in list(self.file_watcher.files()):
            self.file_watcher.removePath(watched)
        self._file_watch_active = False

    def _handle_log_file_changed(self, changed_path: str):
        if Path(changed_path) != self.log_path:
            return
        if not self.file_watch_debounce.isActive():
            self.file_watch_debounce.start()

    def _handle_debounced_file_change(self):
        self._start_log_watcher()
        if self.is_running:
            self.poll_timer.start(self._poll_interval_ms())
            self._poll_log(trigger="watcher")

    def update_session_states(self, states: dict):
        """Pass enabled states to overlay."""
        if self.overlay:
            # Simplify states to just boolean flags for now
            simple_states = {k: v.get('enabled', False) for k, v in states.items()}
            self.overlay.update_session_states(simple_states)

    def _poll_log(self, trigger="timer"):
        """Internal: Poll log file for new lines."""
        if not self.tailer:
            return

        new_lines = self.tailer.read_new_lines()
        if new_lines:
            try:
                file_age_ms = max(
                    0.0,
                    (time.time() - self.log_path.stat().st_mtime) * 1000,
                )
            except OSError:
                file_age_ms = -1.0
            logger.debug(
                "[%s] Read %s new chat line(s) via %s (file age %.0f ms)",
                self.session_id,
                len(new_lines),
                trigger,
                file_age_ms,
            )
            # Emit to worker with session_id
            self.lines_ready.emit(self.session_id, self.source_id, new_lines)

    def add_message(self, text: str, sender: str, timestamp: str,
                    original_text: str, is_translated: bool):
        """Add message to overlay (called by manager after worker processes)."""
        if self.overlay:
            self.overlay.add_message(text, sender, timestamp,
                                    original_text, is_translated)

    def handle_processed_message(self, message):
        """Add or update a structured message emitted by the processing worker."""
        if self.overlay:
            self.overlay.upsert_processed_message(message)

    def get_config(self) -> dict:
        """Get current overlay config (position, size, etc.)."""
        if self.overlay:
            return self.overlay.get_current_config()
        return self.config

    def update_config(self, new_config: dict):
        """Update session configuration from Manager."""
        old_interval = self.config.get('polling_interval', 1.0)
        new_config = dict(new_config)
        pushed_geometry = _geometry_of(new_config)
        if pushed_geometry == self._last_pushed_geometry:
            for key in GEOMETRY_KEYS:
                new_config.pop(key, None)
        else:
            self._last_pushed_geometry = pushed_geometry
        self.config.update(new_config)

        if self.overlay:
            # Overlay config is just the session config
            self.overlay.config.update(new_config)
            self.overlay.apply_config()
            
        # Check Polling Interval change
        new_interval = self.config.get('polling_interval', 1.0)
        if self.is_running and abs(new_interval - old_interval) > 0.01:
            logger.info(f"[{self.session_id}] Updating polling interval: {new_interval}s")
            self.poll_timer.start(self._poll_interval_ms())

    def _handle_config_update(self, new_config):
        self.config.update(new_config)
        # The manager now holds this live geometry; a later push of the same
        # values is not a deliberate change.
        self._last_pushed_geometry = _geometry_of(self.config)
        self.config_changed.emit(self.session_id, self.config)

    def _handle_session_toggle(self, target_session_id):
        self.request_toggle.emit(target_session_id)

    def _handle_speaker_role_changed(self, sender: str, role: str):
        roles = dict(self.config.get('speaker_roles', {}))
        from src.core.combat import normalize_speaker_name
        normalized = normalize_speaker_name(sender)
        if role:
            roles[normalized] = role
        else:
            roles.pop(normalized, None)
        self.config['speaker_roles'] = roles
        self.config_changed.emit(self.session_id, self.config)

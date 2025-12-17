import logging
from pathlib import Path
from PySide6.QtCore import QObject, QTimer, Signal

logger = logging.getLogger(__name__)

class ChatSession(QObject):
    """
    Encapsulates a single chat session (Fleet or Local).
    Manages tailer, overlay, and polling lifecycle.
    """

    # Signal emitted when new lines are ready for processing
    # session_id, lines[]
    lines_ready = Signal(str, list)
    # Signal emitted when config changes in overlay
    config_changed = Signal(str, dict)
    # Signal to request session toggle
    request_toggle = Signal(str)
    # Signal to request settings dialog
    request_settings = Signal()
    # Signal to request character switch
    character_selected = Signal(str)

    def __init__(self, session_id: str, log_path: str,
                 overlay_config: dict, parent=None):
        """
        Initialize chat session.

        Args:
            session_id: Unique ID ('fleet' or 'local')
            log_path: Path to log file to tail
            overlay_config: Session-specific overlay configuration
            parent: Parent QObject
        """
        super().__init__(parent)

        self.session_id = session_id
        self.log_path = Path(log_path)
        self.config = overlay_config

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

        # Polling timer
        self.poll_timer = QTimer()
        self.poll_timer.timeout.connect(self._poll_log)

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
                import time
                if time.time() - mtime < 1800:  # 30 minutes
                    last_lines = self.tailer.read_last_n_lines(history_lines)
                    if last_lines:
                        logger.info(f"[{self.session_id}] Loaded {len(last_lines)} existing messages")
                        self.lines_ready.emit(self.session_id, last_lines)
                else:
                    logger.info(f"[{self.session_id}] Log is stale (>30m old). Skipping history backfill.")
            except OSError:
                pass

        self.tailer.seek_to_end()

        # Show overlay
        self.overlay.show()

        # Start polling
        interval = self.config.get('polling_interval', 1.0)
        self.poll_timer.start(int(interval * 1000))

        self.is_running = True
        logger.info(f"[{self.session_id}] Session started: {self.log_path}")

    def stop(self):
        """Stop session: close tailer, hide overlay, stop polling."""
        if not self.is_running:
            return

        # Stop polling
        self.poll_timer.stop()

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
        """
        Switch to a different fleet log file.
        Used when user selects a different active fleet.

        Args:
            new_log_path: Path to the new fleet log file
            listener_name: Character name for the new fleet
        """
        if not self.is_running:
            logger.warning(f"[{self.session_id}] Cannot switch log - session not running")
            return

        logger.info(f"[{self.session_id}] Switching to new fleet log: {new_log_path}")

        # Stop polling temporarily
        self.poll_timer.stop()

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
                import time
                if time.time() - mtime < 1800:  # 30 minutes
                    last_lines = self.tailer.read_last_n_lines(history_lines)
                    if last_lines:
                        logger.info(f"[{self.session_id}] Loaded {len(last_lines)} messages from new fleet log")
                        # Emit these lines for processing
                        self.lines_ready.emit(self.session_id, last_lines)
                else:
                    logger.info(f"[{self.session_id}] Log is stale (>30m old). Skipping history backfill.")
            except OSError:
                pass

        # Seek to end for future messages
        self.tailer.seek_to_end()

        # Resume polling
        interval = self.config.get('polling_interval', 1.0)
        self.poll_timer.start(int(interval * 1000))

        logger.info(f"[{self.session_id}] Fleet log switch complete")

    def update_session_states(self, states: dict):
        """Pass enabled states to overlay."""
        if self.overlay:
            # Simplify states to just boolean flags for now
            simple_states = {k: v.get('enabled', False) for k, v in states.items()}
            self.overlay.update_session_states(simple_states)

    def _poll_log(self):
        """Internal: Poll log file for new lines."""
        if not self.tailer:
            return

        new_lines = self.tailer.read_new_lines()
        if new_lines:
            # Emit to worker with session_id
            self.lines_ready.emit(self.session_id, new_lines)

    def add_message(self, text: str, sender: str, timestamp: str,
                    original_text: str, is_translated: bool):
        """Add message to overlay (called by manager after worker processes)."""
        if self.overlay:
            self.overlay.add_message(text, sender, timestamp,
                                    original_text, is_translated)

    def get_config(self) -> dict:
        """Get current overlay config (position, size, etc.)."""
        if self.overlay:
            return self.overlay.get_current_config()
        return self.config

    def update_config(self, new_config: dict):
        """Update session configuration from Manager."""
        old_interval = self.config.get('polling_interval', 1.0)
        self.config.update(new_config)
        
        if self.overlay:
            # Overlay config is just the session config
            self.overlay.config.update(new_config)
            self.overlay.apply_config()
            
        # Check Polling Interval change
        new_interval = self.config.get('polling_interval', 1.0)
        if self.is_running and abs(new_interval - old_interval) > 0.01:
            logger.info(f"[{self.session_id}] Updating polling interval: {new_interval}s")
            self.poll_timer.start(int(new_interval * 1000))

    def _handle_config_update(self, new_config):
        self.config.update(new_config)
        self.config_changed.emit(self.session_id, self.config)

    def _handle_session_toggle(self, target_session_id):
        self.request_toggle.emit(target_session_id)

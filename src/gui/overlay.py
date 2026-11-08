import ctypes
import datetime
import re
import html
import sys
from collections import deque
from dataclasses import dataclass, replace
from urllib.parse import quote, unquote

from PySide6.QtWidgets import (QMainWindow, QLabel, QVBoxLayout, QWidget, QApplication,
                               QTextBrowser, QFrame, QMenu, QFileDialog, QMessageBox,
                               QSizeGrip, QInputDialog, QSizePolicy)
from PySide6.QtCore import Qt, QTimer, Signal, QRect, QEvent, QUrl
from PySide6.QtGui import (
    QAction,
    QColor,
    QCursor,
    QFont,
    QFontMetrics,
    QMouseEvent,
    QTextCursor,
)

from src.core.languages import normalize_language_code

from src.core.message import ProcessedMessage, TranslationState
from src.app_config import (
    DISPLAY_MODE_BILINGUAL,
    DISPLAY_MODE_COMBAT,
    DISPLAY_MODE_MIRROR,
    DISPLAY_MODES,
)
from src.core.combat import command_is_trusted, normalize_speaker_name
from src.core.session_specs import SESSION_SPECS, get_session_spec

from src.version import __version__

MAX_VISIBLE_MESSAGES = 100
UI_FLUSH_INTERVAL_MS = 50
# Border width (logical pixels) that resizes the frameless window.
RESIZE_MARGIN_PX = 6
# Persist size/position shortly after the last move or resize event.
GEOMETRY_SAVE_DELAY_MS = 400

WM_NCHITTEST = 0x0084
HTLEFT = 10
HTRIGHT = 11
HTTOP = 12
HTTOPLEFT = 13
HTTOPRIGHT = 14
HTBOTTOM = 15
HTBOTTOMLEFT = 16
HTBOTTOMRIGHT = 17


MIN_VISIBLE_ON_SCREEN_PX = 48


def _rgba(color, alpha: float, fallback: str = '#101820') -> str:
    """CSS rgba() for a named/hex colour with the given 0..1 alpha."""
    qcolor = QColor(str(color or ''))
    if not qcolor.isValid():
        qcolor = QColor(fallback)
    alpha_byte = max(0, min(255, round(float(alpha) * 255)))
    return f"rgba({qcolor.red()}, {qcolor.green()}, {qcolor.blue()}, {alpha_byte})"


def native_resize_hit_test(x, y, left, top, right, bottom, margin):
    """Return the Win32 ``HT*`` resize code for a border point, else ``None``.

    Coordinates are native screen pixels, as delivered with ``WM_NCHITTEST``.
    """
    if not (left <= x < right and top <= y < bottom):
        return None
    on_left = x < left + margin
    on_right = x >= right - margin
    on_top = y < top + margin
    on_bottom = y >= bottom - margin
    if on_top and on_left:
        return HTTOPLEFT
    if on_top and on_right:
        return HTTOPRIGHT
    if on_bottom and on_left:
        return HTBOTTOMLEFT
    if on_bottom and on_right:
        return HTBOTTOMRIGHT
    if on_left:
        return HTLEFT
    if on_right:
        return HTRIGHT
    if on_top:
        return HTTOP
    if on_bottom:
        return HTBOTTOM
    return None


@dataclass(frozen=True)
class OverlayStatus:
    channel: str
    state: str = "Waiting"
    context: str = ""
    provider: str = "Disabled"
    provider_available: bool = True
    detail: str = ""
    display_mode: str = DISPLAY_MODE_MIRROR
    interaction_locked: bool = False

class OverlayWindow(QMainWindow):
    # Signal emitted when config is saved/loaded/changed permanently
    config_updated = Signal(dict)
    # Signal to request session toggle (session_id)
    session_toggled = Signal(str)
    # Signal to request settings dialog
    request_settings = Signal()
    # Signal to request character switch (char_id)
    character_selected = Signal(str)
    # Signal to request fleet switch (fleet_id)
    fleet_selected = Signal(str)
    # Signal to select one arbitrary EVE chat source.
    channel_selected = Signal(str)
    # Signal to request application shutdown
    exit_requested = Signal()
    # Explicit role management; values are original display name and role.
    speaker_role_changed = Signal(str, str)
    # Local exact correction submitted from a rendered translated line.
    correction_submitted = Signal(dict)

    def __init__(self, session_id='fleet', initial_config=None):
        super().__init__()
        self.session_id = session_id
        self.session_spec = get_session_spec(session_id)
        
        # Window Flags for Overlay
        self.setWindowFlags(
            Qt.WindowType.FramelessWindowHint | 
            Qt.WindowType.WindowStaysOnTopHint | 
            Qt.WindowType.Tool  # Tool window doesn't appear in taskbar usually
        )
        self.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating, True)
        session_label = (
            self.session_spec.status_label
            if self.session_spec is not None
            else session_id.capitalize()
        )
        self.setWindowTitle(f"EVE Translator v{__version__} - {session_label}")

        # The opacity setting applies to the background only; text stays
        # fully opaque so it remains readable over a busy game scene.
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)

        # Mouse Tracking for resize cursor
        self.setMouseTracking(True)
        
        # Interaction State
        self.resize_margin = RESIZE_MARGIN_PX
        self.min_width = 120
        self.min_height = 80
        self.is_moving = False
        self.has_moved = False
        self._move_button = None
        self.drag_start_pos = None
        # Native resizes and grip drags only produce resize events. Save the
        # final geometry so later config pushes cannot restore the old size.
        self._geometry_save_timer = QTimer(self)
        self._geometry_save_timer.setSingleShot(True)
        self._geometry_save_timer.setInterval(GEOMETRY_SAVE_DELAY_MS)
        self._geometry_save_timer.timeout.connect(self.save_config)

        # Resize Handle (Visual Grip)
        self.grip = QSizeGrip(self)
        self.grip.setStyleSheet("background-color: transparent;") # Optional: styling
        
        # Compact status header plus transcript. The transcript remains the
        # canonical model view, so changing mode never retranslates messages.
        self.central_container = QWidget(self)
        self.central_layout = QVBoxLayout(self.central_container)
        self.central_layout.setContentsMargins(0, 0, 0, 0)
        self.central_layout.setSpacing(0)
        self.status_header = QLabel(self.central_container)
        self.status_header.setFixedHeight(26)
        self.status_header.setTextFormat(Qt.TextFormat.RichText)
        self.status_header.setTextInteractionFlags(Qt.TextInteractionFlag.NoTextInteraction)
        # Clip long status text instead of letting it force the window wider.
        self.status_header.setSizePolicy(
            QSizePolicy.Policy.Ignored,
            QSizePolicy.Policy.Fixed,
        )
        self.status_header.setMinimumWidth(0)
        # The header is the natural drag handle for a frameless window.
        self.status_header.setCursor(Qt.CursorShape.SizeAllCursor)
        self.status_header.installEventFilter(self)
        self.central_layout.addWidget(self.status_header)

        # Text Browser Setup (Replaces ScrollArea + Widgets)
        self.text_browser = QTextBrowser()
        self.text_browser.setOpenExternalLinks(False) # Handle links manually if needed, or True
        self.text_browser.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)
        self.text_browser.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.text_browser.setFrameShape(QFrame.Shape.NoFrame)
        self.text_browser.setMouseTracking(True)
        self.text_browser.document().setMaximumBlockCount(MAX_VISIBLE_MESSAGES)
        self.text_browser.setUndoRedoEnabled(False)
        # Install event filter to capture mouse events if needed, 
        # though native resize handles most of it, we still need to set cursors manually
        # because the window is borderless.
        self.text_browser.installEventFilter(self)
        self.text_browser.viewport().installEventFilter(self)
        self.text_browser.anchorClicked.connect(self._handle_anchor)
        
        self.central_layout.addWidget(self.text_browser)
        self.setCentralWidget(self.central_container)
        
        # IMPORTANT: Raise grip to top so it's not covered by the text browser
        self.grip.raise_()

        # Default Config
        default_config = {
            'x': -1, 'y': -1, 'w': 600, 'h': 400,
            'opacity': 0.8,
            'font_size': 10,
            'color_default': '#e0e0e0',
            'color_translated': '#00ffff',
            'color_highlight': 'yellow',
            'auto_scroll': True,
            'ignored_languages': ['en', 'de'],
            'target_language': 'en',
            'background_color': (
                self.session_spec.background_color
                if self.session_spec is not None
                else '#101820'
            ),
            'display_mode': DISPLAY_MODE_MIRROR,
            'compact_header': True,
            'command_cards': True,
            'speaker_roles': {},
        }
        
        self.config = initial_config.copy() if initial_config else default_config
        self.chat_history = deque(maxlen=MAX_VISIBLE_MESSAGES)
        self._pending_messages = []
        self._style_state = {}
        self._needs_full_refresh = False
        self._flush_timer = QTimer(self)
        self._flush_timer.setSingleShot(True)
        self._flush_timer.setInterval(UI_FLUSH_INTERVAL_MS)
        self._flush_timer.setTimerType(Qt.TimerType.CoarseTimer)
        self._flush_timer.timeout.connect(self._flush_pending_messages)
        # Track state of all sessions for context menu checkmarks
        self.all_session_states = {}
        # Track available characters for Local selection
        self.available_characters = {}
        # Track available fleets for Fleet selection
        self.available_fleets = {}
        self.selected_fleet_id = None
        # Track arbitrary EVE chat sources for Any Chat selection.
        self.available_channels = {}
        self.selected_channel_id = self.config.get('channel_selection') or None
        self.interaction_locked = False
        self.overlay_status = OverlayStatus(
            channel=(
                self.session_spec.status_label
                if self.session_spec is not None
                else session_id.capitalize()
            ),
            display_mode=self.config.get('display_mode', DISPLAY_MODE_MIRROR),
        )
        
        self.apply_config()
        
        # Demo Label
        # Welcome Message
        eve_time = datetime.datetime.now(datetime.timezone.utc).strftime("%H:%M:%S")
        
        if session_id == 'local':
            char_name = html.escape(str(self.config.get('character_name') or 'Unknown Character'))
            system_name = html.escape(str(self.config.get('system_name') or 'Unknown System'))
            welcome_text = f"Listening to Local Chat for <b>{char_name}</b> in <b>{system_name}</b>."
        elif session_id == 'any':
            channel_name = html.escape(str(self.config.get('channel_name') or 'an arbitrary chat'))
            listener = html.escape(str(self.config.get('listener_name') or ''))
            suffix = f" for <b>{listener}</b>" if listener else ''
            welcome_text = f"Listening to <b>{channel_name}</b>{suffix}."
        else:
            welcome_text = f"Listening to {session_id.capitalize()} Chat."

        self.add_message(welcome_text, "System", eve_time)

    def update_session_states(self, states: dict):
        """Update knowledge of which sessions are enabled."""
        self.all_session_states = states

    def _default_position(self, screen_geo: QRect, w: int, h: int) -> tuple[int, int]:
        # Stagger sessions so default overlays do not cover each other.
        offset = (
            self.session_spec.position_offset
            if self.session_spec is not None
            else 150
        )
        x = screen_geo.x() + screen_geo.width() - w - 50 - offset
        y = screen_geo.y() + screen_geo.height() - h - 100 - offset
        return max(screen_geo.x(), x), max(screen_geo.y(), y)

    @staticmethod
    def _is_reachable(rect: QRect) -> bool:
        """Whether enough of the window is on a connected screen to grab it."""
        for screen in QApplication.screens():
            visible = screen.availableGeometry().intersected(rect)
            if (
                visible.width() >= MIN_VISIBLE_ON_SCREEN_PX
                and visible.height() >= min(MIN_VISIBLE_ON_SCREEN_PX, rect.height())
            ):
                return True
        return False

    def _font_pt(self, scale: float, minimum: int = 7) -> int:
        """Size secondary text relative to the configured overlay font."""
        try:
            base = int(self.config.get('font_size', 10))
        except (TypeError, ValueError):
            base = 10
        return max(minimum, round(base * scale))

    def apply_config(self):
        screen_geo = QApplication.primaryScreen().availableGeometry()
        needs_refresh = False

        # Apply Geometry
        x = self.config.get('x', -1)
        y = self.config.get('y', -1)
        w = self.config.get('w', 600)
        h = self.config.get('h', 400)

        if x == -1 or y == -1:
            x, y = self._default_position(screen_geo, w, h)
        elif not self._is_reachable(QRect(x, y, w, h)):
            # Saved on a monitor that is no longer connected. A frameless
            # tool window has no taskbar entry, so it would be unreachable.
            x, y = self._default_position(screen_geo, w, h)

        new_geometry = QRect(x, y, w, h)
        if self.geometry() != new_geometry:
            self._applying_config = True
            try:
                self.setGeometry(new_geometry)
            finally:
                self._applying_config = False

        # Opacity is background-only (see WA_TranslucentBackground).
        try:
            opacity = max(0.2, min(1.0, float(self.config.get('opacity', 0.8))))
        except (TypeError, ValueError):
            opacity = 0.8
        if abs(self.windowOpacity() - 1.0) > 0.001:
            self.setWindowOpacity(1.0)

        bg_color = self.config.get('background_color', 'black')
        background = _rgba(bg_color, opacity)
        header_pt = self._font_pt(0.85, minimum=8)
        # Add subtle border to visualize edges
        stylesheet = (
            f"QTextBrowser {{ background-color: {background}; "
            f"border: 1px solid rgba(68, 68, 68, 200); }}"
        )
        if self._style_state.get('stylesheet') != (stylesheet, header_pt):
            self.text_browser.setStyleSheet(stylesheet)
            self.central_container.setStyleSheet("background-color: transparent;")
            self.status_header.setStyleSheet(
                "QLabel {"
                f"background-color: {background}; color: #d8dde8; "
                "border: 1px solid rgba(68, 68, 68, 200); border-bottom: 0; "
                f"padding: 0 6px; font-size: {header_pt}pt; font-weight: 600;"
                "}"
            )
            header_font = QFont(self.status_header.font())
            header_font.setPointSize(header_pt)
            self.status_header.setFixedHeight(
                max(26, QFontMetrics(header_font).height() + 10)
            )

        # Apply Font
        font_size = self.config.get('font_size', 10)
        if self._style_state.get('font_size') != font_size:
            self.text_browser.setFont(QFont("Arial", font_size))
            needs_refresh = True

        color_state = (
            self.config.get('color_default', '#e0e0e0'),
            self.config.get('color_translated', '#00ffff'),
            self.config.get('color_highlight', 'yellow'),
        )
        if self._style_state.get('colors') != color_state:
            needs_refresh = True

        display_mode = str(
            self.config.get('display_mode', DISPLAY_MODE_MIRROR)
        ).strip().lower()
        if display_mode not in DISPLAY_MODES:
            display_mode = DISPLAY_MODE_MIRROR
            self.config['display_mode'] = display_mode
        compact_header = bool(self.config.get('compact_header', True))
        self.status_header.setVisible(compact_header)
        if self._style_state.get('display_mode') != display_mode:
            needs_refresh = True

        self._style_state = {
            'stylesheet': (stylesheet, header_pt),
            'font_size': font_size,
            'colors': color_state,
            'display_mode': display_mode,
            'compact_header': compact_header,
        }
        self.overlay_status = replace(
            self.overlay_status,
            display_mode=display_mode,
            interaction_locked=self.interaction_locked,
        )
        self._render_status_header()

        if needs_refresh:
            if self.isVisible():
                self.refresh_ui()
            else:
                self._needs_full_refresh = True

    @property
    def display_mode(self) -> str:
        mode = str(self.config.get('display_mode', DISPLAY_MODE_MIRROR)).lower()
        return mode if mode in DISPLAY_MODES else DISPLAY_MODE_MIRROR

    def set_display_mode(self, mode: str, *, save: bool = True) -> None:
        normalized = str(mode or '').strip().lower()
        if normalized not in DISPLAY_MODES or normalized == self.display_mode:
            return
        self.config['display_mode'] = normalized
        self.overlay_status = replace(self.overlay_status, display_mode=normalized)
        self.refresh_ui()
        self._render_status_header()
        if save:
            self.save_config()

    def set_interaction_locked(self, locked: bool) -> None:
        """Apply real OS-level input pass-through without stealing focus."""
        locked = bool(locked)
        if locked == self.interaction_locked:
            return
        geometry = self.geometry()
        was_visible = self.isVisible()
        mask = (
            Qt.WindowType.WindowTransparentForInput
            | Qt.WindowType.WindowDoesNotAcceptFocus
        )
        flags = self.windowFlags()
        flags = flags | mask if locked else flags & ~mask
        self._applying_config = True
        try:
            self.setWindowFlags(flags)
            self.setGeometry(geometry)
        finally:
            self._applying_config = False
        self.interaction_locked = locked
        self.grip.setVisible(not locked)
        self.overlay_status = replace(self.overlay_status, interaction_locked=locked)
        self._render_status_header()
        if was_visible:
            self.show()

    def _render_status_header(self) -> None:
        status = self.overlay_status
        health_color = '#67d391' if status.provider_available else '#ff8c69'
        lock = '&#128274;' if status.interaction_locked else '&#128275;'
        context = f" &middot; {html.escape(status.context)}" if status.context else ''
        problem = ''
        if not status.provider_available and status.detail:
            # Show the provider problem once here instead of on every line;
            # the locked (click-through) HUD cannot show a tooltip.
            short = status.detail.split(';', 1)[0].rstrip('.')
            if len(short) > 40:
                short = short[:39].rstrip() + '…'
            problem = (
                f" <span style='color:{health_color};'>&#9888; "
                f"{html.escape(short)}</span>"
            )
        label = (
            f"{html.escape(status.channel.upper())} &middot; "
            f"{html.escape(status.state)}{context} &middot; "
            f"{html.escape(status.display_mode.title())} &middot; {lock} "
            f"<span style='color:{health_color};'>&#9679;</span>{problem}"
        )
        self.status_header.setText(label)
        detail = status.detail or f"Translation provider: {status.provider}"
        self.status_header.setToolTip(detail)
        self.setToolTip(detail)

    def _update_config_from_geometry(self):
        geo = self.geometry()
        self.config['x'] = geo.x()
        self.config['y'] = geo.y()
        self.config['w'] = geo.width()
        self.config['h'] = geo.height()

    def save_config(self):
        self._geometry_save_timer.stop()
        self._update_config_from_geometry()
        # Emit signal to notify manager
        self.config_updated.emit(self.config)

    def get_current_config(self):
        self._update_config_from_geometry()
        return self.config

    def closeEvent(self, event):
        self._geometry_save_timer.stop()
        self.save_config()
        super().closeEvent(event)

    def showEvent(self, event):
        super().showEvent(event)
        # Refresh UI when shown to catch up on any messages missed while hidden
        self.refresh_ui()

    def _schedule_geometry_save(self):
        if self.isVisible() and not getattr(self, '_applying_config', False):
            self._geometry_save_timer.start()

    def resizeEvent(self, event):
        rect = self.rect()
        # Position grip at bottom right
        self.grip.move(rect.right() - self.grip.width(), rect.bottom() - self.grip.height())
        self._update_config_from_geometry()
        self._schedule_geometry_save()
        super().resizeEvent(event)

    def moveEvent(self, event):
        self._update_config_from_geometry()
        self._schedule_geometry_save()
        super().moveEvent(event)

    def nativeEvent(self, event_type, message):
        """Give the frameless window real Windows resize borders.

        Answering ``WM_NCHITTEST`` with an ``HT*`` border code lets Windows
        show the resize cursor and run the resize loop on every edge, with
        correct DPI handling, regardless of which child widget is underneath.
        """
        if sys.platform == 'win32' and not self.interaction_locked:
            try:
                raw_type = (
                    event_type.data()
                    if hasattr(event_type, 'data')
                    else bytes(event_type)
                )
                if raw_type == b'windows_generic_MSG':
                    from ctypes import wintypes

                    msg = wintypes.MSG.from_address(int(message))
                    if msg.message == WM_NCHITTEST:
                        rect = wintypes.RECT()
                        if ctypes.windll.user32.GetWindowRect(
                            msg.hWnd,
                            ctypes.byref(rect),
                        ):
                            x = ctypes.c_short(msg.lParam & 0xFFFF).value
                            y = ctypes.c_short((msg.lParam >> 16) & 0xFFFF).value
                            margin = max(
                                1,
                                round(self.resize_margin * self.devicePixelRatioF()),
                            )
                            hit = native_resize_hit_test(
                                x, y,
                                rect.left, rect.top, rect.right, rect.bottom,
                                margin,
                            )
                            if hit is not None:
                                return True, hit
            except Exception:
                pass
        return super().nativeEvent(event_type, message)

    def check_edge(self, pos):
        """Returns Qt.Edge identifier (combined for corners) or None.

        ``pos`` must be in this window's coordinates; child widget events are
        mapped with :meth:`mapFromGlobal` first.
        """
        r = self.rect()
        m = self.resize_margin
        
        on_left = pos.x() < m
        on_right = pos.x() > r.width() - m
        on_top = pos.y() < m
        on_bottom = pos.y() > r.height() - m
        
        if on_top and on_left: return Qt.Edge.TopEdge | Qt.Edge.LeftEdge
        if on_top and on_right: return Qt.Edge.TopEdge | Qt.Edge.RightEdge
        if on_bottom and on_right: return Qt.Edge.BottomEdge | Qt.Edge.RightEdge
        if on_bottom and on_left: return Qt.Edge.BottomEdge | Qt.Edge.LeftEdge
        if on_top: return Qt.Edge.TopEdge
        if on_bottom: return Qt.Edge.BottomEdge
        if on_left: return Qt.Edge.LeftEdge
        if on_right: return Qt.Edge.RightEdge
        return None

    def _edge_at_global(self, global_pos):
        """Resize edge under a global point, in this window's coordinates."""
        return self.check_edge(self.mapFromGlobal(global_pos))

    def eventFilter(self, source, event):
        event_type = event.type()
        if event_type == QEvent.Type.MouseButtonPress and isinstance(event, QMouseEvent):
            global_pos = event.globalPosition().toPoint()
            if event.button() == Qt.MouseButton.LeftButton:
                # Fallback for platforms without the native WM_NCHITTEST path.
                # Child events arrive in child coordinates, so map globally.
                edge = self._edge_at_global(global_pos)
                if edge:
                    handle = self.windowHandle()
                    if handle is not None:
                        handle.startSystemResize(edge)
                    return True
                if source is self.status_header:
                    self._begin_move(global_pos, Qt.MouseButton.LeftButton)
                    return True
            # Right Click: Manual Move (to preserve Context Menu on Release)
            if event.button() == Qt.MouseButton.RightButton:
                self._begin_move(global_pos, Qt.MouseButton.RightButton)
                return True

        elif event_type == QEvent.Type.MouseMove and isinstance(event, QMouseEvent):
            global_pos = event.globalPosition().toPoint()
            if self.is_moving:
                diff = global_pos - self.drag_start_pos
                if diff.manhattanLength() > 5:
                    self.has_moved = True
                    self.move(self.pos() + diff)
                    self.drag_start_pos = global_pos
                return True

            edge = self._edge_at_global(global_pos)
            if edge:
                diagonal_f = (Qt.Edge.TopEdge | Qt.Edge.LeftEdge, Qt.Edge.BottomEdge | Qt.Edge.RightEdge)
                diagonal_b = (Qt.Edge.TopEdge | Qt.Edge.RightEdge, Qt.Edge.BottomEdge | Qt.Edge.LeftEdge)
                if edge in diagonal_f:
                    self.setCursor(Qt.CursorShape.SizeFDiagCursor)
                elif edge in diagonal_b:
                    self.setCursor(Qt.CursorShape.SizeBDiagCursor)
                elif edge in (Qt.Edge.TopEdge, Qt.Edge.BottomEdge):
                    self.setCursor(Qt.CursorShape.SizeVerCursor)
                else:
                    self.setCursor(Qt.CursorShape.SizeHorCursor)
                return True
            self.unsetCursor()

        elif event_type == QEvent.Type.MouseButtonRelease and isinstance(event, QMouseEvent):
            if self.is_moving and event.button() == self._move_button:
                self.is_moving = False
                if self.has_moved:
                    self.save_config()
                elif event.button() == Qt.MouseButton.RightButton:
                    self.show_context_menu()
                return True

        return super().eventFilter(source, event)

    def _begin_move(self, global_pos, button):
        self.is_moving = True
        self.has_moved = False
        self._move_button = button
        self.drag_start_pos = global_pos

    def show_context_menu(self):
        menu = QMenu(self)
        
        # Session Management Submenu
        menu_session = menu.addMenu("Overlays")
        
        for spec in SESSION_SPECS:
            action = QAction(spec.label, self, checkable=True)
            action.setChecked(
                bool(self.all_session_states.get(spec.session_id, False))
            )
            action.triggered.connect(
                lambda _checked=False, target=spec.session_id: (
                    self.session_toggled.emit(target)
                )
            )
            menu_session.addAction(action)
        
        # Character Selection Submenu (Local Only)
        if self.session_id == 'local':
            menu.addSeparator()
            menu_chars = menu.addMenu("Select Character")

            # Current selection
            current_char_id = self.config.get('character_id')

            if self.available_characters:
                # Sort by name
                sorted_chars = sorted(
                    self.available_characters.values(),
                    key=lambda x: x.character_name
                )

                for char_info in sorted_chars:
                    char_name = char_info.character_name
                    system = f" ({char_info.system_name})" if char_info.system_name else ""
                    is_active = " [Offline]" if not char_info.is_active else ""
                    # Or maybe use checkmark for selection?

                    label = f"{char_name}{system}{is_active}"
                    action = QAction(label, self, checkable=True)
                    action.setChecked(char_info.character_id == current_char_id)
                    action.triggered.connect(lambda checked, cid=char_info.character_id: self.character_selected.emit(cid))
                    menu_chars.addAction(action)
            else:
                act = QAction("No characters detected", self)
                act.setEnabled(False)
                menu_chars.addAction(act)

        # Fleet Selection Submenu (Fleet Only)
        if self.session_id == 'fleet':
            menu.addSeparator()
            menu_fleets = menu.addMenu("Switch Fleet")

            # Current selection
            current_fleet_id = getattr(self, 'selected_fleet_id', None)

            if self.available_fleets:
                # Sort by creation time (newest first)
                sorted_fleets = sorted(
                    self.available_fleets.values(),
                    key=lambda x: x.created_time,
                    reverse=True
                )

                for fleet_info in sorted_fleets:
                    # Format time ID
                    # EVE time, like chat timestamps and the Any Chat picker.
                    dt = datetime.datetime.fromtimestamp(
                        fleet_info.created_time,
                        datetime.timezone.utc,
                    )
                    time_str = dt.strftime("%H:%M")

                    label = f"Fleet - {fleet_info.listener_name} [{time_str} EVE]"
                    is_inactive = " [Inactive]" if not fleet_info.is_active else ""
                    label += is_inactive

                    action = QAction(label, self, checkable=True)
                    action.setChecked(fleet_info.fleet_id == current_fleet_id)
                    action.triggered.connect(lambda checked, fid=fleet_info.fleet_id: self.fleet_selected.emit(fid))
                    menu_fleets.addAction(action)
            else:
                act = QAction("No active fleets detected", self)
                act.setEnabled(False)
                menu_fleets.addAction(act)

        if self.session_id == 'any':
            menu.addSeparator()
            menu_channels = menu.addMenu("Switch Chat")
            current_channel_id = getattr(self, 'selected_channel_id', None)

            selectable_channels = list(self.available_channels.values())
            if selectable_channels:
                sorted_channels = sorted(
                    selectable_channels,
                    key=lambda item: (
                        not bool(item.is_active),
                        item.channel_name.casefold(),
                        item.listener_name.casefold(),
                        -float(item.started_time),
                    ),
                )
                for channel in sorted_channels:
                    started = datetime.datetime.fromtimestamp(
                        channel.started_time,
                        datetime.timezone.utc,
                    ).strftime("%m-%d %H:%M")
                    label = (
                        f"{channel.channel_name} - {channel.listener_name} "
                        f"[{started} EVE]"
                    )
                    if not channel.is_active:
                        label += " [Offline]"
                    action = QAction(label, self, checkable=True)
                    action.setChecked(
                        channel.selection_id == current_channel_id
                    )
                    action.triggered.connect(
                        lambda _checked=False, selection=channel.selection_id: (
                            self.channel_selected.emit(selection)
                        )
                    )
                    menu_channels.addAction(action)
            else:
                action = QAction("No chat logs detected", self)
                action.setEnabled(False)
                menu_channels.addAction(action)

        menu.addSeparator()

        mode_menu = menu.addMenu("Display mode")
        for mode, label in (
            (DISPLAY_MODE_COMBAT, "Combat"),
            (DISPLAY_MODE_BILINGUAL, "Bilingual"),
            (DISPLAY_MODE_MIRROR, "Mirror"),
        ):
            action = QAction(label, self, checkable=True)
            action.setChecked(self.display_mode == mode)
            action.triggered.connect(
                lambda _checked=False, selected=mode: self.set_display_mode(selected)
            )
            mode_menu.addAction(action)

        recent_speaker = next(
            (
                item.get('sender', '')
                for item in reversed(self.chat_history)
                if item.get('sender')
                and normalize_speaker_name(item.get('sender')) not in {'system', 'eve system'}
            ),
            '',
        )
        if recent_speaker:
            roles_menu = menu.addMenu(f"Speaker: {recent_speaker}")
            self._populate_speaker_role_menu(roles_menu, recent_speaker)

        recent_translation = next(
            (
                item for item in reversed(self.chat_history)
                if item.get('state') == TranslationState.TRANSLATED.value
                and item.get('original_text')
            ),
            None,
        )
        if recent_translation:
            correction_action = QAction("Improve last translation…", self)
            correction_action.triggered.connect(
                lambda: self._request_correction(recent_translation)
            )
            menu.addAction(correction_action)

        menu.addSeparator()

        action_settings = QAction("Settings", self)
        action_settings.triggered.connect(self.open_settings)
        menu.addAction(action_settings)
        
        action_export = QAction("Export recent history to txt", self)
        action_export.triggered.connect(self.export_chat)
        menu.addAction(action_export)
        
        menu.addSeparator()
        
        action_exit = QAction("Exit", self)
        action_exit.triggered.connect(self.exit_requested.emit)
        menu.addAction(action_exit)
        
        # Show at cursor
        menu.exec(QCursor.pos())

    def _populate_speaker_role_menu(self, menu: QMenu, sender: str) -> None:
        current = str(self.config.get('speaker_roles', {}).get(
            normalize_speaker_name(sender), ''
        )).lower()
        for role, label in (
            ('fc', 'Mark as FC'),
            ('scout', 'Mark as Scout'),
            ('starred', 'Star speaker'),
            ('', 'Remove role'),
        ):
            action = QAction(label, self, checkable=bool(role))
            action.setChecked(bool(role) and current == role)
            action.triggered.connect(
                lambda _checked=False, value=role: self._set_speaker_role(sender, value)
            )
            menu.addAction(action)

    def _set_speaker_role(self, sender: str, role: str) -> None:
        normalized = normalize_speaker_name(sender)
        if not normalized or normalized in {'system', 'eve system'}:
            return
        roles = dict(self.config.get('speaker_roles', {}))
        if role:
            roles[normalized] = role
        else:
            roles.pop(normalized, None)
        self.config['speaker_roles'] = roles
        self.speaker_role_changed.emit(sender, role)
        self.refresh_ui()

    def _handle_anchor(self, url: QUrl) -> None:
        encoded = url.toString()
        if encoded.startswith('speaker:'):
            sender = unquote(encoded.split(':', 1)[1])
            menu = QMenu(self)
            self._populate_speaker_role_menu(menu, sender)
            menu.exec(QCursor.pos())
        elif encoded.startswith('correct:'):
            message_id = unquote(encoded.split(':', 1)[1])
            message = next(
                (
                    item for item in self.chat_history
                    if str(item.get('message_id')) == message_id
                ),
                None,
            )
            if message:
                self._request_correction(message)

    def _request_correction(self, message: dict) -> None:
        original = self._strip_html(message.get('original_text') or '')
        current = self._strip_html(message.get('text') or '')
        if not original:
            return
        corrected, accepted = QInputDialog.getMultiLineText(
            self,
            "Use a better translation",
            f"Exact correction for:\n{original}",
            current,
        )
        corrected = corrected.strip()
        if not accepted or not corrected:
            return
        payload = {
            'message_id': message.get('message_id'),
            'source_text': message.get('lookup_text') or original,
            'translated_text': corrected,
            'source_language': message.get('detected_language', 'unknown'),
            'target_language': self.config.get('target_language', 'en'),
        }
        self.correction_submitted.emit(payload)

    def open_settings(self):
        self.request_settings.emit()

    def preview_settings(self, new_config):
        # Apply incoming settings temporarily through the normal path, which
        # handles background-only opacity and font-relative sizes.
        self.config.update(new_config)
        self.apply_config()
        self.refresh_ui()

    def export_chat(self):
        filename, _ = QFileDialog.getSaveFileName(self, "Export Chat", "", "Text Files (*.txt)")
        if filename:
            try:
                with open(filename, 'w', encoding='utf-8') as f:
                    for msg in self.chat_history:
                        line = f"[{msg['timestamp']}] {msg['sender']} > "
                        
                        clean_text = self._strip_html(msg['text'])
                        
                        state = msg.get('state')
                        if msg['is_translated']:
                            clean_orig = self._strip_html(msg['original_text'])
                            line += f"{clean_text} ({clean_orig})"
                        else:
                            line += f"{clean_text}"

                        if state == TranslationState.PENDING.value:
                            line += " [translation pending]"
                        elif state == TranslationState.FAILED.value:
                            line += f" [{msg.get('error') or 'translation unavailable'}]"
                            
                        f.write(line + "\n")
                QMessageBox.information(self, "Export", "Chat history exported successfully.")
            except Exception as e:
                QMessageBox.warning(self, "Export Error", str(e))

    def _strip_html(self, text: str) -> str:
        """Remove HTML tags and unescape entities."""
        if not text:
            return ""
        # Strip tags: look for <...>
        # Non-greedy or greedy? <[^>]+> is fairly standard for simple tags.
        text = re.sub(r'<[^>]+>', '', text)
        # Unescape entities (&lt; -> <, etc)
        text = html.unescape(text)
        return text

    def set_styling(self):
        # Semi-transparent background for readability? 
        pass

    def refresh_ui(self):
        # Rebuild full HTML from history
        self._pending_messages.clear()
        if self._flush_timer.isActive():
            self._flush_timer.stop()

        preserve_reader_position = not self.config.get('auto_scroll', True)
        scroll_bar = self.text_browser.verticalScrollBar()
        scroll_value = scroll_bar.value()
        old_cursor = self.text_browser.textCursor()
        cursor_position = old_cursor.position()
        cursor_anchor = old_cursor.anchor()

        full_html = "".join(
            self._format_message_html(msg_data)
            for msg_data in self.chat_history
            if self._message_visible(msg_data)
        )
            
        self.text_browser.setHtml(full_html)
        self._needs_full_refresh = False
        
        # Scroll to bottom if needed
        if self.config.get('auto_scroll', True):
            self.text_browser.moveCursor(QTextCursor.MoveOperation.End)
        elif preserve_reader_position:
            max_position = max(0, self.text_browser.document().characterCount() - 1)
            restored_cursor = self.text_browser.textCursor()
            restored_cursor.setPosition(min(cursor_anchor, max_position))
            restored_cursor.setPosition(
                min(cursor_position, max_position),
                QTextCursor.MoveMode.KeepAnchor,
            )
            self.text_browser.setTextCursor(restored_cursor)
            scroll_bar.setValue(min(scroll_value, scroll_bar.maximum()))

    def _queue_message_render(self, msg_data):
        if not self._message_visible(msg_data):
            return
        self._pending_messages.append(msg_data)
        if len(self._pending_messages) > MAX_VISIBLE_MESSAGES:
            self._pending_messages = list(self.chat_history)
        if not self._flush_timer.isActive():
            self._flush_timer.start()

    def _flush_pending_messages(self):
        if not self._pending_messages and not self._needs_full_refresh:
            return

        if not self.isVisible():
            self._pending_messages.clear()
            self._needs_full_refresh = True
            return

        if self._needs_full_refresh:
            self.refresh_ui()
            return

        pending = self._pending_messages
        self._pending_messages = []
        self.text_browser.setUpdatesEnabled(False)
        try:
            for msg_data in pending:
                self.text_browser.append(self._format_message_html(msg_data))
        finally:
            self.text_browser.setUpdatesEnabled(True)
            
        if self.config.get('auto_scroll', True):
             self.text_browser.moveCursor(QTextCursor.MoveOperation.End)

    def _role_for(self, msg_data: dict) -> str:
        roles = self.config.get('speaker_roles', {})
        if isinstance(roles, dict):
            # Current local configuration is authoritative. Falling back to
            # the role captured when the line arrived would make revocation
            # ineffective for existing cards/history.
            configured = roles.get(
                normalize_speaker_name(msg_data.get('sender', '')),
                '',
            )
            return str(configured or '').lower()
        return str(msg_data.get('speaker_role') or '').lower()

    @staticmethod
    def _command_code(command) -> str:
        if isinstance(command, dict):
            return str(command.get('code', ''))
        return str(getattr(command, 'code', ''))

    @staticmethod
    def _command_category(command) -> str:
        if isinstance(command, dict):
            return str(command.get('category', ''))
        return str(getattr(command, 'category', ''))

    def _message_visible(self, msg_data: dict) -> bool:
        mode = self.display_mode
        if mode == DISPLAY_MODE_MIRROR:
            return True
        role = self._role_for(msg_data)
        starred = role in {'fc', 'scout', 'starred'}
        commands = tuple(msg_data.get('commands') or ())
        if mode == DISPLAY_MODE_BILINGUAL:
            return bool(
                msg_data.get('translation_relevant')
                or starred
                or commands
                or msg_data.get('state') in {
                    TranslationState.PENDING.value,
                    TranslationState.FAILED.value,
                    TranslationState.TRANSLATED.value,
                }
            )
        if mode == DISPLAY_MODE_COMBAT:
            trusted_command = any(
                command_is_trusted(
                    role,
                    self._command_code(command),
                    self._command_category(command),
                )
                for command in commands
            )
            return starred or trusted_command
        return True

    @staticmethod
    def _message_anchor(message_id) -> str:
        return f"msg-{quote(str(message_id), safe='')}" if message_id else ""

    def _format_message_html(self, msg_data):
        """Helper to format a single message dict into HTML string."""
        inner = self._format_message_inner(msg_data)
        if inner is None:
            return self._format_message_card_html(msg_data)
        inner_html, margin = inner
        return f"<div style='margin-bottom: {margin}px;'>{inner_html}</div>"

    def _format_message_card_html(self, msg_data):
        """Command-card rendering (one or more blocks per message)."""
        return self._format_message_parts(msg_data, cards=True)

    def _format_message_inner(self, msg_data):
        """Return ``(inline_html, margin_px)`` for a one-block message.

        Command cards span several blocks and return ``None``; they are
        rendered by :meth:`_format_message_card_html`.
        """
        commands = tuple(msg_data.get('commands') or ())
        if commands and self.config.get('command_cards', True):
            return None
        return self._format_message_parts(msg_data, cards=False)

    def _format_message_parts(self, msg_data, *, cards):
        col_def = self.config.get('color_default', '#e0e0e0')
        col_trans = self.config.get('color_translated', '#00ffff')
        col_high = self.config.get('color_highlight', 'yellow')

        timestamp = html.escape(str(msg_data.get('timestamp', '')))
        sender_raw = str(msg_data.get('sender', ''))
        sender = html.escape(sender_raw)
        sender_link = quote(sender_raw, safe='')
        role = self._role_for(msg_data)
        small_pt = self._font_pt(0.8)
        role_badge = (
            f" <span style='color:#ffd166;font-size:{small_pt}pt;'>"
            f"[{html.escape(role.upper())}]</span>"
            if role else ''
        )
        sender_html = (
            f"<a href='speaker:{sender_link}' style='color:{col_def};text-decoration:none;'>"
            f"{sender}</a>{role_badge}"
        )
        state = msg_data.get('state')
        if not state:
            state = (
                TranslationState.TRANSLATED.value
                if msg_data.get('is_translated')
                else TranslationState.ORIGINAL.value
            )
        # Safe to replace on copy
        text = str(msg_data.get('text') or '').replace(
            "color: yellow", f"color: {col_high}"
        )

        commands = tuple(msg_data.get('commands') or ())
        if cards:
            return self._format_command_cards(
                msg_data,
                commands,
                state,
                timestamp,
                sender_html,
                role,
                text,
                col_def,
                col_trans,
                col_high,
            )

        # A named anchor lets a later translation update find this block.
        anchor = self._message_anchor(msg_data.get('message_id'))
        stamp = (
            f"<a name='{anchor}'>[{timestamp}]</a>" if anchor else f"[{timestamp}]"
        )

        if state == TranslationState.PENDING.value:
            return (
                f"<span style='color: {col_def};'>{stamp} {sender_html} &gt; {text}</span> "
                f"<span style='color: #b8b8b8;'><i>translating&#8230;</i></span>",
                2,
            )

        if state == TranslationState.FAILED.value:
            # Compact marker; the reason is in the tooltip and, for provider
            # outages, once in the status header instead of on every line.
            error = html.escape(
                msg_data.get('error') or 'Translation unavailable',
                quote=True,
            )
            return (
                f"<span style='color: {col_def};'>{stamp} {sender_html} &gt; {text}</span> "
                f"<span style='color: #ffbf69;' title=\"{error}\">"
                f"&#9888; not translated</span>",
                2,
            )

        if state == TranslationState.TRANSLATED.value:
            original = msg_data['original_text']
            # Ensure original is not None before replace (though unlikely if is_translated)
            if original:
                original = original.replace("color: yellow", f"color: {col_high}")
            else:
                original = ""

            message_id = quote(str(msg_data.get('message_id') or ''), safe='')
            correction_link = (
                f" <a href='correct:{message_id}' style='color:#9ca3af;text-decoration:none;'"
                f" title='Save an exact local correction'>&#9998;</a>"
                if msg_data.get('message_id') else ''
            )
            return (
                f"<span style='color: {col_def};'>{stamp} {sender_html} &gt; </span>"
                f"<span style='color: {col_trans};'>{text}</span>{correction_link}<br>"
                f"<span style='color: #9ca3af;font-size:{small_pt}pt;margin-left:8px;'>"
                f"&#8618; {original}</span>",
                3,
            )
        return (
            f"<span style='color: {col_def};'>{stamp} {sender_html} &gt; </span>"
            f"<span style='color: {col_def};'>{text}</span>",
            2,
        )

    @staticmethod
    def _command_field(command, name: str, default=''):
        if isinstance(command, dict):
            return command.get(name, default)
        return getattr(command, name, default)

    def _format_command_cards(
        self,
        msg_data,
        commands,
        state,
        timestamp,
        sender_html,
        role,
        text,
        col_def,
        col_trans,
        col_high,
    ) -> str:
        cards = []
        original = str(msg_data.get('original_text') or msg_data.get('text') or '')
        original = original.replace("color: yellow", f"color: {col_high}")
        small_pt = self._font_pt(0.8)
        label_pt = self._font_pt(1.05, minimum=9)
        translated_pt = self._font_pt(0.85)
        # The card already states the command; repeating an original that is
        # in the reader's own language only takes space.
        detected = normalize_language_code(msg_data.get('detected_language'), '')
        target = normalize_language_code(self.config.get('target_language', 'en'), 'en')
        show_original = not (detected and detected == target)
        for command in commands[:3]:
            code = str(self._command_field(command, 'code')).upper()
            category = str(self._command_field(command, 'category', 'COMMAND')).upper()
            display_label = str(
                self._command_field(command, 'display_label', '')
                or code.replace('_', ' ')
            )
            argument = str(self._command_field(command, 'argument', '') or '')
            trusted = command_is_trusted(role, code, category)
            border = '#f4c95d' if trusted else '#697386'
            trust_label = (
                f"{role.upper()} VERIFIED"
                if trusted and role in {'fc', 'scout'}
                else 'UNVERIFIED'
            )
            trust_color = '#f4c95d' if trusted else '#9ca3af'
            argument_html = (
                f" <span style='color:#ffffff;font-size:{label_pt}pt;'>{html.escape(argument)}</span>"
                if argument else ''
            )
            state_note = ''
            if state == TranslationState.PENDING.value:
                state_note = " <span style='color:#b8b8b8;'><i>translating&#8230;</i></span>"
            elif state == TranslationState.FAILED.value:
                error = html.escape(
                    msg_data.get('error') or 'Translation unavailable',
                    quote=True,
                )
                state_note = (
                    f" <span style='color:#ffbf69;' title=\"{error}\">"
                    "&#9888; not translated</span>"
                )
            translated_line = ''
            if state == TranslationState.TRANSLATED.value:
                translated_line = (
                    f"<div style='color:{col_trans};font-size:{translated_pt}pt;margin-top:2px;'>"
                    f"{text}</div>"
                )
            original_line = (
                f"<div style='color:#9ca3af;font-size:{small_pt}pt;'>&#8618; {original}</div>"
                if show_original or translated_line
                else ''
            )
            cards.append(
                f"<div style='border-left:4px solid {border};background:#111827;"
                "padding:5px 7px;margin:3px 1px;'>"
                f"<div><span style='color:{trust_color};font-size:{small_pt}pt;font-weight:700;'>"
                f"{html.escape(category)} &middot; {trust_label}</span> "
                f"<span style='color:#f9fafb;font-size:{label_pt}pt;font-weight:700;'>"
                f"{html.escape(display_label)}</span>{argument_html}{state_note}</div>"
                f"<div style='color:{col_def};font-size:{small_pt}pt;'>[{timestamp}] {sender_html}</div>"
                f"{translated_line}"
                f"{original_line}"
                "</div>"
            )
        return ''.join(cards)

    def add_message(self, text: str, sender: str, timestamp: str, original_text: str = None, is_translated: bool = False):
        msg_data = {
            'message_id': None,
            'text': text,
            'sender': sender,
            'timestamp': timestamp,
            'original_text': original_text,
            'is_translated': is_translated,
            'state': (
                TranslationState.TRANSLATED.value
                if is_translated
                else TranslationState.ORIGINAL.value
            ),
            'provider': '',
            'detected_language': 'unknown',
            'error': '',
            'translation_relevant': bool(is_translated),
            'speaker_role': self._role_for({'sender': sender}),
            'commands': (),
            'revision': 0,
            'lookup_text': self._strip_html(original_text or text),
        }
        self.chat_history.append(msg_data)
        
        # Optimization: Skip rendering if window is hidden
        if not self.isVisible():
             self._needs_full_refresh = True
             return

        self._queue_message_render(msg_data)

    def upsert_processed_message(self, message: ProcessedMessage):
        """Append a new chat line or replace its pending state in place."""
        msg_data = {
            'message_id': message.message_id,
            'text': message.text,
            'sender': message.sender,
            'timestamp': message.timestamp,
            'original_text': message.original_text,
            'is_translated': message.state == TranslationState.TRANSLATED,
            'state': message.state.value,
            'provider': message.provider,
            'detected_language': message.detected_language,
            'error': message.error,
            'translation_relevant': message.translation_relevant,
            'speaker_role': message.speaker_role,
            'commands': message.commands,
            'revision': message.revision,
            'lookup_text': message.lookup_text,
        }

        existing_index = next(
            (
                index
                for index, existing in enumerate(self.chat_history)
                if existing.get('message_id') == message.message_id
            ),
            None,
        )

        if existing_index is None:
            self.chat_history.append(msg_data)
            if not self.isVisible():
                self._needs_full_refresh = True
                return
            self._queue_message_render(msg_data)
            return

        existing = self.chat_history[existing_index]
        state_rank = {
            TranslationState.ORIGINAL.value: 1,
            TranslationState.PENDING.value: 2,
            TranslationState.FAILED.value: 3,
            TranslationState.TRANSLATED.value: 3,
        }
        existing_revision = int(existing.get('revision') or 0)
        incoming_revision = int(msg_data.get('revision') or 0)
        if incoming_revision < existing_revision:
            return
        if (
            incoming_revision == existing_revision
            and state_rank.get(msg_data['state'], 0) < state_rank.get(existing.get('state'), 0)
        ):
            return
        self.chat_history[existing_index] = msg_data
        if not self.isVisible():
            self._needs_full_refresh = True
            return
        was_visible = self._message_visible(existing)
        now_visible = self._message_visible(msg_data)
        if not was_visible and not now_visible:
            # Hidden in this display mode (e.g. Combat): nothing to redraw.
            return
        if was_visible and now_visible and self._replace_message_in_place(existing, msg_data):
            return
        self._needs_full_refresh = True
        if not self._flush_timer.isActive():
            self._flush_timer.start()

    def _replace_message_in_place(self, existing: dict, msg_data: dict) -> bool:
        """Redraw one already-rendered message without rebuilding the view.

        A translation update otherwise re-rendered all (up to 100) messages
        via ``setHtml`` (~10-15 ms per overlay), and could move the reader's
        scroll position. Returns ``False`` when a full refresh is required.
        """
        if self._needs_full_refresh:
            return False
        message_id = msg_data.get('message_id')
        # Not flushed yet: the pending render will use the new state.
        for index, pending in enumerate(self._pending_messages):
            if pending.get('message_id') == message_id:
                self._pending_messages[index] = msg_data
                return True
        old_inner = self._format_message_inner(existing)
        new_inner = self._format_message_inner(msg_data)
        if old_inner is None or new_inner is None:
            return False  # command cards span several blocks
        block = self._find_message_block(message_id)
        if block is None:
            return False
        scroll_bar = self.text_browser.verticalScrollBar()
        at_bottom = scroll_bar.value() >= scroll_bar.maximum() - 2
        scroll_value = scroll_bar.value()
        cursor = QTextCursor(block)
        cursor.movePosition(QTextCursor.MoveOperation.StartOfBlock)
        cursor.movePosition(
            QTextCursor.MoveOperation.EndOfBlock,
            QTextCursor.MoveMode.KeepAnchor,
        )
        cursor.insertHtml(new_inner[0])
        if self.config.get('auto_scroll', True) and at_bottom:
            scroll_bar.setValue(scroll_bar.maximum())
        else:
            scroll_bar.setValue(min(scroll_value, scroll_bar.maximum()))
        return True

    def _find_message_block(self, message_id):
        """Find the rendered block of a message, searching from the newest."""
        anchor = self._message_anchor(message_id)
        if not anchor:
            return None
        block = self.text_browser.document().lastBlock()
        checked = 0
        while block.isValid() and checked <= MAX_VISIBLE_MESSAGES + 10:
            fragments = block.begin()
            # The anchor sits on the timestamp at the start of the block.
            for _ in range(3):
                if fragments.atEnd():
                    break
                if anchor in fragments.fragment().charFormat().anchorNames():
                    return block
                fragments += 1
            block = block.previous()
            checked += 1
        return None

    def update_status(self, status):
        if isinstance(status, OverlayStatus):
            self.overlay_status = status
        elif isinstance(status, dict):
            values = {
                field: status[field]
                for field in OverlayStatus.__dataclass_fields__
                if field in status
            }
            self.overlay_status = replace(self.overlay_status, **values)
        else:
            self.overlay_status = replace(
                self.overlay_status,
                detail=str(status or ''),
            )
        self._render_status_header()

    def update_character_list(self, character_registry: dict):
        """Update list of available characters for context menu."""
        self.available_characters = character_registry

    def update_fleet_list(self, fleet_registry: dict, selected_fleet_id: str):
        """Update list of available fleets for context menu."""
        self.available_fleets = fleet_registry
        self.selected_fleet_id = selected_fleet_id

    def update_channel_list(
        self,
        channel_registry: dict,
        selected_channel_id: str | None,
    ):
        """Update arbitrary chat sources for the Any Chat picker."""
        self.available_channels = channel_registry
        self.selected_channel_id = selected_channel_id
        if selected_channel_id:
            self.config['channel_selection'] = selected_channel_id

    def clear_messages(self):
        """Clear all messages from the overlay."""
        self.chat_history.clear()
        self._pending_messages.clear()
        if self._flush_timer.isActive():
            self._flush_timer.stop()
        self._needs_full_refresh = False
        self.text_browser.clear()

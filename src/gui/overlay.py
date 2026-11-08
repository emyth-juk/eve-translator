"""Always-on-top chat overlay: a translucent glass panel above the game.

Rendering model
---------------
* The window is frameless and per-pixel translucent. The rounded panel,
  status header, resize grip and command-card backgrounds are painted with
  QPainter, so the configured opacity dims only the background, never text.
* Every chat message is exactly one ``QTextDocument`` block with an explicit
  block format. A translation update rewrites that one block in place, new
  lines are appended without rebuilding the view, and the document is capped
  at ``MAX_VISIBLE_MESSAGES`` blocks.
* Each block carries a small integer user state that describes its
  decoration (command card, speaker-role accent). The transcript paints those
  decorations for the visible blocks only, behind the text.
"""

import ctypes
import datetime
import functools
import html
import re
import sys
from collections import deque
from dataclasses import dataclass, replace
from types import SimpleNamespace
from urllib.parse import quote, unquote

import shiboken6
from PySide6.QtCore import QEvent, QPoint, QPointF, QRect, QRectF, Qt, QTimer, QUrl, Signal
from PySide6.QtGui import (
    QColor,
    QCursor,
    QFont,
    QFontDatabase,
    QFontMetricsF,
    QLinearGradient,
    QMouseEvent,
    QPainter,
    QPainterPath,
    QPen,
    QPixmap,
    QRadialGradient,
    QTextBlockFormat,
    QTextCharFormat,
    QTextCursor,
    QTextDocumentFragment,
)
from PySide6.QtWidgets import (
    QApplication,
    QFileDialog,
    QFrame,
    QInputDialog,
    QLabel,
    QMainWindow,
    QMenu,
    QMessageBox,
    QSizeGrip,
    QSizePolicy,
    QTextBrowser,
    QToolTip,
    QVBoxLayout,
    QWidget,
    QWidgetAction,
)

from src.app_config import (
    DEFAULT_COLOR_DEFAULT,
    DEFAULT_COLOR_HIGHLIGHT,
    DEFAULT_COLOR_TRANSLATED,
    DISPLAY_MODE_BILINGUAL,
    DISPLAY_MODE_COMBAT,
    DISPLAY_MODE_MIRROR,
    DISPLAY_MODES,
)
from src.core.combat import command_is_trusted, normalize_speaker_name
from src.core.languages import normalize_language_code
from src.core.message import ProcessedMessage, TranslationState
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

# Geometry of the painted chrome (logical pixels).
PANEL_RADIUS = 8.0
CARD_RADIUS = 5.0
TRANSCRIPT_MARGIN = 7
_CLOCK_WITH_SECONDS = re.compile(r'\s*(\d{1,2}:\d{2}):\d{2}\s*')

# Semantic colours. They are tuned for light text on a dark, translucent
# panel and deliberately independent of the user-configurable text colours.
WARNING_COLOR = '#ffbf69'
ERROR_COLOR = '#ff6b6b'
LIVE_COLOR = '#4ade80'
WAITING_COLOR = '#fbbf24'
OFF_COLOR = '#7b8494'
VERIFIED_COLOR = '#f5c451'
COMBAT_MODE_COLOR = '#f87171'
ROLE_COLORS = {'fc': '#f5c451', 'scout': '#5eead4', 'starred': '#f5c451'}
CATEGORY_COLORS = {
    'MOVE': '#60a5fa',
    'TARGET': '#f87171',
    'HOLD': '#fbbf24',
    'SUPPORT': '#4ade80',
    'INTEL': '#a78bfa',
}
NEUTRAL_CARD_COLOR = '#94a3b8'

_SYSTEM_SENDERS = frozenset({'system', 'eve system'})
_MODE_ORDER = (DISPLAY_MODE_MIRROR, DISPLAY_MODE_BILINGUAL, DISPLAY_MODE_COMBAT)
_MODE_DESCRIPTIONS = {
    DISPLAY_MODE_MIRROR: "Every chat line.",
    DISPLAY_MODE_BILINGUAL: "Translated lines, commands and starred speakers.",
    DISPLAY_MODE_COMBAT: "FC command cards and starred speakers only.",
}

# Block user state: bits 0-3 kind, bits 4-7 accent index, bit 8 trusted.
_KIND_LINE = 1
_KIND_SYSTEM = 2
_KIND_CARD = 3
_ROLE_INDEX = {'fc': 1, 'scout': 2, 'starred': 3}
_CATEGORY_INDEX = {name: index for index, name in enumerate(CATEGORY_COLORS, start=1)}


def _block_state(kind: int, accent: int = 0, trusted: bool = False) -> int:
    return kind | (accent << 4) | (int(trusted) << 8)


def _color(value, fallback: str = '#101820') -> QColor:
    color = QColor(str(value or ''))
    return color if color.isValid() else QColor(fallback)


def _mix(first: QColor, second: QColor, amount: float) -> QColor:
    """Blend ``first`` toward ``second`` by ``amount`` (0..1); keeps first's alpha."""
    t = max(0.0, min(1.0, amount))
    return QColor(
        round(first.red() + (second.red() - first.red()) * t),
        round(first.green() + (second.green() - first.green()) * t),
        round(first.blue() + (second.blue() - first.blue()) * t),
        first.alpha(),
    )


def _faded(color: QColor, alpha: float) -> QColor:
    result = QColor(color)
    result.setAlphaF(max(0.0, min(1.0, alpha)))
    return result


def _css(color: QColor) -> str:
    """CSS colour for Qt rich text and style sheets."""
    if color.alpha() >= 255:
        return color.name()
    return f"rgba({color.red()}, {color.green()}, {color.blue()}, {color.alpha()})"


def _session_accent(background: QColor) -> QColor:
    """A vivid identity colour with the hue of the session background."""
    hue, saturation, _lightness, _alpha = background.getHslF()
    if hue < 0 or saturation < 0.12:
        return QColor('#9db4cc')
    return QColor.fromHslF(hue, max(0.55, min(0.82, saturation)), 0.68)


@functools.lru_cache(maxsize=8)
def _font_family(*candidates: str) -> str:
    available = set(QFontDatabase.families())
    for name in candidates:
        if name in available:
            return name
    return QApplication.font().family()


def _ui_family() -> str:
    return _font_family('Segoe UI', 'Segoe UI Variable Text', 'Noto Sans', 'DejaVu Sans', 'Arial')


def _display_family() -> str:
    # Bahnschrift ships with Windows 10+ and gives labels a crisp HUD look.
    return _font_family('Bahnschrift', 'Segoe UI Semibold', 'Segoe UI', 'Arial')


def _roles_key(roles) -> tuple:
    """Comparable snapshot of the speaker-role mapping."""
    if not isinstance(roles, dict):
        return ()
    return tuple(sorted((str(name), str(role)) for name, role in roles.items()))


def _bounded_opacity(value) -> float:
    try:
        return max(0.2, min(1.0, float(value)))
    except (TypeError, ValueError):
        return 0.8


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


def _status_led_color(status: OverlayStatus) -> str:
    state = (status.state or '').strip().lower()
    if state == 'error':
        return ERROR_COLOR
    if state in {'off', 'disabled'}:
        return OFF_COLOR
    if not status.provider_available:
        return WARNING_COLOR
    if state == 'waiting':
        return WAITING_COLOR
    return LIVE_COLOR


def _status_summary(status: OverlayStatus) -> str:
    parts = [status.channel.upper(), status.state]
    if status.context:
        parts.append(status.context)
    parts.append(status.display_mode.title())
    parts.append('Locked' if status.interaction_locked else 'Unlocked')
    health = 'available' if status.provider_available else 'unavailable'
    parts.append(f"{status.provider} {health}")
    return ' · '.join(part for part in parts if part)


@dataclass(frozen=True, eq=False)
class OverlayTheme:
    """Resolved colours and fonts for one overlay configuration."""

    base_pt: int
    ui_family: str
    display_family: str
    panel_top: QColor
    panel_bottom: QColor
    header_band: QColor
    divider: QColor
    border: QColor
    edge_highlight: QColor
    accent: QColor
    accent_text: QColor
    text: QColor
    text_dim: QColor
    text_faint: QColor
    translated: QColor

    @classmethod
    def from_config(cls, config: dict, base_pt: int) -> 'OverlayTheme':
        opacity = _bounded_opacity(config.get('opacity', 0.8))
        background = _color(config.get('background_color'), '#101820')
        accent = _session_accent(background)
        text = _color(config.get('color_default'), DEFAULT_COLOR_DEFAULT)
        ink = QColor('#0b1018')
        return cls(
            base_pt=base_pt,
            ui_family=_ui_family(),
            display_family=_display_family(),
            panel_top=_faded(_mix(background, QColor('#ffffff'), 0.045), opacity),
            panel_bottom=_faded(_mix(background, QColor('#000000'), 0.30), opacity),
            header_band=QColor(0, 0, 0, 58),
            divider=QColor(255, 255, 255, 22),
            border=_faded(_mix(accent, QColor('#ffffff'), 0.15), 0.36),
            edge_highlight=QColor(255, 255, 255, 24),
            accent=accent,
            accent_text=_mix(accent, QColor('#ffffff'), 0.32),
            text=text,
            text_dim=_mix(text, ink, 0.34),
            text_faint=_mix(text, ink, 0.52),
            translated=_color(config.get('color_translated'), DEFAULT_COLOR_TRANSLATED),
        )


def _paint_lock(painter: QPainter, rect: QRectF, locked: bool, color: QColor) -> None:
    """Draw a small vector padlock, closed or open."""
    width, height = rect.width(), rect.height()
    stroke = max(1.3, width * 0.17)
    body = QRectF(rect.left(), rect.top() + height * 0.46, width, height * 0.54)
    shackle = width * 0.62
    left = rect.left() + (width - shackle) / 2
    top = rect.top() + stroke / 2
    path = QPainterPath()
    if locked:
        path.moveTo(left, body.top())
        path.lineTo(left, top + shackle / 2)
        path.arcTo(QRectF(left, top, shackle, shackle), 180, -180)
        path.lineTo(left + shackle, body.top())
    else:
        # Lifted open: the shackle rises and its left leg floats free.
        top -= height * 0.16
        path.moveTo(left + shackle, body.top())
        path.lineTo(left + shackle, top + shackle / 2)
        path.arcTo(QRectF(left, top, shackle, shackle), 0, 180)
        path.lineTo(left, top + shackle / 2 + height * 0.04)
    painter.save()
    pen = QPen(color, stroke)
    pen.setCapStyle(Qt.PenCapStyle.RoundCap)
    painter.setPen(pen)
    painter.setBrush(Qt.BrushStyle.NoBrush)
    painter.drawPath(path)
    painter.setPen(Qt.PenStyle.NoPen)
    painter.setBrush(color)
    painter.drawRoundedRect(body, 1.6, 1.6)
    painter.restore()


def _dispose_menu(menu: QMenu) -> None:
    """Delete a finished menu immediately, while its caller holds the overlay.

    Menu actions capture the overlay in their slots. A deferred delete could
    release the overlay's last reference from inside the menu's destructor,
    and the overlay would then delete the half-destroyed menu a second time.
    """
    if shiboken6.isValid(menu):
        shiboken6.delete(menu)


def _prepare_menu(menu: QMenu) -> None:
    """Give a menu and its submenus frameless, rounded, translucent chrome."""
    for item in (menu, *menu.findChildren(QMenu)):
        item.setWindowFlags(
            item.windowFlags()
            | Qt.WindowType.FramelessWindowHint
            | Qt.WindowType.NoDropShadowWindowHint
        )
        item.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)


class OverlayPanel(QWidget):
    """Central widget that paints the rounded glass panel and header band."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self._theme = None
        self._header_height = 0
        # The transcript is transparent, so every transcript repaint also
        # repaints the panel beneath it; blit a cached background instead.
        self._cache = None
        self._cache_key = None

    def set_theme(self, theme: OverlayTheme) -> None:
        self._theme = theme
        self._cache = None
        self.update()

    def set_header_height(self, height: int) -> None:
        if height != self._header_height:
            self._header_height = height
            self._cache = None
            self.update()

    def fill_color(self) -> QColor:
        """Panel background colour, including the configured opacity."""
        return QColor(self._theme.panel_bottom) if self._theme else QColor()

    def resizeEvent(self, event):
        self._cache = None
        super().resizeEvent(event)

    def paintEvent(self, event):
        if self._theme is None:
            return
        ratio = self.devicePixelRatioF()
        key = (self.width(), self.height(), ratio)
        if self._cache is None or self._cache_key != key:
            self._cache = QPixmap(
                max(1, round(self.width() * ratio)),
                max(1, round(self.height() * ratio)),
            )
            self._cache.setDevicePixelRatio(ratio)
            self._cache.fill(Qt.GlobalColor.transparent)
            self._paint_panel(QPainter(self._cache))
            self._cache_key = key
        painter = QPainter(self)
        try:
            painter.setCompositionMode(QPainter.CompositionMode.CompositionMode_Source)
            target = QRectF(event.rect())
            source = QRectF(
                target.x() * ratio, target.y() * ratio,
                target.width() * ratio, target.height() * ratio,
            )
            painter.drawPixmap(target, self._cache, source)
        finally:
            painter.end()

    def _paint_panel(self, painter: QPainter) -> None:
        theme = self._theme
        try:
            painter.setRenderHint(QPainter.RenderHint.Antialiasing)
            width = float(self.width())
            bounds = QRectF(self.rect()).adjusted(0.5, 0.5, -0.5, -0.5)
            path = QPainterPath()
            path.addRoundedRect(bounds, PANEL_RADIUS, PANEL_RADIUS)
            gradient = QLinearGradient(0, bounds.top(), 0, bounds.bottom())
            gradient.setColorAt(0.0, theme.panel_top)
            gradient.setColorAt(1.0, theme.panel_bottom)
            painter.fillPath(path, gradient)
            if self._header_height > 0:
                painter.save()
                painter.setClipPath(path)
                painter.fillRect(QRectF(0, 0, width, self._header_height), theme.header_band)
                painter.restore()
                divider_y = self._header_height - 0.5
                painter.setPen(QPen(theme.divider, 1))
                painter.drawLine(QPointF(1, divider_y), QPointF(width - 1, divider_y))
            painter.setBrush(Qt.BrushStyle.NoBrush)
            painter.setPen(QPen(theme.border, 1))
            painter.drawPath(path)
            # A faint inner highlight along the top edge reads as glass.
            painter.setPen(QPen(theme.edge_highlight, 1))
            painter.drawLine(QPointF(PANEL_RADIUS, 1.5), QPointF(width - PANEL_RADIUS, 1.5))
        finally:
            painter.end()


class StatusHeader(QWidget):
    """Compact status bar and drag handle of one overlay.

    Shows a health LED, the session name, its context or provider problem,
    the display-mode chip (click to cycle) and the lock state.
    """

    PAD_LEFT = 10.0
    PAD_RIGHT = 9.0

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setMouseTracking(True)
        self.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Fixed)
        self.setMinimumWidth(0)
        # The header is the natural drag handle for a frameless window.
        self.setCursor(Qt.CursorShape.SizeAllCursor)
        self._status = OverlayStatus(channel='')
        self._theme = None
        self._fonts = None
        self._summary = ''
        self._lock_hint = ''
        self._layout_cache = None
        self._hover = None

    def text(self) -> str:
        """Plain-text summary of everything the header shows."""
        return self._summary

    def set_theme(self, theme: OverlayTheme) -> None:
        self._theme = theme
        base = theme.base_pt
        label = QFont(theme.display_family)
        label.setPointSizeF(max(7.5, base * 0.82))
        label.setWeight(QFont.Weight.DemiBold)
        label.setLetterSpacing(QFont.SpacingType.AbsoluteSpacing, 1.1)
        context = QFont(theme.ui_family)
        context.setPointSizeF(max(7.5, base * 0.84))
        chip = QFont(theme.display_family)
        chip.setPointSizeF(max(6.5, base * 0.66))
        chip.setWeight(QFont.Weight.DemiBold)
        chip.setLetterSpacing(QFont.SpacingType.AbsoluteSpacing, 0.9)
        self._fonts = SimpleNamespace(
            label=label,
            label_metrics=QFontMetricsF(label),
            context=context,
            context_metrics=QFontMetricsF(context),
            chip=chip,
            chip_metrics=QFontMetricsF(chip),
        )
        tallest = max(
            self._fonts.label_metrics.height(),
            self._fonts.context_metrics.height(),
        )
        self.setFixedHeight(max(22, round(tallest + 8)))
        self._layout_cache = None
        self.update()

    def set_status(self, status: OverlayStatus, *, lock_hint: str = '') -> None:
        self._status = status
        self._lock_hint = lock_hint
        self._summary = _status_summary(status)
        self._layout_cache = None
        self.setToolTip(status.detail or f"Translation provider: {status.provider}")
        self.update()

    def region_at(self, pos) -> str | None:
        """Name of the interactive element at ``pos`` (header coordinates)."""
        if self._fonts is None:
            return None
        point = QPointF(pos)
        layout = self._layout()
        if layout.chip is not None and layout.chip.adjusted(-3, -4, 3, 4).contains(point):
            return 'mode'
        if layout.lock.adjusted(-4, -6, 4, 6).contains(point):
            return 'lock'
        return 'status'

    def resizeEvent(self, event):
        self._layout_cache = None
        super().resizeEvent(event)

    def mouseMoveEvent(self, event):
        self._set_hover(self.region_at(event.position()))
        super().mouseMoveEvent(event)

    def event(self, event):
        event_type = event.type()
        if event_type == QEvent.Type.ToolTip:
            region = self.region_at(QPointF(event.pos()))
            text = self._region_tooltip(region)
            if text:
                QToolTip.showText(event.globalPos(), text, self)
                return True
        elif event_type == QEvent.Type.Leave:
            self._set_hover(None)
        return super().event(event)

    def _set_hover(self, region) -> None:
        if region == self._hover:
            return
        self._hover = region
        self.setCursor(
            Qt.CursorShape.PointingHandCursor
            if region == 'mode'
            else Qt.CursorShape.SizeAllCursor
        )
        self.update()

    def _region_tooltip(self, region) -> str:
        status = self._status
        if region == 'mode':
            mode = status.display_mode
            following = _MODE_ORDER[(_MODE_ORDER.index(mode) + 1) % len(_MODE_ORDER)] \
                if mode in _MODE_ORDER else DISPLAY_MODE_MIRROR
            return (
                f"Display mode: {mode.title()}\n"
                f"{_MODE_DESCRIPTIONS.get(mode, '')}\n"
                f"Click to switch to {following.title()}."
            )
        if region == 'lock':
            hint = self._lock_hint or 'the tray menu'
            return (
                "Unlocked: drag the header to move, drag an edge to resize.\n"
                f"Lock all overlays (click-through) with {hint} or the tray menu."
            )
        return ''

    def _layout(self):
        if self._layout_cache is not None:
            return self._layout_cache
        fonts = self._fonts
        status = self._status
        width, height = float(self.width()), float(self.height())
        center_y = height / 2
        x = self.PAD_LEFT
        led_size = max(6.0, round(height * 0.24))
        led = QRectF(x, center_y - led_size / 2, led_size, led_size)
        x = led.right() + 7
        label_text = status.channel.upper()
        label = QRectF(x, 0, fonts.label_metrics.horizontalAdvance(label_text), height)
        x = label.right() + 9

        right = width - self.PAD_RIGHT
        lock_height = max(9.0, round(height * 0.42))
        lock_width = round(lock_height * 0.78)
        lock = QRectF(right - lock_width, center_y - lock_height / 2 + 0.5, lock_width, lock_height)
        right = lock.left() - 9

        chip_text = status.display_mode.upper()
        chip_height = min(height - 6, round(fonts.chip_metrics.capHeight() + 9))
        chip_width = round(fonts.chip_metrics.horizontalAdvance(chip_text) + 13)
        chip = QRectF(right - chip_width, center_y - chip_height / 2, chip_width, chip_height)
        if chip.left() < label.right() + 6:
            chip = None
        context_right = (chip.left() if chip is not None else lock.left()) - 8
        context = QRectF(x, 0, context_right - x, height) if context_right - x >= 24 else None
        self._layout_cache = SimpleNamespace(
            led=led,
            label=label,
            label_text=label_text,
            context=context,
            chip=chip,
            chip_text=chip_text,
            lock=lock,
            baseline=center_y + fonts.label_metrics.capHeight() / 2,
        )
        return self._layout_cache

    def _context_pieces(self) -> list[tuple[str, QColor]]:
        status = self._status
        theme = self._theme
        pieces = []
        state = (status.state or '').strip()
        lowered = state.lower()
        if lowered and lowered != 'active':
            color = {
                'error': ERROR_COLOR,
                'waiting': WAITING_COLOR,
            }.get(lowered, OFF_COLOR)
            pieces.append((state, QColor(color)))
        if not status.provider_available and status.detail:
            # Show the provider problem once here instead of on every line;
            # the locked (click-through) HUD cannot show a tooltip.
            short = status.detail.split(';', 1)[0].strip().rstrip('.')
            pieces.append(('⚠ ' + short, QColor(WARNING_COLOR)))
        elif status.context:
            pieces.append((status.context, theme.text_dim))
        return pieces

    def paintEvent(self, event):
        theme, fonts = self._theme, self._fonts
        if theme is None or fonts is None:
            return
        layout = self._layout()
        status = self._status
        painter = QPainter(self)
        try:
            painter.setRenderHint(QPainter.RenderHint.Antialiasing)
            self._paint_led(painter, layout.led, QColor(_status_led_color(status)))

            painter.setFont(fonts.label)
            painter.setPen(theme.accent_text)
            painter.drawText(QPointF(layout.label.left(), layout.baseline), layout.label_text)

            if layout.context is not None:
                self._paint_context(painter, layout)
            if layout.chip is not None:
                self._paint_chip(painter, layout)

            if status.interaction_locked:
                lock_color = QColor(VERIFIED_COLOR)
            elif self._hover == 'lock':
                lock_color = theme.text_dim
            else:
                lock_color = theme.text_faint
            _paint_lock(painter, layout.lock, status.interaction_locked, lock_color)
        finally:
            painter.end()

    @staticmethod
    def _paint_led(painter: QPainter, rect: QRectF, color: QColor) -> None:
        center = rect.center()
        radius = rect.width() / 2
        glow = QRadialGradient(center, radius * 2.4)
        glow.setColorAt(0.0, _faded(color, 0.45))
        glow.setColorAt(1.0, _faded(color, 0.0))
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(glow)
        painter.drawEllipse(center, radius * 2.4, radius * 2.4)
        painter.setBrush(color)
        painter.drawEllipse(center, radius, radius)
        painter.setBrush(QColor(255, 255, 255, 95))
        painter.drawEllipse(
            QPointF(center.x() - radius * 0.3, center.y() - radius * 0.3),
            radius * 0.34,
            radius * 0.34,
        )

    def _paint_context(self, painter: QPainter, layout) -> None:
        fonts, theme = self._fonts, self._theme
        metrics = fonts.context_metrics
        painter.setFont(fonts.context)
        x, limit = layout.context.left(), layout.context.right()
        separator = ' · '
        for index, (text, color) in enumerate(self._context_pieces()):
            if index:
                separator_width = metrics.horizontalAdvance(separator)
                if x + separator_width >= limit - 12:
                    break
                painter.setPen(theme.text_faint)
                painter.drawText(QPointF(x, layout.baseline), separator)
                x += separator_width
            available = limit - x
            if available < 12:
                break
            shown = metrics.elidedText(text, Qt.TextElideMode.ElideRight, available)
            painter.setPen(color)
            painter.drawText(QPointF(x, layout.baseline), shown)
            x += metrics.horizontalAdvance(shown)

    def _paint_chip(self, painter: QPainter, layout) -> None:
        fonts, theme = self._fonts, self._theme
        mode = self._status.display_mode
        hover = self._hover == 'mode'
        if mode == DISPLAY_MODE_COMBAT:
            base = QColor(COMBAT_MODE_COLOR)
        elif mode == DISPLAY_MODE_BILINGUAL:
            base = QColor(theme.translated)
        else:
            base = None
        if base is None:
            fill = QColor(255, 255, 255, 28 if hover else 12)
            border = QColor(255, 255, 255, 80 if hover else 48)
            text = _mix(theme.text, theme.text_dim, 0.2)
        else:
            fill = _faded(base, 0.28 if hover else 0.16)
            border = _faded(base, 0.65 if hover else 0.45)
            text = _mix(base, QColor('#ffffff'), 0.4)
        rect = layout.chip.adjusted(0.5, 0.5, -0.5, -0.5)
        painter.setPen(QPen(border, 1))
        painter.setBrush(fill)
        painter.drawRoundedRect(rect, rect.height() / 2, rect.height() / 2)
        metrics = fonts.chip_metrics
        spacing = fonts.chip.letterSpacing()
        text_width = metrics.horizontalAdvance(layout.chip_text) - spacing
        painter.setFont(fonts.chip)
        painter.setPen(text)
        painter.drawText(
            QPointF(
                layout.chip.center().x() - text_width / 2,
                layout.chip.center().y() + metrics.capHeight() / 2,
            ),
            layout.chip_text,
        )


class NewMessagesPill(QWidget):
    """Floating "N new messages" button shown while reading older lines."""

    clicked = Signal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self._label = ''
        self._font = QFont()
        self._accent = QColor('#7aa2ff')
        self._hover = False
        self.hide()

    def set_style(self, font: QFont, accent: QColor) -> None:
        self._font = QFont(font)
        self._accent = QColor(accent)
        self._relayout()

    def set_count(self, count: int) -> None:
        label = f"{count} new message{'' if count == 1 else 's'}"
        if label != self._label:
            self._label = label
            self._relayout()

    def _relayout(self) -> None:
        metrics = QFontMetricsF(self._font)
        height = round(metrics.height() + 9)
        width = round(metrics.horizontalAdvance(self._label) + height + 12)
        self.setFixedSize(width, height)
        parent = self.parentWidget()
        if isinstance(parent, TranscriptView):
            parent.position_pill()
        self.update()

    def enterEvent(self, event):
        self._hover = True
        self.update()
        super().enterEvent(event)

    def leaveEvent(self, event):
        self._hover = False
        self.update()
        super().leaveEvent(event)

    def mousePressEvent(self, event):
        event.accept()

    def mouseReleaseEvent(self, event):
        if (
            event.button() == Qt.MouseButton.LeftButton
            and self.rect().contains(event.position().toPoint())
        ):
            self.clicked.emit()
        event.accept()

    def paintEvent(self, event):
        painter = QPainter(self)
        try:
            painter.setRenderHint(QPainter.RenderHint.Antialiasing)
            rect = QRectF(self.rect()).adjusted(0.5, 0.5, -0.5, -0.5)
            fill = _mix(QColor(15, 19, 27), self._accent, 0.30 if self._hover else 0.16)
            fill.setAlpha(242)
            painter.setPen(QPen(_faded(self._accent, 0.8), 1))
            painter.setBrush(fill)
            painter.drawRoundedRect(rect, rect.height() / 2, rect.height() / 2)
            # Down chevron.
            size = rect.height() * 0.15
            center_x = rect.left() + rect.height() / 2 + 2
            center_y = rect.center().y()
            pen = QPen(QColor(255, 255, 255, 235), 1.6)
            pen.setCapStyle(Qt.PenCapStyle.RoundCap)
            pen.setJoinStyle(Qt.PenJoinStyle.RoundJoin)
            painter.setPen(pen)
            chevron = QPainterPath()
            chevron.moveTo(center_x - size, center_y - size * 0.5)
            chevron.lineTo(center_x, center_y + size * 0.5)
            chevron.lineTo(center_x + size, center_y - size * 0.5)
            painter.drawPath(chevron)
            painter.setFont(self._font)
            painter.setPen(QColor(241, 244, 249))
            text_left = center_x + size + 6
            painter.drawText(
                QRectF(text_left, rect.top(), rect.right() - text_left, rect.height()),
                Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft,
                self._label,
            )
        finally:
            painter.end()


class TranscriptView(QTextBrowser):
    """Chat transcript that paints command cards and role accents behind text."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self._decor = None
        self._empty_title = ''
        self._empty_subtitle = ''
        self._empty_fonts = (QFont(), QFont())
        self._empty_colors = (QColor('#8a93a3'), QColor('#5f6877'))
        # Rendered card backgrounds keyed by size, accent, trust and DPR.
        self._card_cache = {}
        self.pill = NewMessagesPill(self)

    def set_decor(self, decor) -> None:
        self._decor = decor
        self._card_cache.clear()
        self.viewport().update()

    def set_empty_style(self, title_font: QFont, subtitle_font: QFont,
                        title_color: QColor, subtitle_color: QColor) -> None:
        self._empty_fonts = (QFont(title_font), QFont(subtitle_font))
        self._empty_colors = (QColor(title_color), QColor(subtitle_color))
        self.viewport().update()

    def set_empty_message(self, title: str, subtitle: str) -> None:
        if (title, subtitle) != (self._empty_title, self._empty_subtitle):
            self._empty_title, self._empty_subtitle = title, subtitle
            if self.document().isEmpty():
                self.viewport().update()

    def position_pill(self) -> None:
        viewport = self.viewport().geometry()
        self.pill.move(
            viewport.x() + (viewport.width() - self.pill.width()) // 2,
            viewport.y() + viewport.height() - self.pill.height() - 8,
        )

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self.position_pill()

    def paintEvent(self, event):
        decor = self._decor
        empty = self.document().isEmpty()
        if decor is not None and not empty:
            painter = QPainter(self.viewport())
            try:
                painter.setRenderHint(QPainter.RenderHint.Antialiasing)
                self._paint_decorations(painter, event.rect(), decor)
            finally:
                painter.end()
        super().paintEvent(event)
        if empty and self._empty_title:
            painter = QPainter(self.viewport())
            try:
                self._paint_empty_state(painter)
            finally:
                painter.end()

    def _paint_decorations(self, painter: QPainter, clip: QRect, decor) -> None:
        document = self.document()
        layout = document.documentLayout()
        dx = -self.horizontalScrollBar().value()
        dy = -self.verticalScrollBar().value()
        reach = decor.card_pad + 2
        block = self.cursorForPosition(QPoint(0, max(0, clip.top() - reach))).block()
        if block.previous().isValid():
            block = block.previous()
        margin = document.documentMargin()
        width = self.viewport().width()
        painter.setPen(Qt.PenStyle.NoPen)
        while block.isValid():
            rect = layout.blockBoundingRect(block).translated(dx, dy)
            if rect.top() - reach > clip.bottom():
                break
            state = block.userState()
            if state > 0:
                kind = state & 0xF
                accent = (state >> 4) & 0xF
                if kind == _KIND_CARD:
                    self._paint_card(painter, rect, accent, bool(state & 0x100), decor, margin, width)
                elif accent and kind == _KIND_LINE:
                    color = decor.role_colors.get(accent)
                    if color is not None:
                        painter.setPen(Qt.PenStyle.NoPen)
                        painter.setBrush(color)
                        painter.drawRoundedRect(
                            QRectF(margin - 5, rect.top() + 1, 2.2, max(2.0, rect.height() - 2)),
                            1.1,
                            1.1,
                        )
            block = block.next()

    def _paint_card(self, painter: QPainter, rect: QRectF, accent_index: int, trusted: bool,
                    decor, margin: float, width: int) -> None:
        """Blit a cached card background for one card block."""
        pad = decor.card_pad
        card_width = int(width - 2 * margin + 6)
        card_height = int(round(rect.height() + 2 * pad))
        if card_width < 8 or card_height < 8:
            return
        ratio = self.devicePixelRatioF()
        key = (card_width, card_height, accent_index, trusted, ratio)
        pixmap = self._card_cache.get(key)
        if pixmap is None:
            if len(self._card_cache) >= 48:
                self._card_cache.clear()
            pixmap = QPixmap(round(card_width * ratio), round(card_height * ratio))
            pixmap.setDevicePixelRatio(ratio)
            pixmap.fill(Qt.GlobalColor.transparent)
            card_painter = QPainter(pixmap)
            try:
                card_painter.setRenderHint(QPainter.RenderHint.Antialiasing)
                self._draw_card(
                    card_painter,
                    QRectF(0, 0, card_width, card_height),
                    accent_index,
                    trusted,
                    decor,
                )
            finally:
                card_painter.end()
            self._card_cache[key] = pixmap
        painter.drawPixmap(QPoint(round(margin - 3), round(rect.top() - pad)), pixmap)

    @staticmethod
    def _draw_card(painter: QPainter, card: QRectF, accent_index: int, trusted: bool,
                   decor) -> None:
        accent = decor.category_colors.get(accent_index, decor.neutral)
        path = QPainterPath()
        path.addRoundedRect(card.adjusted(0.5, 0.5, -0.5, -0.5), CARD_RADIUS, CARD_RADIUS)
        if trusted:
            gradient = QLinearGradient(card.left(), 0, card.right(), 0)
            gradient.setColorAt(0.0, _faded(accent, 0.26))
            gradient.setColorAt(1.0, _faded(accent, 0.07))
            painter.fillPath(path, gradient)
            border = _faded(accent, 0.45)
            bar = accent
        else:
            painter.fillPath(path, QColor(255, 255, 255, 13))
            border = QColor(255, 255, 255, 30)
            bar = _faded(_mix(accent, decor.neutral, 0.6), 0.7)
        painter.save()
        painter.setClipPath(path)
        painter.fillRect(QRectF(card.left(), card.top(), 3, card.height()), bar)
        painter.restore()
        painter.setPen(QPen(border, 1))
        painter.setBrush(Qt.BrushStyle.NoBrush)
        painter.drawPath(path)
        painter.setPen(Qt.PenStyle.NoPen)

    def _paint_empty_state(self, painter: QPainter) -> None:
        painter.setRenderHint(QPainter.RenderHint.TextAntialiasing)
        title_font, subtitle_font = self._empty_fonts
        title_color, subtitle_color = self._empty_colors
        area = QRectF(self.viewport().rect()).adjusted(14, 8, -14, -8)
        if area.width() < 40 or area.height() < 20:
            return
        title_metrics = QFontMetricsF(title_font)
        subtitle_metrics = QFontMetricsF(subtitle_font)
        flags = Qt.AlignmentFlag.AlignHCenter | Qt.TextFlag.TextWordWrap
        subtitle_box = subtitle_metrics.boundingRect(
            QRectF(0, 0, area.width(), 1000), int(flags), self._empty_subtitle
        )
        total = title_metrics.height() + 4 + subtitle_box.height()
        top = area.center().y() - total / 2
        painter.setFont(title_font)
        painter.setPen(title_color)
        painter.drawText(
            QRectF(area.left(), top, area.width(), title_metrics.height()),
            int(Qt.AlignmentFlag.AlignHCenter),
            self._empty_title,
        )
        painter.setFont(subtitle_font)
        painter.setPen(subtitle_color)
        painter.drawText(
            QRectF(area.left(), top + title_metrics.height() + 4, area.width(), subtitle_box.height()),
            int(flags),
            self._empty_subtitle,
        )


class ResizeGrip(QSizeGrip):
    """Size grip drawn as a subtle dot triangle that brightens on hover."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setFixedSize(16, 16)
        self._color = QColor('#ffffff')
        self._hover = False

    def set_color(self, color: QColor) -> None:
        self._color = QColor(color)
        self.update()

    def enterEvent(self, event):
        self._hover = True
        self.update()
        super().enterEvent(event)

    def leaveEvent(self, event):
        self._hover = False
        self.update()
        super().leaveEvent(event)

    def paintEvent(self, event):
        painter = QPainter(self)
        try:
            painter.setRenderHint(QPainter.RenderHint.Antialiasing)
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(_faded(self._color, 0.75 if self._hover else 0.34))
            width, height = self.width(), self.height()
            for row in range(3):
                for column in range(3 - row):
                    painter.drawEllipse(
                        QPointF(width - 4.5 - column * 3.6, height - 4.5 - row * 3.6),
                        1.05,
                        1.05,
                    )
        finally:
            painter.end()


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
    # Ask the manager to lock (click-through) or unlock every overlay.
    lock_requested = Signal(bool)

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

        # The opacity setting applies to the painted background only; text
        # stays fully opaque so it remains readable over a busy game scene.
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)

        # Mouse Tracking for resize cursor
        self.setMouseTracking(True)

        # Interaction State
        self.resize_margin = RESIZE_MARGIN_PX
        self.min_width = 120
        self.min_height = 80
        self.setMinimumSize(self.min_width, self.min_height)
        self.is_moving = False
        self.has_moved = False
        self._move_button = None
        self.drag_start_pos = None
        self._menu_anchor = None
        # Reading position: follow new lines unless the reader scrolled up.
        self._force_follow = False
        self._unread = 0
        # Native resizes and grip drags only produce resize events. Save the
        # final geometry so later config pushes cannot restore the old size.
        self._geometry_save_timer = QTimer(self)
        self._geometry_save_timer.setSingleShot(True)
        self._geometry_save_timer.setInterval(GEOMETRY_SAVE_DELAY_MS)
        self._geometry_save_timer.timeout.connect(self.save_config)

        # Glass panel with a compact status header above the transcript. The
        # transcript remains the canonical model view, so changing mode never
        # retranslates messages.
        self.central_container = OverlayPanel(self)
        self.central_layout = QVBoxLayout(self.central_container)
        self.central_layout.setContentsMargins(0, 0, 0, 0)
        self.central_layout.setSpacing(0)
        self.status_header = StatusHeader(self.central_container)
        self.status_header.installEventFilter(self)
        self.central_layout.addWidget(self.status_header)

        self.text_browser = TranscriptView(self.central_container)
        self.text_browser.setOpenExternalLinks(False)
        self.text_browser.setOpenLinks(False)
        self.text_browser.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)
        self.text_browser.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.text_browser.setFrameShape(QFrame.Shape.NoFrame)
        self.text_browser.setMouseTracking(True)
        self.text_browser.document().setMaximumBlockCount(MAX_VISIBLE_MESSAGES)
        self.text_browser.document().setDocumentMargin(TRANSCRIPT_MARGIN)
        self.text_browser.setUndoRedoEnabled(False)
        # The window is borderless, so moves, menus and edge cursors are
        # handled for the transcript and its viewport here.
        self.text_browser.installEventFilter(self)
        self.text_browser.viewport().installEventFilter(self)
        self.text_browser.anchorClicked.connect(self._handle_anchor)
        self.text_browser.pill.clicked.connect(self._scroll_to_latest)
        self.text_browser.verticalScrollBar().valueChanged.connect(self._on_transcript_scrolled)

        self.central_layout.addWidget(self.text_browser)
        self.setCentralWidget(self.central_container)

        # Resize Handle (Visual Grip), raised above the transcript.
        self.grip = ResizeGrip(self)
        self.grip.raise_()

        # Default Config
        default_config = {
            'x': -1, 'y': -1, 'w': 600, 'h': 400,
            'opacity': 0.8,
            'font_size': 10,
            'color_default': DEFAULT_COLOR_DEFAULT,
            'color_translated': DEFAULT_COLOR_TRANSLATED,
            'color_highlight': DEFAULT_COLOR_HIGHLIGHT,
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
        self._theme = None
        self._tokens = None
        self._block_formats = {}
        self._card_pad = 6
        self._plain_char_format = QTextCharFormat()
        # Parsed message HTML, reused by rebuilds (mode switches, show):
        # id(message) -> (message, render key, kind, user state, fragment).
        self._fragment_cache = {}
        self._render_version = 0
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

        # Welcome Message
        eve_time = datetime.datetime.now(datetime.timezone.utc).strftime("%H:%M:%S")

        if session_id == 'local':
            char_name = html.escape(str(self.config.get('character_name') or 'Unknown Character'))
            system_name = html.escape(str(self.config.get('system_name') or 'Unknown System'))
            welcome_text = f"Listening to Local chat for <b>{char_name}</b> in <b>{system_name}</b>."
        elif session_id == 'any':
            channel_name = html.escape(str(self.config.get('channel_name') or 'an arbitrary chat'))
            listener = html.escape(str(self.config.get('listener_name') or ''))
            suffix = f" for <b>{listener}</b>" if listener else ''
            welcome_text = f"Listening to <b>{channel_name}</b>{suffix}."
        else:
            welcome_text = f"Listening to {html.escape(session_id.capitalize())} chat."

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

    def _base_pt(self) -> int:
        try:
            base = int(self.config.get('font_size', 10))
        except (TypeError, ValueError):
            base = 10
        return max(6, min(48, base))

    def _font_pt(self, scale: float, minimum: int = 7) -> int:
        """Size secondary text relative to the configured overlay font."""
        return max(minimum, round(self._base_pt() * scale))

    def _lock_hint(self) -> str:
        return str(self.config.get('global_hotkey') or 'Ctrl+Shift+L')

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
        if abs(self.windowOpacity() - 1.0) > 0.001:
            self.setWindowOpacity(1.0)

        base_pt = self._base_pt()
        text_key = (
            base_pt,
            str(self.config.get('color_default', DEFAULT_COLOR_DEFAULT)),
            str(self.config.get('color_translated', DEFAULT_COLOR_TRANSLATED)),
            str(self.config.get('color_highlight', DEFAULT_COLOR_HIGHLIGHT)),
        )
        panel_key = (
            str(self.config.get('background_color', '')),
            _bounded_opacity(self.config.get('opacity', 0.8)),
        )
        # Settings that change how already-received lines are drawn.
        content_key = (
            bool(self.config.get('command_cards', True)),
            str(self.config.get('target_language', 'en')),
        )
        roles_key = _roles_key(self.config.get('speaker_roles', {}))
        text_changed = self._style_state.get('text') != text_key
        if text_changed or self._style_state.get('panel') != panel_key:
            self._apply_theme(OverlayTheme.from_config(self.config, base_pt))
        if text_changed or self._style_state.get('content') != content_key:
            # Opacity and background only repaint; these re-render lines.
            self._render_version += 1
            needs_refresh = True
        if self._style_state.get('roles') != roles_key:
            # Roles pushed from another overlay change badges, accents and
            # Combat filtering; cached lines of other speakers stay valid.
            needs_refresh = True

        display_mode = str(
            self.config.get('display_mode', DISPLAY_MODE_MIRROR)
        ).strip().lower()
        if display_mode not in DISPLAY_MODES:
            display_mode = DISPLAY_MODE_MIRROR
            self.config['display_mode'] = display_mode
        compact_header = bool(self.config.get('compact_header', True))
        self.status_header.setVisible(compact_header)
        self.central_container.set_header_height(
            self.status_header.height() if compact_header else 0
        )
        if self._style_state.get('display_mode') != display_mode:
            needs_refresh = True

        self._style_state = {
            'text': text_key,
            'panel': panel_key,
            'content': content_key,
            'roles': roles_key,
            'display_mode': display_mode,
            'compact_header': compact_header,
        }
        self.overlay_status = replace(
            self.overlay_status,
            display_mode=display_mode,
            interaction_locked=self.interaction_locked,
        )
        self._update_empty_message()
        self._render_status_header()

        if needs_refresh:
            if self.isVisible():
                self.refresh_ui()
            else:
                self._needs_full_refresh = True

    def _apply_theme(self, theme: OverlayTheme) -> None:
        """Push resolved colours, fonts and metrics into every painted part."""
        self._theme = theme
        self.central_container.set_theme(theme)
        self.status_header.set_theme(theme)
        self.grip.set_color(theme.text)

        base_font = QFont(theme.ui_family)
        base_font.setPointSize(theme.base_pt)
        self.text_browser.setFont(base_font)
        self.text_browser.setStyleSheet(self._transcript_stylesheet(theme))
        self._tokens = self._build_tokens(theme)
        self._block_formats = self._build_block_formats(theme, base_font)
        self.text_browser.set_decor(SimpleNamespace(
            card_pad=self._card_pad,
            role_colors={
                index: QColor(ROLE_COLORS[role]) for role, index in _ROLE_INDEX.items()
            },
            category_colors={
                index: QColor(CATEGORY_COLORS[name]) for name, index in _CATEGORY_INDEX.items()
            },
            neutral=QColor(NEUTRAL_CARD_COLOR),
        ))

        pill_font = QFont(theme.ui_family)
        pill_font.setPointSizeF(max(7.5, theme.base_pt * 0.8))
        pill_font.setWeight(QFont.Weight.DemiBold)
        self.text_browser.pill.set_style(pill_font, theme.accent)

        empty_title = QFont(theme.display_family)
        empty_title.setPointSizeF(max(8.0, theme.base_pt * 0.82))
        empty_title.setWeight(QFont.Weight.DemiBold)
        empty_title.setLetterSpacing(QFont.SpacingType.AbsoluteSpacing, 1.2)
        empty_title.setCapitalization(QFont.Capitalization.AllUppercase)
        empty_subtitle = QFont(theme.ui_family)
        empty_subtitle.setPointSizeF(max(7.5, theme.base_pt * 0.84))
        self.text_browser.set_empty_style(
            empty_title, empty_subtitle, theme.text_dim, theme.text_faint
        )
        # Menus, tooltips and dialogs opened from the overlay inherit this.
        self.setStyleSheet(self._chrome_stylesheet(theme))

    @staticmethod
    def _transcript_stylesheet(theme: OverlayTheme) -> str:
        selection = _css(_faded(theme.accent, 0.42))
        return (
            "QTextBrowser { background: transparent; border: none; "
            f"color: {theme.text.name()}; selection-background-color: {selection}; "
            "selection-color: #ffffff; }"
            "QScrollBar:vertical { background: transparent; width: 9px; "
            "margin: 4px 3px 4px 0px; }"
            "QScrollBar::handle:vertical { background: rgba(255, 255, 255, 40); "
            "min-height: 28px; border-radius: 3px; }"
            "QScrollBar::handle:vertical:hover, QScrollBar::handle:vertical:pressed "
            "{ background: rgba(255, 255, 255, 110); }"
            "QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical "
            "{ height: 0px; width: 0px; border: none; background: transparent; }"
            "QScrollBar::add-page:vertical, QScrollBar::sub-page:vertical "
            "{ background: transparent; }"
        )

    @staticmethod
    def _chrome_stylesheet(theme: OverlayTheme) -> str:
        family = theme.ui_family
        point_size = max(8, min(11, round(theme.base_pt * 0.9)))
        accent = theme.accent.name()
        accent_soft = _css(_faded(theme.accent, 0.35))
        return (
            "QToolTip { background-color: #151a23; color: #e4e9f0; "
            "border: 1px solid rgba(255, 255, 255, 40); border-radius: 5px; "
            f"padding: 5px 8px; font-family: \"{family}\"; font-size: {point_size}pt; }}"
            "QMenu { background-color: #11151d; color: #e2e7ee; "
            "border: 1px solid rgba(255, 255, 255, 36); border-radius: 9px; padding: 5px; "
            f"font-family: \"{family}\"; font-size: {point_size}pt; }}"
            "QMenu::item { background: transparent; padding: 5px 28px 5px 26px; "
            "border-radius: 5px; }"
            "QMenu::item:selected { background-color: rgba(255, 255, 255, 24); color: #ffffff; }"
            "QMenu::item:disabled { color: #6c7482; }"
            "QMenu::separator { height: 1px; background: rgba(255, 255, 255, 22); "
            "margin: 5px 10px; }"
            "QMenu::indicator { width: 7px; height: 7px; left: 10px; border-radius: 3px; }"
            f"QMenu::indicator:checked {{ background-color: {accent}; }}"
            "QMenu::indicator:unchecked { background-color: transparent; }"
            "QLabel#overlayMenuTitle { color: #f1f4f8; padding: 6px 12px 5px 14px; }"
            "QDialog { background-color: #12161e; }"
            "QDialog QLabel { color: #c9d0db; }"
            "QDialog QPlainTextEdit, QDialog QTextEdit, QDialog QLineEdit { "
            "background-color: #0b0e14; color: #eef2f7; "
            "border: 1px solid rgba(255, 255, 255, 36); border-radius: 6px; padding: 6px; "
            f"selection-background-color: {accent_soft}; }}"
            "QDialog QPlainTextEdit:focus, QDialog QTextEdit:focus, QDialog QLineEdit:focus "
            f"{{ border-color: {accent}; }}"
            "QDialog QPushButton { background-color: rgba(255, 255, 255, 16); color: #e4e9f0; "
            "border: 1px solid rgba(255, 255, 255, 36); border-radius: 6px; "
            "padding: 5px 16px; min-width: 64px; }"
            "QDialog QPushButton:hover { background-color: rgba(255, 255, 255, 28); }"
            f"QDialog QPushButton:default {{ border-color: {accent}; }}"
        )

    def _build_tokens(self, theme: OverlayTheme) -> SimpleNamespace:
        """Pre-render the inline styles shared by every message."""
        base = theme.base_pt
        faint = theme.text_faint.name()
        dim = theme.text_dim.name()
        badge_pt = max(6, round(base * 0.7))
        display = theme.display_family
        role_badges = {
            role: (
                f"&nbsp;<span style='color:{ROLE_COLORS[role]};font-size:{badge_pt}pt;"
                f"font-weight:700;letter-spacing:0.5px;'>{role.upper()}</span>"
            )
            for role in ('fc', 'scout')
        }
        role_badges['starred'] = f"&nbsp;<span style='color:{ROLE_COLORS['starred']};'>&#9733;</span>"
        small_pt = max(7, round(base * 0.84))
        return SimpleNamespace(
            faint=faint,
            dim=dim,
            timestamp=f"color:{faint};font-size:{self._font_pt(0.8)}pt;",
            gap='&nbsp;&nbsp;',
            separator=f"<span style='color:{faint};'>&nbsp;&#8250;&nbsp;</span>",
            original=f"color:{dim};font-size:{small_pt}pt;",
            tag=(
                f"color:{faint};font-size:{max(6, round(base * 0.66))}pt;"
                "font-weight:700;letter-spacing:1px;"
            ),
            edit=f"color:{faint};text-decoration:none;font-size:{max(7, round(base * 0.8))}pt;",
            note_pt=max(7, round(base * 0.76)),
            system=f"color:{dim};font-style:italic;",
            role_badges=role_badges,
            meta=(
                f"font-size:{max(6, round(base * 0.7))}pt;font-weight:700;"
                "letter-spacing:0.8px;"
            ),
            card_label=(
                f"font-family:\"{display}\";font-size:{max(9, round(base * 1.08))}pt;"
                "font-weight:600;letter-spacing:0.5px;text-transform:uppercase;"
            ),
            card_argument=f"font-size:{max(8, round(base * 1.0))}pt;font-weight:600;",
            card_small_pt=small_pt,
            label_trusted='#ffffff',
            label_muted=_mix(theme.text, theme.text_dim, 0.35).name(),
        )

    def _build_block_formats(self, theme: OverlayTheme, base_font: QFont) -> dict:
        """Block formats per message kind (one block per message)."""
        timestamp_font = QFont(theme.ui_family)
        timestamp_font.setPointSize(self._font_pt(0.8))
        base_metrics = QFontMetricsF(base_font)
        # Wrapped lines hang under the speaker, leaving a clean time column.
        hang = (
            QFontMetricsF(timestamp_font).horizontalAdvance('00:00')
            + 2 * base_metrics.horizontalAdvance(' ')
        )
        em = base_metrics.height()
        line_gap = max(2, round(em * 0.18))
        self._card_pad = max(3, round(em * 0.24))
        card_gap = max(3, round(em * 0.2))

        line = QTextBlockFormat()
        line.setLeftMargin(hang)
        line.setTextIndent(-hang)
        line.setBottomMargin(line_gap)

        card = QTextBlockFormat()
        card.setLeftMargin(3 + round(em * 0.4))
        card.setRightMargin(round(em * 0.3))
        # Margins collapse to max(previous bottom, next top); the painted card
        # extends card_pad into them on both sides.
        card.setTopMargin(2 * self._card_pad + card_gap)
        card.setBottomMargin(2 * self._card_pad + card_gap)
        return {_KIND_LINE: line, _KIND_SYSTEM: QTextBlockFormat(line), _KIND_CARD: card}

    @property
    def display_mode(self) -> str:
        mode = str(self.config.get('display_mode', DISPLAY_MODE_MIRROR)).lower()
        return mode if mode in DISPLAY_MODES else DISPLAY_MODE_MIRROR

    def set_display_mode(self, mode: str, *, save: bool = True) -> None:
        normalized = str(mode or '').strip().lower()
        if normalized not in DISPLAY_MODES or normalized == self.display_mode:
            return
        self.config['display_mode'] = normalized
        self._style_state['display_mode'] = normalized
        self.overlay_status = replace(self.overlay_status, display_mode=normalized)
        self._update_empty_message()
        self._force_follow = True
        self.refresh_ui()
        self._render_status_header()
        if save:
            self.save_config()

    def _cycle_display_mode(self) -> None:
        mode = self.display_mode
        index = _MODE_ORDER.index(mode) if mode in _MODE_ORDER else -1
        self.set_display_mode(_MODE_ORDER[(index + 1) % len(_MODE_ORDER)])

    def _update_empty_message(self) -> None:
        mode = self.display_mode
        if mode == DISPLAY_MODE_COMBAT:
            title, subtitle = 'Combat mode', 'FC commands and starred speakers appear here.'
        elif mode == DISPLAY_MODE_BILINGUAL:
            title, subtitle = 'Bilingual mode', 'Translated messages appear here.'
        else:
            title, subtitle = 'No messages yet', 'New chat lines appear here.'
        self.text_browser.set_empty_message(title, subtitle)

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
        # A locked overlay is a pure HUD: it cannot be scrolled, so it hides
        # the scroll bar and always follows the newest line.
        self.text_browser.setVerticalScrollBarPolicy(
            Qt.ScrollBarPolicy.ScrollBarAlwaysOff
            if locked
            else Qt.ScrollBarPolicy.ScrollBarAsNeeded
        )
        if locked:
            self._scroll_to_latest()
        else:
            self._set_unread(self._unread)
        self.overlay_status = replace(self.overlay_status, interaction_locked=locked)
        self._render_status_header()
        if was_visible:
            self.show()

    def _render_status_header(self) -> None:
        status = self.overlay_status
        self.status_header.set_status(status, lock_hint=self._lock_hint())
        detail = status.detail or f"Translation provider: {status.provider}"
        # With the header visible, hovering chat text should not raise status
        # tooltips; without it, the window is the only place to show them.
        self.setToolTip('' if not self.status_header.isHidden() else detail)

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
        self._force_follow = True
        self.refresh_ui()

    def _schedule_geometry_save(self):
        if self.isVisible() and not getattr(self, '_applying_config', False):
            self._geometry_save_timer.start()

    def resizeEvent(self, event):
        rect = self.rect()
        # Keep the grip inside the panel's rounded bottom-right corner.
        self.grip.move(
            rect.right() - self.grip.width() - 1,
            rect.bottom() - self.grip.height() - 1,
        )
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
                    self._menu_anchor = event.globalPosition().toPoint()
                    self.show_context_menu()
                elif source is self.status_header:
                    # A click (not a drag) on the mode chip cycles the mode.
                    if self.status_header.region_at(event.position()) == 'mode':
                        self._cycle_display_mode()
                return True

        return super().eventFilter(source, event)

    def _begin_move(self, global_pos, button):
        self.is_moving = True
        self.has_moved = False
        self._move_button = button
        self.drag_start_pos = global_pos

    # ------------------------------------------------------------------
    # Context menu

    def show_context_menu(self):
        anchor = self._menu_anchor or QCursor.pos()
        self._menu_anchor = None
        menu = self._build_context_menu(self._message_at_global(anchor))
        try:
            menu.exec(anchor)
        finally:
            _dispose_menu(menu)

    def _build_context_menu(self, target: dict | None = None) -> QMenu:
        """Context menu, led by actions for the right-clicked ``target`` line."""
        menu = QMenu(self)
        menu.setToolTipsVisible(True)

        if target is not None:
            self._add_message_actions(menu, target)
            menu.addSeparator()

        mode_menu = menu.addMenu("Display mode")
        for mode, label in (
            (DISPLAY_MODE_MIRROR, "Mirror"),
            (DISPLAY_MODE_BILINGUAL, "Bilingual"),
            (DISPLAY_MODE_COMBAT, "Combat"),
        ):
            action = mode_menu.addAction(label)
            action.setCheckable(True)
            action.setChecked(self.display_mode == mode)
            action.setToolTip(_MODE_DESCRIPTIONS[mode])
            action.triggered.connect(
                lambda _checked=False, selected=mode: self.set_display_mode(selected)
            )
        mode_menu.setToolTipsVisible(True)

        # Session Management Submenu
        menu_session = menu.addMenu("Overlays")
        for spec in SESSION_SPECS:
            action = menu_session.addAction(spec.label)
            action.setCheckable(True)
            action.setChecked(
                bool(self.all_session_states.get(spec.session_id, False))
            )
            action.triggered.connect(
                lambda _checked=False, target_id=spec.session_id: (
                    self.session_toggled.emit(target_id)
                )
            )

        self._add_source_menu(menu)

        if target is None:
            self._add_latest_message_actions(menu)

        menu.addSeparator()
        lock_action = menu.addAction(f"Lock overlays\t{self._lock_hint()}")
        lock_action.setToolTip(
            "Make every overlay click-through for play. "
            "Unlock with the same shortcut or from the tray."
        )
        lock_action.triggered.connect(lambda: self.lock_requested.emit(True))

        menu.addSeparator()
        action_settings = menu.addAction("Settings…")
        action_settings.triggered.connect(self.open_settings)
        action_export = menu.addAction("Export recent history…")
        action_export.triggered.connect(self.export_chat)

        menu.addSeparator()
        action_exit = menu.addAction("Exit")
        action_exit.triggered.connect(self.exit_requested.emit)

        _prepare_menu(menu)
        return menu

    def _add_message_actions(self, menu: QMenu, message: dict) -> None:
        """Actions for the chat line that was right-clicked."""
        tokens = self._tokens
        sender = str(message.get('sender') or '')
        is_system = normalize_speaker_name(sender) in _SYSTEM_SENDERS
        role = self._role_for(message)
        badge = tokens.role_badges.get(role, '') if tokens else ''
        # No parent: setDefaultWidget() transfers ownership to the action, and
        # a second (menu) owner would double-free when the menu is deleted.
        title = QLabel(
            f"<span style='font-weight:600;'>{html.escape(sender or 'Message')}</span>{badge}"
            f"&nbsp;&nbsp;<span style='color:#7d8696;'>"
            f"{html.escape(str(message.get('timestamp') or ''))}</span>"
        )
        title.setObjectName('overlayMenuTitle')
        title.setTextFormat(Qt.TextFormat.RichText)
        title_action = QWidgetAction(menu)
        title_action.setDefaultWidget(title)
        menu.addAction(title_action)

        translated = (
            message.get('state') == TranslationState.TRANSLATED.value
            and bool(message.get('original_text'))
        )
        text = self._strip_html(message.get('text') or '')
        copy_action = menu.addAction("Copy translation" if translated else "Copy message")
        copy_action.triggered.connect(
            lambda _checked=False, value=text: QApplication.clipboard().setText(value)
        )
        if translated:
            original = self._strip_html(message.get('original_text') or '')
            original_action = menu.addAction("Copy original")
            original_action.triggered.connect(
                lambda _checked=False, value=original: QApplication.clipboard().setText(value)
            )
            improve = menu.addAction("Improve this translation…")
            improve.triggered.connect(
                lambda _checked=False, item=message: self._request_correction(item)
            )
        if sender and not is_system:
            roles_menu = menu.addMenu("Speaker role")
            self._populate_speaker_role_menu(roles_menu, sender)

    def _add_latest_message_actions(self, menu: QMenu) -> None:
        """Shortcuts for the newest speaker and translation (no line targeted)."""
        recent_speaker = next(
            (
                item.get('sender', '')
                for item in reversed(self.chat_history)
                if item.get('sender')
                and normalize_speaker_name(item.get('sender')) not in _SYSTEM_SENDERS
            ),
            '',
        )
        recent_translation = next(
            (
                item for item in reversed(self.chat_history)
                if item.get('state') == TranslationState.TRANSLATED.value
                and item.get('original_text')
            ),
            None,
        )
        if not recent_speaker and not recent_translation:
            return
        menu.addSeparator()
        if recent_speaker:
            roles_menu = menu.addMenu(f"Speaker: {recent_speaker}")
            self._populate_speaker_role_menu(roles_menu, recent_speaker)
        if recent_translation:
            correction_action = menu.addAction("Improve last translation…")
            correction_action.triggered.connect(
                lambda: self._request_correction(recent_translation)
            )

    def _add_source_menu(self, menu: QMenu) -> None:
        """Character, fleet or chat-source picker for this session."""
        if self.session_id == 'local':
            menu_chars = menu.addMenu("Select character")
            current_char_id = self.config.get('character_id')
            if self.available_characters:
                for char_info in sorted(
                    self.available_characters.values(),
                    key=lambda item: item.character_name,
                ):
                    system = f" ({char_info.system_name})" if char_info.system_name else ""
                    offline = " [Offline]" if not char_info.is_active else ""
                    action = menu_chars.addAction(f"{char_info.character_name}{system}{offline}")
                    action.setCheckable(True)
                    action.setChecked(char_info.character_id == current_char_id)
                    action.triggered.connect(
                        lambda _checked=False, cid=char_info.character_id: (
                            self.character_selected.emit(cid)
                        )
                    )
            else:
                menu_chars.addAction("No characters detected").setEnabled(False)

        elif self.session_id == 'fleet':
            menu_fleets = menu.addMenu("Switch fleet")
            current_fleet_id = getattr(self, 'selected_fleet_id', None)
            if self.available_fleets:
                # Newest first.
                for fleet_info in sorted(
                    self.available_fleets.values(),
                    key=lambda item: item.created_time,
                    reverse=True,
                ):
                    # EVE time, like chat timestamps and the Any Chat picker.
                    time_str = datetime.datetime.fromtimestamp(
                        fleet_info.created_time,
                        datetime.timezone.utc,
                    ).strftime("%H:%M")
                    label = f"Fleet - {fleet_info.listener_name} [{time_str} EVE]"
                    if not fleet_info.is_active:
                        label += " [Inactive]"
                    action = menu_fleets.addAction(label)
                    action.setCheckable(True)
                    action.setChecked(fleet_info.fleet_id == current_fleet_id)
                    action.triggered.connect(
                        lambda _checked=False, fid=fleet_info.fleet_id: (
                            self.fleet_selected.emit(fid)
                        )
                    )
            else:
                menu_fleets.addAction("No active fleets detected").setEnabled(False)

        elif self.session_id == 'any':
            menu_channels = menu.addMenu("Switch chat")
            current_channel_id = getattr(self, 'selected_channel_id', None)
            selectable_channels = list(self.available_channels.values())
            if selectable_channels:
                for channel in sorted(
                    selectable_channels,
                    key=lambda item: (
                        not bool(item.is_active),
                        item.channel_name.casefold(),
                        item.listener_name.casefold(),
                        -float(item.started_time),
                    ),
                ):
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
                    action = menu_channels.addAction(label)
                    action.setCheckable(True)
                    action.setChecked(channel.selection_id == current_channel_id)
                    action.triggered.connect(
                        lambda _checked=False, selection=channel.selection_id: (
                            self.channel_selected.emit(selection)
                        )
                    )
            else:
                menu_channels.addAction("No chat logs detected").setEnabled(False)

    def _message_at_global(self, global_pos) -> dict | None:
        """The rendered chat message under a global point, if any."""
        view = self.text_browser
        viewport = view.viewport()
        pos = viewport.mapFromGlobal(global_pos)
        if not viewport.rect().contains(pos) or view.document().isEmpty():
            return None
        block = view.cursorForPosition(pos).block()
        if not block.isValid():
            return None
        rect = view.document().documentLayout().blockBoundingRect(block).translated(
            -view.horizontalScrollBar().value(),
            -view.verticalScrollBar().value(),
        )
        slack = self._card_pad + 1
        if not (rect.top() - slack <= pos.y() <= rect.bottom() + slack):
            return None
        message_id = self._block_message_id(block)
        if not message_id:
            return None
        return next(
            (
                item for item in reversed(self.chat_history)
                if str(item.get('message_id')) == message_id
            ),
            None,
        )

    @staticmethod
    def _block_message_id(block) -> str | None:
        fragments = block.begin()
        for _ in range(3):
            if fragments.atEnd():
                break
            for name in fragments.fragment().charFormat().anchorNames():
                if name.startswith('msg-'):
                    return unquote(name[4:])
            fragments += 1
        return None

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
            action = menu.addAction(label)
            action.setCheckable(bool(role))
            action.setChecked(bool(role) and current == role)
            if not role:
                action.setEnabled(bool(current))
            action.triggered.connect(
                lambda _checked=False, value=role: self._set_speaker_role(sender, value)
            )

    def _set_speaker_role(self, sender: str, role: str) -> None:
        normalized = normalize_speaker_name(sender)
        if not normalized or normalized in _SYSTEM_SENDERS:
            return
        roles = dict(self.config.get('speaker_roles', {}))
        if role:
            roles[normalized] = role
        else:
            roles.pop(normalized, None)
        self.config['speaker_roles'] = roles
        # The manager echoes this change back; do not rebuild a second time.
        self._style_state['roles'] = _roles_key(roles)
        self.speaker_role_changed.emit(sender, role)
        self.refresh_ui()

    def _handle_anchor(self, url: QUrl) -> None:
        encoded = url.toString()
        if encoded.startswith('speaker:'):
            sender = unquote(encoded.split(':', 1)[1])
            menu = QMenu(self)
            self._populate_speaker_role_menu(menu, sender)
            _prepare_menu(menu)
            try:
                menu.exec(QCursor.pos())
            finally:
                _dispose_menu(menu)
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
            "Improve translation",
            f"Original:\n{original}\n\nSave an exact local correction:",
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
        text = re.sub(r'<[^>]+>', '', text)
        return html.unescape(text)

    # ------------------------------------------------------------------
    # Transcript rendering

    def _is_at_bottom(self) -> bool:
        bar = self.text_browser.verticalScrollBar()
        return bar.value() >= bar.maximum() - 4

    def _scroll_to_latest(self) -> None:
        self.text_browser.moveCursor(QTextCursor.MoveOperation.End)
        bar = self.text_browser.verticalScrollBar()
        bar.setValue(bar.maximum())
        self._set_unread(0)

    def _set_unread(self, count: int) -> None:
        self._unread = max(0, int(count))
        pill = self.text_browser.pill
        if self._unread and not self.interaction_locked and not self._is_at_bottom():
            pill.set_count(self._unread)
            self.text_browser.position_pill()
            pill.show()
            pill.raise_()
        elif not pill.isHidden():
            pill.hide()

    def _on_transcript_scrolled(self, _value: int) -> None:
        if self._unread and self._is_at_bottom():
            self._set_unread(0)

    def refresh_ui(self):
        """Rebuild the transcript from history (mode or style changes)."""
        self._pending_messages.clear()
        if self._flush_timer.isActive():
            self._flush_timer.stop()

        view = self.text_browser
        scroll_bar = view.verticalScrollBar()
        follow = self._force_follow or (
            bool(self.config.get('auto_scroll', True)) and self._is_at_bottom()
        )
        self._force_follow = False
        scroll_value = scroll_bar.value()

        visible = [msg for msg in self.chat_history if self._message_visible(msg)]
        view.setUpdatesEnabled(False)
        try:
            # Replace everything inside one edit block: clearing first would
            # flip the scroll bar off and on and lay the transcript out twice.
            cursor = QTextCursor(view.document())
            cursor.beginEditBlock()
            cursor.select(QTextCursor.SelectionType.Document)
            cursor.removeSelectedText()
            for index, msg_data in enumerate(visible):
                self._insert_message(cursor, msg_data, first=index == 0)
            if not visible:
                cursor.setBlockFormat(QTextBlockFormat())
                cursor.block().setUserState(-1)
            cursor.endEditBlock()
        finally:
            view.setUpdatesEnabled(True)
        self._needs_full_refresh = False
        self._prune_fragment_cache()

        if follow:
            self._scroll_to_latest()
        else:
            scroll_bar.setValue(min(scroll_value, scroll_bar.maximum()))
            self._set_unread(self._unread)

    def _message_fragment(self, msg_data: dict) -> tuple[int, int, QTextDocumentFragment]:
        """Parsed rich text of a message, cached until its rendering changes.

        Message dicts are replaced, never mutated, when a line updates, so
        the dict identity plus the render version and speaker role key it.
        """
        key = (self._render_version, self._role_for(msg_data))
        entry = self._fragment_cache.get(id(msg_data))
        if entry is not None and entry[0] is msg_data and entry[1] == key:
            return entry[2], entry[3], entry[4]
        kind, state, inner = self._render_message(msg_data)
        fragment = QTextDocumentFragment.fromHtml(inner, self.text_browser.document())
        self._fragment_cache[id(msg_data)] = (msg_data, key, kind, state, fragment)
        return kind, state, fragment

    def _prune_fragment_cache(self, slack: int = 0) -> None:
        if len(self._fragment_cache) > len(self.chat_history) + slack:
            live = {id(msg_data) for msg_data in self.chat_history}
            self._fragment_cache = {
                key: entry for key, entry in self._fragment_cache.items() if key in live
            }

    def _insert_message(self, cursor: QTextCursor, msg_data: dict, *, first: bool) -> None:
        """Insert one message as one block with its explicit block format."""
        kind, state, fragment = self._message_fragment(msg_data)
        block_format = self._block_formats[kind]
        if first:
            cursor.setBlockFormat(block_format)
        else:
            cursor.insertBlock(block_format, self._plain_char_format)
        cursor.insertFragment(fragment)
        # Inserting rich text can replace the block format of an empty block.
        cursor.setBlockFormat(block_format)
        cursor.block().setUserState(state)

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
        view = self.text_browser
        follow = bool(self.config.get('auto_scroll', True)) and self._is_at_bottom()
        document = view.document()
        view.setUpdatesEnabled(False)
        try:
            cursor = QTextCursor(document)
            cursor.movePosition(QTextCursor.MoveOperation.End)
            cursor.beginEditBlock()
            first = document.isEmpty()
            for msg_data in pending:
                self._insert_message(cursor, msg_data, first=first)
                first = False
            cursor.endEditBlock()
        finally:
            view.setUpdatesEnabled(True)
        # Lines that left the bounded history no longer need their fragments.
        self._prune_fragment_cache(slack=16)

        if follow:
            self._scroll_to_latest()
        else:
            # Reading older lines: keep the position and offer a jump.
            self._set_unread(self._unread + len(pending))

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

    @staticmethod
    def _command_field(command, name: str, default=''):
        if isinstance(command, dict):
            return command.get(name, default)
        return getattr(command, name, default)

    def _format_message_html(self, msg_data):
        """HTML of one message block (for diagnostics and tests).

        Rendering inserts the same inline HTML with an explicit block format,
        see :meth:`_insert_message`.
        """
        _kind, state, inner = self._render_message(msg_data)
        return f"<p style='-qt-user-state:{state};'>{inner}</p>"

    def _render_message(self, msg_data: dict) -> tuple[int, int, str]:
        """Return ``(kind, block_user_state, inline_html)`` for a message."""
        state = msg_data.get('state')
        if not state:
            state = (
                TranslationState.TRANSLATED.value
                if msg_data.get('is_translated')
                else TranslationState.ORIGINAL.value
            )
        sender = str(msg_data.get('sender', ''))
        commands = tuple(msg_data.get('commands') or ())
        if commands and self.config.get('command_cards', True):
            return self._render_card(msg_data, commands, state, sender)
        if normalize_speaker_name(sender) in _SYSTEM_SENDERS:
            return self._render_system(msg_data, sender)
        return self._render_line(msg_data, state, sender)

    def _colors(self) -> tuple[str, str, str]:
        return (
            self.config.get('color_default', DEFAULT_COLOR_DEFAULT),
            self.config.get('color_translated', DEFAULT_COLOR_TRANSLATED),
            self.config.get('color_highlight', DEFAULT_COLOR_HIGHLIGHT),
        )

    @staticmethod
    def _highlighted(text, highlight: str) -> str:
        # The pipeline marks EVE names with a "color: yellow" span.
        return str(text or '').replace("color: yellow", f"color: {highlight}")

    @staticmethod
    def _short_time(timestamp: str) -> str:
        """``HH:MM`` for an ``HH:MM:SS`` clock; anything else unchanged."""
        match = _CLOCK_WITH_SECONDS.fullmatch(timestamp)
        return match.group(1) if match else timestamp

    def _time_html(self, msg_data: dict, style: str) -> str:
        # Minutes keep the time column narrow; seconds stay on hover.
        full = str(msg_data.get('timestamp', ''))
        short = self._short_time(full)
        title = f" title='{html.escape(full, quote=True)}'" if short != full else ''
        return f"<span style='{style}'{title}>{html.escape(short)}</span>"

    def _stamp_html(self, msg_data: dict) -> str:
        stamp = self._time_html(msg_data, self._tokens.timestamp)
        # A named anchor lets a later translation update find this block.
        anchor = self._message_anchor(msg_data.get('message_id'))
        return f"<a name='{anchor}'>{stamp}</a>" if anchor else stamp

    def _sender_html(self, sender: str, role: str, color: str, *, badge: bool = True) -> str:
        role_badge = self._tokens.role_badges.get(role, '') if badge else ''
        return (
            f"<a href='speaker:{quote(sender, safe='')}' "
            f"style='color:{color};text-decoration:none;font-weight:600;'>"
            f"{html.escape(sender)}</a>{role_badge}"
        )

    def _language_tag(self, msg_data: dict) -> str:
        code = normalize_language_code(msg_data.get('detected_language'), '')
        code = code.split('-', 1)[0].upper() if code and code != 'unknown' else ''
        return html.escape(code[:3]) if code else '&#8618;'

    def _origin_html(self, msg_data: dict, original: str) -> str:
        """Second line of a translated message: language tag and original."""
        tokens = self._tokens
        correction = ''
        if msg_data.get('message_id'):
            message_id = quote(str(msg_data.get('message_id')), safe='')
            correction = (
                f"&nbsp;&nbsp;<a href='correct:{message_id}' style='{tokens.edit}' "
                "title='Improve this translation'>&#9998;</a>"
            )
        return (
            f"<span style='{tokens.tag}'>{self._language_tag(msg_data)}</span>&nbsp;"
            f"<span style='{tokens.original}'>{original}</span>{correction}"
        )

    def _adds_information(self, original: str, translated: str) -> bool:
        """Whether an original line says more than its translation."""
        source = self._strip_html(original).strip()
        return bool(source) and source.casefold() != self._strip_html(translated).strip().casefold()

    def _state_note(self, msg_data: dict, state: str) -> str:
        tokens = self._tokens
        if state == TranslationState.PENDING.value:
            return (
                f"&nbsp;&nbsp;<span style='color:{tokens.faint};font-style:italic;"
                f"font-size:{tokens.note_pt}pt;'>translating&#8230;</span>"
            )
        if state == TranslationState.FAILED.value:
            # Compact marker; the reason is in the tooltip and, for provider
            # outages, once in the status header instead of on every line.
            error = html.escape(
                msg_data.get('error') or 'Translation unavailable',
                quote=True,
            )
            return (
                f"&nbsp;&nbsp;<span style='color:{WARNING_COLOR};"
                f"font-size:{tokens.note_pt}pt;' title=\"{error}\">"
                "&#9888;&nbsp;not&nbsp;translated</span>"
            )
        return ''

    def _render_line(self, msg_data: dict, state: str, sender: str) -> tuple[int, int, str]:
        tokens = self._tokens
        col_def, col_trans, col_high = self._colors()
        role = self._role_for(msg_data)
        text = self._highlighted(msg_data.get('text'), col_high)
        if state == TranslationState.TRANSLATED.value:
            original = self._highlighted(msg_data.get('original_text'), col_high)
            body = f"<span style='color:{col_trans};'>{text}</span>"
            if self._adds_information(original, text):
                body += f"<br>{self._origin_html(msg_data, original)}"
        else:
            body = (
                f"<span style='color:{col_def};'>{text}</span>"
                f"{self._state_note(msg_data, state)}"
            )
        inner = (
            f"{self._stamp_html(msg_data)}{tokens.gap}"
            f"{self._sender_html(sender, role, col_def)}{tokens.separator}{body}"
        )
        return _KIND_LINE, _block_state(_KIND_LINE, _ROLE_INDEX.get(role, 0)), inner

    def _render_system(self, msg_data: dict, sender: str) -> tuple[int, int, str]:
        tokens = self._tokens
        _col_def, _col_trans, col_high = self._colors()
        text = self._highlighted(msg_data.get('text'), col_high)
        prefix = (
            f"<span style='{tokens.tag}'>EVE</span>&nbsp;"
            if normalize_speaker_name(sender) == 'eve system'
            else ''
        )
        inner = (
            f"{self._stamp_html(msg_data)}{tokens.gap}{prefix}"
            f"<span style='{tokens.system}'>{text}</span>"
        )
        return _KIND_SYSTEM, _block_state(_KIND_SYSTEM), inner

    def _render_card(self, msg_data: dict, commands: tuple, state: str,
                     sender: str) -> tuple[int, int, str]:
        """Compact command card.

        Command labels first, then one ``trust · speaker · time`` line, then
        the translation and source line only when they add information.
        """
        tokens = self._tokens
        col_def, col_trans, col_high = self._colors()
        role = self._role_for(msg_data)
        shown = commands[:3]
        trusted = all(
            command_is_trusted(
                role,
                str(self._command_field(command, 'code')).upper(),
                str(self._command_field(command, 'category', 'COMMAND')).upper(),
            )
            for command in shown
        )
        categories = []
        for command in shown:
            category = str(self._command_field(command, 'category', '') or 'COMMAND').upper()
            if category not in categories:
                categories.append(category)

        label_color = tokens.label_trusted if trusted else tokens.label_muted
        # Unverified cards stay quiet: even the target name is muted.
        argument_color = (
            col_high if trusted
            else _mix(_color(col_high, DEFAULT_COLOR_HIGHLIGHT), self._theme.text_faint, 0.45).name()
        )
        label_lines = []
        labels = set()
        for command in shown:
            code = str(self._command_field(command, 'code')).upper()
            label = str(self._command_field(command, 'display_label', '') or code.replace('_', ' '))
            labels.add(label.casefold())
            argument = str(self._command_field(command, 'argument', '') or '')
            if argument:
                labels.add(f"{label} {argument}".casefold())
            argument_html = (
                f"&nbsp;&nbsp;<span style='{tokens.card_argument}color:{argument_color};'>"
                f"{html.escape(argument)}</span>"
                if argument else ''
            )
            label_lines.append(
                f"<span style='{tokens.card_label}color:{label_color};'>"
                f"{html.escape(label)}</span>{argument_html}"
            )
        # A named anchor lets a later translation update find this block.
        anchor = self._message_anchor(msg_data.get('message_id'))
        if anchor and label_lines:
            label_lines[0] = f"<a name='{anchor}'>{label_lines[0]}</a>"

        text = self._highlighted(msg_data.get('text'), col_high)
        original = self._highlighted(
            msg_data.get('original_text') or msg_data.get('text'), col_high
        )
        # The card already states the command; repeating an original that is
        # in the reader's own language only takes space.
        detected = normalize_language_code(msg_data.get('detected_language'), '')
        target = normalize_language_code(self.config.get('target_language', 'en'), 'en')
        show_original = not (detected and detected == target)
        said = origin = ''
        if state == TranslationState.TRANSLATED.value:
            plain = self._strip_html(text).strip(' .!').casefold()
            if plain and plain not in labels:
                said = f"<span style='color:{col_trans};'>{text}</span>"
            if msg_data.get('original_text') and self._adds_information(original, text):
                origin = self._origin_html(msg_data, original)
        elif show_original:
            said = f"<span style='color:{col_def};'>{original}</span>"

        trust_label = f"{role.upper()} VERIFIED" if trusted else 'UNVERIFIED'
        meta_color = VERIFIED_COLOR if trusted else tokens.faint
        check = '&#10003;&nbsp;' if trusted else ''
        dot = f"<span style='color:{tokens.faint};'>&nbsp;&#183;&nbsp;</span>"
        # A short original whose translation only repeats the label rides on
        # the meta line instead of taking a line of its own.
        fold_origin = (
            bool(origin) and not said
            and len(self._strip_html(original)) <= 24
        )
        meta = (
            f"<span style='font-size:{tokens.card_small_pt}pt;'>"
            f"<span style='{tokens.meta}color:{meta_color};'>{check}{trust_label}</span>"
            f"{dot}{self._sender_html(sender, role, col_def, badge=False)}{dot}"
            f"{self._time_html(msg_data, f'color:{tokens.faint};')}"
            f"{dot + origin if fold_origin else ''}"
            f"{self._state_note(msg_data, state)}</span>"
        )
        source = said
        if origin and not fold_origin:
            source += f"{'&nbsp;&nbsp;' if said else ''}{origin}"
        lines = [*label_lines, meta]
        if source:
            lines.append(f"<span style='font-size:{tokens.card_small_pt}pt;'>{source}</span>")
        accent = _CATEGORY_INDEX.get(categories[0] if categories else '', 0)
        return _KIND_CARD, _block_state(_KIND_CARD, accent, trusted), '<br>'.join(lines)

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

        A rebuild re-renders up to 100 messages (~15-20 ms per overlay) and
        can move the reader's scroll position. Every message, command cards
        included, is one block, so an update rewrites just that block.
        Returns ``False`` when a full refresh is required.
        """
        if self._needs_full_refresh:
            return False
        message_id = msg_data.get('message_id')
        # Not flushed yet: the pending render will use the new state.
        for index, pending in enumerate(self._pending_messages):
            if pending.get('message_id') == message_id:
                self._pending_messages[index] = msg_data
                return True
        block = self._find_message_block(message_id)
        if block is None:
            return False
        kind, state, fragment = self._message_fragment(msg_data)
        view = self.text_browser
        scroll_bar = view.verticalScrollBar()
        at_bottom = self._is_at_bottom()
        scroll_value = scroll_bar.value()
        cursor = QTextCursor(block)
        cursor.beginEditBlock()
        cursor.movePosition(QTextCursor.MoveOperation.StartOfBlock)
        cursor.movePosition(
            QTextCursor.MoveOperation.EndOfBlock,
            QTextCursor.MoveMode.KeepAnchor,
        )
        cursor.insertFragment(fragment)
        cursor.setBlockFormat(self._block_formats[kind])
        cursor.block().setUserState(state)
        cursor.endEditBlock()
        if self.config.get('auto_scroll', True) and at_bottom:
            scroll_bar.setValue(scroll_bar.maximum())
        else:
            scroll_bar.setValue(min(scroll_value, scroll_bar.maximum()))
        # Card decorations extend into the block margins.
        view.viewport().update()
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
            # The anchor sits on the first text of the block.
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
        self._fragment_cache.clear()
        self.text_browser.clear()
        self._set_unread(0)

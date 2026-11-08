"""Render the documentation screenshots from the application's own widgets.

    python scripts/render_screenshots.py [--out docs/screenshots] [--scale 2]

The overlays, menus and dialogs are the real UI fed with fictional sample
chat; only the space backdrop is generated. Rendering is deterministic, so
the screenshots can be refreshed whenever the interface changes. Run it on
Windows, where the overlay fonts (Segoe UI, Bahnschrift) are available.
Windows briefly appear on screen while they are captured.
"""

from __future__ import annotations

import argparse
import math
import os
import random
import sys
import time
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

from PySide6.QtCore import QPoint, QPointF, QRectF, Qt  # noqa: E402
from PySide6.QtGui import (  # noqa: E402
    QColor,
    QFont,
    QImage,
    QLinearGradient,
    QPainter,
    QPainterPath,
    QPen,
    QRadialGradient,
)
from PySide6.QtWidgets import QApplication  # noqa: E402

from src.app_config import get_default_config  # noqa: E402
from src.core.combat import CommandMatch  # noqa: E402
from src.core.message import ProcessedMessage, TranslationState  # noqa: E402
from src.gui.overlay import OverlayStatus, OverlayWindow, _dispose_menu  # noqa: E402

LISTENER = "Emyth Juk"
FLEET_ROLES = {
    "kaelen drake": "fc",
    "zhao yun": "fc",
    "wang lei": "starred",
    "kim min-jun": "scout",
}
CAPTION_COLOR = QColor("#7dd3fc")
MARGIN = 36


def _place(name: str) -> str:
    """Mark an EVE name the way the translation pipeline highlights it."""
    return f"<span style='color: yellow;'>{name}</span>"


def _message(index, sender, timestamp, text, *, original="", state="original",
             language="en", relevant=False, commands=(), error="", session="fleet"):
    state = TranslationState(state)
    return ProcessedMessage(
        message_id=f"{session}:docs:{index}",
        session_id=session,
        source_id="docs",
        text=text,
        original_text=original,
        sender=sender,
        timestamp=timestamp,
        state=state,
        provider="DeepL" if state == TranslationState.TRANSLATED else "",
        detected_language=language,
        error=error,
        translation_relevant=relevant or state != TranslationState.ORIGINAL,
        commands=tuple(commands),
        revision=1,
        lookup_text=original or text,
    )


def fleet_messages() -> list[ProcessedMessage]:
    align = CommandMatch("ALIGN", "MOVE", argument="Tama", source_clause="align Tama")
    jump = CommandMatch("JUMP", "MOVE", source_clause="跳", source_language="zh")
    hold = CommandMatch("HOLD", "HOLD", source_clause="hold cloak")
    primary = CommandMatch(
        "PRIMARY", "TARGET", argument="Vexor Navy Issue",
        source_clause="primary Vexor Navy Issue",
    )
    rogue = CommandMatch("PRIMARY", "TARGET", argument="Drake", source_clause="primary Drake")
    return [
        _message(1, "Kaelen Drake", "19:38:40",
                 f"o7 fleet, x up and form on me at the {_place('Jita')} 4-4 undock"),
        _message(2, "Wang Lei", "19:39:05", "Hello everyone! Where are we heading tonight?",
                 original="大家好！今晚我们去哪里？", state="translated", language="zh"),
        _message(3, "Kaelen Drake", "19:39:31", f"align {_place('Tama')}", commands=(align,)),
        _message(4, "Li Na", "19:39:58", "Logi broadcast please, thanks",
                 original="来个后勤广播，谢谢", state="translated", language="zh"),
        _message(5, "Dmitri Volkov", "19:40:12", "Добрый вечер, пилоты",
                 state="pending", language="ru"),
        _message(6, "Zhao Yun", "19:40:40", "Jump", original="跳",
                 state="translated", language="zh", commands=(jump,)),
        _message(7, "Ivy Marlowe", "19:41:02", "primary Drake", commands=(rogue,)),
        _message(8, "Tanaka Ren", "19:41:20", "ゲートでワープしてください",
                 state="failed", language="ja", error="DeepL quota exceeded"),
        _message(9, "Kim Min-jun", "19:41:45",
                 f"{_place('Tama')} gate: 12 hostiles, Sabre on grid"),
        _message(10, "Kaelen Drake", "19:42:07", "hold cloak", commands=(hold,)),
        _message(11, "Chen Hao", "19:42:30", f"Stay aligned, {_place('Tama')} gate is hot",
                 original=f"保持对齐，{_place('Tama')} 门很热", state="translated",
                 language="zh"),
        _message(12, "Kaelen Drake", "19:42:52", "primary Vexor Navy Issue",
                 commands=(primary,)),
    ]


def local_messages() -> list[ProcessedMessage]:
    return [
        _message(1, "Sergei Orlov", "19:35:10", "Anyone selling ammo in this system?",
                 original="Кто-нибудь продаёт боеприпасы в этой системе?",
                 state="translated", language="ru", session="local"),
        _message(2, "Liu Yang", "19:36:02",
                 f"Gate camp on the {_place('Amamake')} gate, be careful",
                 original=f"{_place('Amamake')} 门有人蹲，小心", state="translated",
                 language="zh", session="local"),
        _message(3, "Ana Souza", "19:36:40", "Thanks for the warning!",
                 original="Obrigada pelo aviso!", state="translated", language="pt",
                 session="local"),
        _message(4, "Park Ji-hoon", "19:37:15", "Good luck out there",
                 original="행운을 빌어요", state="translated", language="ko",
                 session="local"),
        _message(5, "Diego Ramos", "19:37:48",
                 f"The wormhole to {_place('Jita')} closes soon",
                 original=f"El agujero de gusano a {_place('Jita')} se cierra pronto",
                 state="translated", language="es", session="local"),
    ]


def intel_messages() -> list[ProcessedMessage]:
    return [
        _message(1, "Sven Halvorsen", "19:41:02", f"{_place('1DQ1-A')} +3 Cerberus",
                 session="any"),
        _message(2, "Zhang Wei", "19:41:30", f"{_place('B-9C24')} clear",
                 original=f"{_place('B-9C24')} 没人", state="translated", language="zh",
                 session="any"),
        _message(3, "Marta Kowalska", "19:42:11", f"{_place('Y-2ANO')} Sabre bubble on gate",
                 session="any"),
        _message(4, "Ilya Petrov", "19:42:40",
                 f"{_place('Y-2ANO')} +5, heading to {_place('1DQ1-A')}",
                 original=f"{_place('Y-2ANO')} +5, идут в {_place('1DQ1-A')}",
                 state="translated", language="ru", session="any"),
        _message(5, "Sven Halvorsen", "19:43:05", f"{_place('1DQ1-A')} clr", session="any"),
    ]


def settle(seconds: float = 0.35) -> None:
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        QApplication.processEvents()
        time.sleep(0.01)


def make_overlay(session: str, *, width: int, height: int, mode: str = "mirror",
                 messages=(), welcome: str = "", status: dict | None = None,
                 locked: bool = False) -> OverlayWindow:
    config = get_default_config()
    merged = {**config["shared"], **config["sessions"][session]}
    merged.update({
        "x": 80, "y": 80, "w": width, "h": height, "display_mode": mode,
        "speaker_roles": FLEET_ROLES, "target_language": "en",
    })
    overlay = OverlayWindow(session, merged)
    overlay.show()
    settle(0.1)
    overlay.clear_messages()
    if welcome:
        overlay.add_message(welcome, "System", "19:38:02")
    for message in messages:
        overlay.upsert_processed_message(message)
    labels = {"fleet": "Fleet", "local": "Local", "any": "Any Chat"}
    contexts = {"fleet": LISTENER, "local": f"{LISTENER} · B-9C24",
                "any": f"Intel · {LISTENER}"}
    values = {
        "channel": labels[session], "state": "Active", "context": contexts[session],
        "provider": "DeepL", "provider_available": True,
        "detail": "Translation provider: DeepL", "display_mode": mode,
        "interaction_locked": locked,
    }
    values.update(status or {})
    overlay.update_status(OverlayStatus(**values))
    if locked:
        overlay.set_interaction_locked(True)
    settle()
    overlay.refresh_ui()
    settle(0.15)
    return overlay


class Canvas:
    """A backdrop image painted in logical coordinates at the render scale."""

    def __init__(self, width: int, height: int, scale: float, seed: int):
        self.width, self.height, self.scale = width, height, scale
        self.image = QImage(round(width * scale), round(height * scale),
                            QImage.Format.Format_ARGB32_Premultiplied)
        self.image.setDevicePixelRatio(scale)
        painter = QPainter(self.image)
        try:
            paint_space(painter, width, height, random.Random(seed))
        finally:
            painter.end()

    def paste(self, pixmap, x: float, y: float) -> None:
        painter = QPainter(self.image)
        try:
            painter.drawPixmap(QPointF(x, y), pixmap)
        finally:
            painter.end()

    def paste_panel(self, pixmap, x: float, y: float, radius: float = 9.0) -> None:
        """Paste a dialog capture as a rounded panel with a soft shadow."""
        ratio = pixmap.devicePixelRatio()
        rect = QRectF(x, y, pixmap.width() / ratio, pixmap.height() / ratio)
        painter = QPainter(self.image)
        try:
            painter.setRenderHint(QPainter.RenderHint.Antialiasing)
            painter.setPen(Qt.PenStyle.NoPen)
            for spread, alpha in ((16, 14), (10, 22), (6, 32), (3, 44)):
                painter.setBrush(QColor(0, 0, 0, alpha))
                painter.drawRoundedRect(rect.adjusted(-spread, -spread + 4, spread, spread + 4),
                                        radius + spread, radius + spread)
            path = QPainterPath()
            path.addRoundedRect(rect, radius, radius)
            painter.setClipPath(path)
            painter.drawPixmap(QPointF(x, y), pixmap)
            painter.setClipping(False)
            painter.setPen(QPen(QColor(255, 255, 255, 46), 1))
            painter.setBrush(Qt.BrushStyle.NoBrush)
            painter.drawPath(path)
        finally:
            painter.end()

    def caption(self, text: str, x: float, y: float) -> None:
        painter = QPainter(self.image)
        try:
            painter.setRenderHint(QPainter.RenderHint.TextAntialiasing)
            font = QFont("Bahnschrift")
            font.setPointSizeF(8.5)
            font.setWeight(QFont.Weight.DemiBold)
            font.setLetterSpacing(QFont.SpacingType.AbsoluteSpacing, 1.2)
            font.setCapitalization(QFont.Capitalization.AllUppercase)
            painter.setFont(font)
            painter.setPen(CAPTION_COLOR)
            painter.drawText(QPointF(x, y), text)
        finally:
            painter.end()

    def save(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        if not self.image.save(str(path), "PNG", 0):
            raise OSError(f"could not write {path}")
        print(f"wrote {path.name} ({path.stat().st_size // 1024} KB)")


def paint_space(painter: QPainter, width: int, height: int, rng: random.Random) -> None:
    """Deep space: nebula clouds, a starfield and a planet limb."""
    painter.setRenderHint(QPainter.RenderHint.Antialiasing)
    base = QLinearGradient(0, 0, width * 0.35, height)
    base.setColorAt(0.0, QColor("#071022"))
    base.setColorAt(1.0, QColor("#020409"))
    painter.fillRect(QRectF(0, 0, width, height), base)

    def blob(color: str, cx: float, cy: float, radius: float, alpha: float) -> None:
        gradient = QRadialGradient(QPointF(cx, cy), radius)
        inner = QColor(color)
        inner.setAlphaF(alpha)
        outer = QColor(color)
        outer.setAlphaF(0.0)
        gradient.setColorAt(0.0, inner)
        gradient.setColorAt(1.0, outer)
        bounds = QRectF(cx - radius, cy - radius, radius * 2, radius * 2)
        painter.fillRect(bounds.intersected(QRectF(0, 0, width, height)), gradient)

    # A band of nebula clouds running diagonally across the frame.
    for _ in range(26):
        t = rng.random()
        cx = width * (t * 1.2 - 0.1) + rng.gauss(0, width * 0.06)
        cy = height * (0.85 - t * 0.7) + rng.gauss(0, height * 0.12)
        color = rng.choice(("#1b6f8f", "#1b6f8f", "#3a2f7a", "#5a2c6e", "#8a5a24"))
        blob(color, cx, cy, rng.uniform(0.12, 0.34) * max(width, height),
             rng.uniform(0.10, 0.28))
    for _ in range(9):  # dust lanes
        blob("#000000", rng.uniform(0, width), rng.uniform(0, height),
             rng.uniform(0.08, 0.2) * max(width, height), rng.uniform(0.25, 0.45))

    painter.setPen(Qt.PenStyle.NoPen)
    for _ in range(int(width * height / 520)):
        x, y = rng.uniform(0, width), rng.uniform(0, height)
        tint = rng.choice(((255, 255, 255), (200, 225, 255), (255, 236, 210)))
        size = rng.choice((0.35, 0.45, 0.55, 0.7, 0.9))
        painter.setBrush(QColor(*tint, rng.randint(70, 230)))
        painter.drawEllipse(QPointF(x, y), size, size)
    for _ in range(max(4, int(width * height / 60000))):  # bright stars
        x, y = rng.uniform(0, width), rng.uniform(0, height)
        glow = QRadialGradient(QPointF(x, y), 7)
        glow.setColorAt(0.0, QColor(210, 232, 255, 170))
        glow.setColorAt(1.0, QColor(210, 232, 255, 0))
        painter.setBrush(glow)
        painter.drawEllipse(QPointF(x, y), 7, 7)
        painter.setPen(QPen(QColor(220, 238, 255, 90), 0.6))
        painter.drawLine(QPointF(x - 9, y), QPointF(x + 9, y))
        painter.drawLine(QPointF(x, y - 9), QPointF(x, y + 9))
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(QColor(255, 255, 255, 240))
        painter.drawEllipse(QPointF(x, y), 1.1, 1.1)

    # Planet limb in the lower right, lit from the upper left.
    radius = height * 0.62
    center = QPointF(width + radius * 0.18, height + radius * 0.35)
    body = QRadialGradient(QPointF(center.x() - radius * 0.55, center.y() - radius * 0.6),
                           radius * 1.4)
    body.setColorAt(0.0, QColor("#1c3550"))
    body.setColorAt(0.45, QColor("#0a1626"))
    body.setColorAt(1.0, QColor("#03070d"))
    painter.setBrush(body)
    painter.drawEllipse(center, radius, radius)
    for width_px, alpha in ((9, 18), (5, 40), (2.2, 110), (1, 190)):
        painter.setPen(QPen(QColor(120, 200, 255, alpha), width_px))
        painter.setBrush(Qt.BrushStyle.NoBrush)
        start = 95 * 16
        painter.drawArc(QRectF(center.x() - radius, center.y() - radius, radius * 2, radius * 2),
                        start, 80 * 16)
    painter.setPen(Qt.PenStyle.NoPen)


def shot_mirror(out: Path, scale: float) -> None:
    overlay = make_overlay("fleet", width=390, height=560, messages=fleet_messages(),
                           welcome="Listening to Fleet chat.")
    canvas = Canvas(390 + 2 * MARGIN, 560 + 2 * MARGIN, scale, seed=3)
    canvas.paste(overlay.grab(), MARGIN, MARGIN)
    canvas.save(out / "overlay_mirror.png")
    overlay.close()


def shot_combat(out: Path, scale: float) -> None:
    overlay = make_overlay("fleet", width=390, height=388, mode="combat",
                           messages=fleet_messages(), welcome="Listening to Fleet chat.")
    canvas = Canvas(390 + 2 * MARGIN, 388 + 2 * MARGIN, scale, seed=5)
    canvas.paste(overlay.grab(), MARGIN, MARGIN)
    canvas.save(out / "overlay_combat.png")
    overlay.close()


def shot_sessions(out: Path, scale: float) -> None:
    fleet_h, local_h, gap = 560, 300, 24
    intel_h = fleet_h - local_h - gap
    fleet = make_overlay("fleet", width=390, height=fleet_h, messages=fleet_messages(),
                         welcome="Listening to Fleet chat.")
    local = make_overlay("local", width=350, height=local_h, mode="bilingual",
                         messages=local_messages())
    intel = make_overlay("any", width=350, height=intel_h, messages=intel_messages(),
                         welcome=f"Listening to <b>Intel</b> for <b>{LISTENER}</b>.")
    width = MARGIN * 2 + 390 + gap + 350
    canvas = Canvas(width, fleet_h + 2 * MARGIN, scale, seed=7)
    canvas.paste(fleet.grab(), MARGIN, MARGIN)
    canvas.paste(local.grab(), MARGIN + 390 + gap, MARGIN)
    canvas.paste(intel.grab(), MARGIN + 390 + gap, MARGIN + local_h + gap)
    canvas.save(out / "overlay_sessions.png")
    for overlay in (fleet, local, intel):
        overlay.close()


def shot_context_menu(out: Path, scale: float) -> None:
    overlay = make_overlay("fleet", width=390, height=430, messages=fleet_messages(),
                           welcome="Listening to Fleet chat.")
    view = overlay.text_browser
    document = view.document()
    target_block = next(
        block for block in _blocks(document)
        if "Logi broadcast" in block.text()
    )
    rect = document.documentLayout().blockBoundingRect(target_block).translated(
        0, -view.verticalScrollBar().value()
    )
    click = QPoint(150, int(rect.top() + 8))
    target = overlay._message_at_global(view.viewport().mapToGlobal(click))
    menu = overlay._build_context_menu(target)
    menu.popup(QPoint(2000, 200))
    settle(0.25)
    display = next(action for action in menu.actions() if action.text() == "Display mode")
    menu.setActiveAction(display)
    settle(0.2)
    submenu = display.menu()
    submenu.popup(QPoint(2400, 200))
    settle(0.25)
    menu_pixmap, sub_pixmap = menu.grab(), submenu.grab()
    item = menu.actionGeometry(display)
    submenu.close()
    menu.close()

    overlay_origin = QPointF(MARGIN, MARGIN)
    viewport_origin = view.viewport().mapTo(overlay, QPoint(0, 0))
    menu_x = overlay_origin.x() + viewport_origin.x() + click.x()
    menu_y = overlay_origin.y() + viewport_origin.y() + click.y()
    sub_x = menu_x + menu_pixmap.width() / menu_pixmap.devicePixelRatio() - 6
    sub_y = menu_y + item.top() - 5
    width = math.ceil(sub_x + sub_pixmap.width() / sub_pixmap.devicePixelRatio() + MARGIN)
    height = math.ceil(max(MARGIN * 2 + 430,
                           menu_y + menu_pixmap.height() / menu_pixmap.devicePixelRatio()
                           + MARGIN))
    canvas = Canvas(width, height, scale, seed=9)
    canvas.paste(overlay.grab(), overlay_origin.x(), overlay_origin.y())
    canvas.paste(menu_pixmap, menu_x, menu_y)
    canvas.paste(sub_pixmap, sub_x, sub_y)
    canvas.save(out / "overlay_context_menu.png")
    _dispose_menu(menu)
    overlay.close()


def _blocks(document):
    block = document.begin()
    while block.isValid():
        yield block
        block = block.next()


def shot_header_states(out: Path, scale: float) -> None:
    messages = {m.message_id: m for m in fleet_messages()}
    rows = [
        ("Live", {}, [messages["fleet:docs:4"]], "", False, "mirror"),
        ("Waiting for a chat log", {"state": "Waiting"}, [], "Listening to Fleet chat.",
         False, "mirror"),
        ("Translation provider unavailable",
         {"provider_available": False,
          "detail": "DeepL quota exceeded; showing originals until it recovers."},
         [messages["fleet:docs:8"]], "", False, "mirror"),
        ("Error", {"state": "Error", "detail": "The chat log could not be opened."},
         [], "Retrying when the chat log is readable.", False, "mirror"),
        ("Locked (click-through)", {}, [messages["fleet:docs:6"]], "", True, "combat"),
    ]
    strip_w, strip_h, caption_h, gap = 400, 88, 22, 14
    height = MARGIN * 2 + len(rows) * (caption_h + strip_h) + (len(rows) - 1) * gap
    canvas = Canvas(strip_w + 2 * MARGIN, height, scale, seed=13)
    y = MARGIN
    for label, status, items, welcome, locked, mode in rows:
        overlay = make_overlay("fleet", width=strip_w, height=strip_h, mode=mode,
                               messages=items, welcome=welcome, status=status,
                               locked=locked)
        canvas.caption(label, MARGIN + 2, y + 13)
        canvas.paste(overlay.grab(), MARGIN, y + caption_h)
        overlay.close()
        y += caption_h + strip_h + gap
    canvas.save(out / "overlay_header_states.png")


def shot_new_messages(out: Path, scale: float) -> None:
    history = fleet_messages()
    overlay = make_overlay("fleet", width=390, height=300, messages=history[:9],
                           welcome="Listening to Fleet chat.")
    overlay.text_browser.verticalScrollBar().setValue(0)
    settle(0.1)
    for message in history[9:]:
        overlay.upsert_processed_message(message)
    settle(0.4)
    canvas = Canvas(390 + 2 * MARGIN, 300 + 2 * MARGIN, scale, seed=17)
    canvas.paste(overlay.grab(), MARGIN, MARGIN)
    canvas.save(out / "overlay_new_messages.png")
    overlay.close()


def shot_settings(out: Path, scale: float) -> None:
    from types import SimpleNamespace

    from src.core.entity_protection import ProtectedEntityIndex
    from src.gui.settings import SettingsDialog

    config = get_default_config()
    config["shared"].update({
        "log_dir": r"C:\Users\Pilot\Documents\EVE\logs\Chatlogs",
        "translation_provider": "google",
        "provider_choice_confirmed": True,
    })
    # Show what an installed build reports: the bundled SDE entity index.
    service = SimpleNamespace(entity_index=ProtectedEntityIndex.load(), translation_memory=None)
    dialog = SettingsDialog(config, translation_service=service)
    dialog.move(80, 60)
    dialog.show()
    for index, name, height in ((0, "settings_general", 700), (1, "settings_overlay", 590),
                                (4, "settings_intelligence", 520)):
        dialog.resize(560, height)
        dialog.tabs.setCurrentIndex(index)
        settle(0.25)
        pixmap = dialog.grab()
        size = pixmap.size() / pixmap.devicePixelRatio()
        canvas = Canvas(size.width() + 2 * MARGIN, size.height() + 2 * MARGIN, scale,
                        seed=19 + index)
        canvas.paste_panel(pixmap, MARGIN, MARGIN)
        canvas.save(out / f"{name}.png")
    dialog.close()


def shot_compose(out: Path, scale: float) -> None:
    from src.gui.compose import ComposeDialog

    dialog = ComposeDialog(source_language="en", target_language="zh",
                           provider_label="Google provider")
    dialog.resize(600, 470)
    dialog.move(80, 60)
    dialog.show()
    settle(0.2)
    dialog.source_text_edit.setPlainText("Hold cloak on the gate, we jump in 30 seconds.")
    request_id = dialog.request_translation()
    if request_id is not None:
        dialog.finish_translation(request_id, "在星门保持隐身，我们30秒后跳。")
    # The editable language box would otherwise show a text caret.
    dialog.copy_button.setFocus()
    settle(0.2)
    pixmap = dialog.grab()
    size = pixmap.size() / pixmap.devicePixelRatio()
    canvas = Canvas(size.width() + 2 * MARGIN, size.height() + 2 * MARGIN, scale, seed=29)
    canvas.paste_panel(pixmap, MARGIN, MARGIN)
    canvas.save(out / "compose.png")
    dialog.close()


SHOTS = {
    "sessions": shot_sessions,
    "mirror": shot_mirror,
    "combat": shot_combat,
    "context_menu": shot_context_menu,
    "header_states": shot_header_states,
    "new_messages": shot_new_messages,
    "settings": shot_settings,
    "compose": shot_compose,
}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.strip().splitlines()[0])
    parser.add_argument("--out", type=Path, default=PROJECT_ROOT / "docs" / "screenshots")
    parser.add_argument("--scale", type=float, default=2.0,
                        help="device pixel ratio of the images (default: 2)")
    parser.add_argument("--only", nargs="*", choices=sorted(SHOTS),
                        help="render only these screenshots")
    args = parser.parse_args()
    # Read when the QApplication is created.
    os.environ["QT_SCALE_FACTOR"] = str(args.scale)
    app = QApplication.instance() or QApplication(sys.argv)
    app.setQuitOnLastWindowClosed(False)
    out = args.out.resolve()
    for name in args.only or SHOTS:
        SHOTS[name](out, args.scale)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

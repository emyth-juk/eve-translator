"""Overlay geometry: resizes persist, edges resize, the header drags."""

import sys
import time

import pytest
from PySide6.QtCore import QEvent, QObject, QPoint, QPointF, Qt
from PySide6.QtGui import QMouseEvent
from PySide6.QtWidgets import QApplication

from src.app_config import get_default_config
from src.core.session import ChatSession
from src.gui.overlay import (
    HTBOTTOM,
    HTBOTTOMRIGHT,
    HTLEFT,
    HTTOP,
    OverlayWindow,
    native_resize_hit_test,
)


@pytest.fixture(scope="module", autouse=True)
def qt_app():
    app = QApplication.instance() or QApplication(sys.argv)
    yield app


def _wait_until(predicate, timeout=2.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        QApplication.processEvents()
        if predicate():
            return True
        time.sleep(0.02)
    return predicate()


def _merged_config(session_id="fleet", **overrides):
    config = get_default_config()
    merged = {**config["shared"], **config["sessions"][session_id]}
    merged.update({"x": 100, "y": 100, "w": 420, "h": 360})
    merged.update(overrides)
    return merged


def _log_file(tmp_path):
    path = tmp_path / "Fleet_20261108_100000_2110000001.txt"
    path.write_bytes("﻿\r\n".encode("utf-16-le"))
    return str(path)


def test_resized_overlay_keeps_its_size_when_the_manager_pushes_config(tmp_path):
    owner = QObject()
    manager_config = _merged_config()
    session = ChatSession("fleet", _log_file(tmp_path), dict(manager_config), parent=owner)
    session.start()

    session.overlay.resize(520, 300)
    QApplication.processEvents()
    # A Settings preview or speaker-role change pushes the manager's copy,
    # which still holds the size from before the resize.
    session.update_config(dict(manager_config, opacity=0.6))

    assert (session.overlay.width(), session.overlay.height()) == (520, 300)
    # Opacity now applies to the background only; text stays opaque.
    assert session.overlay.windowOpacity() == 1.0
    assert ", 153)" in session.overlay.text_browser.styleSheet()
    session.stop()


def test_manager_geometry_change_is_still_applied(tmp_path):
    owner = QObject()
    session = ChatSession("fleet", _log_file(tmp_path), _merged_config(), parent=owner)
    session.start()

    session.update_config(_merged_config(x=150, y=160, w=480, h=330))

    geometry = session.overlay.geometry()
    assert (geometry.x(), geometry.y(), geometry.width(), geometry.height()) == (
        150, 160, 480, 330,
    )
    session.stop()


def test_resize_is_saved_after_a_short_delay():
    overlay = OverlayWindow("fleet", _merged_config())
    overlay.show()
    QApplication.processEvents()
    saved = []
    overlay.config_updated.connect(lambda config: saved.append((config["w"], config["h"])))

    overlay.resize(500, 310)

    assert _wait_until(lambda: saved)
    assert saved[-1] == (500, 310)


def test_edges_are_detected_in_window_coordinates_below_the_header():
    overlay = OverlayWindow("fleet", _merged_config())
    overlay.show()
    QApplication.processEvents()
    viewport = overlay.text_browser.viewport()

    def edge_at_viewport(point):
        return overlay._edge_at_global(viewport.mapToGlobal(point))

    bottom = QPoint(viewport.width() // 2, viewport.height() - 2)
    first_line = QPoint(viewport.width() // 2, 4)

    assert edge_at_viewport(bottom) == Qt.Edge.BottomEdge
    assert edge_at_viewport(first_line) is None


def test_left_drag_on_header_moves_the_window_without_a_menu():
    overlay = OverlayWindow("fleet", _merged_config())
    overlay.show()
    QApplication.processEvents()
    menus = []
    overlay.show_context_menu = lambda: menus.append(True)
    header = overlay.status_header
    start = header.mapToGlobal(QPoint(60, 13))
    end = start + QPoint(40, 25)
    before = overlay.pos()

    def send(event_type, global_point, buttons):
        local = QPointF(header.mapFromGlobal(global_point))
        event = QMouseEvent(
            event_type,
            local,
            QPointF(global_point),
            Qt.MouseButton.LeftButton,
            buttons,
            Qt.KeyboardModifier.NoModifier,
        )
        return overlay.eventFilter(header, event)

    assert send(QEvent.Type.MouseButtonPress, start, Qt.MouseButton.LeftButton)
    assert send(QEvent.Type.MouseMove, end, Qt.MouseButton.LeftButton)
    assert send(QEvent.Type.MouseButtonRelease, end, Qt.MouseButton.NoButton)

    assert overlay.pos() == before + QPoint(40, 25)
    assert menus == []


def test_native_hit_test_maps_borders_to_windows_codes():
    rect = (100, 100, 500, 400)

    assert native_resize_hit_test(102, 250, *rect, 6) == HTLEFT
    assert native_resize_hit_test(300, 101, *rect, 6) == HTTOP
    assert native_resize_hit_test(300, 398, *rect, 6) == HTBOTTOM
    assert native_resize_hit_test(498, 398, *rect, 6) == HTBOTTOMRIGHT
    assert native_resize_hit_test(300, 250, *rect, 6) is None
    assert native_resize_hit_test(600, 250, *rect, 6) is None


def test_stopped_sessions_and_overlays_are_released(tmp_path):
    import shiboken6
    from PySide6.QtCore import QCoreApplication

    from src.main import TranslatorManager

    owner = QObject()
    log = _log_file(tmp_path)
    created = []
    for _ in range(5):
        session = ChatSession("fleet", log, _merged_config(), parent=owner)
        session.start()
        QApplication.processEvents()
        created.append((session, session.overlay))
        TranslatorManager._dispose_session(session)
    QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete.value)
    QApplication.processEvents()

    assert [child for child in owner.children() if isinstance(child, ChatSession)] == []
    assert not any(shiboken6.isValid(overlay) for _session, overlay in created)


def test_overlay_saved_on_a_disconnected_monitor_is_moved_back_on_screen():
    overlay = OverlayWindow("fleet", _merged_config(x=-5000, y=-4000))

    geometry = overlay.geometry()
    screen = QApplication.primaryScreen().availableGeometry()
    assert screen.intersects(geometry)


def test_secondary_text_scales_with_the_configured_font_size():
    small = OverlayWindow("fleet", _merged_config(font_size=10))
    large = OverlayWindow("fleet", _merged_config(font_size=20))
    message = {
        "message_id": "m1", "text": "hello", "sender": "Pilot", "timestamp": "12:00:00",
        "original_text": "bonjour", "state": "translated", "commands": (),
        "detected_language": "fr",
    }

    assert "font-size:8pt" in small._format_message_html(message)
    assert "font-size:16pt" in large._format_message_html(message)
    assert large.status_header.height() > small.status_header.height()

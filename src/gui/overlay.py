import json
import os
import datetime
import re
import html

from PySide6.QtWidgets import (QMainWindow, QLabel, QVBoxLayout, QWidget, QApplication, 
                               QTextBrowser, QFrame, QMenu, QFileDialog, QMessageBox)
from PySide6.QtCore import Qt, QTimer, Slot, Signal, QPoint, QRect, QEvent
from PySide6.QtGui import QColor, QPalette, QFont, QCursor, QMouseEvent, QAction, QTextCursor

from src.gui.settings import SettingsDialog

from src.version import __version__

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

    def __init__(self, session_id='fleet', initial_config=None):
        super().__init__()
        self.session_id = session_id
        
        # Window Flags for Overlay
        self.setWindowFlags(
            Qt.WindowType.FramelessWindowHint | 
            Qt.WindowType.WindowStaysOnTopHint | 
            Qt.WindowType.Tool  # Tool window doesn't appear in taskbar usually
        )
        self.setWindowTitle(f"EVE Translator v{__version__} - {session_id.capitalize()}")
        
        # Opacity
        self.setWindowOpacity(0.8)

        # Mouse Tracking for resize cursor
        self.setMouseTracking(True)
        
        # Interaction State
        self.is_moving = False
        self.drag_start_pos = QPoint()
        self.is_resizing = False
        self.resize_edge = None
        self.resize_margin = 10
        self.min_width = 300
        self.min_height = 200

        # Text Browser Setup (Replaces ScrollArea + Widgets)
        self.text_browser = QTextBrowser()
        self.text_browser.setOpenExternalLinks(False) # Handle links manually if needed, or True
        self.text_browser.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)
        self.text_browser.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.text_browser.setFrameShape(QFrame.Shape.NoFrame)
        self.text_browser.setMouseTracking(True)
        # Install event filter to capture mouse events
        self.text_browser.installEventFilter(self)
        self.text_browser.viewport().installEventFilter(self)
        
        self.setCentralWidget(self.text_browser)

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
            'background_color': '#33001a' if session_id == 'fleet' else '#001a33'
        }
        
        self.config = initial_config.copy() if initial_config else default_config
        self.chat_history = []
        # Track state of all sessions for context menu checkmarks
        self.all_session_states = {}
        # Track available characters for Local selection
        self.available_characters = {}
        # Track available fleets for Fleet selection
        self.available_fleets = {}
        self.selected_fleet_id = None 
        
        self.apply_config()
        
        # Demo Label
        # Welcome Message
        eve_time = datetime.datetime.now(datetime.timezone.utc).strftime("%H:%M:%S")
        
        if session_id == 'local':
            char_name = self.config.get('character_name', 'Unknown Character')
            system_name = self.config.get('system_name', 'Unknown System')
            welcome_text = f"Listening to Local Chat for <b>{char_name}</b> in <b>{system_name}</b>."
        else:
            welcome_text = f"Listening to {session_id.capitalize()} Chat."

        self.add_message(welcome_text, "System", eve_time)

    def update_session_states(self, states: dict):
        """Update knowledge of which sessions are enabled."""
        self.all_session_states = states

    def apply_config(self):
        screen_geo = QApplication.primaryScreen().availableGeometry()
        
        # Apply Geometry
        x = self.config.get('x', -1)
        y = self.config.get('y', -1)
        w = self.config.get('w', 600)
        h = self.config.get('h', 400)
        
        if x == -1 or y == -1:
            # Stagger default positions if multiple?
            offset = 0 if self.session_id == 'fleet' else 50
            x = screen_geo.width() - w - 50 - offset
            y = screen_geo.height() - h - 100 - offset
            
        self.setGeometry(x, y, w, h)
        
        # Apply visual settings
        self.setWindowOpacity(self.config.get('opacity', 0.8))
        
        bg_color = self.config.get('background_color', 'black')
        self.text_browser.setStyleSheet(f"background-color: {bg_color};")
        
        # Apply Font
        font_size = self.config.get('font_size', 10)
        font = QFont("Arial", font_size)
        self.text_browser.setFont(font)
        
        self.refresh_ui()

    def _update_config_from_geometry(self):
        geo = self.geometry()
        self.config['x'] = geo.x()
        self.config['y'] = geo.y()
        self.config['w'] = geo.width()
        self.config['h'] = geo.height()

    def save_config(self):
        self._update_config_from_geometry()
        # Emit signal to notify manager
        self.config_updated.emit(self.config)

    def get_current_config(self):
        self._update_config_from_geometry() 
        return self.config

    def closeEvent(self, event):
        self.save_config()
        super().closeEvent(event)

    def showEvent(self, event):
        super().showEvent(event)
        # Refresh UI when shown to catch up on any messages missed while hidden
        self.refresh_ui()

    def check_edge(self, pos):
        """Returns string identifier of edge/corner or None"""
        r = self.rect()
        m = self.resize_margin
        
        on_left = pos.x() < m
        on_right = pos.x() > r.width() - m
        on_top = pos.y() < m
        on_bottom = pos.y() > r.height() - m
        
        if on_top and on_left: return 'TopLeft'
        if on_top and on_right: return 'TopRight'
        if on_bottom and on_right: return 'BottomRight'
        if on_bottom and on_left: return 'BottomLeft'
        if on_top: return 'Top'
        if on_bottom: return 'Bottom'
        if on_left: return 'Left'
        if on_right: return 'Right'
        return None

    def eventFilter(self, source, event):
        if event.type() == QEvent.Type.MouseButtonPress:
            if isinstance(event, QMouseEvent):
                global_pos = event.globalPosition().toPoint()
                local_pos = self.mapFromGlobal(global_pos)
                
                # Right Click: Move
                if event.button() == Qt.MouseButton.RightButton:
                    self.is_moving = True
                    self.has_moved = False # Track if we actually dragged
                    self.drag_start_pos = local_pos
                    return True 
                
                # Left Click: Resize check
                if event.button() == Qt.MouseButton.LeftButton:
                    edge = self.check_edge(local_pos)
                    if edge:
                        self.is_resizing = True
                        self.resize_edge = edge
                        self.drag_start_pos = global_pos
                        return True 

        elif event.type() == QEvent.Type.MouseMove:
            if isinstance(event, QMouseEvent):
                global_pos = event.globalPosition().toPoint()
                local_pos = self.mapFromGlobal(global_pos)
                
                if self.is_moving:
                    # Check threshold to avoid jitter opening menu
                    if (global_pos - self.mapToGlobal(self.drag_start_pos)).manhattanLength() > 5:
                         self.has_moved = True
                         self.move(global_pos - self.drag_start_pos)
                    return True
                
                if self.is_resizing:
                    rect = self.geometry()
                    dx = global_pos.x() - self.drag_start_pos.x()
                    dy = global_pos.y() - self.drag_start_pos.y()
                    
                    if 'Right' in self.resize_edge:
                        new_w = max(self.min_width, rect.width() + dx)
                        rect.setWidth(new_w)
                    if 'Bottom' in self.resize_edge:
                        new_h = max(self.min_height, rect.height() + dy)
                        rect.setHeight(new_h)
                    
                    self.setGeometry(rect)
                    self.drag_start_pos = global_pos
                    return True
                    
                # Cursor Update
                # Cursor Update
                edge = self.check_edge(local_pos)
                if edge:
                    if edge in ['TopLeft', 'BottomRight']:
                        self.setCursor(Qt.CursorShape.SizeFDiagCursor)
                    elif edge in ['TopRight', 'BottomLeft']:
                        self.setCursor(Qt.CursorShape.SizeBDiagCursor)
                    elif edge in ['Top', 'Bottom']:
                        self.setCursor(Qt.CursorShape.SizeVerCursor)
                    elif edge in ['Left', 'Right']:
                        self.setCursor(Qt.CursorShape.SizeHorCursor)
                    return True  # Consume event to prevent child widget from overriding cursor
                else:
                    self.unsetCursor()  # Let child widget (TextBrowser) handle cursor (e.g. IBeam)
                    
        elif event.type() == QEvent.Type.MouseButtonRelease:
            if self.is_moving:
                self.is_moving = False
                if not self.has_moved:
                    self.show_context_menu()
                else:
                    self.save_config()
                return True
            
            if self.is_resizing:
                self.is_resizing = False
                self.save_config()
                return True
                
        return super().eventFilter(source, event)

    def show_context_menu(self):
        menu = QMenu(self)
        
        # Session Management Submenu
        menu_session = menu.addMenu("Select Target Chat")
        
        # Fleet
        is_fleet_active = self.all_session_states.get('fleet', False)
        # print(f"[Overlay] Context Menu Fleet State: {is_fleet_active} from {self.all_session_states}")
        
        act_fleet = QAction("Fleet Chat", self, checkable=True)
        act_fleet.setChecked(is_fleet_active)
        act_fleet.triggered.connect(lambda: self.session_toggled.emit('fleet'))
        menu_session.addAction(act_fleet)
        
        # Local
        is_local_active = self.all_session_states.get('local', False)
        
        act_local = QAction("Local Chat", self, checkable=True)
        act_local.setChecked(is_local_active)
        act_local.triggered.connect(lambda: self.session_toggled.emit('local'))
        menu_session.addAction(act_local)
        
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
                    import datetime
                    dt = datetime.datetime.fromtimestamp(fleet_info.created_time)
                    time_str = dt.strftime("%H:%M")
                    
                    label = f"Fleet - {fleet_info.listener_name} [{time_str}]"
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

        menu.addSeparator()

        action_settings = QAction("Settings", self)
        action_settings.triggered.connect(self.open_settings)
        menu.addAction(action_settings)
        
        action_export = QAction("Export translation to txt", self)
        action_export.triggered.connect(self.export_chat)
        menu.addAction(action_export)
        
        menu.addSeparator()
        
        action_exit = QAction("Exit", self)
        action_exit.triggered.connect(QApplication.instance().quit)
        menu.addAction(action_exit)
        
        # Show at cursor
        menu.exec(QCursor.pos())

    def open_settings(self):
        self.request_settings.emit()

    def preview_settings(self, new_config):
        # Apply incoming settings temporarily
        old_opacity = self.config.get('opacity', 0.8)
        self.config.update(new_config)
        
        # Apply visual changes immediately
        new_opacity = self.config.get('opacity', 0.8)
        if new_opacity != old_opacity:
            self.setWindowOpacity(new_opacity)

        # Only refresh UI if visual properties changed that affect content
        # For now, we just refresh since Font/Color might have changed.
        # But we avoided the spam from text inputs.
        self.refresh_ui()

    def export_chat(self):
        filename, _ = QFileDialog.getSaveFileName(self, "Export Chat", "", "Text Files (*.txt)")
        if filename:
            try:
                with open(filename, 'w', encoding='utf-8') as f:
                    for msg in self.chat_history:
                        line = f"[{msg['timestamp']}] {msg['sender']} > "
                        
                        clean_text = self._strip_html(msg['text'])
                        
                        if msg['is_translated']:
                            clean_orig = self._strip_html(msg['original_text'])
                            line += f"{clean_text} ({clean_orig})"
                        else:
                            line += f"{clean_text}"
                            
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
        full_html = ""
        for msg_data in self.chat_history:
            full_html += self._format_message_html(msg_data)
            
        self.text_browser.setHtml(full_html)
        
        # Scroll to bottom if needed
        if self.config.get('auto_scroll', True):
            self.text_browser.moveCursor(QTextCursor.MoveOperation.End)

    def _format_message_html(self, msg_data):
        """Helper to format a single message dict into HTML string."""
        col_def = self.config.get('color_default', '#e0e0e0')
        col_trans = self.config.get('color_translated', '#00ffff')
        col_high = self.config.get('color_highlight', 'yellow')

        timestamp = msg_data['timestamp']
        sender = msg_data['sender']
        # Safe to replace on copy
        text = msg_data['text'].replace("color: yellow", f"color: {col_high}")

        if msg_data['is_translated']:
            original = msg_data['original_text']
            # Ensure original is not None before replace (though unlikely if is_translated)
            if original:
                original = original.replace("color: yellow", f"color: {col_high}")
            else:
                original = ""
                
            return (f"<div style='margin-bottom: 2px;'>"
                    f"<span style='color: {col_def};'>[{timestamp}] {sender} &gt; </span>"
                    f"<span style='color: {col_trans};'>{text}</span> "
                    f"<span style='color: {col_def};'>({original})</span></div>")
        else:
            return (f"<div style='margin-bottom: 2px;'>"
                    f"<span style='color: {col_def};'>[{timestamp}] {sender} &gt; </span>"
                    f"<span style='color: {col_def};'>{text}</span></div>")

    def add_message(self, text: str, sender: str, timestamp: str, original_text: str = None, is_translated: bool = False):
        msg_data = {
            'text': text,
            'sender': sender,
            'timestamp': timestamp,
            'original_text': original_text,
            'is_translated': is_translated
        }
        self.chat_history.append(msg_data)
        
        # Optimization: Skip rendering if window is hidden
        if not self.isVisible():
             return

        if len(self.chat_history) > 100:
            self.chat_history.pop(0)
            # Full rebuild might be needed to remove first item visually, 
            # OR we just rebuild when 100 limit hit infrequently.
            # Efficiency: If we append, the top remains.
            # Ideally we reload occasionally?
            # For now, let's just append. If history is HUGE, text browser handles it.
            # But if we really want to drop the first line from display, we need to reload.
            # Rebuilding 100 lines is CHEAP for string ops.
            self.refresh_ui()
        else:
            # Append single message
            html_chunk = self._format_message_html(msg_data)
            self.text_browser.append(html_chunk)
            
            # Auto-scroll
            if self.config.get('auto_scroll', True):
                 self.text_browser.moveCursor(QTextCursor.MoveOperation.End)

    def update_status(self, text):
        pass

    def update_character_list(self, character_registry: dict):
        """Update list of available characters for context menu."""
        self.available_characters = character_registry

    def update_fleet_list(self, fleet_registry: dict, selected_fleet_id: str):
        """Update list of available fleets for context menu."""
        self.available_fleets = fleet_registry
        self.selected_fleet_id = selected_fleet_id

    def clear_messages(self):
        """Clear all messages from the overlay."""
        # Clear chat history
        self.chat_history = []

        # Remove all message widgets from layout
        while self.layout.count():
            child = self.layout.takeAt(0)
            if child.widget():
                child.widget().deleteLater()


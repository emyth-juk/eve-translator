
import unittest
from unittest.mock import MagicMock

# We copy the relevant logic or import it?
# Importing OverlayWindow might trigger QApp requirement.
# Let's try importing. If it fails, we stub.
# Ideally we should refactor logic out of GUI class, but for now we test the class.

from PySide6.QtWidgets import QApplication
import sys

# Ensure QApp exists for any Qt classes
app = QApplication.instance() or QApplication(sys.argv)

from src.gui.overlay import OverlayWindow

class TestGuiFormatting(unittest.TestCase):
    def setUp(self):
        # We don't want to actually show the window or init full UI
        # We can subclass or patch __init__
        pass

    def test_format_message_html_logic(self):
        """Test the HTML string generation without GUI interaction."""
        
        # Create a dummy object that mimics OverlayWindow's config structure
        # stealing the method logic or using the real class with mocked init
        
        # Option A: Real class with mocks
        # But OverlayWindow.__init__ does a lot (flags, ui setup).
        # Better to just test the method logic if possible, but it is bound to the class.
        
        # Let's use a Dummy class that has the SAME method code? 
        # No, that duplicates code.
        
        # Let's instantiate OverlayWindow but verify we can do it headless.
        # "overlay = OverlayWindow(config)" might work if QApp exists.
        
        config = {
            'color_default': '#cccccc',
            'color_translated': '#00ff00',
            'color_highlight': 'red'
        }
        
        # Patch the UI setup parts to avoid heavily lifting
        with unittest.mock.patch('src.gui.overlay.OverlayWindow.apply_config'), \
             unittest.mock.patch('src.gui.overlay.OverlayWindow.setWindowFlags'), \
             unittest.mock.patch('src.gui.overlay.OverlayWindow.setAttribute'):
            
            overlay = OverlayWindow(initial_config=config)
            overlay.config = config # Ensure config is set
            
            # 1. Normal Message
            msg_data = {
                'timestamp': '12:00:00',
                'sender': 'PilotA',
                'text': 'Hello',
                'original_text': None,
                'is_translated': False
            }
            html = overlay._format_message_html(msg_data)
            
            self.assertIn("[12:00:00]", html)
            self.assertIn("PilotA", html)
            self.assertIn("#cccccc", html) # Default color
            self.assertIn("Hello", html)
            
            # 2. Translated Message
            msg_trans = {
                'timestamp': '12:00:01',
                'sender': 'PilotB',
                'text': 'Translated Text',
                'original_text': 'Original Text',
                'is_translated': True
            }
            html_trans = overlay._format_message_html(msg_trans)
            self.assertIn("#00ff00", html_trans) # Translated color
            self.assertIn("Translated Text", html_trans)
            self.assertIn("Original Text", html_trans)
            
            # 3. Highlighting
            # The method replaces "color: yellow" with config['color_highlight']
            # We simulate a message coming in that was already marked for highlight by logic?
            # Wait, looking at code:
            # text = msg_data['text'].replace("color: yellow", f"color: {col_high}")
            # This implies the input text key might already have "color: yellow" embedded?
            # Or the parser does it?
            # Let's send text with "color: yellow"
            
            msg_high = {
                'timestamp': '12:00:02',
                'sender': 'FC',
                'text': '<span style="color: yellow">Primary</span>',
                'original_text': None,
                'is_translated': False
            }
            html_high = overlay._format_message_html(msg_high)
            self.assertIn("color: red", html_high) # Replaced 'yellow' with 'red' from config
            
if __name__ == '__main__':
    unittest.main()

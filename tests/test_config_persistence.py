
import unittest
import json
from unittest.mock import MagicMock, patch, mock_open
from pathlib import Path
from src.main import TranslatorManager

class TestConfigPersistence(unittest.TestCase):

    def setUp(self):
        # We need to minimally init TranslatorManager without QApp if possible, or mocked.
        # TranslatorManager inherits QObject? No, usually explicitly.
        # Let's inspect main.py imports.
        pass

    @patch('src.main.TranslatorManager.__init__', return_value=None) # Skip init
    def test_save_config_logic(self, mock_init):
        # Create instance bypassing init
        app = TranslatorManager()
        
        # Manually setup state required for _save_config
        app.sessions = {}
        # Mock a session
        mock_session = MagicMock()
        # Session returns a config dict with VALID and INVALID keys
        mock_session.get_config.return_value = {
            'x': 100,
            'y': 200,
            'opacity': 0.8, # Excluded key
            'polling_interval': 2.0, # Excluded key
            'some_unique_setting': 'value'
        }
        app.sessions['session1'] = mock_session
        
        # Setup app.config
        app.config = {
            'sessions': {
                'session1': {
                    'old_key': 1,
                    'opacity': 0.5 # Should be removed/preserved? Logic says removed from session dict if in excluded list?
                    # wait, _save_config logic:
                    # 1. filtered_config = {k:v for k,v in current... if k not in excluded}
                    # 2. config['sessions'][id].update(filtered_config)
                    # 3. Explicitly delete excluded keys from config['sessions'][id]
                }
            }
        }
        
        # Mock file writing
        with patch('builtins.open', mock_open()) as mocked_file, \
             patch('src.main.Path.cwd', return_value=Path('/tmp')):
            
            app._save_config()
            
            # Verify file write
            mocked_file.assert_called_with(Path('/tmp/translator_config.json'), 'w')
            
            # Get the JSON written
            # handle = mocked_file()
            # handle.write.assert_called... but json.dump calls write multiple times often.
            # Easier to inspect app.config state which was dumped.
            
            session_conf = app.config['sessions']['session1']
            
            # 1. Included keys should be updated
            self.assertEqual(session_conf['x'], 100)
            self.assertEqual(session_conf['some_unique_setting'], 'value')
            
            # 2. Excluded keys should be REMOVED (opacity, polling_interval)
            self.assertNotIn('opacity', session_conf)
            self.assertNotIn('polling_interval', session_conf)
            self.assertNotIn('font_size', session_conf)

if __name__ == '__main__':
    unittest.main()

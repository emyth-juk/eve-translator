
import unittest
import json
import tempfile
import shutil
from unittest.mock import MagicMock, patch
from pathlib import Path
from src.app_config import ConfigStore
from src.main import TranslatorManager

class TestConfigPersistence(unittest.TestCase):

    def setUp(self):
        self.test_dir = tempfile.mkdtemp()
        self.config_path = Path(self.test_dir) / ".eve_translator" / "translator_config.json"

    def tearDown(self):
        shutil.rmtree(self.test_dir, ignore_errors=True)

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
        
        app.config_store = ConfigStore(path=self.config_path, legacy_dir=self.test_dir)

        app._save_config()

        self.assertTrue(self.config_path.exists())
        with open(self.config_path, 'r', encoding='utf-8') as config_file:
            saved = json.load(config_file)

        session_conf = app.config['sessions']['session1']

        # 1. Included keys should be updated
        self.assertEqual(session_conf['x'], 100)
        self.assertEqual(session_conf['some_unique_setting'], 'value')

        # 2. Excluded keys should be REMOVED (opacity, polling_interval)
        self.assertNotIn('opacity', session_conf)
        self.assertNotIn('polling_interval', session_conf)
        self.assertNotIn('font_size', session_conf)
        self.assertNotIn('opacity', saved['sessions']['session1'])

if __name__ == '__main__':
    unittest.main()

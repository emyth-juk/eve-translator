
import unittest
import time
import tempfile
import shutil
from unittest.mock import MagicMock, patch
from pathlib import Path

# Imports
from src.core.session import ChatSession
from src.main import LogProcessingWorker # Requires main.py setup logic fix we did
from src.core.parser import LineParser
from src.core.tokenizer import EVELinkTokenizer
from src.core.detector import LanguageDetector
from src.services.translator import TranslationService, MockTranslator

class TestIntegrationPipeline(unittest.TestCase):
    def setUp(self):
        self.test_dir = tempfile.mkdtemp()
        self.log_file = Path(self.test_dir) / "20251217_100000_123456.txt"
        
        # Create initial log file
        with open(self.log_file, 'w', encoding='utf-16-le') as f:
            f.write("Log Header\n")
            
        self.config = {
            'fleet_history_lines': 5,
            'ignored_languages': [],
            'target_language': 'en',
            'polling_interval': 0.1
        }

    def tearDown(self):
        if hasattr(self, 'session') and self.session:
            self.session.stop()
        try:
            shutil.rmtree(self.test_dir)
        except PermissionError:
            time.sleep(0.1)
            try:
                shutil.rmtree(self.test_dir)
            except:
                pass

    @patch('src.gui.overlay.OverlayWindow')
    def test_full_pipeline_flow(self, MockOverlayClass):
        """
        Simulate full flow:
        Log Update -> Tailer -> ChatSession -> Signal -> Worker -> Parser/Trans -> Signal -> Session -> MockOverlay
        """
        mock_overlay = MockOverlayClass.return_value
        
        # 1. Init Shared Components (Real Logic)
        parser = LineParser()
        tokenizer = EVELinkTokenizer()
        detector = LanguageDetector()
        # Use MockTranslator to avoid API calls but test the service wrapper
        trans_service = TranslationService(provider=MockTranslator())
        
        worker = LogProcessingWorker(parser, tokenizer, detector, trans_service)
        
        # 2. Init Session
        self.session = ChatSession('fleet', str(self.log_file), self.config)
        self.session.start()
        
        # 3. Wiring (Mimic Manager)
        # Session -> Worker
        self.session.lines_ready.connect(worker.process_lines)
        
        # Worker -> Session (Custom slot to adapt arguments)
        # Worker emits (session_id, text, sender, timestamp, original, is_translated)
        # Session.add_message(text, sender, timestamp, original, is_translated)
        def route_message(sid, text, sender, timestamp, orig, is_trans):
            if sid == self.session.session_id:
                self.session.add_message(text, sender, timestamp, orig, is_trans)
        
        worker.signals.message_ready.connect(route_message)
        
        # 4. Simulate Log Update
        # Write "Chinese" line -> Should be processed as "Mock Translated"
        # "大家好" -> (Mock) -> "[MOCK] 大家好"
        
        encoded_line = "[ 2025.12.17 12:00:00 ] Pilot A > 大家好\n".encode('utf-16-le')
        with open(self.log_file, 'ab') as f:
            f.write(encoded_line)
            
        # 5. Trigger Processing
        # verify _poll_log exists and calls tailer
        # Instead of waiting for QTimer (which requires event loop), call directly.
        
        self.session._poll_log()
        
        # 6. Verify Overlay Call
        # Since logic is synchronous in this test (no threads, direct signal connection), 
        # calls should complete immediately.
        
        self.assertTrue(mock_overlay.add_message.called, "Overlay add_message should have been called")
        
        args, _ = mock_overlay.add_message.call_args
        # args: (text, sender, timestamp, original_text, is_translated)
        
        self.assertIn("[MOCK] 大家好", args[0]) # Translated text
        self.assertEqual(args[1], "Pilot A")
        self.assertEqual(args[3], "大家好")     # Original
        self.assertTrue(args[4])              # Is Translated flag

if __name__ == '__main__':
    unittest.main()

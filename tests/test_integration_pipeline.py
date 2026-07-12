
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
        
        # Worker -> Session
        def route_message(message):
            if (
                message.session_id == self.session.session_id
                and message.source_id == self.session.source_id
            ):
                self.session.handle_processed_message(message)
        
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
        
        self.assertTrue(
            mock_overlay.upsert_processed_message.called,
            "Overlay should receive a processed message",
        )
        messages = [
            call.args[0]
            for call in mock_overlay.upsert_processed_message.call_args_list
        ]
        final = messages[-1]
        self.assertIn("[MOCK] 大家好", final.text)
        self.assertEqual(final.sender, "Pilot A")
        self.assertEqual(final.original_text, "大家好")
        self.assertEqual(final.state.value, "translated")

if __name__ == '__main__':
    unittest.main()

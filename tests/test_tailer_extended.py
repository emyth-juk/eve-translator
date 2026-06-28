import os
import tempfile
import shutil
import time
import unittest
from pathlib import Path

from src.core.tailer import FleetLogTailer


class TestFleetLogTailer(unittest.TestCase):
    """Extended tests for FleetLogTailer covering open, seek, read, close."""

    def setUp(self):
        self.test_dir = tempfile.mkdtemp()

    def tearDown(self):
        # Small delay to release file handles on Windows
        time.sleep(0.05)
        shutil.rmtree(self.test_dir, ignore_errors=True)

    def _create_log(self, filename="Fleet_test.txt", lines=None):
        """Helper to create a UTF-16-LE log file."""
        path = os.path.join(self.test_dir, filename)
        if lines is None:
            lines = [
                "Channel ID:      (None)",
                "Channel Name:    Fleet",
                "Listener:        TestChar",
                "Session started: 2025.12.16 08:24:57",
                "",
                "[ 2025.12.16 08:25:00 ] Player1 > Hello fleet",
                "[ 2025.12.16 08:25:05 ] Player2 > o7",
            ]
        content = "\r\n".join(lines)
        with open(path, 'w', encoding='utf-16-le') as f:
            f.write(content)
        return path

    # --- _open ---

    def test_open_valid_file(self):
        path = self._create_log()
        tailer = FleetLogTailer(path)
        self.assertIsNotNone(tailer.file_handle)
        tailer.close()

    def test_open_nonexistent_file(self):
        path = os.path.join(self.test_dir, "nonexistent.txt")
        tailer = FleetLogTailer(path)
        self.assertIsNone(tailer.file_handle)

    # --- seek_to_end ---

    def test_seek_to_end(self):
        path = self._create_log()
        tailer = FleetLogTailer(path)
        tailer.seek_to_end()
        # After seeking to end, reading should return no new lines
        lines = tailer.read_new_lines()
        self.assertEqual(lines, [])
        tailer.close()

    def test_seek_to_end_reopens_closed_file(self):
        path = self._create_log()
        tailer = FleetLogTailer(path)
        tailer.close()
        self.assertIsNone(tailer.file_handle)
        tailer.seek_to_end()
        self.assertIsNotNone(tailer.file_handle)
        tailer.close()

    def test_seek_to_end_nonexistent_file(self):
        path = os.path.join(self.test_dir, "missing.txt")
        tailer = FleetLogTailer(path)
        tailer.seek_to_end()
        # Should not crash, handle should remain None
        self.assertIsNone(tailer.file_handle)

    # --- read_new_lines ---

    def test_read_new_lines_after_seek(self):
        path = self._create_log()
        tailer = FleetLogTailer(path)
        tailer.seek_to_end()

        # Append new content
        with open(path, 'a', encoding='utf-16-le') as f:
            f.write("\r\n[ 2025.12.16 08:26:00 ] Player3 > New message")

        lines = tailer.read_new_lines()
        self.assertTrue(any("New message" in line for line in lines))
        tailer.close()

    def test_read_new_lines_empty_when_no_new_content(self):
        path = self._create_log()
        tailer = FleetLogTailer(path)
        tailer.seek_to_end()
        lines = tailer.read_new_lines()
        self.assertEqual(lines, [])
        tailer.close()

    def test_read_new_lines_nonexistent_file(self):
        path = os.path.join(self.test_dir, "gone.txt")
        tailer = FleetLogTailer(path)
        lines = tailer.read_new_lines()
        self.assertEqual(lines, [])

    def test_read_new_lines_strips_newlines(self):
        path = self._create_log()
        tailer = FleetLogTailer(path)
        lines = tailer.read_new_lines()
        for line in lines:
            self.assertFalse(line.endswith('\n'))
            self.assertFalse(line.endswith('\r'))
        tailer.close()

    # --- close ---

    def test_close_sets_handle_none(self):
        path = self._create_log()
        tailer = FleetLogTailer(path)
        self.assertIsNotNone(tailer.file_handle)
        tailer.close()
        self.assertIsNone(tailer.file_handle)

    def test_close_idempotent(self):
        path = self._create_log()
        tailer = FleetLogTailer(path)
        tailer.close()
        tailer.close()  # Should not raise
        self.assertIsNone(tailer.file_handle)

    # --- read_last_n_lines ---

    def test_read_last_n_lines_returns_messages(self):
        path = self._create_log()
        tailer = FleetLogTailer(path)
        lines = tailer.read_last_n_lines(5)
        # Should only return lines with timestamp pattern, not header
        for line in lines:
            self.assertIn("[ 2", line)
        self.assertEqual(len(lines), 2)  # Only 2 message lines in our test log
        tailer.close()

    def test_read_last_n_lines_limits_output(self):
        messages = [
            "Channel ID:      (None)",
            "Channel Name:    Fleet",
            "Listener:        TestChar",
            "Session started: 2025.12.16 08:24:57",
            "",
        ]
        for i in range(20):
            messages.append(f"[ 2025.12.16 08:{i:02d}:00 ] Player > Message {i}")

        path = self._create_log(lines=messages)
        tailer = FleetLogTailer(path)
        lines = tailer.read_last_n_lines(5)
        self.assertEqual(len(lines), 5)
        # Should be the LAST 5 messages
        self.assertIn("Message 15", lines[0])
        self.assertIn("Message 19", lines[4])
        tailer.close()

    def test_read_last_n_lines_fewer_than_n(self):
        path = self._create_log()
        tailer = FleetLogTailer(path)
        lines = tailer.read_last_n_lines(100)
        # Only 2 message lines exist, should return all of them
        self.assertEqual(len(lines), 2)
        tailer.close()

    def test_read_last_n_lines_nonexistent_file(self):
        path = os.path.join(self.test_dir, "missing.txt")
        tailer = FleetLogTailer(path)
        lines = tailer.read_last_n_lines(5)
        self.assertEqual(lines, [])

    def test_read_last_n_lines_skips_header(self):
        path = self._create_log()
        tailer = FleetLogTailer(path)
        lines = tailer.read_last_n_lines(100)
        for line in lines:
            self.assertNotIn("Channel ID", line)
            self.assertNotIn("Listener:", line)
            self.assertNotIn("Session started", line)
        tailer.close()

    def test_read_last_n_zero_returns_all(self):
        """n=0 results in message_lines[-0:] which returns all lines in Python."""
        path = self._create_log()
        tailer = FleetLogTailer(path)
        lines = tailer.read_last_n_lines(0)
        # Python slicing: [-0:] == [0:] == all items
        self.assertEqual(len(lines), 2)
        tailer.close()

    def test_read_last_n_lines_matches_full_scan_for_large_log(self):
        messages = [
            "Channel ID:      (None)",
            "Channel Name:    Fleet",
            "Listener:        TestChar",
            "Session started: 2025.12.16 08:24:57",
            "",
        ]
        expected_messages = []
        for i in range(250):
            line = f"[ 2025.12.16 10:{i % 60:02d}:00 ] Player > Message {i}"
            messages.append(line)
            expected_messages.append(line)

        path = self._create_log(lines=messages)
        tailer = FleetLogTailer(path)

        self.assertEqual(tailer.read_last_n_lines(7), expected_messages[-7:])
        tailer.close()

    # --- Integration ---

    def test_full_tail_workflow(self):
        """Simulate real usage: open, seek to end, read new lines as they arrive."""
        path = self._create_log()
        tailer = FleetLogTailer(path)
        tailer.seek_to_end()

        # No new lines yet
        self.assertEqual(tailer.read_new_lines(), [])

        # Simulate new message arriving
        with open(path, 'a', encoding='utf-16-le') as f:
            f.write("\r\n[ 2025.12.16 09:00:00 ] FC > Align to gate")

        lines = tailer.read_new_lines()
        self.assertTrue(len(lines) >= 1)
        self.assertTrue(any("Align to gate" in l for l in lines))

        # Another message
        with open(path, 'a', encoding='utf-16-le') as f:
            f.write("\r\n[ 2025.12.16 09:00:05 ] FC > Jump jump jump")

        lines = tailer.read_new_lines()
        self.assertTrue(any("Jump jump jump" in l for l in lines))

        tailer.close()


if __name__ == '__main__':
    unittest.main()

import os
import time
from pathlib import Path
from typing import List, Optional

class FleetLogTailer:
    """
    Tails a fleet log file, reading new lines as they are written.
    Handles UTF-16LE encoding and file rotation/truncation detection.
    """
    def __init__(self, filepath: str):
        self.filepath = Path(filepath)
        self.file_handle = None
        self.last_position = 0
        # End of the snapshot read by read_last_n_lines(); seek_to_end()
        # resumes there so a line written in between is never skipped.
        self._pending_anchor = None
        self._open()

    def _open(self):
        """Open file and seek to last position."""
        if not self.filepath.exists():
            # Wait or just set handle to None? 
            # For now, let's assume caller ensures file exists or we handle it gracefully.
            self.file_handle = None
            return

        try:
            # EVE logs are UTF-16 LE
            self.file_handle = open(self.filepath, 'r', encoding='utf-16-le', errors='replace')
            # If opening for the first time, we might want to start at the end 
            # (only read NEW messages) or start at beginning. 
            # For this implementation, we rely on seek_to_end() being called explicitly if needed.
            if self.last_position > 0:
                self.file_handle.seek(self.last_position)
        except OSError:
            self.file_handle = None

    def seek_to_end(self):
        """Skip existing content, continuing after the last complete line.

        After :meth:`read_last_n_lines` this resumes exactly where that history
        snapshot ended. A partially written last line is left for the next
        poll instead of being skipped.
        """
        if self.file_handle is None:
            if not self.filepath.exists():
                return
            self._open()
            if self.file_handle is None:
                return
        anchor = self._pending_anchor
        self._pending_anchor = None
        if anchor is None:
            try:
                size = self.filepath.stat().st_size
                anchor = self._last_line_end(size - (size % 2))
            except OSError:
                anchor = None
        if anchor is None:
            self.file_handle.seek(0, os.SEEK_END)
            self.last_position = self.file_handle.tell()
            return
        # At a line boundary the UTF-16 decoder state is clean, so the byte
        # offset is a valid text-mode seek cookie.
        self.file_handle.seek(anchor)
        self.last_position = anchor

    def _last_line_end(self, end: int) -> int:
        """Byte offset just after the last complete UTF-16 line before ``end``."""
        newline = b"\n\x00"
        position = end
        with open(self.filepath, 'rb') as log_file:
            while position > 0:
                start = max(0, position - 8192)
                start -= start % 2
                log_file.seek(start)
                chunk = log_file.read(position - start)
                search_end = len(chunk)
                while True:
                    index = chunk.rfind(newline, 0, search_end)
                    if index < 0:
                        break
                    if (start + index) % 2 == 0:
                        return start + index + 2
                    search_end = index + 1
                position = start
        return 0

    def read_new_lines(self) -> List[str]:
        """Read new lines since last call."""
        if not self.filepath.exists():
            return []

        if self.file_handle is None:
            self._open()
            if self.file_handle is None:
                return []

        # Check if file was truncated or rotated (size became smaller)
        try:
            current_size = self.filepath.stat().st_size
            if current_size < self.last_position:
                # File was truncated/rotated, reopen from start
                self.file_handle.close()
                self.last_position = 0
                self._open()
        except OSError:
            # File might have been locked or deleted momentarily
            return []

        # Read only complete lines. EVE can flush a UTF-16 message body before
        # its trailing CRLF; advancing past that partial record would make the
        # parser discard it permanently on the next poll.
        try:
            self.file_handle.seek(self.last_position)
            lines = []
            while True:
                line_start = self.file_handle.tell()
                line = self.file_handle.readline()
                if not line:
                    break
                if not line.endswith('\n'):
                    self.file_handle.seek(line_start)
                    break
                stripped = line.rstrip('\r\n')
                if stripped:
                    lines.append(stripped)
            self.last_position = self.file_handle.tell()
            return lines
        except (UnicodeError, OSError):
            # Encoding or IO error, potentially due to partial write
            return []

    def close(self):
        if self.file_handle:
            self.file_handle.close()
            self.file_handle = None

    @staticmethod
    def _is_message_line(line: str) -> bool:
        return '[ 2' in line and '] ' in line

    def _read_all_message_lines(self) -> List[str]:
        with open(self.filepath, 'r', encoding='utf-16-le', errors='replace') as f:
            return [
                line.rstrip('\r\n')
                for line in f
                if self._is_message_line(line)
            ]

    def read_last_n_lines(self, n: int = 30) -> List[str]:
        """
        Read the last N message lines from the log file.
        Robustly handles:
        - Files with < N lines
        - UTF-16LE encoding
        - Header lines (skipped)
        - Blank lines (skipped)
        - Files with no messages

        Args:
            n: Number of message lines to read (default 30)

        Returns:
            List of message lines (may be less than N if file is shorter)
        """
        if not self.filepath.exists():
            return []

        try:
            if n <= 0:
                return self._read_all_message_lines()

            file_size = self.filepath.stat().st_size
            # Read history only up to the last complete line, and remember
            # that point for seek_to_end() (see _pending_anchor).
            file_size = self._last_line_end(file_size - (file_size % 2))
            self._pending_anchor = file_size
            if file_size == 0:
                return []

            chunk_size = 8192
            position = file_size
            buffer = b''
            message_lines = []

            with open(self.filepath, 'rb') as f:
                while position > 0 and len(message_lines) < n:
                    read_size = min(chunk_size, position)
                    if read_size % 2 and read_size < position:
                        read_size -= 1
                    position -= read_size
                    f.seek(position)
                    buffer = f.read(read_size) + buffer

                    text = buffer.decode('utf-16-le', errors='replace')
                    lines = text.splitlines()
                    if position > 0 and lines:
                        lines = lines[1:]

                    message_lines = [
                        line.rstrip('\r\n')
                        for line in lines
                        if self._is_message_line(line)
                    ]

            if len(message_lines) <= n:
                return message_lines
            return message_lines[-n:]

        except (OSError, UnicodeError) as e:
            # File read error
            return []

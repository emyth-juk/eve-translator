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
        """Move read pointer to end of file (skip existing content)."""
        if self.file_handle:
            self.file_handle.seek(0, os.SEEK_END)
            self.last_position = self.file_handle.tell()
        else:
            # If file not open, try to open and seek
            if self.filepath.exists():
                self._open()
                if self.file_handle:
                    self.file_handle.seek(0, os.SEEK_END)
                    self.last_position = self.file_handle.tell()

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

        # Read new lines
        try:
            self.file_handle.seek(self.last_position)
            lines = self.file_handle.readlines()
            self.last_position = self.file_handle.tell()
            # rstrip to remove newline characters, but keep indentation if any
            return [line.rstrip('\r\n') for line in lines]
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

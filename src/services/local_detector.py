import os
import glob
import re
import time
from pathlib import Path
from typing import List, Tuple, Optional, Dict
from datetime import datetime
from src.core.character_info import CharacterInfo
import logging
import ctypes
from ctypes import wintypes

logger = logging.getLogger(__name__)


class LocalChatDetector:
    """
    Detects active local chat log files.
    Supports auto-detection of most recent log and character name parsing.
    """

    def parse_character_id_from_filename(self, filename: str) -> Optional[str]:
        """
        Extract character ID from Local chat filename.
        Args:
            filename: e.g., "Local_20251201_095136_1117005149.txt"
        """
        pattern = r'Local_\d{8}_\d{6}_(\d+)\.txt$'
        match = re.search(pattern, filename)
        return match.group(1) if match else None

    def extract_system_name(self, filepath: str) -> Optional[str]:
        """
        Extract system name from Local chat log.
        Scans values to find the LATEST system change (not just the first).
        "EVE System > Channel changed to Local : <SYSTEM_NAME>"
        """
        last_system = None
        try:
            with open(filepath, 'r', encoding='utf-16-le', errors='replace') as f:
                # We can iterate the whole file. EVE logs aren't massive.
                # If they are huge, we might want to seek to end and read back,
                # but for simplicity and robustness, forward scan is fine for text files < 50MB.
                for line in f:
                    if 'EVE System' in line and 'Channel changed to Local' in line:
                        parts = line.split('Channel changed to Local')
                        if len(parts) > 1:
                            # Extract system name after colon
                            system_part = parts[1].split(':')
                            if len(system_part) > 1:
                                last_system = system_part[1].strip()
        except (OSError, UnicodeError):
            pass
        return last_system

    def is_character_window_open(self, char_name: str) -> bool:
        """
        Check if an EVE Online window exists with title 'EVE - CharacterName'.
        Returns True if found.
        """
        if not char_name:
            return False

        found = False
        target_title = f"EVE - {char_name}"
        
        try:
            user32 = ctypes.windll.user32
            WNDENUMPROC = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
            
            def enum_proc(hwnd, lParam):
                nonlocal found
                length = user32.GetWindowTextLengthW(hwnd)
                if length > 0:
                    buff = ctypes.create_unicode_buffer(length + 1)
                    user32.GetWindowTextW(hwnd, buff, length + 1)
                    title = buff.value
                    if title == target_title:
                        found = True
                        return False # Stop enumeration
                return True
            
            user32.EnumWindows(WNDENUMPROC(enum_proc), 0)
        except Exception as e:
            logger.debug(f"Window enumeration failed: {e}")
        
        return found

    def scan_active_characters(self, log_dir: str) -> Dict[str, CharacterInfo]:
        """
        Scan all Local chat logs and group by character.
        Returns:
            Dict mapping CharacterID -> CharacterInfo
            Only includes the LATEST log per character.
        """
        character_logs = {}  # CharID -> list of (path, mtime)

        try:
            with os.scandir(log_dir) as entries:
                for entry in entries:
                    if entry.is_file() and entry.name.startswith("Local_") and entry.name.endswith(".txt"):
                        char_id = self.parse_character_id_from_filename(entry.name)
                        if char_id:
                            try:
                                mtime = entry.stat().st_mtime
                                if char_id not in character_logs:
                                    character_logs[char_id] = []
                                character_logs[char_id].append((entry.path, mtime))
                            except OSError:
                                continue
        except OSError:
            return {}

        # For each character, get latest log and extract info
        result = {}
        for char_id, logs in character_logs.items():
            # Sort by mtime, get most recent
            logs.sort(key=lambda x: x[1], reverse=True)
            latest_path, latest_mtime = logs[0]

            # Extract character name from log header (try latest, then older logs)
            char_name = None
            for path, _ in logs:
                char_name = self.get_character_from_log(path)
                if char_name:
                    break
            
            if not char_name:
                char_name = f"Character_{char_id}"  # Fallback

            # Extract system name from first message
            system_name = self.extract_system_name(latest_path)

            # Check if active (modified within last 5 minutes OR Window Open)
            is_active_log = (time.time() - latest_mtime) < 300
            is_window_open = self.is_character_window_open(char_name)
            
            is_active = is_active_log or is_window_open

            result[char_id] = CharacterInfo(
                character_id=char_id,
                character_name=char_name,
                latest_log_path=latest_path,
                log_mtime=latest_mtime,
                system_name=system_name,
                is_active=is_active
            )

        return result

    def get_latest_log_for_character(self, log_dir: str, char_id: str) -> Optional[str]:
        """
        Get the most recent Local chat log for a specific character.
        """
        latest_path = None
        latest_mtime = 0

        try:
            with os.scandir(log_dir) as entries:
                for entry in entries:
                    if entry.is_file() and entry.name.startswith("Local_") and entry.name.endswith(".txt"):
                        entry_char_id = self.parse_character_id_from_filename(entry.name)
                        if entry_char_id == char_id:
                            try:
                                mtime = entry.stat().st_mtime
                                if mtime > latest_mtime:
                                    latest_mtime = mtime
                                    latest_path = entry.path
                            except OSError:
                                continue
        except OSError:
            return None
        return latest_path

    def find_local_logs(self, log_dir: str) -> List[Tuple[str, float]]:
        """
        Find all Local_*.txt files with their modification times.
        Uses scandir for better performance on large directories.
        """
        result = []
        try:
            with os.scandir(log_dir) as entries:
                for entry in entries:
                    if entry.is_file() and entry.name.startswith("Local_") and entry.name.endswith(".txt"):
                        try:
                            # entry.stat().st_mtime is cached on Windows
                            result.append((entry.path, entry.stat().st_mtime))
                        except OSError:
                            continue
        except OSError:
             return []
        return result

    def get_most_recent_local(self, log_dir: str) -> Optional[str]:
        """
        Get most recently modified Local chat log efficiently.
        """
        most_recent_file = None
        most_recent_time = 0
        
        try:
            with os.scandir(log_dir) as entries:
                for entry in entries:
                    if entry.is_file() and entry.name.startswith("Local_") and entry.name.endswith(".txt"):
                        try:
                            # entry.stat().st_mtime is cached on Windows
                            mtime = entry.stat().st_mtime
                            if mtime > most_recent_time:
                                most_recent_time = mtime
                                most_recent_file = entry.path
                        except OSError:
                            continue
        except OSError as e:
            logger.error(f"Error scanning logs: {e}")
            return None
            
        return most_recent_file

    def get_character_from_log(self, filepath: str) -> Optional[str]:
        """
        Parse character name from log header.
        Reads first ~15 lines looking for "Listener: <CharacterName>".
        """
        try:
            with open(filepath, 'r', encoding='utf-16-le', errors='replace') as f:
                for _ in range(15):
                    line = f.readline()
                    if not line:
                        break
                    if 'Listener:' in line:
                        parts = line.split('Listener:')
                        if len(parts) > 1:
                            return parts[1].strip()
        except (OSError, UnicodeError):
            pass
        return None

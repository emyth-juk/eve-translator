import sys
import time
from collections.abc import Iterable
from pathlib import Path
from typing import Dict, Optional

from src.core.character_info import CharacterInfo
from src.services.log_discovery import (
    EVELogEntry,
    EVE_LOG_FUTURE_MTIME_SKEW_SECONDS,
    UTF16HeaderCache,
    normalized_log_path,
    parse_eve_log_filename,
    scan_eve_log_entries,
)
import logging

if sys.platform == 'win32':
    import ctypes
    from ctypes import wintypes

logger = logging.getLogger(__name__)


class LocalChatDetector:
    """
    Detects active local chat log files.
    Supports auto-detection of most recent log and character name parsing.
    """

    def __init__(self):
        self._header_cache = UTF16HeaderCache()
        self._system_cache = {}

    def _file_cache_key(self, filepath: str):
        try:
            path = Path(filepath)
            stat = path.stat()
            return (str(path), stat.st_size, stat.st_mtime_ns)
        except OSError:
            return None

    def parse_character_id_from_filename(self, filename: str) -> Optional[str]:
        """
        Extract character ID from Local chat filename.
        Args:
            filename: e.g., "Local_20251201_095136_1117005149.txt"
        """
        parsed = parse_eve_log_filename(filename)
        if parsed is None or parsed.channel_name.casefold() != "local":
            return None
        return parsed.character_id

    def extract_system_name(self, filepath: str) -> Optional[str]:
        """
        Extract system name from Local chat log.
        Scans values to find the LATEST system change (not just the first).
        "EVE System > Channel changed to Local : <SYSTEM_NAME>"
        """
        return self._extract_system_name(
            filepath,
            self._file_cache_key(filepath),
        )

    def _extract_system_name(
        self,
        filepath: str,
        cache_key: tuple[str, int, int] | None,
    ) -> Optional[str]:
        if cache_key in self._system_cache:
            return self._system_cache[cache_key]

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
        if cache_key is not None:
            self._system_cache[cache_key] = last_system
        return last_system

    def is_character_window_open(self, char_name: str) -> bool:
        """
        Check if an EVE Online window exists with title 'EVE - CharacterName'.
        Returns True if found.
        """
        if not char_name or sys.platform != 'win32':
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

    def scan_active_characters(
        self,
        log_dir: str,
        *,
        entries: Iterable[EVELogEntry] | None = None,
    ) -> Dict[str, CharacterInfo]:
        """
        Scan all Local chat logs and group by character.
        Returns:
            Dict mapping CharacterID -> CharacterInfo
            Only includes the LATEST log per character.
        """
        character_logs: dict[str, list[EVELogEntry]] = {}
        snapshot = entries if entries is not None else scan_eve_log_entries(log_dir)
        for entry in snapshot:
            if not (
                entry.name.startswith("Local_")
                and entry.name.endswith(".txt")
            ):
                continue
            parsed = entry.filename
            if (
                parsed is None
                or parsed.channel_name.casefold() != "local"
                or parsed.character_id is None
            ):
                continue
            character_logs.setdefault(parsed.character_id, []).append(entry)

        # For each character, get latest log and extract info
        result = {}
        for char_id, logs in character_logs.items():
            # Preserve Local's mtime-first rotation behavior while making ties
            # deterministic across filesystem enumeration orders.
            logs.sort(
                key=lambda item: (
                    item.mtime,
                    item.filename.started_time if item.filename else 0.0,
                    normalized_log_path(item.path),
                    item.path,
                ),
                reverse=True,
            )
            latest = logs[0]

            # Extract character name from log header (try latest, then older logs)
            char_name = None
            for entry in logs:
                header = self._header_cache.read(
                    entry.path,
                    entry.size,
                    entry.mtime_ns,
                )
                char_name = (
                    header.listener_name if header is not None else ""
                )
                if char_name:
                    break
            
            if not char_name:
                char_name = f"Character_{char_id}"  # Fallback

            # Extract system name from first message
            system_name = self._extract_system_name(
                latest.path,
                (latest.path, latest.size, latest.mtime_ns),
            )

            # Check if active (modified within last 5 minutes OR Window Open)
            log_age = time.time() - latest.mtime
            is_active_log = (
                -EVE_LOG_FUTURE_MTIME_SKEW_SECONDS <= log_age <= 300
            )
            is_window_open = self.is_character_window_open(char_name)
            
            is_active = is_active_log or is_window_open

            result[char_id] = CharacterInfo(
                character_id=char_id,
                character_name=char_name,
                latest_log_path=latest.path,
                log_mtime=latest.mtime,
                system_name=system_name,
                is_active=is_active
            )

        return result

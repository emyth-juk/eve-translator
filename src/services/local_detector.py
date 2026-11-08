import os
import sys
import time
from collections import OrderedDict
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, Optional

from src.core.character_info import CharacterInfo
from src.services.log_discovery import (
    EVELogEntry,
    EVE_LOG_FUTURE_MTIME_SKEW_SECONDS,
    UTF16HeaderCache,
    check_discovery_stop,
    normalized_log_path,
    parse_eve_log_filename,
    scan_eve_log_entries,
)
import logging

if sys.platform == 'win32':
    import ctypes
    from ctypes import wintypes

logger = logging.getLogger(__name__)
SYSTEM_CACHE_SIZE = 128


@dataclass
class _SystemScanState:
    size: int
    mtime_ns: int
    offset: int
    last_system: str | None
    pending_bytes: bytes = b""
    pending_text: str = ""


class LocalChatDetector:
    """
    Detects active local chat log files.
    Supports auto-detection of most recent log and character name parsing.
    """

    def __init__(self):
        self._header_cache = UTF16HeaderCache()
        self._system_cache: OrderedDict[str, _SystemScanState] = OrderedDict()
        # Optional shutdown probe installed by the discovery worker.
        self.should_stop = None
        # EVE window titles collected once per scan pass (None = not cached).
        self._window_titles: set[str] | None = None

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
        if cache_key is None:
            last_system = None
            try:
                with open(
                    filepath,
                    'r',
                    encoding='utf-16-le',
                    errors='replace',
                ) as log_file:
                    for line in log_file:
                        last_system = self._system_from_line(
                            line,
                            last_system,
                        )
            except (OSError, UnicodeError):
                pass
            return last_system

        path, size, mtime_ns = cache_key
        normalized_path = normalized_log_path(path)
        state = self._system_cache.get(normalized_path)
        if (
            state is not None
            and state.size == size
            and state.mtime_ns == mtime_ns
        ):
            self._system_cache.move_to_end(normalized_path)
            return state.last_system

        is_append = (
            state is not None
            and size > state.size
            and state.offset <= size
        )
        offset = state.offset if is_append else 0
        last_system = state.last_system if is_append else None
        pending_bytes = state.pending_bytes if is_append else b""
        pending_text = state.pending_text if is_append else ""
        try:
            with open(filepath, 'rb') as log_file:
                log_file.seek(offset)
                raw = log_file.read()
        except (OSError, UnicodeError):
            return last_system

        data = pending_bytes + raw
        decoded_bytes = data[: len(data) - (len(data) % 2)]
        next_pending_bytes = data[len(decoded_bytes):]
        encoding = (
            'utf-16'
            if offset == 0
            and decoded_bytes.startswith((b'\xff\xfe', b'\xfe\xff'))
            else 'utf-16-le'
        )
        decoded = decoded_bytes.decode(encoding, errors='replace')
        text = pending_text + decoded
        for line in text.splitlines():
            last_system = self._system_from_line(line, last_system)

        split = text.splitlines(keepends=True)
        next_pending_text = (
            split[-1]
            if split and not split[-1].endswith(('\r', '\n'))
            else ''
        )
        self._system_cache[normalized_path] = _SystemScanState(
            size=max(size, offset + len(raw)),
            mtime_ns=mtime_ns,
            offset=offset + len(raw),
            last_system=last_system,
            pending_bytes=next_pending_bytes,
            pending_text=next_pending_text,
        )
        self._system_cache.move_to_end(normalized_path)
        while len(self._system_cache) > SYSTEM_CACHE_SIZE:
            self._system_cache.popitem(last=False)
        return last_system

    @staticmethod
    def _system_from_line(
        line: str,
        current: str | None,
    ) -> str | None:
        marker = 'Channel changed to Local'
        if 'EVE System' not in line or marker not in line:
            return current
        _prefix, _separator, suffix = line.partition(marker)
        _before_colon, colon, system_name = suffix.partition(':')
        if not colon:
            return current
        normalized = system_name.strip()
        return normalized or current

    def is_character_window_open(self, char_name: str) -> bool:
        """
        Check if an EVE Online window exists with title 'EVE - CharacterName'.
        Returns True if found.
        """
        if not char_name or sys.platform != 'win32':
            return False
        titles = self._window_titles
        if titles is None:
            titles = self._eve_window_names()
        return char_name in titles

    @staticmethod
    def _eve_window_names() -> set[str]:
        """Return character names from all ``EVE - <name>`` window titles.

        Windows owned by this process are skipped: reading their titles sends
        WM_GETTEXT to the GUI thread, which deadlocks while that thread waits
        for discovery to stop. ``InternalGetWindowText`` never sends messages,
        so a hung window in another program cannot block discovery either.
        """
        names: set[str] = set()
        if sys.platform != 'win32':
            return names
        try:
            user32 = ctypes.windll.user32
            WNDENUMPROC = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
            own_pid = os.getpid()
            process_id = wintypes.DWORD()
            buffer = ctypes.create_unicode_buffer(256)
            read_title = getattr(user32, 'InternalGetWindowText', None)

            def enum_proc(hwnd, _lparam):
                user32.GetWindowThreadProcessId(hwnd, ctypes.byref(process_id))
                if process_id.value == own_pid:
                    return True
                if read_title is not None:
                    length = read_title(hwnd, buffer, len(buffer))
                else:
                    length = user32.GetWindowTextW(hwnd, buffer, len(buffer))
                if length > 6:
                    title = buffer.value
                    if title.startswith("EVE - "):
                        names.add(title[6:])
                return True

            user32.EnumWindows(WNDENUMPROC(enum_proc), 0)
        except Exception as e:
            logger.debug(f"Window enumeration failed: {e}")
        return names

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
        try:
            return self._scan_active_characters(log_dir, entries)
        finally:
            self._window_titles = None

    def _scan_active_characters(
        self,
        log_dir: str,
        entries: Iterable[EVELogEntry] | None,
    ) -> Dict[str, CharacterInfo]:
        character_logs: dict[str, list[EVELogEntry]] = {}
        snapshot = (
            entries
            if entries is not None
            else scan_eve_log_entries(log_dir, self.should_stop)
        )
        for index, entry in enumerate(snapshot):
            check_discovery_stop(self.should_stop, index)
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
        for index, (char_id, logs) in enumerate(character_logs.items()):
            check_discovery_stop(self.should_stop, index)
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
            if (
                not is_active_log
                and self._window_titles is None
                and sys.platform == 'win32'
            ):
                # One window enumeration per pass, not one per character.
                self._window_titles = self._eve_window_names()
            # A recent write already proves activity; skip the window probe.
            is_window_open = (
                False if is_active_log else self.is_character_window_open(char_name)
            )

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

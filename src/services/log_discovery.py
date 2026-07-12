"""Shared parsing and bounded metadata caching for EVE chat-log discovery."""

from __future__ import annotations

import os
import re
import stat as stat_module
from collections import OrderedDict
from dataclasses import dataclass
from datetime import datetime, timezone
from functools import lru_cache

EVE_FILENAME_CACHE_SIZE = 4_096
EVE_HEADER_CACHE_SIZE = 2_048
EVE_HEADER_READ_BYTES = 16_384
EVE_LOG_FUTURE_MTIME_SKEW_SECONDS = 2.0

_FILENAME_PATTERN = re.compile(
    r"^(?P<channel>.+)_(?P<date>\d{8})_(?P<time>\d{6})"
    r"(?:_(?P<character_id>\d+))?\.txt$",
    re.IGNORECASE,
)
_HEADER_PATTERN = re.compile(
    r"^\s*(Channel ID|Channel Name|Listener|Session started):\s*(.*?)\s*$"
)


@dataclass(frozen=True)
class EVELogFilename:
    channel_name: str
    character_id: str | None
    date_text: str
    time_text: str
    started_time: float


@dataclass(frozen=True)
class EVELogHeader:
    channel_id: str = ""
    channel_name: str = ""
    listener_name: str = ""
    started_time: float | None = None


@dataclass(frozen=True)
class EVELogEntry:
    """One immutable file/stat/name record shared by chat detectors."""

    name: str
    path: str
    size: int
    mtime: float
    mtime_ns: int
    ctime: float
    filename: EVELogFilename | None


def normalized_log_path(path: str | os.PathLike[str]) -> str:
    """Return the Windows-safe identity form of a log path."""
    return os.path.normcase(os.path.abspath(os.fspath(path)))


def log_recency_key(
    started_time: float,
    mtime: float,
    path: str | os.PathLike[str],
) -> tuple[float, float, str, str]:
    """Return a deterministic newest-log key with stable path tie breakers."""
    absolute_path = os.path.abspath(os.fspath(path))
    return (
        float(started_time),
        float(mtime),
        normalized_log_path(absolute_path),
        absolute_path,
    )


def parse_eve_timestamp(value: object) -> float | None:
    """Parse an EVE UTC timestamp or preserve an epoch timestamp."""
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, datetime):
        if value.tzinfo is None:
            value = value.replace(tzinfo=timezone.utc)
        return value.timestamp()
    if isinstance(value, str):
        stripped = value.strip()
        for format_string in (
            "%Y.%m.%d %H:%M:%S",
            "%Y%m%d_%H%M%S",
            "%Y%m%d%H%M%S",
        ):
            try:
                return datetime.strptime(stripped, format_string).replace(
                    tzinfo=timezone.utc
                ).timestamp()
            except ValueError:
                continue
    return None


@lru_cache(maxsize=EVE_FILENAME_CACHE_SIZE)
def _parse_eve_log_basename(filename: str) -> EVELogFilename | None:
    match = _FILENAME_PATTERN.fullmatch(filename)
    if not match:
        return None
    date_text = match.group("date")
    time_text = match.group("time")
    started_time = parse_eve_timestamp(date_text + time_text)
    if started_time is None:
        return None
    return EVELogFilename(
        channel_name=match.group("channel"),
        character_id=match.group("character_id"),
        date_text=date_text,
        time_text=time_text,
        started_time=started_time,
    )


def parse_eve_log_filename(filename: str | os.PathLike[str]) -> EVELogFilename | None:
    """Parse an EVE log name greedily from its timestamp-bearing right side."""
    return _parse_eve_log_basename(os.path.basename(os.fspath(filename)))


def scan_eve_log_entries(
    log_dir: str | os.PathLike[str],
) -> tuple[EVELogEntry, ...]:
    """Enumerate a directory once and snapshot every regular file once.

    An empty directory is a successful empty snapshot. Directory access and
    file metadata failures propagate so callers can preserve their last-good
    registries and retry instead of mistaking an incomplete scan for logout.
    Files that disappear while the directory is being enumerated are skipped;
    that is the expected shape of ordinary EVE log rotation.
    """
    entries: list[EVELogEntry] = []
    with os.scandir(log_dir) as directory:
        for entry in directory:
            try:
                entry_stat = entry.stat()
            except FileNotFoundError:
                continue
            if not stat_module.S_ISREG(entry_stat.st_mode):
                continue
            entries.append(
                EVELogEntry(
                    name=entry.name,
                    path=os.path.abspath(entry.path),
                    size=entry_stat.st_size,
                    mtime=entry_stat.st_mtime,
                    mtime_ns=entry_stat.st_mtime_ns,
                    ctime=entry_stat.st_ctime,
                    filename=parse_eve_log_filename(entry.name),
                )
            )
    return tuple(entries)


class UTF16HeaderCache:
    """Read standard EVE headers once per path/size/mtime version."""

    def __init__(self, max_size: int = EVE_HEADER_CACHE_SIZE):
        self.max_size = max(1, int(max_size))
        self._entries: OrderedDict[
            str, tuple[int, int, EVELogHeader | None]
        ] = OrderedDict()

    @staticmethod
    def _key(path: str | os.PathLike[str]) -> str:
        return normalized_log_path(path)

    @staticmethod
    def _decode(raw: bytes) -> str:
        if len(raw) % 2:
            raw = raw[:-1]
        encoding = (
            "utf-16"
            if raw.startswith((b"\xff\xfe", b"\xfe\xff"))
            else "utf-16-le"
        )
        return raw.decode(encoding, errors="replace")

    def peek(
        self,
        path: str | os.PathLike[str],
        size: int,
        mtime_ns: int,
    ) -> tuple[bool, EVELogHeader | None]:
        key = self._key(path)
        cached = self._entries.get(key)
        if cached is not None and cached[:2] == (size, mtime_ns):
            self._entries.move_to_end(key)
            return True, cached[2]
        return False, None

    def read(
        self,
        path: str | os.PathLike[str],
        size: int | None = None,
        mtime_ns: int | None = None,
    ) -> EVELogHeader | None:
        key = self._key(path)
        if size is None or mtime_ns is None:
            try:
                stat = os.stat(path)
            except OSError:
                return None
            size = stat.st_size
            mtime_ns = stat.st_mtime_ns
        found, cached = self.peek(key, size, mtime_ns)
        if found:
            return cached

        header: EVELogHeader | None = None
        try:
            with open(key, "rb") as log_file:
                text = self._decode(log_file.read(EVE_HEADER_READ_BYTES))
            values: dict[str, str] = {}
            for line in text.splitlines():
                match = _HEADER_PATTERN.match(line.lstrip("\ufeff"))
                if match:
                    values[match.group(1)] = match.group(2).strip()
                if len(values) == 4:
                    break
            if values:
                header = EVELogHeader(
                    channel_id=values.get("Channel ID", "").strip(),
                    channel_name=values.get("Channel Name", "").strip(),
                    listener_name=values.get("Listener", "").strip(),
                    started_time=parse_eve_timestamp(
                        values.get("Session started", "")
                    ),
                )
        except (OSError, UnicodeError):
            header = None

        self._entries[key] = (size, mtime_ns, header)
        self._entries.move_to_end(key)
        while len(self._entries) > self.max_size:
            self._entries.popitem(last=False)
        return header

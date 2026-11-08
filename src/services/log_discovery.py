"""Shared parsing and bounded metadata caching for EVE chat-log discovery."""

from __future__ import annotations

import os
import re
import stat as stat_module
import sys
from collections import OrderedDict
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from datetime import datetime, timezone
from functools import lru_cache

EVE_FILENAME_CACHE_SIZE = 4_096
# Legacy bound kept for callers that pass it explicitly. Discovery caches are
# unbounded and pruned to the folder instead: a Chatlogs folder often holds
# 20k-100k files, and an LRU bound smaller than that evicts in scan order, so
# every "warm" pass re-opened nearly every log.
EVE_HEADER_CACHE_SIZE = 2_048
EVE_HEADER_READ_BYTES = 16_384
EVE_LOG_FUTURE_MTIME_SKEW_SECONDS = 2.0
# Check for shutdown this often inside long per-file loops.
DISCOVERY_STOP_CHECK_INTERVAL = 128


class DiscoveryCancelled(Exception):
    """Raised inside a discovery pass when shutdown or a newer pass wins."""


def check_discovery_stop(
    should_stop: Callable[[], bool] | None,
    index: int,
) -> None:
    """Raise :class:`DiscoveryCancelled` every few iterations if requested."""
    if (
        should_stop is not None
        and index % DISCOVERY_STOP_CHECK_INTERVAL == 0
        and should_stop()
    ):
        raise DiscoveryCancelled()

_FILENAME_PATTERN = re.compile(
    r"^(?P<channel>.+)_(?P<date>\d{8})_(?P<time>\d{6})"
    r"(?:_(?P<character_id>\d+))?\.txt$",
    re.IGNORECASE,
)
_HEADER_PATTERN = re.compile(
    r"^\s*(Channel ID|Channel Name|Listener|Session started):\s*(.*?)\s*$"
)


@dataclass(frozen=True, slots=True)
class EVELogFilename:
    channel_name: str
    character_id: str | None
    date_text: str
    time_text: str
    started_time: float


@dataclass(frozen=True, slots=True)
class EVELogHeader:
    channel_id: str = ""
    channel_name: str = ""
    listener_name: str = ""
    started_time: float | None = None


@dataclass(frozen=True, slots=True)
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
    try:
        # Direct digits parse: strptime via parse_eve_timestamp tried two
        # failing formats per file, a large share of a 60k-file pass.
        started_time = datetime(
            int(date_text[0:4]),
            int(date_text[4:6]),
            int(date_text[6:8]),
            int(time_text[0:2]),
            int(time_text[2:4]),
            int(time_text[4:6]),
            tzinfo=timezone.utc,
        ).timestamp()
    except ValueError:
        return None
    character_id = match.group("character_id")
    return EVELogFilename(
        channel_name=sys.intern(match.group("channel")),
        character_id=sys.intern(character_id) if character_id else character_id,
        date_text=date_text,
        time_text=time_text,
        started_time=started_time,
    )


def parse_eve_log_filename(filename: str | os.PathLike[str]) -> EVELogFilename | None:
    """Parse an EVE log name greedily from its timestamp-bearing right side."""
    return _parse_eve_log_basename(os.path.basename(os.fspath(filename)))


def resolve_chatlogs_dir(path: str | os.PathLike[str]) -> str:
    """Use the Chatlogs subfolder when the EVE ``logs`` folder was chosen.

    Picking the EVE ``logs`` folder instead of its ``Chatlogs`` subfolder
    scanned successfully but found nothing, leaving every session Waiting.
    """
    text = os.fspath(path)
    if os.path.basename(os.path.normpath(text)).casefold() == "chatlogs":
        return text
    candidate = os.path.join(text, "Chatlogs")
    return candidate if os.path.isdir(candidate) else text


def folder_has_eve_logs(path: str | os.PathLike[str], limit: int = 2000) -> bool:
    """Whether the folder holds at least one file named like an EVE chat log."""
    try:
        with os.scandir(path) as directory:
            for index, entry in enumerate(directory):
                if index >= limit:
                    return True  # a big folder; do not keep scanning on the GUI
                if parse_eve_log_filename(entry.name) is not None:
                    return True
    except OSError:
        return False
    return False


def scan_eve_log_entries(
    log_dir: str | os.PathLike[str],
    should_stop: Callable[[], bool] | None = None,
) -> tuple[EVELogEntry, ...]:
    """Enumerate a directory once and snapshot every regular file once.

    An empty directory is a successful empty snapshot. Directory access and
    file metadata failures propagate so callers can preserve their last-good
    registries and retry instead of mistaking an incomplete scan for logout.
    Files that disappear while the directory is being enumerated are skipped;
    that is the expected shape of ordinary EVE log rotation. ``should_stop``
    is polled periodically and aborts with :class:`DiscoveryCancelled`.
    """
    entries: list[EVELogEntry] = []
    with os.scandir(log_dir) as directory:
        for index, entry in enumerate(directory):
            check_discovery_stop(should_stop, index)
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
    """Read standard EVE headers once per path/size/mtime version.

    By default the cache is unbounded and callers prune it to the current
    folder snapshot with :meth:`retain`, so memory follows the folder size
    instead of evicting entries that the next pass needs again.
    """

    def __init__(self, max_size: int | None = None):
        self.max_size = None if max_size is None else max(1, int(max_size))
        self._entries: OrderedDict[
            str, tuple[int, int, EVELogHeader | None]
        ] = OrderedDict()

    def __len__(self) -> int:
        return len(self._entries)

    def retain(self, paths: Iterable[str | os.PathLike[str]]) -> None:
        """Drop cached headers for files that are no longer in the folder."""
        keep = {self._key(path) for path in paths}
        for key in [key for key in self._entries if key not in keep]:
            del self._entries[key]

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
                # Channel and listener names repeat across thousands of logs.
                header = EVELogHeader(
                    channel_id=sys.intern(values.get("Channel ID", "").strip()),
                    channel_name=sys.intern(values.get("Channel Name", "").strip()),
                    listener_name=sys.intern(values.get("Listener", "").strip()),
                    started_time=parse_eve_timestamp(
                        values.get("Session started", "")
                    ),
                )
        except (OSError, UnicodeError):
            header = None

        self._entries[key] = (size, mtime_ns, header)
        self._entries.move_to_end(key)
        if self.max_size is not None:
            while len(self._entries) > self.max_size:
                self._entries.popitem(last=False)
        return header

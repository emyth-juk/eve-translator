"""Discover selectable EVE chat logs outside the Fleet and Local sessions."""

from __future__ import annotations

import logging
import os
import re
from collections import OrderedDict
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import quote

from src.core.channel_info import ChatChannelInfo

logger = logging.getLogger(__name__)

_FILENAME_PATTERN = re.compile(
    r"^(?P<channel>.+)_(?P<date>\d{8})_(?P<time>\d{6})_"
    r"(?P<character_id>\d+)\.txt$",
    re.IGNORECASE,
)
_HEADER_PATTERN = re.compile(
    r"^\s*(Channel ID|Channel Name|Listener|Session started):\s*(.*?)\s*$"
)
_HEADER_READ_BYTES = 16_384
_INVALID_CHANNEL_IDS = frozenset({"", "(none)", "none"})
CHANNEL_HEADER_CACHE_SIZE = 2_048


@dataclass(frozen=True)
class _ChannelHeader:
    channel_id: str
    channel_name: str
    listener_name: str
    started_time: float | None


def make_selection_id(channel_id: str, character_id: str) -> str:
    """Build an unambiguous, stable key for a channel/character source."""
    normalized_channel = str(channel_id or "").strip()
    normalized_character = str(character_id or "").strip()
    if not normalized_channel or not normalized_character:
        raise ValueError("channel_id and character_id are required")
    return (
        f"{quote(normalized_channel, safe='')}::"
        f"{quote(normalized_character, safe='')}"
    )


class AnyChatDetector:
    """Find and collapse arbitrary EVE chat-log rotations.

    A selectable source is one authoritative header Channel ID combined with
    one character ID from the right side of the filename. Logs for the same
    channel on two characters deliberately remain separate: merging them would
    duplicate messages that both EVE clients received.
    """

    def __init__(self):
        self._header_cache: OrderedDict[
            str, tuple[int, int, _ChannelHeader | None]
        ] = OrderedDict()

    @staticmethod
    def parse_filename(filename: str) -> tuple[str, str, float] | None:
        """Return ``(filename_channel, character_id, UTC start time)``."""
        match = _FILENAME_PATTERN.fullmatch(Path(filename).name)
        if not match:
            return None
        try:
            started = datetime.strptime(
                match.group("date") + match.group("time"),
                "%Y%m%d%H%M%S",
            ).replace(tzinfo=timezone.utc)
        except ValueError:
            return None
        return (
            match.group("channel"),
            match.group("character_id"),
            started.timestamp(),
        )

    @staticmethod
    def _parse_started_time(value: object) -> float | None:
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
            for format_string in ("%Y.%m.%d %H:%M:%S", "%Y%m%d_%H%M%S"):
                try:
                    return datetime.strptime(stripped, format_string).replace(
                        tzinfo=timezone.utc
                    ).timestamp()
                except ValueError:
                    continue
        return None

    @staticmethod
    def _decode_header(raw: bytes) -> str:
        if len(raw) % 2:
            raw = raw[:-1]
        encoding = (
            "utf-16"
            if raw.startswith((b"\xff\xfe", b"\xfe\xff"))
            else "utf-16-le"
        )
        return raw.decode(encoding, errors="replace")

    def _read_header(
        self,
        path: Path,
        size: int,
        mtime_ns: int,
    ) -> _ChannelHeader | None:
        cache_key = str(path.resolve(strict=False))
        cached = self._header_cache.get(cache_key)
        if cached is not None and cached[:2] == (size, mtime_ns):
            self._header_cache.move_to_end(cache_key)
            return cached[2]

        header: _ChannelHeader | None = None
        try:
            with path.open("rb") as log_file:
                text = self._decode_header(log_file.read(_HEADER_READ_BYTES))
            values: dict[str, str] = {}
            for line in text.splitlines():
                match = _HEADER_PATTERN.match(line.lstrip("\ufeff"))
                if match:
                    values[match.group(1)] = match.group(2).strip()
                if len(values) == 4:
                    break

            channel_id = values.get("Channel ID", "").strip()
            channel_name = values.get("Channel Name", "").strip()
            listener_name = values.get("Listener", "").strip()
            if (
                channel_id.casefold() not in _INVALID_CHANNEL_IDS
                and channel_name
                and listener_name
            ):
                header = _ChannelHeader(
                    channel_id=channel_id,
                    channel_name=channel_name,
                    listener_name=listener_name,
                    started_time=self._parse_started_time(
                        values.get("Session started", "")
                    ),
                )
        except (OSError, UnicodeError):
            header = None

        self._header_cache[cache_key] = (size, mtime_ns, header)
        self._header_cache.move_to_end(cache_key)
        while len(self._header_cache) > CHANNEL_HEADER_CACHE_SIZE:
            self._header_cache.popitem(last=False)
        return header

    @staticmethod
    def _is_native_session(channel_id: str) -> bool:
        normalized = channel_id.strip().casefold()
        return (
            normalized in {"fleet", "local"}
            or normalized.startswith("fleet_")
            or normalized.startswith("local_")
        )

    @staticmethod
    def _candidate_order(info: ChatChannelInfo) -> tuple[float, float, str, str]:
        path = str(Path(info.log_path).resolve(strict=False))
        return (
            info.started_time,
            info.log_mtime,
            path.casefold(),
            path,
        )

    @classmethod
    def _active_character_starts(
        cls,
        active_characters: Iterable[str] | Mapping[str, object] | None,
    ) -> dict[str, float | None]:
        if active_characters is None:
            return {}
        if isinstance(active_characters, Mapping):
            result: dict[str, float | None] = {}
            for character_id, session_start in active_characters.items():
                normalized = str(character_id or "").strip()
                if not normalized or session_start is False:
                    continue
                if session_start is None or session_start is True:
                    result[normalized] = None
                    continue
                parsed_start = cls._parse_started_time(session_start)
                if parsed_start is not None:
                    result[normalized] = parsed_start
            return result
        if isinstance(active_characters, str):
            active_characters = (active_characters,)
        return {
            normalized: None
            for character_id in active_characters
            if (normalized := str(character_id or "").strip())
        }

    def scan_channels(
        self,
        log_dir: str | os.PathLike[str],
        active_characters: Iterable[str] | Mapping[str, object] | None = None,
    ) -> dict[str, ChatChannelInfo]:
        """Return the newest rotation for every arbitrary channel source.

        ``active_characters`` may be an iterable of active character IDs, or a
        mapping from character ID to that character's current Local-session
        start. With a supplied start, an older arbitrary log is not considered
        active even though the same character is currently online.
        """
        active_starts = self._active_character_starts(active_characters)
        selected: dict[str, ChatChannelInfo] = {}
        try:
            with os.scandir(log_dir) as entries:
                for entry in entries:
                    try:
                        is_file = entry.is_file()
                    except OSError:
                        continue
                    if not is_file:
                        continue
                    parsed_filename = self.parse_filename(entry.name)
                    if parsed_filename is None:
                        continue
                    _filename_channel, character_id, filename_started = parsed_filename
                    try:
                        stat = entry.stat()
                    except OSError:
                        continue
                    path = Path(entry.path)
                    header = self._read_header(path, stat.st_size, stat.st_mtime_ns)
                    if header is None or self._is_native_session(header.channel_id):
                        continue

                    started_time = (
                        header.started_time
                        if header.started_time is not None
                        else filename_started
                    )
                    selection_id = make_selection_id(
                        header.channel_id,
                        character_id,
                    )
                    candidate = ChatChannelInfo(
                        selection_id=selection_id,
                        channel_id=header.channel_id,
                        channel_name=header.channel_name,
                        character_id=character_id,
                        listener_name=header.listener_name,
                        log_path=str(path.resolve(strict=False)),
                        log_mtime=stat.st_mtime,
                        started_time=started_time,
                    )
                    current = selected.get(selection_id)
                    if (
                        current is None
                        or self._candidate_order(candidate)
                        > self._candidate_order(current)
                    ):
                        selected[selection_id] = candidate
        except OSError as exc:
            logger.error(
                "Error scanning arbitrary EVE chat logs in %s: %s",
                log_dir,
                exc,
            )
            return {}

        for selection_id, info in tuple(selected.items()):
            active_start = active_starts.get(info.character_id)
            is_active = info.character_id in active_starts and (
                active_start is None or info.started_time >= active_start
            )
            selected[selection_id] = replace(info, is_active=is_active)

        return {
            selection_id: selected[selection_id]
            for selection_id in sorted(selected)
        }

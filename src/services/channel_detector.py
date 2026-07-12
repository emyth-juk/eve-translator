"""Discover selectable EVE chat logs outside the Fleet and Local sessions."""

from __future__ import annotations

import logging
import os
import time
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from urllib.parse import quote

from src.core.channel_info import ChatChannelInfo
from src.services.log_discovery import (
    EVELogEntry,
    EVELogHeader,
    EVE_LOG_FUTURE_MTIME_SKEW_SECONDS,
    UTF16HeaderCache,
    log_recency_key,
    parse_eve_log_filename,
    parse_eve_timestamp,
    scan_eve_log_entries,
)

logger = logging.getLogger(__name__)

_INVALID_CHANNEL_IDS = frozenset({"", "(none)", "none"})
CHANNEL_SESSION_START_GRACE_SECONDS = 2.0
CHANNEL_RECENT_ACTIVITY_SECONDS = 300.0


@dataclass(frozen=True)
class _LogCandidate:
    path: str
    character_id: str
    filename_started: float
    size: int
    mtime_ns: int
    mtime: float


@dataclass(frozen=True)
class _SelectedCandidate:
    selection_id: str
    header: EVELogHeader
    log: _LogCandidate
    started_time: float


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
        self._header_cache = UTF16HeaderCache()

    @staticmethod
    def parse_filename(filename: str) -> tuple[str, str, float] | None:
        """Return ``(filename_channel, character_id, UTC start time)``."""
        parsed = parse_eve_log_filename(filename)
        if parsed is None or parsed.character_id is None:
            return None
        return parsed.channel_name, parsed.character_id, parsed.started_time

    @staticmethod
    def _parse_started_time(value: object) -> float | None:
        return parse_eve_timestamp(value)

    @staticmethod
    def _usable_header(header: EVELogHeader | None) -> EVELogHeader | None:
        if (
            header is None
            or header.channel_id.casefold() in _INVALID_CHANNEL_IDS
            or not header.channel_name
            or not header.listener_name
        ):
            return None
        return header

    def _cached_header(
        self,
        path: str,
        size: int,
        mtime_ns: int,
    ) -> tuple[bool, EVELogHeader | None]:
        found, header = self._header_cache.peek(path, size, mtime_ns)
        return found, self._usable_header(header)

    def _read_header(
        self,
        path: str,
        size: int,
        mtime_ns: int,
    ) -> EVELogHeader | None:
        return self._usable_header(
            self._header_cache.read(path, size, mtime_ns)
        )

    @staticmethod
    def _is_native_session(channel_id: str) -> bool:
        normalized = channel_id.strip().casefold()
        return (
            normalized in {"fleet", "local"}
            or normalized.startswith("fleet_")
            or normalized.startswith("local_")
        )

    @staticmethod
    def _candidate_order(
        candidate: _SelectedCandidate,
    ) -> tuple[float, float, str, str]:
        return log_recency_key(
            candidate.started_time,
            candidate.log.mtime,
            candidate.log.path,
        )

    @classmethod
    def _active_character_starts(
        cls,
        active_characters: Iterable[str] | Mapping[str, object] | None,
    ) -> tuple[dict[str, float | None], set[str]]:
        if active_characters is None:
            return {}, set()
        if isinstance(active_characters, Mapping):
            result: dict[str, float | None] = {}
            supplied: set[str] = set()
            for character_id, session_start in active_characters.items():
                normalized = str(character_id or "").strip()
                if not normalized:
                    continue
                supplied.add(normalized)
                if session_start is False:
                    continue
                if session_start is None or session_start is True:
                    result[normalized] = None
                    continue
                parsed_start = cls._parse_started_time(session_start)
                if parsed_start is not None:
                    result[normalized] = parsed_start
            return result, supplied
        if isinstance(active_characters, str):
            active_characters = (active_characters,)
        result = {
            normalized: None
            for character_id in active_characters
            if (normalized := str(character_id or "").strip())
        }
        return result, set(result)

    def _select_candidate(
        self,
        selected: dict[str, _SelectedCandidate],
        log: _LogCandidate,
        header: EVELogHeader | None,
    ) -> None:
        if header is None or self._is_native_session(header.channel_id):
            return
        started_time = (
            header.started_time
            if header.started_time is not None
            else log.filename_started
        )
        selection_id = make_selection_id(header.channel_id, log.character_id)
        candidate = _SelectedCandidate(
            selection_id=selection_id,
            header=header,
            log=log,
            started_time=started_time,
        )
        current = selected.get(selection_id)
        if (
            current is None
            or self._candidate_order(candidate) > self._candidate_order(current)
        ):
            selected[selection_id] = candidate

    def scan_channels(
        self,
        log_dir: str | os.PathLike[str],
        active_characters: Iterable[str] | Mapping[str, object] | None = None,
        *,
        entries: Iterable[EVELogEntry] | None = None,
    ) -> dict[str, ChatChannelInfo]:
        """Return the newest rotation for every arbitrary channel source.

        ``active_characters`` may be an iterable of active character IDs, or a
        mapping from character ID to that character's current Local-session
        start. A same-session start or a write after that Local start is
        positive evidence; recent writes also bridge missing Local discovery.
        """
        active_starts, supplied_characters = self._active_character_starts(
            active_characters
        )
        selected: dict[str, _SelectedCandidate] = {}
        uncached: list[_LogCandidate] = []
        snapshot = entries if entries is not None else scan_eve_log_entries(log_dir)
        for entry in snapshot:
            parsed = entry.filename
            if parsed is None or parsed.character_id is None:
                continue
            character_id = parsed.character_id
            log = _LogCandidate(
                path=entry.path,
                character_id=character_id,
                filename_started=parsed.started_time,
                size=entry.size,
                mtime_ns=entry.mtime_ns,
                mtime=entry.mtime,
            )
            found, header = self._cached_header(
                log.path,
                log.size,
                log.mtime_ns,
            )
            if found:
                # Exact native basenames are skipped early only after their
                # authoritative cached ID agrees. A player channel named Fleet
                # or Local therefore remains a valid Any source.
                filename_native = parsed.channel_name.casefold() in {
                    "fleet", "local"
                }
                if (
                    filename_native
                    and header is not None
                    and self._is_native_session(header.channel_id)
                ):
                    continue
                # Warm scans can collapse rotations from authoritative cached
                # IDs before any file is opened again.
                self._select_candidate(selected, log, header)
            else:
                uncached.append(log)

        # Cold/new files still require their authoritative header. Never infer
        # identity from the display name embedded in the filename.
        for log in uncached:
            self._select_candidate(
                selected,
                log,
                self._read_header(log.path, log.size, log.mtime_ns),
            )

        now = time.time()
        result: dict[str, ChatChannelInfo] = {}
        for selection_id, candidate in selected.items():
            character_id = candidate.log.character_id
            active_start = active_starts.get(character_id)
            if character_id in active_starts:
                is_active = active_start is None or (
                    candidate.started_time + CHANNEL_SESSION_START_GRACE_SECONDS
                    >= active_start
                    or candidate.log.mtime >= active_start
                )
            elif character_id in supplied_characters:
                # Explicitly inactive or invalid activity metadata fails closed.
                is_active = False
            else:
                # A fresh write is positive evidence when Local/window activity
                # has not arrived yet. Stale mtime never negates session evidence.
                age = now - candidate.log.mtime
                is_active = (
                    -EVE_LOG_FUTURE_MTIME_SKEW_SECONDS
                    <= age
                    <= CHANNEL_RECENT_ACTIVITY_SECONDS
                )
            result[selection_id] = ChatChannelInfo(
                selection_id=selection_id,
                channel_id=candidate.header.channel_id,
                channel_name=candidate.header.channel_name,
                character_id=character_id,
                listener_name=candidate.header.listener_name,
                log_path=candidate.log.path,
                log_mtime=candidate.log.mtime,
                started_time=candidate.started_time,
                is_active=is_active,
            )
        return {
            selection_id: result[selection_id]
            for selection_id in sorted(result)
        }

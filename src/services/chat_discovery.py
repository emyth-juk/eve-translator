"""Background discovery of EVE Local, arbitrary, and Fleet chat logs."""

from __future__ import annotations

import logging
import os
import threading
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Any

from PySide6.QtCore import QObject, QThread, Signal, Slot

from src.core.channel_info import ChatChannelInfo
from src.core.character_info import CharacterInfo
from src.core.fleet_info import FleetInfo
from src.services.channel_detector import AnyChatDetector
from src.services.fleet_detector import FleetDetector
from src.services.local_detector import LocalChatDetector
from src.services.log_discovery import EVELogEntry, scan_eve_log_entries

logger = logging.getLogger(__name__)


def _read_only_copy(values: Mapping[str, Any]) -> Mapping[str, Any]:
    """Detach a detector-owned registry and expose it as read-only."""
    return MappingProxyType(dict(values))


@dataclass(frozen=True, slots=True)
class ChatDiscoveryError:
    """A serializable description of an unexpected discovery failure."""

    stage: str
    exception_type: str
    message: str


@dataclass(frozen=True, slots=True)
class ChatDiscoveryResult:
    """One complete, generation-tagged discovery attempt.

    The mappings are shallow read-only copies. Callers can therefore safely
    compare generations and publish a result without detector cache mutations
    changing the registry containers underneath them.
    """

    generation: int
    log_dir: str
    character_registry: Mapping[str, CharacterInfo] = field(default_factory=dict)
    channel_registry: Mapping[str, ChatChannelInfo] = field(default_factory=dict)
    fleet_registry: Mapping[str, FleetInfo] = field(default_factory=dict)
    error: ChatDiscoveryError | None = None
    cancelled: bool = False

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "character_registry",
            _read_only_copy(self.character_registry),
        )
        object.__setattr__(
            self,
            "channel_registry",
            _read_only_copy(self.channel_registry),
        )
        object.__setattr__(
            self,
            "fleet_registry",
            _read_only_copy(self.fleet_registry),
        )

    @property
    def request_id(self) -> int:
        """Alias used by callers that name their generation a request ID."""
        return self.generation

    @property
    def succeeded(self) -> bool:
        return self.error is None and not self.cancelled


class ChatDiscoveryWorker(QObject):
    """Run all chat discovery on one persistent background-thread object.

    Move this object to a ``QThread`` and connect a signal with the signature
    ``(int, str, int)`` to :meth:`scan`. Detector instances and their header
    caches then remain owned by, and confined to, that worker thread.
    """

    result_ready = Signal(object)
    scan_failed = Signal(object)

    def __init__(
        self,
        parent: QObject | None = None,
        *,
        local_detector: LocalChatDetector | None = None,
        channel_detector: AnyChatDetector | None = None,
        fleet_detector: FleetDetector | None = None,
        entry_scanner: Callable[[str], tuple[EVELogEntry, ...]] | None = None,
    ) -> None:
        super().__init__(parent)
        self.local_detector = (
            local_detector if local_detector is not None else LocalChatDetector()
        )
        self.channel_detector = (
            channel_detector if channel_detector is not None else AnyChatDetector()
        )
        self.fleet_detector = (
            fleet_detector if fleet_detector is not None else FleetDetector()
        )
        self.entry_scanner = (
            entry_scanner if entry_scanner is not None else scan_eve_log_entries
        )
        self._stop_requested = threading.Event()

    def request_stop(self) -> None:
        """Thread-safely prevent new work and cancel between detector passes."""
        self._stop_requested.set()

    @Slot()
    def stop(self) -> None:
        """Qt-slot form of :meth:`request_stop` for orderly thread shutdown."""
        self.request_stop()

    def _cancel_requested(self) -> bool:
        if self._stop_requested.is_set():
            return True
        thread = QThread.currentThread()
        return bool(thread and thread.isInterruptionRequested())

    @staticmethod
    def _active_character_starts(
        characters: Mapping[str, CharacterInfo],
        detector: AnyChatDetector,
    ) -> dict[str, float | bool | None]:
        """Map known characters to a Local start or explicit inactivity."""
        active: dict[str, float | bool | None] = {}
        for character_id, character in characters.items():
            if not character.is_active:
                active[str(character_id)] = False
                continue
            parsed = detector.parse_filename(
                os.path.basename(os.fspath(character.latest_log_path))
            )
            active[str(character_id)] = parsed[2] if parsed is not None else None
        return active

    def _cancelled_result(
        self,
        generation: int,
        log_dir: str,
        *,
        characters: Mapping[str, CharacterInfo] | None = None,
        channels: Mapping[str, ChatChannelInfo] | None = None,
        fleets: Mapping[str, FleetInfo] | None = None,
    ) -> ChatDiscoveryResult:
        return ChatDiscoveryResult(
            generation=generation,
            log_dir=log_dir,
            character_registry=characters or {},
            channel_registry=channels or {},
            fleet_registry=fleets or {},
            cancelled=True,
        )

    @Slot(int, str, int)
    def scan(
        self,
        generation: int,
        log_dir: str,
        fleet_inactive_threshold: int,
    ) -> None:
        """Discover Local first, then arbitrary chats, then active fleets.

        ``result_ready`` is emitted exactly once for every invocation. On an
        unexpected exception, the result contains the registries completed so
        far and a structured error; ``scan_failed`` emits that same object for
        optional logging/telemetry. A later request can still use this worker.
        """
        characters: dict[str, CharacterInfo] = {}
        channels: dict[str, ChatChannelInfo] = {}
        fleets: dict[str, FleetInfo] = {}
        entries = ()
        stage = "request"
        normalized_log_dir = log_dir if isinstance(log_dir, str) else ""

        try:
            normalized_log_dir = os.fspath(log_dir)
            threshold = max(0, int(fleet_inactive_threshold))
            if self._cancel_requested():
                self.result_ready.emit(
                    self._cancelled_result(generation, normalized_log_dir)
                )
                return

            stage = "snapshot"
            entries = self.entry_scanner(normalized_log_dir)
            if self._cancel_requested():
                self.result_ready.emit(
                    self._cancelled_result(generation, normalized_log_dir)
                )
                return

            stage = "local"
            characters = dict(
                self.local_detector.scan_active_characters(
                    normalized_log_dir,
                    entries=entries,
                )
            )
            if self._cancel_requested():
                self.result_ready.emit(
                    self._cancelled_result(
                        generation,
                        normalized_log_dir,
                        characters=characters,
                    )
                )
                return

            stage = "local-session-starts"
            active_starts = self._active_character_starts(
                characters,
                self.channel_detector,
            )

            stage = "any"
            channels = dict(
                self.channel_detector.scan_channels(
                    normalized_log_dir,
                    active_characters=active_starts,
                    entries=entries,
                )
            )
            if self._cancel_requested():
                self.result_ready.emit(
                    self._cancelled_result(
                        generation,
                        normalized_log_dir,
                        characters=characters,
                        channels=channels,
                    )
                )
                return

            stage = "fleet"
            fleets = dict(
                self.fleet_detector.scan_active_fleets(
                    normalized_log_dir,
                    active_threshold_seconds=threshold,
                    entries=entries,
                )
            )
            result = ChatDiscoveryResult(
                generation=generation,
                log_dir=normalized_log_dir,
                character_registry=characters,
                channel_registry=channels,
                fleet_registry=fleets,
            )
        except Exception as exc:
            error = ChatDiscoveryError(
                stage=stage,
                exception_type=type(exc).__name__,
                message=str(exc),
            )
            result = ChatDiscoveryResult(
                generation=generation,
                log_dir=normalized_log_dir,
                character_registry=characters,
                channel_registry=channels,
                fleet_registry=fleets,
                error=error,
            )
            logger.exception(
                "Chat discovery generation %s failed during %s",
                generation,
                stage,
            )
            self.scan_failed.emit(result)

        self.result_ready.emit(result)


__all__ = [
    "ChatDiscoveryError",
    "ChatDiscoveryResult",
    "ChatDiscoveryWorker",
]

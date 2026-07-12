import time
from collections.abc import Iterable
from typing import Dict, Optional

from src.core.fleet_info import FleetInfo
from src.services.log_discovery import (
    EVELogEntry,
    EVE_LOG_FUTURE_MTIME_SKEW_SECONDS,
    UTF16HeaderCache,
    scan_eve_log_entries,
)
import logging

logger = logging.getLogger(__name__)


class FleetDetector:
    """
    Detects active fleet chat log files.
    Supports parsing listener names and tracking multiple active fleets.
    """

    def __init__(self):
        self._header_cache = UTF16HeaderCache()

    def scan_active_fleets(
        self,
        log_dir: str,
        active_threshold_seconds: int = 1800,
        *,
        entries: Iterable[EVELogEntry] | None = None,
    ) -> Dict[str, FleetInfo]:
        """
        Scan all Fleet chat logs and return active ones.

        Args:
            log_dir: Directory containing EVE chat logs
            active_threshold_seconds: Consider logs active if modified within this time (default 30 minutes)

        Returns:
            Dict mapping fleet_id (log path) -> FleetInfo
            Only includes logs modified within active_threshold_seconds
        """
        result = {}
        current_time = time.time()
        snapshot = entries if entries is not None else scan_eve_log_entries(log_dir)
        for entry in snapshot:
            if not (
                entry.name.startswith("Fleet_")
                and entry.name.endswith(".txt")
            ):
                continue

            # Check if active (modified within threshold).
            age_seconds = current_time - entry.mtime
            if (
                age_seconds < -EVE_LOG_FUTURE_MTIME_SKEW_SECONDS
                or age_seconds > active_threshold_seconds
            ):
                continue

            header = self._header_cache.read(
                entry.path,
                entry.size,
                entry.mtime_ns,
            )
            listener_name = (
                header.listener_name if header is not None else ""
            )
            if not listener_name:
                logger.debug("Could not parse listener from %s", entry.name)
                continue

            parsed = entry.filename
            created_time = (
                parsed.started_time
                if parsed is not None
                and parsed.channel_name.casefold() == "fleet"
                else entry.ctime
            )

            # Use log path as unique ID.
            result[entry.path] = FleetInfo(
                fleet_id=entry.path,
                listener_name=listener_name,
                log_path=entry.path,
                log_mtime=entry.mtime,
                created_time=created_time,
                is_active=True,
            )

        return result

    @staticmethod
    def get_most_recent_fleet(
        fleets: Dict[str, FleetInfo],
    ) -> Optional[FleetInfo]:
        """
        Get the most recently created fleet from a dict of fleets.

        Args:
            fleets: Dict of fleet_id -> FleetInfo

        Returns:
            FleetInfo with the highest created_time, or None if empty
        """
        if not fleets:
            return None

        return max(fleets.values(), key=lambda f: f.created_time)

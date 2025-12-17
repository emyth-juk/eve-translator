import os
import re
import time
from pathlib import Path
from typing import Optional, Dict
from datetime import datetime
from src.core.fleet_info import FleetInfo
import logging

logger = logging.getLogger(__name__)


class FleetDetector:
    """
    Detects active fleet chat log files.
    Supports parsing listener names and tracking multiple active fleets.
    """

    def parse_listener_from_log(self, filepath: str) -> Optional[str]:
        """
        Parse listener (character) name from fleet log header.
        Reads first ~15 lines looking for "Listener: <CharacterName>".

        Args:
            filepath: Path to fleet log file

        Returns:
            Character name or None if not found
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

    def parse_timestamp_from_filename(self, filename: str) -> Optional[float]:
        """
        Extract creation timestamp from fleet log filename.

        Args:
            filename: e.g., "Fleet_20251216_082457.txt" or "Fleet_20251216_082457_1117005149.txt"

        Returns:
            Unix timestamp or None if parsing fails
        """
        # Pattern: Fleet_YYYYMMDD_HHMMSS[_CharID].txt
        pattern = r'Fleet_(\d{8})_(\d{6})(?:_\d+)?\.txt$'
        match = re.search(pattern, filename)
        if match:
            date_str = match.group(1)  # YYYYMMDD
            time_str = match.group(2)  # HHMMSS
            try:
                dt = datetime.strptime(f"{date_str}_{time_str}", "%Y%m%d_%H%M%S")
                return dt.timestamp()
            except ValueError:
                pass
        return None

    def scan_active_fleets(self, log_dir: str, active_threshold_seconds: int = 1800) -> Dict[str, FleetInfo]:
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

        try:
            with os.scandir(log_dir) as entries:
                for entry in entries:
                    if entry.is_file() and entry.name.startswith("Fleet_") and entry.name.endswith(".txt"):
                        try:
                            mtime = entry.stat().st_mtime

                            # Check if active (modified within threshold)
                            age_seconds = current_time - mtime
                            if age_seconds > active_threshold_seconds:
                                continue  # Skip inactive logs

                            # Parse listener name from log header
                            listener_name = self.parse_listener_from_log(entry.path)
                            if not listener_name:
                                logger.debug(f"Could not parse listener from {entry.name}")
                                continue

                            # Parse creation time from filename
                            created_time = self.parse_timestamp_from_filename(entry.name)
                            if not created_time:
                                # Fallback to file ctime
                                created_time = entry.stat().st_ctime

                            # Use log path as unique ID
                            fleet_id = entry.path

                            result[fleet_id] = FleetInfo(
                                fleet_id=fleet_id,
                                listener_name=listener_name,
                                log_path=entry.path,
                                log_mtime=mtime,
                                created_time=created_time,
                                is_active=True
                            )

                        except OSError as e:
                            logger.debug(f"Error processing {entry.name}: {e}")
                            continue
        except OSError as e:
            logger.error(f"Error scanning fleet logs: {e}")
            return {}

        return result

    def get_most_recent_fleet(self, fleets: Dict[str, FleetInfo]) -> Optional[FleetInfo]:
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

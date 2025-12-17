from dataclasses import dataclass
from typing import Optional

@dataclass
class FleetInfo:
    """Represents an active fleet chat detected from Fleet logs."""
    fleet_id: str                  # Unique identifier (e.g., log filename)
    listener_name: str             # Character name listening to fleet (from header)
    log_path: str                  # Full path to fleet log file
    log_mtime: float               # Modification time
    created_time: float            # Creation time from filename timestamp
    is_active: bool = True         # False if log hasn't been updated in >30min

    def __str__(self):
        return f"Fleet - {self.listener_name}"

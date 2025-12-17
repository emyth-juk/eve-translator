from dataclasses import dataclass
from typing import Optional

@dataclass
class CharacterInfo:
    """Represents an EVE character detected from Local chat logs."""
    character_id: str              # e.g., "1117005149"
    character_name: str            # e.g., "Emyth Juk"
    latest_log_path: str           # Full path to most recent log
    log_mtime: float               # Modification time
    system_name: Optional[str]     # Current system (if extractable)
    is_active: bool = True         # False if log hasn't been updated in >5min

    def __str__(self):
        system = f" ({self.system_name})" if self.system_name else ""
        return f"{self.character_name}{system}"

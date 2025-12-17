import re
from dataclasses import dataclass
from datetime import datetime
from typing import Optional, Dict

@dataclass
class ChatMessage:
    """Represents a parsed fleet chat message."""
    timestamp: datetime           # Parsed datetime
    timestamp_str: str            # Original timestamp string
    sender: str                   # Character name
    message: str                  # Raw message content
    line_number: int              # Line number in source file
    is_system: bool               # True if sender is "EVE System"

    # Optional fields (populated by tokenizer)
    message_cleaned: Optional[str] = None         # Message with EVE links tokenized
    eve_link_tokens: Optional[Dict[str, str]] = None  # Token map

class LineParser:
    """
    Parses individual lines from EVE chat logs.
    """
    # Regex to extract parts:
    # Group 1: Timestamp (YYYY.MM.DD HH:MM:SS)
    # Group 2: Sender name
    # Group 3: Message content
    # Note: Use non-greedy match for sender to handle potential odd characters, 
    # though usually sender doesn't contain '>'
    MESSAGE_PATTERN = re.compile(
        r'^[\ufeff\s]*\[\s*(\d{4}\.\d{2}\.\d{2}\s+\d{2}:\d{2}:\d{2})\s*\]\s+([^>]+?)\s+>\s+(.*)$'
    )

    def parse(self, line: str, line_number: int) -> Optional[ChatMessage]:
        """Parse a single line. Returns None if not a valid message line."""
        if not line:
            return None

        match = self.MESSAGE_PATTERN.match(line)
        if not match:
            return None

        ts_str, sender, message = match.groups()
        
        # Parse timestamp
        try:
            timestamp = datetime.strptime(ts_str, '%Y.%m.%d %H:%M:%S')
        except ValueError:
            # Should not happen if regex matches, but safety first
            return None

        sender = sender.strip()
        is_system = (sender == 'EVE System')

        return ChatMessage(
            timestamp=timestamp,
            timestamp_str=ts_str,
            sender=sender,
            message=message,
            line_number=line_number,
            is_system=is_system
        )

    def is_header_line(self, line: str) -> bool:
        """Returns True if line is likely part of the header section."""
        # Separator line
        if re.match(r'^\s*-{20,}\s*$', line):
            return True
        # Metadata lines
        if re.search(r'(Channel ID|Channel Name|Listener|Session started):', line):
            return True
        # Blank or mostly whitespace
        if not line.strip():
            return True
        return False

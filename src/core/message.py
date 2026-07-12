from dataclasses import dataclass
from enum import Enum

from src.core.combat import CommandMatch


class TranslationState(str, Enum):
    """Lifecycle state for a chat line shown in an overlay."""

    ORIGINAL = "original"
    PENDING = "pending"
    TRANSLATED = "translated"
    FAILED = "failed"


@dataclass(frozen=True)
class ProcessedMessage:
    """A routed chat line whose translation state can be updated in place."""

    message_id: str
    session_id: str
    text: str
    sender: str
    timestamp: str
    source_id: str = ""
    original_text: str = ""
    state: TranslationState = TranslationState.ORIGINAL
    provider: str = ""
    detected_language: str = "unknown"
    error: str = ""
    translation_relevant: bool = False
    speaker_role: str = ""
    commands: tuple[CommandMatch, ...] = ()
    revision: int = 0
    lookup_text: str = ""

"""Metadata for one selectable non-Fleet, non-Local EVE chat source."""

from dataclasses import dataclass


@dataclass(frozen=True)
class ChatChannelInfo:
    """The newest log rotation for one channel and one listening character."""

    selection_id: str
    channel_id: str
    channel_name: str
    character_id: str
    listener_name: str
    log_path: str
    log_mtime: float
    started_time: float
    is_active: bool = False

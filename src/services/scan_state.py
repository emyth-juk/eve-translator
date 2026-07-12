from dataclasses import dataclass, field
from typing import Dict, Optional

from src.core.character_info import CharacterInfo
from src.core.channel_info import ChatChannelInfo
from src.core.fleet_info import FleetInfo


@dataclass
class ChatScanState:
    character_registry: Dict[str, CharacterInfo] = field(default_factory=dict)
    fleet_registry: Dict[str, FleetInfo] = field(default_factory=dict)
    channel_registry: Dict[str, ChatChannelInfo] = field(default_factory=dict)
    selected_character_id: Optional[str] = None
    selected_fleet_id: Optional[str] = None
    selected_channel_id: Optional[str] = None

    @classmethod
    def from_config(cls, config: dict) -> "ChatScanState":
        sessions = config.get('sessions', {})
        return cls(
            selected_character_id=sessions.get('local', {}).get('character_id'),
            selected_fleet_id=sessions.get('fleet', {}).get('fleet_id'),
            selected_channel_id=(
                sessions.get('any', {}).get('channel_selection') or None
            ),
        )

    def select_character(self, character_id: str, config: dict) -> None:
        character = self.character_registry[character_id]
        self.selected_character_id = character_id
        local_config = config['sessions']['local']
        local_config['character_id'] = character.character_id
        local_config['character_name'] = character.character_name
        local_config['system_name'] = character.system_name

    def select_fleet(self, fleet_id: str, config: dict) -> None:
        self.selected_fleet_id = fleet_id
        config['sessions']['fleet']['fleet_id'] = fleet_id

    def select_channel(self, selection_id: str, config: dict) -> None:
        channel = self.channel_registry[selection_id]
        self.selected_channel_id = selection_id
        any_config = config['sessions']['any']
        any_config['channel_selection'] = channel.selection_id
        any_config['channel_id'] = channel.channel_id
        any_config['channel_name'] = channel.channel_name
        any_config['character_id'] = channel.character_id
        any_config['listener_name'] = channel.listener_name

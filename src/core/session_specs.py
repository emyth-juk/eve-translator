"""Single source of truth for built-in chat-session presentation and defaults."""

from dataclasses import dataclass
from types import MappingProxyType


@dataclass(frozen=True, slots=True)
class SessionSpec:
    session_id: str
    label: str
    status_label: str
    settings_label: str
    title_prefix: str
    default_enabled: bool
    default_x: int
    default_y: int
    default_width: int
    default_height: int
    background_color: str
    position_offset: int
    log_finder: str
    source_identity_keys: frozenset[str] = frozenset()


SESSION_SPECS = (
    SessionSpec(
        session_id="fleet",
        label="Fleet Chat",
        status_label="Fleet",
        settings_label="Fleet Overlay",
        title_prefix="[FLEET]",
        default_enabled=True,
        default_x=100,
        default_y=100,
        default_width=300,
        default_height=200,
        background_color="#33001a",
        position_offset=0,
        log_finder="_find_latest_fleet_log",
        source_identity_keys=frozenset({"fleet_id"}),
    ),
    SessionSpec(
        session_id="local",
        label="Local Chat",
        status_label="Local",
        settings_label="Local Overlay",
        title_prefix="[LOCAL]",
        default_enabled=False,
        default_x=410,
        default_y=100,
        default_width=300,
        default_height=200,
        background_color="#001a33",
        position_offset=50,
        log_finder="_find_latest_local_log",
        source_identity_keys=frozenset({
            "character_id",
            "character_name",
            "system_name",
        }),
    ),
    SessionSpec(
        session_id="any",
        label="Any Chat",
        status_label="Any Chat",
        settings_label="Any Chat Overlay",
        title_prefix="[ANY]",
        default_enabled=False,
        default_x=720,
        default_y=100,
        default_width=300,
        default_height=200,
        background_color="#21173d",
        position_offset=100,
        log_finder="_find_latest_any_log",
        source_identity_keys=frozenset({
            "channel_selection",
            "channel_id",
            "channel_name",
            "character_id",
            "listener_name",
        }),
    ),
)

SESSION_SPECS_BY_ID = MappingProxyType({
    spec.session_id: spec for spec in SESSION_SPECS
})
SESSION_IDS = tuple(spec.session_id for spec in SESSION_SPECS)


def get_session_spec(session_id: str) -> SessionSpec | None:
    return SESSION_SPECS_BY_ID.get(session_id)

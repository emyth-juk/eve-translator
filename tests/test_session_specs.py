from src.app_config import get_default_config
from src.core.session_specs import (
    SESSION_IDS,
    SESSION_SPECS,
    SESSION_SPECS_BY_ID,
    get_session_spec,
)


def test_session_descriptors_are_unique_and_indexed_in_display_order():
    assert len(SESSION_IDS) == len(set(SESSION_IDS))
    assert SESSION_IDS == tuple(spec.session_id for spec in SESSION_SPECS)
    assert tuple(SESSION_SPECS_BY_ID) == SESSION_IDS
    assert all(get_session_spec(spec.session_id) is spec for spec in SESSION_SPECS)
    assert get_session_spec('unknown') is None


def test_default_session_configuration_is_derived_from_descriptors():
    sessions = get_default_config()['sessions']

    assert tuple(sessions) == SESSION_IDS
    for spec in SESSION_SPECS:
        configured = sessions[spec.session_id]
        assert configured['enabled'] is spec.default_enabled
        assert configured['x'] == spec.default_x
        assert configured['y'] == spec.default_y
        assert configured['w'] == spec.default_width
        assert configured['h'] == spec.default_height
        assert configured['background_color'] == spec.background_color
        assert configured['title_prefix'] == spec.title_prefix


def test_only_fleet_inherits_legacy_single_overlay_geometry():
    sessions = get_default_config(
        {'x': 17, 'y': 23, 'w': 419, 'h': 211}
    )['sessions']

    assert (sessions['fleet']['x'], sessions['fleet']['y']) == (17, 23)
    assert (sessions['fleet']['w'], sessions['fleet']['h']) == (419, 211)
    assert (sessions['local']['x'], sessions['local']['y']) == (410, 100)
    assert (sessions['any']['x'], sessions['any']['y']) == (720, 100)

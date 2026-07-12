import os
from datetime import datetime, timezone
from pathlib import Path

import pytest

from src.services.channel_detector import AnyChatDetector, make_selection_id


def _timestamp(value: str) -> float:
    return datetime.strptime(value, "%Y.%m.%d %H:%M:%S").replace(
        tzinfo=timezone.utc
    ).timestamp()


def _write_log(
    directory: Path,
    *,
    filename_channel: str = "Intel_Channel",
    channel_id: str = "player_abc123",
    channel_name: str = "Intel Channel",
    character_id: str = "123",
    listener: str = "Pilot One",
    started: str = "2026.07.12 11:22:09",
    mtime: float | None = None,
) -> Path:
    compact_started = datetime.strptime(started, "%Y.%m.%d %H:%M:%S").strftime(
        "%Y%m%d_%H%M%S"
    )
    path = directory / f"{filename_channel}_{compact_started}_{character_id}.txt"
    path.write_text(
        "\r\n".join([
            "",
            f"Channel ID:      {channel_id}",
            f"Channel Name:    {channel_name}",
            f"Listener:        {listener}",
            f"Session started: {started}",
            "",
        ]),
        encoding="utf-16",
    )
    if mtime is not None:
        os.utime(path, (mtime, mtime))
    return path


def test_filename_parser_is_greedy_from_the_right_and_uses_utc():
    parsed = AnyChatDetector.parse_filename(
        "Ops_alpha's Room_20260712_112209_987654321.txt"
    )

    assert parsed == (
        "Ops_alpha's Room",
        "987654321",
        _timestamp("2026.07.12 11:22:09"),
    )
    assert AnyChatDetector.parse_filename("not-an-eve-log.txt") is None


def test_selection_id_is_unambiguous_and_requires_both_parts():
    assert make_selection_id("player:a::b", "123") != make_selection_id(
        "player:a", "b::123"
    )
    with pytest.raises(ValueError):
        make_selection_id("", "123")


def test_scan_uses_authoritative_header_metadata(tmp_path):
    path = _write_log(
        tmp_path,
        filename_channel="Misleading_File_Name",
        channel_id="player_deadbeef",
        channel_name="Header Name",
    )

    channels = AnyChatDetector().scan_channels(tmp_path)

    info = channels[make_selection_id("player_deadbeef", "123")]
    assert info.channel_name == "Header Name"
    assert info.listener_name == "Pilot One"
    assert info.log_path == str(path.resolve())
    assert info.started_time == _timestamp("2026.07.12 11:22:09")
    assert info.is_active is False


def test_native_fleet_and_local_ids_are_excluded_but_player_names_are_retained(
    tmp_path,
):
    _write_log(
        tmp_path,
        filename_channel="Fleet",
        channel_id="fleet_999",
        channel_name="Fleet",
        character_id="1",
    )
    _write_log(
        tmp_path,
        filename_channel="Local",
        channel_id="local",
        channel_name="Local",
        character_id="2",
    )
    _write_log(
        tmp_path,
        filename_channel="Fleet",
        channel_id="player_custom_fleet",
        channel_name="Fleet",
        character_id="3",
    )
    _write_log(
        tmp_path,
        filename_channel="Local",
        channel_id="player_custom_local",
        channel_name="Local",
        character_id="4",
    )

    channels = AnyChatDetector().scan_channels(tmp_path)

    assert set(channels) == {
        make_selection_id("player_custom_fleet", "3"),
        make_selection_id("player_custom_local", "4"),
    }


def test_rotations_collapse_only_within_exact_channel_and_character(tmp_path):
    older = _write_log(
        tmp_path,
        channel_id="player_shared",
        character_id="10",
        started="2026.07.12 10:00:00",
        mtime=5000,
    )
    newer = _write_log(
        tmp_path,
        channel_id="player_shared",
        character_id="10",
        started="2026.07.12 11:00:00",
        mtime=4000,
    )
    other_character = _write_log(
        tmp_path,
        channel_id="player_shared",
        character_id="20",
        started="2026.07.12 11:00:00",
    )

    channels = AnyChatDetector().scan_channels(tmp_path)

    assert len(channels) == 2
    assert channels[make_selection_id("player_shared", "10")].log_path == str(
        newer.resolve()
    )
    assert channels[make_selection_id("player_shared", "20")].log_path == str(
        other_character.resolve()
    )
    assert str(older.resolve()) not in {item.log_path for item in channels.values()}


def test_same_display_name_with_different_ids_remains_separate(tmp_path):
    _write_log(
        tmp_path,
        filename_channel="First_File",
        channel_id="player_first",
        channel_name="Shared Name",
        character_id="1",
    )
    _write_log(
        tmp_path,
        filename_channel="Second_File",
        channel_id="player_second",
        channel_name="Shared Name",
        character_id="1",
    )

    channels = AnyChatDetector().scan_channels(tmp_path)

    assert len(channels) == 2
    assert {item.channel_id for item in channels.values()} == {
        "player_first",
        "player_second",
    }


def test_rotation_ties_and_result_order_are_deterministic(tmp_path):
    first = _write_log(
        tmp_path,
        filename_channel="Alpha",
        channel_id="player_tie",
        channel_name="Same Header",
        character_id="8",
        mtime=1000,
    )
    second = _write_log(
        tmp_path,
        filename_channel="Zulu",
        channel_id="player_tie",
        channel_name="Same Header",
        character_id="8",
        mtime=1000,
    )
    _write_log(
        tmp_path,
        channel_id="player_a",
        character_id="2",
    )

    channels = AnyChatDetector().scan_channels(tmp_path)

    assert first != second
    assert channels[make_selection_id("player_tie", "8")].log_path == str(
        max(first.resolve(), second.resolve(), key=lambda path: (str(path).casefold(), str(path)))
    )
    assert list(channels) == sorted(channels)


def test_activity_uses_character_ids_and_optional_current_local_start(tmp_path):
    _write_log(
        tmp_path,
        channel_id="player_old",
        character_id="1",
        started="2026.07.12 10:00:00",
    )
    _write_log(
        tmp_path,
        channel_id="player_current",
        character_id="2",
        started="2026.07.12 11:30:00",
    )
    _write_log(
        tmp_path,
        channel_id="player_offline",
        character_id="3",
        started="2026.07.12 12:00:00",
    )
    detector = AnyChatDetector()

    by_id_only = detector.scan_channels(tmp_path, active_characters={"1", "2"})
    assert by_id_only[make_selection_id("player_old", "1")].is_active is True
    assert by_id_only[make_selection_id("player_current", "2")].is_active is True

    with_session_start = detector.scan_channels(
        tmp_path,
        active_characters={
            "1": "2026.07.12 11:00:00",
            "2": _timestamp("2026.07.12 11:00:00"),
        },
    )
    assert with_session_start[make_selection_id("player_old", "1")].is_active is False
    assert with_session_start[
        make_selection_id("player_current", "2")
    ].is_active is True
    assert with_session_start[
        make_selection_id("player_offline", "3")
    ].is_active is False

    invalid_session_start = detector.scan_channels(
        tmp_path,
        active_characters={"2": "not a session timestamp"},
    )
    assert invalid_session_start[
        make_selection_id("player_current", "2")
    ].is_active is False


def test_incomplete_header_is_retried_after_file_changes(tmp_path):
    path = tmp_path / "Intel_20260712_112209_123.txt"
    path.write_text("Channel ID: player_partial\r\n", encoding="utf-16")
    detector = AnyChatDetector()

    assert detector.scan_channels(tmp_path) == {}

    path.write_text(
        "\r\n".join([
            "Channel ID: player_partial",
            "Channel Name: Intel",
            "Listener: Pilot",
            "Session started: 2026.07.12 11:22:09",
        ]),
        encoding="utf-16",
    )

    assert set(detector.scan_channels(tmp_path)) == {
        make_selection_id("player_partial", "123")
    }


def test_logs_without_an_authoritative_channel_id_are_skipped(tmp_path):
    _write_log(tmp_path, channel_id="(None)")

    assert AnyChatDetector().scan_channels(tmp_path) == {}

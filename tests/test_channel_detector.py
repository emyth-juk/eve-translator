import os
import stat
from dataclasses import FrozenInstanceError
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from src.services.channel_detector import AnyChatDetector, make_selection_id
from src.services.fleet_detector import FleetDetector
from src.services.local_detector import LocalChatDetector
from src.services.log_discovery import (
    log_recency_key,
    normalized_log_path,
    parse_eve_log_filename,
    scan_eve_log_entries,
)


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


def test_shared_path_identity_and_recency_helpers_are_deterministic(tmp_path):
    first = tmp_path / "Alpha.txt"
    second = tmp_path / "Zulu.txt"

    assert normalized_log_path(first) == os.path.normcase(
        os.path.abspath(first)
    )
    assert log_recency_key(2, 1, first) > log_recency_key(1, 99, second)
    assert log_recency_key(2, 2, second) > log_recency_key(2, 1, second)
    assert log_recency_key(2, 2, second) > log_recency_key(2, 2, first)


def test_shared_snapshot_stats_and_parses_each_regular_file_once(tmp_path):
    class FakeEntry:
        def __init__(self, name, mode):
            self.name = name
            self.path = str(tmp_path / name)
            self._mode = mode
            self.stat_calls = 0

        def stat(self):
            self.stat_calls += 1
            return SimpleNamespace(
                st_mode=self._mode,
                st_size=10,
                st_mtime=20.0,
                st_mtime_ns=20_000_000_000,
                st_ctime=5.0,
            )

    class FakeScandir:
        def __init__(self, entries):
            self.entries = entries

        def __enter__(self):
            return iter(self.entries)

        def __exit__(self, *_args):
            return False

    regular = FakeEntry("Intel_20260712_112209_123.txt", stat.S_IFREG)
    directory = FakeEntry("not-a-log-directory", stat.S_IFDIR)

    with (
        patch(
            "src.services.log_discovery.os.scandir",
            return_value=FakeScandir((regular, directory)),
        ) as scandir,
        patch(
            "src.services.log_discovery.parse_eve_log_filename",
            wraps=parse_eve_log_filename,
        ) as parse_filename,
    ):
        snapshot = scan_eve_log_entries(tmp_path)

    scandir.assert_called_once_with(tmp_path)
    assert regular.stat_calls == 1
    assert directory.stat_calls == 1
    assert parse_filename.call_count == 1
    assert isinstance(snapshot, tuple)
    with pytest.raises(FrozenInstanceError):
        snapshot[0].size = 11


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
    # A recent write is positive evidence until Local/window discovery catches up.
    assert info.is_active is True


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
        mtime=_timestamp("2026.07.12 10:30:00"),
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
        mtime=_timestamp("2026.07.12 12:00:00"),
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


def test_explicit_false_activity_fails_closed_even_for_a_recent_write(tmp_path):
    now = _timestamp("2026.07.12 12:00:00")
    _write_log(
        tmp_path,
        channel_id="player_explicitly_inactive",
        character_id="1",
        mtime=now,
    )

    with patch("src.services.channel_detector.time.time", return_value=now):
        channels = AnyChatDetector().scan_channels(
            tmp_path,
            active_characters={"1": False},
        )

    assert channels[
        make_selection_id("player_explicitly_inactive", "1")
    ].is_active is False


def test_activity_accepts_equality_and_two_second_start_grace(tmp_path):
    _write_log(
        tmp_path,
        filename_channel="Equal",
        channel_id="player_equal",
        character_id="1",
        started="2026.07.12 11:00:00",
        mtime=_timestamp("2026.07.12 10:00:00"),
    )
    _write_log(
        tmp_path,
        filename_channel="Grace",
        channel_id="player_grace",
        character_id="1",
        started="2026.07.12 10:59:58",
        mtime=_timestamp("2026.07.12 10:00:00"),
    )
    _write_log(
        tmp_path,
        filename_channel="Too_Old",
        channel_id="player_too_old",
        character_id="1",
        started="2026.07.12 10:59:57",
        mtime=_timestamp("2026.07.12 10:59:59"),
    )

    channels = AnyChatDetector().scan_channels(
        tmp_path,
        active_characters={"1": "2026.07.12 11:00:00"},
    )

    assert channels[make_selection_id("player_equal", "1")].is_active is True
    assert channels[make_selection_id("player_grace", "1")].is_active is True
    assert channels[make_selection_id("player_too_old", "1")].is_active is False


def test_mtime_after_local_start_is_positive_but_never_a_negative_gate(tmp_path):
    _write_log(
        tmp_path,
        filename_channel="Written_Current",
        channel_id="player_written_current",
        character_id="1",
        started="2026.07.12 10:00:00",
        mtime=_timestamp("2026.07.12 11:00:01"),
    )
    _write_log(
        tmp_path,
        filename_channel="Quiet_Current",
        channel_id="player_quiet_current",
        character_id="2",
        started="2026.07.12 11:00:00",
        mtime=_timestamp("2026.07.12 10:00:00"),
    )

    channels = AnyChatDetector().scan_channels(
        tmp_path,
        active_characters={
            "1": "2026.07.12 11:00:00",
            "2": "2026.07.12 11:00:00",
        },
    )

    assert channels[
        make_selection_id("player_written_current", "1")
    ].is_active is True
    assert channels[
        make_selection_id("player_quiet_current", "2")
    ].is_active is True


def test_recent_own_write_is_positive_when_character_activity_is_missing(tmp_path):
    now = _timestamp("2026.07.12 12:00:00")
    _write_log(
        tmp_path,
        filename_channel="Recent",
        channel_id="player_recent",
        character_id="1",
        started="2026.07.12 10:00:00",
        mtime=now - 299,
    )
    _write_log(
        tmp_path,
        filename_channel="Stale",
        channel_id="player_stale",
        character_id="2",
        started="2026.07.12 10:00:00",
        mtime=now - 301,
    )

    with patch("src.services.channel_detector.time.time", return_value=now):
        channels = AnyChatDetector().scan_channels(tmp_path)

    assert channels[make_selection_id("player_recent", "1")].is_active is True
    assert channels[make_selection_id("player_stale", "2")].is_active is False


def test_recent_write_fallback_allows_small_clock_skew_but_not_future_mtime(
    tmp_path,
):
    now = _timestamp("2026.07.12 12:00:00")
    _write_log(
        tmp_path,
        filename_channel="Small_Skew",
        channel_id="player_small_skew",
        character_id="1",
        mtime=now + 1.5,
    )
    _write_log(
        tmp_path,
        filename_channel="Far_Future",
        channel_id="player_far_future",
        character_id="2",
        mtime=now + 3600,
    )

    with patch("src.services.channel_detector.time.time", return_value=now):
        channels = AnyChatDetector().scan_channels(tmp_path)

    assert channels[make_selection_id("player_small_skew", "1")].is_active is True
    assert channels[make_selection_id("player_far_future", "2")].is_active is False


def test_one_snapshot_is_reused_by_all_detectors_without_rescan_or_restat(
    tmp_path,
):
    fleet_path = _write_log(
        tmp_path,
        filename_channel="Fleet",
        channel_id="fleet_42",
        channel_name="Fleet",
        character_id="42",
        listener="Fleet Pilot",
    )
    local_path = _write_log(
        tmp_path,
        filename_channel="Local",
        channel_id="local",
        channel_name="Local",
        character_id="42",
        listener="Local Pilot",
    )
    any_path = _write_log(
        tmp_path,
        filename_channel="Alliance_Intel",
        channel_id="player_alliance_intel",
        channel_name="Alliance Intel",
        character_id="42",
        listener="Intel Pilot",
    )
    expected_paths = {
        "fleet": str(fleet_path.resolve()),
        "local": str(local_path.resolve()),
        "any": str(any_path.resolve()),
    }
    snapshot = scan_eve_log_entries(tmp_path)
    snapshot_paths = {
        os.path.normcase(os.path.abspath(entry.path)) for entry in snapshot
    }
    real_stat = os.stat

    def reject_snapshot_stat(path, *args, **kwargs):
        if isinstance(path, (str, os.PathLike)):
            normalized = os.path.normcase(os.path.abspath(path))
            if normalized in snapshot_paths:
                raise AssertionError("detector restated snapshot entry")
        return real_stat(path, *args, **kwargs)

    with (
        patch(
            "src.services.channel_detector.scan_eve_log_entries",
            side_effect=AssertionError("Any detector rescanned"),
        ),
        patch(
            "src.services.fleet_detector.scan_eve_log_entries",
            side_effect=AssertionError("Fleet detector rescanned"),
        ),
        patch(
            "src.services.local_detector.scan_eve_log_entries",
            side_effect=AssertionError("Local detector rescanned"),
        ),
        patch(
            "src.services.log_discovery.os.stat",
            side_effect=reject_snapshot_stat,
        ),
        patch.object(
            LocalChatDetector,
            "is_character_window_open",
            return_value=False,
        ),
    ):
        fleets = FleetDetector().scan_active_fleets(
            tmp_path,
            entries=snapshot,
        )
        characters = LocalChatDetector().scan_active_characters(
            tmp_path,
            entries=snapshot,
        )
        channels = AnyChatDetector().scan_channels(
            tmp_path,
            active_characters={"42"},
            entries=snapshot,
        )

    assert set(fleets) == {expected_paths["fleet"]}
    assert characters["42"].latest_log_path == expected_paths["local"]
    assert set(channels) == {
        make_selection_id("player_alliance_intel", "42")
    }
    assert channels[
        make_selection_id("player_alliance_intel", "42")
    ].log_path == expected_paths["any"]


def test_utc_activity_inputs_are_representation_independent(tmp_path):
    _write_log(
        tmp_path,
        channel_id="player_utc",
        character_id="1",
        started="2026.07.12 11:00:00",
        mtime=_timestamp("2026.07.12 10:00:00"),
    )
    detector = AnyChatDetector()
    expected_id = make_selection_id("player_utc", "1")

    text = detector.scan_channels(
        tmp_path,
        active_characters={"1": "2026.07.12 11:00:00"},
    )
    numeric = detector.scan_channels(
        tmp_path,
        active_characters={"1": _timestamp("2026.07.12 11:00:00")},
    )
    aware = detector.scan_channels(
        tmp_path,
        active_characters={
            "1": datetime.fromisoformat("2026-07-12T13:00:00+02:00")
        },
    )

    assert text[expected_id].is_active is True
    assert numeric[expected_id].is_active is True
    assert aware[expected_id].is_active is True


def test_warm_scan_reuses_filename_and_header_metadata_without_reopening(tmp_path):
    for number in range(12):
        _write_log(
            tmp_path,
            filename_channel=f"Intel_{number % 3}",
            channel_id=f"player_{number % 3}",
            character_id="1",
            started=f"2026.07.12 11:{number:02d}:00",
        )
    detector = AnyChatDetector()
    first = detector.scan_channels(tmp_path)

    with patch("builtins.open", side_effect=AssertionError("warm header reopen")):
        second = detector.scan_channels(tmp_path)

    assert second == first
    assert len(second) == 3


def test_warm_scan_reads_only_new_rotation_and_preserves_new_authoritative_id(
    tmp_path,
):
    _write_log(
        tmp_path,
        filename_channel="Same_Display",
        channel_id="player_original",
        channel_name="Same Display",
        character_id="1",
        started="2026.07.12 10:00:00",
    )
    detector = AnyChatDetector()
    detector.scan_channels(tmp_path)
    _write_log(
        tmp_path,
        filename_channel="Same_Display",
        channel_id="player_recreated",
        channel_name="Same Display",
        character_id="1",
        started="2026.07.12 11:00:00",
    )

    with patch("builtins.open", wraps=open) as open_file:
        channels = detector.scan_channels(tmp_path)

    assert open_file.call_count == 1
    assert set(channels) == {
        make_selection_id("player_original", "1"),
        make_selection_id("player_recreated", "1"),
    }


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

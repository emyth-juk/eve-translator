"""Discovery stops promptly, caches follow the folder, Fleet waits for discovery."""

import os
import sys
import time
from datetime import datetime, timezone
from unittest.mock import MagicMock, patch

import pytest
from PySide6.QtCore import QObject
from PySide6.QtWidgets import QApplication

from src.core.fleet_info import FleetInfo
from src.services.chat_discovery import ChatDiscoveryWorker
from src.services.log_discovery import (
    DiscoveryCancelled,
    UTF16HeaderCache,
    parse_eve_log_filename,
    scan_eve_log_entries,
)


@pytest.fixture(scope="module", autouse=True)
def qt_app():
    app = QApplication.instance() or QApplication(sys.argv)
    yield app


def _write_log(directory, name, listener="Pilot", channel="Corp", channel_id="-1"):
    header = (
        "﻿\r\n"
        "        Channel ID:      {channel_id}\r\n"
        "        Channel Name:    {channel}\r\n"
        "        Listener:        {listener}\r\n"
        "        Session started: 2026.11.08 10:00:00\r\n"
    ).format(channel_id=channel_id, channel=channel, listener=listener)
    path = directory / name
    path.write_bytes(header.encode("utf-16-le"))
    return path


def test_filename_timestamp_is_parsed_directly_as_utc():
    parsed = parse_eve_log_filename("Local_20251216_120250_2114583377.txt")

    assert parsed.started_time == datetime(
        2025, 12, 16, 12, 2, 50, tzinfo=timezone.utc
    ).timestamp()
    assert parsed.character_id == "2114583377"
    assert parse_eve_log_filename("Local_20251399_120250_1.txt") is None


def test_snapshot_scan_honours_stop_requests(tmp_path):
    for index in range(5):
        _write_log(tmp_path, f"Corp_20261108_1000{index:02d}_{2110000000 + index}.txt")

    with pytest.raises(DiscoveryCancelled):
        scan_eve_log_entries(tmp_path, should_stop=lambda: True)


def test_worker_cancels_inside_a_long_header_pass(tmp_path):
    for index in range(300):
        _write_log(
            tmp_path,
            f"Corp_20261108_10{index // 60:02d}{index % 60:02d}_{2110000000 + index}.txt",
            channel_id=str(-1000 - index),
        )
    worker = ChatDiscoveryWorker()
    worker.local_detector.is_character_window_open = lambda _name: False
    calls = {"count": 0}

    def stop_after_snapshot():
        calls["count"] += 1
        return calls["count"] > 6

    worker._cancel_requested = stop_after_snapshot
    for detector in (worker.local_detector, worker.channel_detector, worker.fleet_detector):
        detector.should_stop = stop_after_snapshot
    results = []
    worker.result_ready.connect(results.append)

    worker.scan(1, str(tmp_path), 1800)

    assert len(results) == 1
    assert results[0].cancelled is True
    assert results[0].error is None


def test_worker_detectors_share_one_unbounded_header_cache():
    worker = ChatDiscoveryWorker()

    assert worker.local_detector._header_cache is worker.header_cache
    assert worker.channel_detector._header_cache is worker.header_cache
    assert worker.fleet_detector._header_cache is worker.header_cache
    assert worker.header_cache.max_size is None
    assert worker.local_detector.should_stop is not None


def test_header_cache_keeps_every_folder_entry_and_prunes_deleted_files(tmp_path):
    cache = UTF16HeaderCache()
    paths = [
        _write_log(tmp_path, f"Corp_20261108_1000{index:02d}_{2110000000 + index}.txt")
        for index in range(40)
    ]
    for path in paths:
        stat = path.stat()
        cache.read(path, stat.st_size, stat.st_mtime_ns)

    assert len(cache) == 40
    with patch("builtins.open", side_effect=AssertionError("re-read")):
        for path in paths:
            stat = path.stat()
            assert cache.read(path, stat.st_size, stat.st_mtime_ns).listener_name == "Pilot"

    cache.retain(paths[:10])
    assert len(cache) == 10


def _fleet_manager(tmp_path):
    with patch("src.main.TranslatorManager.__init__", lambda self: None):
        from src.main import TranslatorManager

        manager = TranslatorManager()
        QObject.__init__(manager)
    manager.config = {
        "shared": {"log_dir": str(tmp_path), "fleet_auto_switch": True},
        "sessions": {
            "fleet": {"enabled": True},
            "local": {"enabled": False},
            "any": {"enabled": False},
        },
    }
    manager.sessions = {"fleet": None, "local": None, "any": None}
    manager._save_config = MagicMock()
    manager._broadcast_fleet_list = MagicMock()
    manager._update_tray_menu = MagicMock()
    return manager


def test_fleet_waits_for_discovery_instead_of_globbing_the_folder(tmp_path):
    manager = _fleet_manager(tmp_path)
    _write_log(tmp_path, "Fleet_20261108_100000_2110000001.txt", channel="Fleet")

    with patch("os.scandir", side_effect=AssertionError("GUI-thread folder scan")):
        assert manager._find_latest_fleet_log() is None


def test_fleet_session_follows_the_selected_fleet_log(tmp_path):
    manager = _fleet_manager(tmp_path)
    selected = str(tmp_path / "Fleet_20261108_100000_2110000001.txt")
    other = str(tmp_path / "Fleet_20261108_110000_2110000002.txt")
    now = time.time()
    registry = {
        selected: FleetInfo(selected, "Pilot X", selected, now, now - 600, True),
        other: FleetInfo(other, "Pilot Y", other, now, now - 60, True),
    }
    session = MagicMock()
    session.log_path = other  # started from a different log at startup
    manager.sessions["fleet"] = session
    manager._get_scan_state().selected_fleet_id = selected
    manager.fleet_registry = dict(registry)

    manager._scan_fleets(new_registry=registry)

    session.switch_fleet_log.assert_called_once_with(selected, "Pilot X")
    assert os.path.basename(session.switch_fleet_log.call_args.args[0]).startswith(
        "Fleet_20261108_100000"
    )

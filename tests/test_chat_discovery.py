import threading
from dataclasses import FrozenInstanceError
from unittest.mock import MagicMock

import pytest
from PySide6.QtCore import QCoreApplication, QObject, QThread, Qt, Signal

from src.core.channel_info import ChatChannelInfo
from src.core.character_info import CharacterInfo
from src.core.fleet_info import FleetInfo
from src.services.channel_detector import AnyChatDetector
from src.services.chat_discovery import ChatDiscoveryResult, ChatDiscoveryWorker
from src.services.log_discovery import parse_eve_timestamp
from src.services.fleet_detector import FleetDetector
from src.services.local_detector import LocalChatDetector


def _character(
    character_id: str,
    path: str,
    *,
    active: bool = True,
) -> CharacterInfo:
    return CharacterInfo(
        character_id=character_id,
        character_name=f"Pilot {character_id}",
        latest_log_path=path,
        log_mtime=1_000.0,
        system_name="Jita",
        is_active=active,
    )


def _channel(selection_id: str) -> ChatChannelInfo:
    return ChatChannelInfo(
        selection_id=selection_id,
        channel_id="player_intel",
        channel_name="Intel",
        character_id="7",
        listener_name="Pilot 7",
        log_path="C:/logs/Intel_20260712_120000_7.txt",
        log_mtime=1_000.0,
        started_time=900.0,
        is_active=True,
    )


def _fleet(fleet_id: str) -> FleetInfo:
    return FleetInfo(
        fleet_id=fleet_id,
        listener_name="Pilot 7",
        log_path=fleet_id,
        log_mtime=1_000.0,
        created_time=900.0,
        is_active=True,
    )


def _detectors():
    return (
        MagicMock(spec=LocalChatDetector),
        MagicMock(spec=AnyChatDetector),
        MagicMock(spec=FleetDetector),
    )


def test_scan_orders_detectors_and_emits_one_immutable_snapshot():
    local, channel, fleet = _detectors()
    events = []
    characters = {
        "7": _character("7", "C:/logs/Local_20260712_120000_7.txt"),
        "8": _character(
            "8",
            "C:/logs/Local_20260712_120001_8.txt",
            active=False,
        ),
        "9": _character("9", "C:/logs/not-a-local-log.txt"),
    }
    channels = {"intel::7": _channel("intel::7")}
    fleets = {"C:/logs/Fleet.txt": _fleet("C:/logs/Fleet.txt")}

    def scan_local(log_dir, *, entries):
        assert entries == ()
        events.append(("local", log_dir))
        return characters

    def scan_channels(log_dir, *, active_characters, entries):
        assert entries == ()
        events.append(("any", log_dir, active_characters))
        return channels

    def scan_fleets(log_dir, *, active_threshold_seconds, entries):
        assert entries == ()
        events.append(("fleet", log_dir, active_threshold_seconds))
        return fleets

    local.scan_active_characters.side_effect = scan_local
    channel.scan_channels.side_effect = scan_channels
    fleet.scan_active_fleets.side_effect = scan_fleets
    entry_scanner = MagicMock(return_value=())
    worker = ChatDiscoveryWorker(
        local_detector=local,
        channel_detector=channel,
        fleet_detector=fleet,
        entry_scanner=entry_scanner,
    )
    results = []
    failures = []
    worker.result_ready.connect(results.append)
    worker.scan_failed.connect(failures.append)

    worker.scan(42, "C:/logs", 1_800)

    entry_scanner.assert_called_once_with("C:/logs")
    assert [event[0] for event in events] == [
        "local",
        "any",
        "fleet",
    ]
    assert events[1] == (
        "any",
        "C:/logs",
        {
            "7": parse_eve_timestamp("20260712120000"),
            "8": False,
            "9": None,
        },
    )
    assert events[2] == ("fleet", "C:/logs", 1_800)
    assert len(results) == 1
    assert failures == []
    result = results[0]
    assert isinstance(result, ChatDiscoveryResult)
    assert result.generation == 42
    assert result.request_id == 42
    assert result.succeeded is True
    assert dict(result.character_registry) == characters
    assert dict(result.channel_registry) == channels
    assert dict(result.fleet_registry) == fleets
    with pytest.raises(TypeError):
        result.character_registry["10"] = characters["7"]
    with pytest.raises(FrozenInstanceError):
        result.generation = 43


def test_unexpected_failure_emits_structured_result_and_worker_recovers():
    local, channel, fleet = _detectors()
    local.scan_active_characters.side_effect = [RuntimeError("read failed"), {}]
    channel.scan_channels.return_value = {}
    fleet.scan_active_fleets.return_value = {}
    worker = ChatDiscoveryWorker(
        local_detector=local,
        channel_detector=channel,
        fleet_detector=fleet,
        entry_scanner=lambda _log_dir: (),
    )
    results = []
    failures = []
    worker.result_ready.connect(results.append)
    worker.scan_failed.connect(failures.append)

    worker.scan(1, "C:/logs", 1_800)
    worker.scan(2, "C:/logs", 1_800)

    assert len(results) == 2
    assert failures == [results[0]]
    failed, recovered = results
    assert failed.succeeded is False
    assert failed.cancelled is False
    assert failed.error is not None
    assert failed.error.stage == "local"
    assert failed.error.exception_type == "RuntimeError"
    assert failed.error.message == "read failed"
    assert recovered.generation == 2
    assert recovered.succeeded is True
    channel.scan_channels.assert_called_once_with(
        "C:/logs",
        active_characters={},
        entries=(),
    )
    fleet.scan_active_fleets.assert_called_once_with(
        "C:/logs",
        active_threshold_seconds=1_800,
        entries=(),
    )


def test_later_stage_failure_preserves_completed_registries():
    local, channel, fleet = _detectors()
    characters = {
        "7": _character("7", "C:/logs/Local_20260712_120000_7.txt")
    }
    local.scan_active_characters.return_value = characters
    channel.scan_channels.side_effect = ValueError("bad header")
    worker = ChatDiscoveryWorker(
        local_detector=local,
        channel_detector=channel,
        fleet_detector=fleet,
        entry_scanner=lambda _log_dir: (),
    )
    results = []
    worker.result_ready.connect(results.append)

    worker.scan(7, "C:/logs", 300)

    assert len(results) == 1
    assert results[0].error is not None
    assert results[0].error.stage == "any"
    assert dict(results[0].character_registry) == characters
    assert dict(results[0].channel_registry) == {}
    assert dict(results[0].fleet_registry) == {}
    fleet.scan_active_fleets.assert_not_called()


def test_stop_cancels_before_start_without_reporting_an_error():
    local, channel, fleet = _detectors()
    worker = ChatDiscoveryWorker(
        local_detector=local,
        channel_detector=channel,
        fleet_detector=fleet,
        entry_scanner=lambda _log_dir: (),
    )
    results = []
    failures = []
    worker.result_ready.connect(results.append)
    worker.scan_failed.connect(failures.append)

    worker.request_stop()
    worker.scan(9, "C:/logs", 300)

    assert len(results) == 1
    assert results[0].generation == 9
    assert results[0].cancelled is True
    assert results[0].succeeded is False
    assert results[0].error is None
    assert failures == []
    local.scan_active_characters.assert_not_called()
    channel.scan_channels.assert_not_called()
    fleet.scan_active_fleets.assert_not_called()


class _ScanRequester(QObject):
    scan_requested = Signal(int, str, int)


def test_scan_slot_runs_through_a_queued_qthread_connection():
    app = QCoreApplication.instance() or QCoreApplication([])
    assert app is not None
    local, channel, fleet = _detectors()
    calling_thread = threading.get_ident()
    worker_threads = []

    def scan_local(_log_dir, *, entries):
        assert entries == ()
        worker_threads.append(threading.get_ident())
        return {}

    local.scan_active_characters.side_effect = scan_local
    channel.scan_channels.return_value = {}
    fleet.scan_active_fleets.return_value = {}
    worker = ChatDiscoveryWorker(
        local_detector=local,
        channel_detector=channel,
        fleet_detector=fleet,
        entry_scanner=lambda _log_dir: (),
    )
    requester = _ScanRequester()
    thread = QThread()
    worker.moveToThread(thread)
    requester.scan_requested.connect(
        worker.scan,
        Qt.ConnectionType.QueuedConnection,
    )
    completed = threading.Event()
    results = []

    def receive(result):
        results.append(result)
        completed.set()

    worker.result_ready.connect(receive)
    thread.start()
    try:
        requester.scan_requested.emit(11, "C:/logs", 600)
        assert completed.wait(2.0)
        assert results[0].generation == 11
        assert results[0].succeeded is True
        assert worker.thread() is thread
        assert worker_threads and worker_threads[0] != calling_thread
    finally:
        worker.request_stop()
        thread.requestInterruption()
        thread.quit()
        assert thread.wait(2_000)

import io
import os
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from scripts import benchmark_discovery as benchmark
from src.services.chat_discovery import (
    ChatDiscoveryError,
    ChatDiscoveryResult,
    ChatDiscoveryWorker,
)


class _FakeSignal:
    def __init__(self):
        self._callbacks = []

    def connect(self, callback):
        self._callbacks.append(callback)

    def emit(self, value):
        for callback in self._callbacks:
            callback(value)


class _FakeWorker:
    def __init__(self, result_factory):
        self.result_ready = _FakeSignal()
        self.result_factory = result_factory
        self.calls = []

    def scan(self, generation, log_dir, fleet_inactive_threshold):
        self.calls.append((generation, log_dir, fleet_inactive_threshold))
        self.result_ready.emit(self.result_factory(generation, log_dir))


def _successful_result(generation, log_dir):
    return ChatDiscoveryResult(
        generation=generation,
        log_dir=log_dir,
        character_registry={
            "1": SimpleNamespace(is_active=True),
            "2": SimpleNamespace(is_active=False),
        },
        channel_registry={
            "intel::1": SimpleNamespace(is_active=True),
            "corp::1": SimpleNamespace(is_active=True),
            "trade::1": SimpleNamespace(is_active=False),
        },
        fleet_registry={"fleet": SimpleNamespace(is_active=True)},
    )


def _clock(*values):
    iterator = iter(values)
    return lambda: next(iterator)


def _write_eve_log(
    directory,
    filename,
    *,
    channel_id,
    channel_name,
    listener,
    started,
    messages=(),
):
    path = directory / filename
    path.write_text(
        "\r\n".join(
            [
                "",
                f"Channel ID:      {channel_id}",
                f"Channel Name:    {channel_name}",
                f"Listener:        {listener}",
                f"Session started: {started}",
                "",
                *messages,
            ]
        ),
        encoding="utf-16",
    )
    return path


def test_run_benchmark_reuses_worker_and_reports_timings_and_counts(tmp_path):
    worker = _FakeWorker(_successful_result)

    report = benchmark.run_benchmark(
        tmp_path,
        warm_runs=3,
        fleet_inactive_threshold=900,
        worker_factory=lambda: worker,
        clock=_clock(1.0, 1.010, 2.0, 2.004, 3.0, 3.006, 4.0, 4.002),
    )

    expected_log_dir = os.path.abspath(tmp_path)
    assert worker.calls == [
        (1, expected_log_dir, 900),
        (2, expected_log_dir, 900),
        (3, expected_log_dir, 900),
        (4, expected_log_dir, 900),
    ]
    assert report.cold_ms == pytest.approx(10.0)
    assert report.warm_ms == pytest.approx((4.0, 6.0, 2.0))
    assert report.warm_median_ms == pytest.approx(4.0)
    assert report.registry_counts == benchmark.DiscoveryCounts(2, 3, 1)
    assert report.active_counts == benchmark.DiscoveryCounts(1, 2, 1)

    formatted = benchmark.format_report(report)
    assert "Cold: 10.000 ms" in formatted
    assert "Warm runs: [4.000, 6.000, 2.000] ms" in formatted
    assert "Warm median: 4.000 ms" in formatted
    assert "Registries: characters=2, channels=3, fleets=1" in formatted
    assert "Active: characters=1, channels=2, fleets=1" in formatted


def test_cli_exits_nonzero_and_names_failed_worker_stage(tmp_path):
    def failed_result(generation, log_dir):
        return ChatDiscoveryResult(
            generation=generation,
            log_dir=log_dir,
            error=ChatDiscoveryError(
                stage="snapshot",
                exception_type="OSError",
                message="cannot read logs",
            ),
        )

    worker = _FakeWorker(failed_result)
    stdout = io.StringIO()
    stderr = io.StringIO()

    exit_code = benchmark.main(
        ["--log-dir", str(tmp_path), "--warm-runs", "2"],
        worker_factory=lambda: worker,
        clock=_clock(1.0, 1.025),
        stdout=stdout,
        stderr=stderr,
    )

    assert exit_code == 1
    assert stdout.getvalue() == ""
    assert "cold run after 25.000 ms" in stderr.getvalue()
    assert "snapshot: OSError: cannot read logs" in stderr.getvalue()
    assert len(worker.calls) == 1


def test_cli_rejects_missing_log_directory(tmp_path):
    stderr = io.StringIO()

    exit_code = benchmark.main(
        ["--log-dir", str(tmp_path / "missing")],
        stderr=stderr,
    )

    assert exit_code == 2
    assert "chat log directory does not exist" in stderr.getvalue()


def test_real_worker_warm_scan_reuses_all_unchanged_header_caches(tmp_path):
    now = datetime.now(timezone.utc)
    compact_started = now.strftime("%Y%m%d_%H%M%S")
    header_started = now.strftime("%Y.%m.%d %H:%M:%S")
    character_id = "2112345678"
    listener = "Benchmark Pilot"
    local_path = _write_eve_log(
        tmp_path,
        f"Local_{compact_started}_{character_id}.txt",
        channel_id="(None)",
        channel_name="Local",
        listener=listener,
        started=header_started,
        messages=(
            f"[ {header_started} ] EVE System > Channel changed to Local : Jita",
            f"[ {header_started} ] {listener} > clear on undock",
        ),
    )
    custom_path = _write_eve_log(
        tmp_path,
        f"Alliance_Intel_{compact_started}_{character_id}.txt",
        channel_id="player_alliance_intel",
        channel_name="Alliance Intel",
        listener=listener,
        started=header_started,
        messages=(f"[ {header_started} ] Scout One > hostile in M-O",),
    )
    fleet_path = _write_eve_log(
        tmp_path,
        f"Fleet_{compact_started}.txt",
        channel_id="(None)",
        channel_name="Fleet",
        listener=listener,
        started=header_started,
        messages=(f"[ {header_started} ] FC > align sun",),
    )

    worker = ChatDiscoveryWorker()
    # Avoid making this filesystem/cache integration test depend on the
    # developer machine having a matching EVE window open.
    worker.local_detector.is_character_window_open = lambda _name: False
    results = []
    worker.result_ready.connect(results.append)

    with patch("builtins.open", wraps=open) as cold_open:
        worker.scan(1, str(tmp_path), 1_800)

    assert cold_open.call_count >= 4
    assert len(results) == 1
    cold = results[0]
    assert cold.succeeded, cold.error
    assert set(cold.character_registry) == {character_id}
    assert cold.character_registry[character_id].latest_log_path == str(local_path)
    assert cold.character_registry[character_id].system_name == "Jita"
    assert cold.character_registry[character_id].is_active is True
    assert len(cold.channel_registry) == 1
    cold_channel = next(iter(cold.channel_registry.values()))
    assert cold_channel.log_path == str(custom_path)
    assert cold_channel.is_active is True
    assert len(cold.fleet_registry) == 1
    cold_fleet = next(iter(cold.fleet_registry.values()))
    assert cold_fleet.log_path == str(fleet_path)
    assert cold_fleet.is_active is True

    with patch(
        "builtins.open",
        side_effect=AssertionError("warm discovery reopened an unchanged log"),
    ) as forbidden_open:
        worker.scan(2, str(tmp_path), 1_800)

    forbidden_open.assert_not_called()
    assert len(results) == 2
    warm = results[1]
    assert warm.succeeded, warm.error
    assert dict(warm.character_registry) == dict(cold.character_registry)
    assert dict(warm.channel_registry) == dict(cold.channel_registry)
    assert dict(warm.fleet_registry) == dict(cold.fleet_registry)

import io
import os
from types import SimpleNamespace

import pytest

from scripts import benchmark_discovery as benchmark
from src.services.chat_discovery import ChatDiscoveryError, ChatDiscoveryResult


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

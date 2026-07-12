"""Benchmark one complete production chat-discovery scan.

The first scan uses a fresh :class:`ChatDiscoveryWorker`; subsequent scans
reuse that worker so detector and header caches have the same lifetime they do
in the application.  This harness reports measurements but deliberately does
not enforce a machine-dependent speed threshold.
"""

from __future__ import annotations

import argparse
import os
import statistics
import sys
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol, TextIO


# ``python scripts/benchmark_discovery.py`` puts ``scripts`` rather than the
# repository root on sys.path.  Make the documented direct invocation work
# independently of the caller's Python path.
if __package__ in {None, ""}:
    project_root = str(Path(__file__).resolve().parents[1])
    if project_root not in sys.path:
        sys.path.insert(0, project_root)

from src.services.chat_discovery import ChatDiscoveryResult, ChatDiscoveryWorker


DEFAULT_WARM_RUNS = 5
DEFAULT_FLEET_INACTIVE_THRESHOLD = 1_800


class _ResultSignal(Protocol):
    def connect(self, callback: Callable[[object], None]) -> object: ...


class _DiscoveryWorker(Protocol):
    result_ready: _ResultSignal

    def scan(
        self,
        generation: int,
        log_dir: str,
        fleet_inactive_threshold: int,
    ) -> None: ...


@dataclass(frozen=True, slots=True)
class DiscoveryCounts:
    characters: int
    channels: int
    fleets: int


@dataclass(frozen=True, slots=True)
class DiscoveryBenchmarkReport:
    log_dir: str
    cold_ms: float
    warm_ms: tuple[float, ...]
    warm_median_ms: float
    registry_counts: DiscoveryCounts
    active_counts: DiscoveryCounts


class DiscoveryBenchmarkFailure(RuntimeError):
    """Raised when a measured worker run does not produce a successful result."""

    def __init__(
        self,
        run_label: str,
        elapsed_ms: float,
        reason: str,
    ) -> None:
        super().__init__(reason)
        self.run_label = run_label
        self.elapsed_ms = elapsed_ms
        self.reason = reason


def _positive_int(value: str) -> int:
    parsed = int(value)
    if parsed < 1:
        raise argparse.ArgumentTypeError("must be at least 1")
    return parsed


def _non_negative_int(value: str) -> int:
    parsed = int(value)
    if parsed < 0:
        raise argparse.ArgumentTypeError("must be at least 0")
    return parsed


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Measure the production EVE chat discovery worker without applying "
            "a machine-dependent pass/fail speed threshold."
        )
    )
    parser.add_argument(
        "--log-dir",
        required=True,
        type=Path,
        help="EVE chat-log directory to scan",
    )
    parser.add_argument(
        "--warm-runs",
        type=_positive_int,
        default=DEFAULT_WARM_RUNS,
        help=f"number of cached runs after the cold run (default: {DEFAULT_WARM_RUNS})",
    )
    parser.add_argument(
        "--fleet-inactive-threshold",
        type=_non_negative_int,
        default=DEFAULT_FLEET_INACTIVE_THRESHOLD,
        metavar="SECONDS",
        help=(
            "fleet inactivity window passed to discovery "
            f"(default: {DEFAULT_FLEET_INACTIVE_THRESHOLD})"
        ),
    )
    return parser


def _result_failure_reason(result: ChatDiscoveryResult) -> str | None:
    if result.cancelled:
        return "worker cancelled the scan"
    if result.error is not None:
        error = result.error
        return f"{error.stage}: {error.exception_type}: {error.message}"
    if not result.succeeded:
        return "worker returned an unsuccessful result"
    return None


def _measure_scan(
    worker: _DiscoveryWorker,
    results: list[object],
    *,
    generation: int,
    log_dir: str,
    fleet_inactive_threshold: int,
    clock: Callable[[], float],
    run_label: str,
) -> tuple[float, ChatDiscoveryResult]:
    result_count = len(results)
    started = clock()
    worker.scan(generation, log_dir, fleet_inactive_threshold)
    elapsed_ms = (clock() - started) * 1_000.0

    emitted = results[result_count:]
    if len(emitted) != 1:
        raise DiscoveryBenchmarkFailure(
            run_label,
            elapsed_ms,
            f"worker emitted {len(emitted)} results; expected exactly one",
        )
    if not isinstance(emitted[0], ChatDiscoveryResult):
        raise DiscoveryBenchmarkFailure(
            run_label,
            elapsed_ms,
            f"worker emitted unexpected result type {type(emitted[0]).__name__}",
        )

    result = emitted[0]
    reason = _result_failure_reason(result)
    if reason is not None:
        raise DiscoveryBenchmarkFailure(run_label, elapsed_ms, reason)
    return elapsed_ms, result


def _counts(result: ChatDiscoveryResult) -> tuple[DiscoveryCounts, DiscoveryCounts]:
    registries = (
        result.character_registry,
        result.channel_registry,
        result.fleet_registry,
    )
    registry_counts = DiscoveryCounts(*(len(registry) for registry in registries))
    active_counts = DiscoveryCounts(
        *(
            sum(bool(getattr(item, "is_active", False)) for item in registry.values())
            for registry in registries
        )
    )
    return registry_counts, active_counts


def run_benchmark(
    log_dir: str | os.PathLike[str],
    *,
    warm_runs: int = DEFAULT_WARM_RUNS,
    fleet_inactive_threshold: int = DEFAULT_FLEET_INACTIVE_THRESHOLD,
    worker_factory: Callable[[], _DiscoveryWorker] = ChatDiscoveryWorker,
    clock: Callable[[], float] = time.perf_counter,
) -> DiscoveryBenchmarkReport:
    """Time a cold direct scan and cached scans on the same worker instance."""
    if warm_runs < 1:
        raise ValueError("warm_runs must be at least 1")
    if fleet_inactive_threshold < 0:
        raise ValueError("fleet_inactive_threshold must be at least 0")

    normalized_log_dir = os.path.abspath(os.fspath(log_dir))
    worker = worker_factory()
    emitted_results: list[object] = []
    worker.result_ready.connect(emitted_results.append)

    cold_ms, last_result = _measure_scan(
        worker,
        emitted_results,
        generation=1,
        log_dir=normalized_log_dir,
        fleet_inactive_threshold=fleet_inactive_threshold,
        clock=clock,
        run_label="cold",
    )
    warm_measurements: list[float] = []
    for warm_index in range(1, warm_runs + 1):
        elapsed_ms, last_result = _measure_scan(
            worker,
            emitted_results,
            generation=warm_index + 1,
            log_dir=normalized_log_dir,
            fleet_inactive_threshold=fleet_inactive_threshold,
            clock=clock,
            run_label=f"warm {warm_index}",
        )
        warm_measurements.append(elapsed_ms)

    registry_counts, active_counts = _counts(last_result)
    return DiscoveryBenchmarkReport(
        log_dir=normalized_log_dir,
        cold_ms=cold_ms,
        warm_ms=tuple(warm_measurements),
        warm_median_ms=float(statistics.median(warm_measurements)),
        registry_counts=registry_counts,
        active_counts=active_counts,
    )


def format_report(report: DiscoveryBenchmarkReport) -> str:
    warm_values = ", ".join(f"{elapsed:.3f}" for elapsed in report.warm_ms)
    registries = report.registry_counts
    active = report.active_counts
    return "\n".join(
        (
            "EVE chat discovery benchmark",
            f"Log directory: {report.log_dir}",
            f"Cold: {report.cold_ms:.3f} ms",
            f"Warm runs: [{warm_values}] ms",
            f"Warm median: {report.warm_median_ms:.3f} ms",
            (
                "Registries: "
                f"characters={registries.characters}, "
                f"channels={registries.channels}, fleets={registries.fleets}"
            ),
            (
                "Active: "
                f"characters={active.characters}, "
                f"channels={active.channels}, fleets={active.fleets}"
            ),
        )
    )


def main(
    argv: Sequence[str] | None = None,
    *,
    worker_factory: Callable[[], _DiscoveryWorker] = ChatDiscoveryWorker,
    clock: Callable[[], float] = time.perf_counter,
    stdout: TextIO | None = None,
    stderr: TextIO | None = None,
) -> int:
    output = stdout if stdout is not None else sys.stdout
    error_output = stderr if stderr is not None else sys.stderr
    args = build_parser().parse_args(argv)
    log_dir = args.log_dir.expanduser()
    if not log_dir.is_dir():
        print(f"error: chat log directory does not exist: {log_dir}", file=error_output)
        return 2

    try:
        report = run_benchmark(
            log_dir,
            warm_runs=args.warm_runs,
            fleet_inactive_threshold=args.fleet_inactive_threshold,
            worker_factory=worker_factory,
            clock=clock,
        )
    except DiscoveryBenchmarkFailure as exc:
        print(
            f"Discovery failed on {exc.run_label} run after "
            f"{exc.elapsed_ms:.3f} ms: {exc.reason}",
            file=error_output,
        )
        return 1

    print(format_report(report), file=output)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

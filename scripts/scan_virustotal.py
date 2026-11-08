"""Submit a public release executable to VirusTotal and report its findings."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
import tempfile
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Mapping
from urllib.parse import quote, urlsplit, urlunsplit

import requests


API_ROOT = "https://www.virustotal.com/api/v3"
DIRECT_UPLOAD_LIMIT = 32 * 1024 * 1024
MAX_UPLOAD_SIZE = 650 * 1024 * 1024
DEFAULT_POLL_INTERVAL_SECONDS = 30.0
DEFAULT_SCAN_TIMEOUT_SECONDS = 15 * 60.0
DEFAULT_RETRY_DELAY_SECONDS = 30.0
MAX_TRANSIENT_ATTEMPTS = 3
# The API documents large-file upload URLs on www.virustotal.com, but live
# responses (observed November 2026) use a dedicated upload host. The API key
# travels with the upload, so only these VirusTotal hosts are accepted.
UPLOAD_HOSTS = frozenset({"www.virustotal.com", "bigfiles.virustotal.com"})
REPORT_START = "<!-- virustotal-report:start -->"
REPORT_END = "<!-- virustotal-report:end -->"
SHA256_PATTERN = re.compile(r"^[0-9a-f]{64}$")
STAT_CATEGORIES = (
    "malicious",
    "suspicious",
    "harmless",
    "undetected",
    "type-unsupported",
    "failure",
    "timeout",
    "confirmed-timeout",
)


class VirusTotalError(RuntimeError):
    """Raised when a release scan cannot produce a trustworthy report."""


@dataclass(frozen=True)
class ScanReport:
    file_name: str
    sha256: str
    analysis_id: str
    completed_at: datetime
    stats: Mapping[str, int]

    @property
    def report_url(self) -> str:
        return f"https://www.virustotal.com/gui/file/{self.sha256}/detection"

    @property
    def has_detections(self) -> bool:
        return self.stats["malicious"] > 0 or self.stats["suspicious"] > 0

    @property
    def engine_results(self) -> int:
        return sum(self.stats.values())


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def validate_packaged_executable(
    manifest_path: Path,
    executable: Path,
    sha256: str,
) -> None:
    """Confirm that the scan target is the executable recorded in the release ZIP."""

    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        packaged = manifest["executable"]
        packaged_name = packaged["filename"]
        packaged_hash = packaged["sha256"]
    except (OSError, json.JSONDecodeError, KeyError, TypeError) as exc:
        raise VirusTotalError(
            f"Could not validate release manifest {manifest_path.name}"
        ) from exc

    if packaged_name != executable.name:
        raise VirusTotalError(
            "VirusTotal target does not match the executable named in the release manifest"
        )
    if not isinstance(packaged_hash, str) or packaged_hash.casefold() != sha256:
        raise VirusTotalError(
            "VirusTotal target hash does not match the packaged release executable"
        )


def _response_json(response: requests.Response, operation: str) -> dict[str, Any]:
    try:
        payload = response.json()
    except ValueError as exc:
        raise VirusTotalError(
            f"VirusTotal returned malformed JSON during {operation}"
        ) from exc
    if not isinstance(payload, dict):
        raise VirusTotalError(
            f"VirusTotal returned an unexpected response during {operation}"
        )
    return payload


def _retry_after_seconds(response: requests.Response) -> float:
    raw_value = response.headers.get("Retry-After", "").strip()
    try:
        requested = float(raw_value)
    except ValueError:
        requested = DEFAULT_RETRY_DELAY_SECONDS
    return max(DEFAULT_RETRY_DELAY_SECONDS, requested)


def _validate_upload_url(raw_url: object) -> str:
    if not isinstance(raw_url, str):
        raise VirusTotalError("VirusTotal did not return a large-file upload URL")
    try:
        parsed = urlsplit(raw_url)
        hostname = (parsed.hostname or "").casefold()
        port = parsed.port
    except ValueError as exc:
        raise VirusTotalError("VirusTotal returned an unsafe large-file upload URL") from exc
    if (
        parsed.scheme.casefold() not in {"http", "https"}
        or parsed.username is not None
        or parsed.password is not None
        or port not in (None, 443)
        or hostname not in UPLOAD_HOSTS
        or parsed.fragment
    ):
        # Name only the origin: the path and query carry the one-time upload token.
        origin = f"{parsed.scheme}://{hostname}" + (f":{port}" if port is not None else "")
        raise VirusTotalError(
            f"VirusTotal returned an unsafe large-file upload URL on {origin}"
        )
    return urlunsplit(("https", hostname, parsed.path, parsed.query, ""))


class VirusTotalClient:
    """Small API v3 client with bounded polling and public-quota backoff."""

    def __init__(
        self,
        api_key: str,
        *,
        session: requests.Session | None = None,
        sleep: Callable[[float], None] = time.sleep,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        if not api_key.strip():
            raise VirusTotalError("VIRUSTOTAL_API_KEY is not configured")
        self._api_key = api_key.strip()
        self._session = session or requests.Session()
        self._sleep = sleep
        self._monotonic = monotonic

    def deadline_after(self, seconds: float) -> float:
        return self._monotonic() + seconds

    def _request_json(
        self,
        method: str,
        url: str,
        operation: str,
        *,
        deadline: float,
        files: Mapping[str, object] | None = None,
        retry_transient: bool = True,
        timeout: tuple[float, float] = (15.0, 60.0),
    ) -> dict[str, Any]:
        attempts = MAX_TRANSIENT_ATTEMPTS if retry_transient else 1
        for attempt in range(1, attempts + 1):
            try:
                response = self._session.request(
                    method,
                    url,
                    headers={
                        "Accept": "application/json",
                        "x-apikey": self._api_key,
                    },
                    files=files,
                    timeout=timeout,
                    allow_redirects=False,
                )
            except requests.RequestException as exc:
                if attempt == attempts:
                    raise VirusTotalError(
                        f"VirusTotal network request failed during {operation}"
                    ) from exc
                delay = DEFAULT_RETRY_DELAY_SECONDS
            else:
                if 200 <= response.status_code < 300:
                    return _response_json(response, operation)
                if response.status_code not in {429, 503, 504} or attempt == attempts:
                    raise VirusTotalError(
                        f"VirusTotal returned HTTP {response.status_code} during {operation}"
                    )
                delay = _retry_after_seconds(response)

            if self._monotonic() + delay >= deadline:
                raise VirusTotalError(
                    f"VirusTotal scan timed out while retrying {operation}"
                )
            self._sleep(delay)

        raise AssertionError("unreachable")

    def submit_file(self, path: Path, *, deadline: float) -> str:
        if not path.is_file():
            raise VirusTotalError(f"Release executable was not found: {path}")
        size = path.stat().st_size
        if size <= 0:
            raise VirusTotalError("VirusTotal cannot scan an empty release executable")
        if size > MAX_UPLOAD_SIZE:
            raise VirusTotalError("Release executable exceeds VirusTotal's 650 MB limit")

        upload_url = f"{API_ROOT}/files"
        if size > DIRECT_UPLOAD_LIMIT:
            upload_descriptor = self._request_json(
                "GET",
                f"{API_ROOT}/files/upload_url",
                "large-file upload URL request",
                deadline=deadline,
            )
            upload_url = _validate_upload_url(upload_descriptor.get("data"))

        with path.open("rb") as source:
            upload_response = self._request_json(
                "POST",
                upload_url,
                "file upload",
                deadline=deadline,
                files={"file": (path.name, source, "application/octet-stream")},
                retry_transient=False,
                timeout=(15.0, 300.0),
            )

        data = upload_response.get("data")
        if not isinstance(data, dict) or data.get("type") != "analysis":
            raise VirusTotalError("VirusTotal upload did not return an analysis descriptor")
        analysis_id = data.get("id")
        if not isinstance(analysis_id, str) or not analysis_id.strip():
            raise VirusTotalError("VirusTotal upload did not return an analysis ID")
        return analysis_id

    def wait_for_analysis(
        self,
        analysis_id: str,
        *,
        deadline: float,
        poll_interval_seconds: float,
    ) -> dict[str, Any]:
        analysis_url = f"{API_ROOT}/analyses/{quote(analysis_id, safe='')}"
        while True:
            if self._monotonic() + poll_interval_seconds >= deadline:
                raise VirusTotalError("VirusTotal analysis did not complete before timeout")
            self._sleep(poll_interval_seconds)
            payload = self._request_json(
                "GET",
                analysis_url,
                "analysis polling",
                deadline=deadline,
            )
            data = payload.get("data")
            attributes = data.get("attributes") if isinstance(data, dict) else None
            status = attributes.get("status") if isinstance(attributes, dict) else None
            if status == "completed":
                return payload
            if status not in {"queued", "in-progress"}:
                raise VirusTotalError(
                    "VirusTotal returned an unexpected analysis status"
                )


def _normalized_stats(raw_stats: object) -> dict[str, int]:
    if not isinstance(raw_stats, dict):
        raise VirusTotalError("Completed VirusTotal analysis has no result statistics")
    if any(category not in STAT_CATEGORIES for category in raw_stats):
        raise VirusTotalError(
            "VirusTotal returned unsupported result-statistic categories"
        )
    stats: dict[str, int] = {}
    for category in STAT_CATEGORIES:
        value = raw_stats.get(category, 0)
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            raise VirusTotalError(
                f"VirusTotal returned an invalid {category!r} result count"
            )
        stats[category] = value
    if sum(stats.values()) == 0:
        raise VirusTotalError("Completed VirusTotal analysis has no engine results")
    return stats


def report_from_analysis(
    executable: Path,
    sha256: str,
    analysis_id: str,
    payload: Mapping[str, Any],
) -> ScanReport:
    data = payload.get("data")
    if not isinstance(data, dict) or data.get("type") != "analysis":
        raise VirusTotalError("VirusTotal returned an invalid completed analysis")
    attributes = data.get("attributes")
    if not isinstance(attributes, dict) or attributes.get("status") != "completed":
        raise VirusTotalError("VirusTotal analysis is not complete")

    analysis_date = attributes.get("date")
    if isinstance(analysis_date, bool) or not isinstance(analysis_date, int):
        raise VirusTotalError("VirusTotal analysis has no valid timestamp")
    try:
        completed_at = datetime.fromtimestamp(analysis_date, tz=timezone.utc)
    except (OverflowError, OSError, ValueError) as exc:
        raise VirusTotalError("VirusTotal analysis timestamp is invalid") from exc

    meta = payload.get("meta")
    if isinstance(meta, dict):
        file_info = meta.get("file_info")
        remote_hash = file_info.get("sha256") if isinstance(file_info, dict) else None
        if remote_hash is not None and (
            not isinstance(remote_hash, str) or remote_hash.casefold() != sha256
        ):
            raise VirusTotalError("VirusTotal analysed a different file hash")

    return ScanReport(
        file_name=executable.name,
        sha256=sha256,
        analysis_id=analysis_id,
        completed_at=completed_at,
        stats=_normalized_stats(attributes.get("stats")),
    )


def scan_release_executable(
    executable: Path,
    manifest_path: Path,
    client: VirusTotalClient,
    *,
    timeout_seconds: float = DEFAULT_SCAN_TIMEOUT_SECONDS,
    poll_interval_seconds: float = DEFAULT_POLL_INTERVAL_SECONDS,
) -> ScanReport:
    if timeout_seconds <= 0 or poll_interval_seconds < DEFAULT_POLL_INTERVAL_SECONDS:
        raise VirusTotalError(
            "VirusTotal timeout must be positive and polling must be at least 30 seconds"
        )
    executable = executable.resolve()
    if not executable.is_file():
        raise VirusTotalError(f"Release executable was not found: {executable}")
    sha256 = sha256_file(executable)
    if not SHA256_PATTERN.fullmatch(sha256):
        raise AssertionError("Local SHA-256 calculation returned an invalid digest")
    validate_packaged_executable(manifest_path, executable, sha256)

    deadline = client.deadline_after(timeout_seconds)
    analysis_id = client.submit_file(executable, deadline=deadline)
    payload = client.wait_for_analysis(
        analysis_id,
        deadline=deadline,
        poll_interval_seconds=poll_interval_seconds,
    )
    return report_from_analysis(executable, sha256, analysis_id, payload)


def render_markdown(report: ScanReport) -> str:
    stats = report.stats
    completed = report.completed_at.strftime("%Y-%m-%d %H:%M UTC")
    findings = (
        f"**{stats['malicious']} malicious, {stats['suspicious']} suspicious**, "
        f"{stats['harmless']} harmless, {stats['undetected']} undetected, "
        f"{stats['type-unsupported']} unsupported, {stats['failure']} analysis "
        f"failures, {stats['timeout']} timeouts, and "
        f"{stats['confirmed-timeout']} confirmed timeouts across "
        f"{report.engine_results} engine results"
    )
    lines = [
        "### VirusTotal analysis",
        "",
        "The release workflow submitted the packaged Windows executable after building it.",
        "",
        f"- Artifact: `{report.file_name}`",
        f"- SHA-256: `{report.sha256}`",
        f"- Analysis timestamp: `{completed}`",
        f"- Findings at completion: {findings}.",
        f"- [View the live VirusTotal report]({report.report_url})",
        "",
    ]
    if report.has_detections:
        lines.extend(
            [
                "> **Review required before publication:** at least one engine classified "
                "the file as malicious or suspicious.",
                "",
            ]
        )
    lines.append(
        "> VirusTotal aggregates independent engine classifications. This point-in-time "
        "result is not a guarantee of safety, and the live report can change."
    )
    return "\n".join(lines)


def update_release_notes(notes_path: Path, report_markdown: str) -> None:
    """Atomically append or replace the generated VirusTotal notes section."""

    try:
        existing = notes_path.read_text(encoding="utf-8")
    except OSError as exc:
        raise VirusTotalError(f"Could not read release notes {notes_path}") from exc

    if existing.count(REPORT_START) != existing.count(REPORT_END):
        raise VirusTotalError("Release notes contain a malformed VirusTotal report section")
    if existing.count(REPORT_START) > 1:
        raise VirusTotalError("Release notes contain duplicate VirusTotal report sections")

    marked_report = f"{REPORT_START}\n{report_markdown.rstrip()}\n{REPORT_END}"
    start = existing.find(REPORT_START)
    end = existing.find(REPORT_END)
    if start == -1:
        updated = f"{existing.rstrip()}\n\n{marked_report}\n"
    elif end < start:
        raise VirusTotalError("Release notes contain a malformed VirusTotal report section")
    else:
        end += len(REPORT_END)
        updated = f"{existing[:start]}{marked_report}{existing[end:]}"
        if not updated.endswith("\n"):
            updated += "\n"

    notes_path.parent.mkdir(parents=True, exist_ok=True)
    temp_name: str | None = None
    try:
        descriptor, temp_name = tempfile.mkstemp(
            prefix=f".{notes_path.name}.",
            suffix=".tmp",
            dir=notes_path.parent,
        )
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as target:
            target.write(updated)
        os.replace(temp_name, notes_path)
    except OSError as exc:
        raise VirusTotalError(f"Could not update release notes {notes_path}") from exc
    finally:
        if temp_name is not None:
            try:
                Path(temp_name).unlink(missing_ok=True)
            except OSError:
                pass


def _append_text(path: Path, text: str) -> None:
    try:
        with path.open("a", encoding="utf-8", newline="\n") as target:
            target.write(text)
    except OSError as exc:
        raise VirusTotalError(f"Could not update GitHub workflow file {path}") from exc


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--file", required=True, type=Path)
    parser.add_argument("--manifest", required=True, type=Path)
    parser.add_argument("--append-notes", required=True, type=Path)
    parser.add_argument("--github-output", type=Path)
    parser.add_argument("--github-summary", type=Path)
    parser.add_argument(
        "--timeout-seconds",
        type=float,
        default=DEFAULT_SCAN_TIMEOUT_SECONDS,
    )
    parser.add_argument(
        "--poll-interval-seconds",
        type=float,
        default=DEFAULT_POLL_INTERVAL_SECONDS,
    )
    return parser.parse_args()


def main() -> int:
    args = _parse_args()
    try:
        client = VirusTotalClient(os.environ.get("VIRUSTOTAL_API_KEY", ""))
        report = scan_release_executable(
            args.file,
            args.manifest,
            client,
            timeout_seconds=args.timeout_seconds,
            poll_interval_seconds=args.poll_interval_seconds,
        )
        markdown = render_markdown(report)
        update_release_notes(args.append_notes, markdown)
        if args.github_output is not None:
            _append_text(
                args.github_output,
                f"has_detections={'true' if report.has_detections else 'false'}\n",
            )
        if args.github_summary is not None:
            _append_text(args.github_summary, f"{markdown}\n")
    except VirusTotalError as exc:
        print(f"VirusTotal release scan failed: {exc}", file=sys.stderr)
        return 1

    print(
        "VirusTotal analysis completed: "
        f"{report.stats['malicious']} malicious, "
        f"{report.stats['suspicious']} suspicious; {report.report_url}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

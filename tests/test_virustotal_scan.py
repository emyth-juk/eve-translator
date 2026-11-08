import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path

import pytest
import requests

from scripts.scan_virustotal import (
    API_ROOT,
    DIRECT_UPLOAD_LIMIT,
    REPORT_END,
    REPORT_START,
    ScanReport,
    VirusTotalClient,
    VirusTotalError,
    render_markdown,
    report_from_analysis,
    scan_release_executable,
    update_release_notes,
    validate_packaged_executable,
)


class FakeResponse:
    def __init__(self, status_code, payload=None, *, headers=None):
        self.status_code = status_code
        self._payload = payload
        self.headers = headers or {}

    def json(self):
        if isinstance(self._payload, Exception):
            raise self._payload
        return self._payload


class FakeSession:
    def __init__(self, responses):
        self.responses = list(responses)
        self.requests = []

    def request(self, method, url, **kwargs):
        file_part = kwargs.get("files", {}).get("file") if kwargs.get("files") else None
        self.requests.append(
            {
                "method": method,
                "url": url,
                "headers": kwargs.get("headers"),
                "file_name": file_part[0] if file_part else None,
                "allow_redirects": kwargs.get("allow_redirects"),
            }
        )
        return self.responses.pop(0)


class FakeClock:
    def __init__(self):
        self.now = 0.0
        self.sleeps = []

    def monotonic(self):
        return self.now

    def sleep(self, seconds):
        self.sleeps.append(seconds)
        self.now += seconds


def _analysis_payload(sha256, *, status="completed", stats=None):
    return {
        "meta": {"file_info": {"sha256": sha256}},
        "data": {
            "type": "analysis",
            "id": "analysis-id",
            "attributes": {
                "status": status,
                "date": 1_720_000_000,
                "stats": stats
                or {
                    "malicious": 0,
                    "suspicious": 0,
                    "harmless": 1,
                    "undetected": 68,
                    "type-unsupported": 2,
                    "failure": 0,
                    "timeout": 1,
                    "confirmed-timeout": 0,
                },
            },
        },
    }


def _write_manifest(path: Path, executable: Path, sha256: str):
    path.write_text(
        json.dumps(
            {
                "executable": {
                    "filename": executable.name,
                    "sha256": sha256,
                }
            }
        ),
        encoding="utf-8",
    )


def test_small_file_uses_direct_upload_endpoint(tmp_path):
    executable = tmp_path / "release.exe"
    executable.write_bytes(b"binary")
    session = FakeSession(
        [FakeResponse(200, {"data": {"type": "analysis", "id": "a-small"}})]
    )
    client = VirusTotalClient("secret", session=session)

    analysis_id = client.submit_file(executable, deadline=float("inf"))

    assert analysis_id == "a-small"
    assert [(call["method"], call["url"]) for call in session.requests] == [
        ("POST", f"{API_ROOT}/files")
    ]
    assert session.requests[0]["headers"]["x-apikey"] == "secret"
    assert session.requests[0]["file_name"] == "release.exe"
    assert session.requests[0]["allow_redirects"] is False


@pytest.mark.parametrize(
    ("returned_url", "expected_post_url"),
    (
        # The documented form is upgraded to HTTPS.
        (
            "http://www.virustotal.com/_ah/upload/one-use-token",
            "https://www.virustotal.com/_ah/upload/one-use-token",
        ),
        # Live large-file URLs come from a dedicated upload host.
        (
            "https://bigfiles.virustotal.com/_ah/upload/one-use-token/",
            "https://bigfiles.virustotal.com/_ah/upload/one-use-token/",
        ),
        (
            "https://BigFiles.VirusTotal.com:443/upload?token=one-use",
            "https://bigfiles.virustotal.com/upload?token=one-use",
        ),
    ),
)
def test_large_file_uses_single_use_upload_url(tmp_path, returned_url, expected_post_url):
    executable = tmp_path / "release.exe"
    with executable.open("wb") as target:
        target.truncate(DIRECT_UPLOAD_LIMIT + 1)
    session = FakeSession(
        [
            FakeResponse(200, {"data": returned_url}),
            FakeResponse(200, {"data": {"type": "analysis", "id": "a-large"}}),
        ]
    )
    client = VirusTotalClient("secret", session=session)

    assert client.submit_file(executable, deadline=float("inf")) == "a-large"
    assert [(call["method"], call["url"]) for call in session.requests] == [
        ("GET", f"{API_ROOT}/files/upload_url"),
        ("POST", expected_post_url),
    ]
    assert session.requests[1]["headers"]["x-apikey"] == "secret"


@pytest.mark.parametrize(
    "upload_url",
    (
        "http://evil.example/_ah/upload/token",
        "https://virustotal.example/_ah/upload/token",
        "https://other.virustotal.com/_ah/upload/token",
        "https://bigfiles.virustotal.com.example.net/_ah/upload/token",
        "https://bigfiles.virustotal.com@example.net/_ah/upload/token",
        "https://attacker@www.virustotal.com/_ah/upload/token",
        "https://www.virustotal.com:444/_ah/upload/token",
        "https://www.virustotal.com/_ah/upload/token#fragment",
    ),
)
def test_large_file_rejects_unsafe_upload_url(tmp_path, upload_url):
    executable = tmp_path / "release.exe"
    with executable.open("wb") as target:
        target.truncate(DIRECT_UPLOAD_LIMIT + 1)
    session = FakeSession([FakeResponse(200, {"data": upload_url})])
    client = VirusTotalClient("secret", session=session)

    with pytest.raises(VirusTotalError, match="unsafe") as raised:
        client.submit_file(executable, deadline=float("inf"))
    assert "_ah/upload" not in str(raised.value)
    assert [call["method"] for call in session.requests] == ["GET"]


def test_polling_waits_30_seconds_and_retries_rate_limit():
    sha256 = "a" * 64
    clock = FakeClock()
    session = FakeSession(
        [
            FakeResponse(200, _analysis_payload(sha256, status="queued")),
            FakeResponse(429, {}, headers={"Retry-After": "5"}),
            FakeResponse(200, _analysis_payload(sha256)),
        ]
    )
    client = VirusTotalClient(
        "secret",
        session=session,
        sleep=clock.sleep,
        monotonic=clock.monotonic,
    )

    payload = client.wait_for_analysis(
        "analysis/id",
        deadline=300.0,
        poll_interval_seconds=30.0,
    )

    assert payload["data"]["attributes"]["status"] == "completed"
    assert clock.sleeps == [30.0, 30.0, 30.0]
    assert all("analysis%2Fid" in call["url"] for call in session.requests)


def test_scan_validates_manifest_and_completed_hash_before_reporting(tmp_path):
    executable = tmp_path / "release.exe"
    executable.write_bytes(b"binary")
    sha256 = hashlib.sha256(b"binary").hexdigest()
    manifest = tmp_path / "BUNDLED_COMPONENTS.json"
    _write_manifest(manifest, executable, sha256)
    clock = FakeClock()
    session = FakeSession(
        [
            FakeResponse(200, {"data": {"type": "analysis", "id": "a1"}}),
            FakeResponse(200, _analysis_payload(sha256)),
        ]
    )
    client = VirusTotalClient(
        "secret",
        session=session,
        sleep=clock.sleep,
        monotonic=clock.monotonic,
    )

    report = scan_release_executable(
        executable,
        manifest,
        client,
        timeout_seconds=120.0,
        poll_interval_seconds=30.0,
    )

    assert report.sha256 == sha256
    assert report.engine_results == 72
    assert report.has_detections is False


def test_manifest_or_remote_hash_mismatch_fails_closed(tmp_path):
    executable = tmp_path / "release.exe"
    executable.write_bytes(b"binary")
    sha256 = hashlib.sha256(b"binary").hexdigest()
    manifest = tmp_path / "BUNDLED_COMPONENTS.json"
    _write_manifest(manifest, executable, "0" * 64)

    with pytest.raises(VirusTotalError, match="packaged release executable"):
        validate_packaged_executable(manifest, executable, sha256)

    payload = _analysis_payload("f" * 64)
    with pytest.raises(VirusTotalError, match="different file hash"):
        report_from_analysis(executable, sha256, "analysis-id", payload)


def test_completed_analysis_requires_nonempty_valid_stats(tmp_path):
    executable = tmp_path / "release.exe"
    sha256 = "a" * 64
    empty_stats = {
        category: 0
        for category in (
            "malicious",
            "suspicious",
            "harmless",
            "undetected",
            "type-unsupported",
            "failure",
            "timeout",
            "confirmed-timeout",
        )
    }

    with pytest.raises(VirusTotalError, match="no engine results"):
        report_from_analysis(
            executable,
            sha256,
            "analysis-id",
            _analysis_payload(sha256, stats=empty_stats),
        )

    payload = _analysis_payload(sha256)
    payload["data"]["attributes"]["stats"]["malicious"] = -1
    with pytest.raises(VirusTotalError, match="invalid 'malicious'"):
        report_from_analysis(executable, sha256, "analysis-id", payload)

    payload = _analysis_payload(sha256)
    payload["data"]["attributes"]["stats"]["new-remote-category"] = 1
    with pytest.raises(VirusTotalError, match="unsupported result-statistic"):
        report_from_analysis(executable, sha256, "analysis-id", payload)


def test_markdown_reports_aggregate_findings_and_detection_review():
    report = ScanReport(
        file_name="EVETranslator_v1.4.exe",
        sha256="a" * 64,
        analysis_id="analysis-id",
        completed_at=datetime(2026, 7, 12, 12, 34, tzinfo=timezone.utc),
        stats={
            "malicious": 1,
            "suspicious": 2,
            "harmless": 3,
            "undetected": 64,
            "type-unsupported": 2,
            "failure": 0,
            "timeout": 1,
            "confirmed-timeout": 0,
        },
    )

    markdown = render_markdown(report)

    assert "**1 malicious, 2 suspicious**" in markdown
    assert "across 73 engine results" in markdown
    assert "Analysis timestamp" in markdown
    assert "Review required before publication" in markdown
    assert f"/gui/file/{report.sha256}/detection" in markdown
    assert "virus-free" not in markdown.casefold()
    assert "is safe" not in markdown.casefold()


def test_release_notes_update_is_atomic_and_idempotent(tmp_path):
    notes = tmp_path / "RELEASE_NOTES.md"
    notes.write_text("Release details\n", encoding="utf-8")

    update_release_notes(notes, "### VirusTotal analysis\n\nFirst report")
    update_release_notes(notes, "### VirusTotal analysis\n\nReplacement report")

    content = notes.read_text(encoding="utf-8")
    assert content.count(REPORT_START) == 1
    assert content.count(REPORT_END) == 1
    assert "First report" not in content
    assert "Replacement report" in content


def test_missing_key_and_poll_timeout_fail_without_request():
    with pytest.raises(VirusTotalError, match="not configured"):
        VirusTotalClient("  ")

    clock = FakeClock()
    session = FakeSession([])
    client = VirusTotalClient(
        "secret",
        session=session,
        sleep=clock.sleep,
        monotonic=clock.monotonic,
    )
    with pytest.raises(VirusTotalError, match="did not complete"):
        client.wait_for_analysis(
            "analysis-id",
            deadline=20.0,
            poll_interval_seconds=30.0,
        )
    assert session.requests == []


def test_malformed_json_error_does_not_expose_api_key():
    malformed = requests.JSONDecodeError("bad", "x", 0)
    session = FakeSession([FakeResponse(200, malformed)])
    client = VirusTotalClient("super-secret-api-key", session=session)

    with pytest.raises(VirusTotalError) as error:
        client._request_json(
            "GET",
            f"{API_ROOT}/files/upload_url",
            "test request",
            deadline=float("inf"),
        )

    assert "super-secret-api-key" not in str(error.value)

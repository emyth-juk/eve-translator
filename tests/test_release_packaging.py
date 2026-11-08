import json
import zipfile
from pathlib import Path

import pytest

from scripts.package_windows_release import (
    create_windows_release,
    load_release_requirements,
)


ROOT = Path(__file__).resolve().parents[1]


def test_release_dependency_tree_is_exact_and_excludes_retired_scraper():
    pins = load_release_requirements(ROOT)

    assert pins["pyside6-essentials"] == "6.12.0"
    assert "pyside6" not in pins  # meta package pulls in unused WebEngine
    assert pins["requests"] == "2.34.2"
    assert pins["pyinstaller"] == "6.22.3"
    assert pins["certifi"] == "2026.10.23"
    assert "deep-translator" not in pins
    assert "beautifulsoup4" not in pins


def test_release_scan_requirements_are_exact_and_match_runtime_pins():
    runtime_pins = load_release_requirements(ROOT)
    scan_lines = (ROOT / "requirements-release-scan.txt").read_text(
        encoding="utf-8"
    ).splitlines()
    scan_pins = {}
    for line in scan_lines:
        line = line.partition("#")[0].strip()
        if not line:
            continue
        name, version = line.split("==", maxsplit=1)
        scan_pins[name.casefold()] = version

    assert scan_pins
    assert scan_pins == {
        name: runtime_pins[name]
        for name in (
            "requests",
            "charset-normalizer",
            "idna",
            "urllib3",
            "certifi",
        )
    }


def _fake_project(tmp_path: Path) -> tuple[Path, Path]:
    root = tmp_path / "repo"
    (root / "src").mkdir(parents=True)
    (root / "dist").mkdir()
    (root / "LICENSES").mkdir()
    (root / "src" / "version.py").write_text(
        '__version__ = "1.4"\n',
        encoding="utf-8",
    )
    (root / "LICENSE").write_text("project license", encoding="utf-8")
    (root / "THIRD_PARTY_NOTICES.txt").write_text("notices", encoding="utf-8")
    (root / "LICENSES" / "LGPL-3.0-only.txt").write_text(
        "lgpl",
        encoding="utf-8",
    )
    (root / "LICENSES" / "GPL-3.0-only.txt").write_text(
        "gpl",
        encoding="utf-8",
    )
    executable = root / "dist" / "EVETranslator_v1.4.exe"
    executable.write_bytes(b"release executable")
    return root, executable


def test_windows_release_zip_keeps_binary_and_notices_together(tmp_path):
    root, executable = _fake_project(tmp_path)

    def collect_components(_root, licenses_root):
        package_licenses = licenses_root / "THIRD_PARTY" / "example-1.0"
        package_licenses.mkdir(parents=True)
        (package_licenses / "LICENSE.txt").write_text("example", encoding="utf-8")
        return {
            "python": {"name": "Python", "version": "3.15.2"},
            "packages": [{"name": "example", "version": "1.0"}],
        }

    package_root, archive_path = create_windows_release(
        executable,
        "v1.4",
        root=root,
        collect_components=collect_components,
    )

    assert package_root.name == "EVETranslator_v1.4_windows_x64"
    assert archive_path.name == "EVETranslator_v1.4_windows_x64.zip"
    manifest = json.loads(
        (package_root / "BUNDLED_COMPONENTS.json").read_text(encoding="utf-8")
    )
    assert manifest["version"] == "1.4"
    assert manifest["executable"]["filename"] == "EVETranslator_v1.4.exe"
    assert len(manifest["executable"]["sha256"]) == 64
    assert manifest["packages"] == [{"name": "example", "version": "1.0"}]

    prefix = f"{package_root.name}/"
    with zipfile.ZipFile(archive_path) as archive:
        names = set(archive.namelist())
    assert prefix + "EVETranslator_v1.4.exe" in names
    assert prefix + "LICENSE" in names
    assert prefix + "THIRD_PARTY_NOTICES.txt" in names
    assert prefix + "LICENSES/LGPL-3.0-only.txt" in names
    assert prefix + "LICENSES/GPL-3.0-only.txt" in names
    assert prefix + "LICENSES/THIRD_PARTY/example-1.0/LICENSE.txt" in names
    assert prefix + "BUNDLED_COMPONENTS.json" in names
    assert prefix + "SHA256SUMS.txt" in names


def test_windows_release_rejects_wrong_version_and_external_executable(tmp_path):
    root, executable = _fake_project(tmp_path)

    with pytest.raises(ValueError, match="does not match app version"):
        create_windows_release(executable, "1.5", root=root)

    outside = tmp_path / "EVETranslator_v1.4.exe"
    outside.write_bytes(b"outside")
    with pytest.raises(ValueError, match="must stay inside"):
        create_windows_release(outside, "1.4", root=root)

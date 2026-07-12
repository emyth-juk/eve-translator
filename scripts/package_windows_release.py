"""Build a self-contained Windows release ZIP with required notices."""

from __future__ import annotations

import argparse
import hashlib
import importlib.metadata as metadata
import json
import platform
import re
import shutil
import sys
import zipfile
from pathlib import Path
from typing import Callable

from packaging.requirements import InvalidRequirement, Requirement


PROJECT_ROOT = Path(__file__).resolve().parents[1]
VERSION_PATTERN = re.compile(r'^__version__\s*=\s*["\']([^"\']+)["\']\s*$', re.MULTILINE)
PACKAGE_NAME_PATTERN = re.compile(r"[-_.]+")
PYSIDE_DISTRIBUTIONS = {
    "pyside6",
    "pyside6-addons",
    "pyside6-essentials",
    "shiboken6",
}
NOTICE_PREFIXES = ("authors", "copying", "copyright", "license", "notice")


def _normalized_name(name: str) -> str:
    return PACKAGE_NAME_PATTERN.sub("-", name).casefold()


def _project_version(root: Path) -> str:
    version_file = root / "src" / "version.py"
    match = VERSION_PATTERN.search(version_file.read_text(encoding="utf-8"))
    if match is None:
        raise ValueError(f"Could not read app version from {version_file}")
    return match.group(1)


def _load_exact_requirements(path: Path, seen: set[Path] | None = None) -> dict[str, str]:
    """Load one requirements tree and reject release dependency ranges."""

    resolved = path.resolve()
    visited = seen if seen is not None else set()
    if resolved in visited:
        return {}
    visited.add(resolved)

    pins: dict[str, str] = {}
    for raw_line in resolved.read_text(encoding="utf-8").splitlines():
        line = raw_line.partition("#")[0].strip()
        if not line:
            continue
        if line.startswith("-r "):
            included = (resolved.parent / line[3:].strip()).resolve()
            pins.update(_load_exact_requirements(included, visited))
            continue
        try:
            requirement = Requirement(line)
        except InvalidRequirement as exc:
            raise ValueError(f"Unsupported release dependency pin: {line}") from exc
        if requirement.marker is not None and not requirement.marker.evaluate():
            continue
        specifiers = list(requirement.specifier)
        if (
            requirement.url is not None
            or requirement.extras
            or len(specifiers) != 1
            or specifiers[0].operator != "=="
            or "*" in specifiers[0].version
        ):
            raise ValueError(f"Release dependency is not exactly pinned: {line}")
        name = requirement.name
        version = specifiers[0].version
        normalized = _normalized_name(name)
        previous = pins.get(normalized)
        if previous is not None and previous != version:
            raise ValueError(f"Conflicting pins for {name}: {previous} and {version}")
        pins[normalized] = version
    return pins


def load_release_requirements(root: Path = PROJECT_ROOT) -> dict[str, str]:
    """Return the exact runtime and PyInstaller versions used by the BAT/CI build."""

    return _load_exact_requirements(root / "requirements-build.txt")


def _metadata_url(distribution: metadata.Distribution) -> str:
    project_urls = distribution.metadata.get_all("Project-URL") or []
    for entry in project_urls:
        label, separator, url = entry.partition(",")
        if separator and label.strip().casefold() in {
            "homepage",
            "repository",
            "source",
            "source code",
        }:
            return url.strip()
    return (distribution.metadata.get("Home-page") or "").strip()


def _license_expression(distribution: metadata.Distribution) -> str:
    return (
        distribution.metadata.get("License-Expression")
        or distribution.metadata.get("License")
        or "See copied license files"
    ).strip()


def _is_notice_file(relative_path: Path) -> bool:
    name = relative_path.name.casefold()
    return name.startswith(NOTICE_PREFIXES)


def _copy_distribution_notices(
    distribution: metadata.Distribution,
    destination: Path,
) -> list[str]:
    copied: list[str] = []
    for relative in distribution.files or ():
        relative_path = Path(str(relative))
        if not _is_notice_file(relative_path):
            continue
        if relative_path.name.casefold() == "licenseref-qt-commercial.txt":
            continue
        source = Path(distribution.locate_file(relative))
        if not source.is_file():
            continue
        destination.mkdir(parents=True, exist_ok=True)
        target = destination / relative_path.name
        suffix = 2
        while target.exists():
            target = destination / f"{relative_path.stem}-{suffix}{relative_path.suffix}"
            suffix += 1
        shutil.copy2(source, target)
        copied.append(target.name)
    return copied


def _python_license() -> Path:
    candidates = (
        Path(sys.base_prefix) / "LICENSE.txt",
        Path(sys.base_prefix) / "LICENSE",
        Path(sys.executable).resolve().parent / "LICENSE.txt",
    )
    for candidate in candidates:
        if candidate.is_file():
            return candidate
    raise FileNotFoundError("The Python distribution license was not found")


def collect_bundled_components(root: Path, licenses_root: Path) -> dict:
    """Copy exact installed notices and return a machine-readable component list."""

    pins = load_release_requirements(root)
    third_party_root = licenses_root / "THIRD_PARTY"
    packages: list[dict] = []

    for normalized_name, expected_version in sorted(pins.items()):
        distribution = metadata.distribution(normalized_name)
        actual_version = distribution.version
        if actual_version != expected_version:
            raise RuntimeError(
                f"Installed {normalized_name} {actual_version} does not match "
                f"the release pin {expected_version}"
            )
        package_folder = third_party_root / f"{normalized_name}-{actual_version}"
        copied = _copy_distribution_notices(distribution, package_folder)
        if not copied and normalized_name not in PYSIDE_DISTRIBUTIONS:
            raise RuntimeError(f"No license or notice files found for {normalized_name}")
        license_files = [
            f"LICENSES/THIRD_PARTY/{package_folder.name}/{name}" for name in copied
        ]
        if normalized_name in PYSIDE_DISTRIBUTIONS:
            license_files.extend(
                ("LICENSES/LGPL-3.0-only.txt", "LICENSES/GPL-3.0-only.txt")
            )
        packages.append(
            {
                "name": distribution.metadata.get("Name") or normalized_name,
                "version": actual_version,
                "license": _license_expression(distribution),
                "homepage": _metadata_url(distribution),
                "license_files": license_files,
            }
        )

    python_version = platform.python_version()
    python_folder = third_party_root / f"python-{python_version}"
    python_folder.mkdir(parents=True, exist_ok=True)
    shutil.copy2(_python_license(), python_folder / "LICENSE.txt")

    return {
        "python": {
            "name": "Python",
            "version": python_version,
            "license": "PSF-2.0",
            "homepage": "https://www.python.org/",
            "license_files": [
                f"LICENSES/THIRD_PARTY/{python_folder.name}/LICENSE.txt"
            ],
        },
        "packages": packages,
    }


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _require_direct_child(path: Path, parent: Path, label: str) -> None:
    try:
        relative = path.relative_to(parent)
    except ValueError as exc:
        raise ValueError(f"{label} must stay inside {parent}") from exc
    if len(relative.parts) != 1:
        raise ValueError(f"{label} must be a direct child of {parent}")


def create_windows_release(
    executable: Path,
    version: str,
    *,
    root: Path = PROJECT_ROOT,
    collect_components: Callable[[Path, Path], dict] = collect_bundled_components,
) -> tuple[Path, Path]:
    """Create the unpacked release folder and ZIP, returning both paths."""

    root = root.resolve()
    normalized_version = version.removeprefix("v")
    if normalized_version != _project_version(root):
        raise ValueError(
            f"Requested version {version!r} does not match app version "
            f"{_project_version(root)!r}"
        )

    dist = (root / "dist").resolve()
    executable = executable.resolve()
    _require_direct_child(executable, dist, "Executable")
    expected_name = f"EVETranslator_v{normalized_version}.exe"
    if executable.name != expected_name or not executable.is_file():
        raise FileNotFoundError(f"Expected built executable {dist / expected_name}")

    package_name = f"EVETranslator_v{normalized_version}_windows_x64"
    package_root = (dist / package_name).resolve()
    archive_path = (dist / f"{package_name}.zip").resolve()
    _require_direct_child(package_root, dist, "Package directory")
    _require_direct_child(archive_path, dist, "Release archive")

    if package_root.exists():
        shutil.rmtree(package_root)
    if archive_path.exists():
        archive_path.unlink()
    package_root.mkdir(parents=True)

    packaged_executable = package_root / expected_name
    shutil.copy2(executable, packaged_executable)
    for filename in ("LICENSE", "THIRD_PARTY_NOTICES.txt"):
        shutil.copy2(root / filename, package_root / filename)

    licenses_root = package_root / "LICENSES"
    licenses_root.mkdir()
    for filename in ("LGPL-3.0-only.txt", "GPL-3.0-only.txt"):
        shutil.copy2(root / "LICENSES" / filename, licenses_root / filename)

    components = collect_components(root, licenses_root)
    executable_hash = _sha256(packaged_executable)
    manifest = {
        "application": "EVE Translator",
        "version": normalized_version,
        "platform": "windows_x64",
        "executable": {
            "filename": expected_name,
            "sha256": executable_hash,
        },
        **components,
    }
    (package_root / "BUNDLED_COMPONENTS.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    (package_root / "SHA256SUMS.txt").write_text(
        f"{executable_hash}  {expected_name}\n",
        encoding="ascii",
    )

    with zipfile.ZipFile(
        archive_path,
        "w",
        compression=zipfile.ZIP_DEFLATED,
        compresslevel=9,
    ) as archive:
        for path in sorted(package_root.rglob("*")):
            if path.is_file():
                archive.write(path, path.relative_to(dist))
    return package_root, archive_path


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--executable", required=True, type=Path)
    parser.add_argument("--version", required=True)
    return parser.parse_args()


def main() -> int:
    args = _parse_args()
    if sys.platform != "win32" or platform.machine().casefold() not in {
        "amd64",
        "x86_64",
    }:
        raise RuntimeError("Windows x64 release packaging requires 64-bit Windows")
    package_root, archive_path = create_windows_release(
        args.executable,
        args.version,
    )
    print(f"Release folder: {package_root}")
    print(f"Release archive: {archive_path}")
    print(f"Archive SHA-256: {_sha256(archive_path)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

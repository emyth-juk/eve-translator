from pathlib import Path

from src.version import __version__


ROOT = Path(__file__).resolve().parents[1]


def test_windows_build_metadata_matches_app_version_and_is_wired_everywhere():
    metadata = (ROOT / "packaging" / "windows_version_info.txt").read_text(
        encoding="utf-8"
    )
    numeric = tuple(int(part) for part in __version__.split(".")) + (0, 0)
    four_part = numeric[:4]

    assert f"filevers={four_part}" in metadata
    assert f"prodvers={four_part}" in metadata
    assert f"StringStruct('FileVersion', '{__version__}')" in metadata
    assert f"StringStruct('ProductVersion', '{__version__}')" in metadata

    build_script = (ROOT / "build_exe.bat").read_text(encoding="utf-8")
    release_workflow = (
        ROOT / ".github" / "workflows" / "release.yml"
    ).read_text(encoding="utf-8")
    assert '--version-file "packaging\\windows_version_info.txt"' in build_script
    assert '--version-file="packaging\\windows_version_info.txt"' in release_workflow


def test_release_validates_tag_and_runs_quality_checks_before_build():
    release_workflow = (
        ROOT / ".github" / "workflows" / "release.yml"
    ).read_text(encoding="utf-8")

    validate_position = release_workflow.index("- name: Validate Tag Matches App Version")
    checks_position = release_workflow.index("- name: Run Release Checks")
    build_position = release_workflow.index("- name: Build Executable")

    assert "RELEASE_TAG: ${{ github.ref_name }}" in release_workflow
    assert 'expected_tag = f"v{__version__}"' in release_workflow
    assert "-r requirements-build.txt -r requirements-dev.txt" in release_workflow
    assert "python -m ruff check src tests scripts" in release_workflow
    assert "python -m pytest -q" in release_workflow
    assert validate_position < checks_position < build_position


def test_development_requirements_are_exactly_pinned():
    requirements = (ROOT / "requirements-dev.txt").read_text(
        encoding="utf-8"
    ).splitlines()

    packages = [
        line.strip()
        for line in requirements
        if line.strip() and not line.lstrip().startswith(("#", "-r "))
    ]
    assert packages
    assert all("==" in package for package in packages)

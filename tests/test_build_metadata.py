import re
import runpy
from pathlib import Path

from src.version import __version__


ROOT = Path(__file__).resolve().parents[1]


def test_current_version_has_a_dated_top_level_changelog_section():
    changelog = (ROOT / "CHANGELOG.md").read_text(encoding="utf-8")
    headings = re.findall(
        r"^## \[([^]]+)] - (\d{4}-\d{2}-\d{2})$",
        changelog,
        flags=re.MULTILINE,
    )

    assert headings
    assert headings[0] == (__version__, "2026-07-12")


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
    assert '--additional-hooks-dir "packaging\\hooks"' in build_script
    assert '--additional-hooks-dir="packaging\\hooks"' in release_workflow
    assert "python -m pip install --upgrade pip==26.3" in build_script
    for excluded in (
        "PySide6.QtNetwork",
        "setuptools",
        "packaging",
        "wheel",
        "typing_extensions",
    ):
        assert f'--exclude-module "{excluded}"' in build_script
        assert f'--exclude-module "{excluded}"' in release_workflow


def test_release_validates_tag_and_runs_quality_checks_before_build():
    release_workflow = (
        ROOT / ".github" / "workflows" / "release.yml"
    ).read_text(encoding="utf-8")

    validate_position = release_workflow.index("- name: Validate Release Tag and Commit")
    checks_position = release_workflow.index("- name: Run Release Checks")
    build_position = release_workflow.index("- name: Build Executable")

    assert "RELEASE_TAG: ${{ github.ref_name }}" in release_workflow
    assert "RELEASE_COMMIT: ${{ github.sha }}" in release_workflow
    assert 'expected_tag = f"v{__version__}"' in release_workflow
    assert '"--is-ancestor"' in release_workflow
    assert '"refs/remotes/origin/main"' in release_workflow
    assert "-r requirements-build.txt -r requirements-dev.txt" in release_workflow
    assert "python -m ruff check src tests scripts packaging" in release_workflow
    assert "python -m pytest -q" in release_workflow
    assert "python -m pip install --upgrade pip==26.3" in release_workflow
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


def test_qt_gui_hook_collects_only_the_required_windows_plugin():
    hook_path = ROOT / "packaging" / "hooks" / "hook-PySide6.QtGui.py"
    namespace = runpy.run_path(str(hook_path))

    binaries = namespace["binaries"]
    assert binaries
    plugin_names = {
        Path(str(binary[0]).replace("\\", "/")).name.casefold()
        for binary in binaries
        if "/plugins/" in str(binary[0]).replace("\\", "/").casefold()
    }
    assert plugin_names == {"qwindows.dll"}


def test_workflows_pin_third_party_actions_to_reviewed_commits():
    workflows = "\n".join(
        path.read_text(encoding="utf-8")
        for path in (
            ROOT / ".github" / "workflows" / "run_tests.yml",
            ROOT / ".github" / "workflows" / "release.yml",
        )
    )

    assert "actions/checkout@de0fac2e4500dabe0009e67214ff5f5447ce83dd" in workflows
    assert "actions/setup-python@a309ff8b426b58ec0e2a45f0f869d46889d02405" in workflows
    assert "actions/upload-artifact@043fb46d1a93c77aae656e7c1c64a875d1fc6a0a" in workflows
    assert "actions/download-artifact@3e5f45b2cfb9172054b4087a40e8e0b5a5461e7c" in workflows
    assert "softprops/action-gh-release@718ea10b132b3b2eba29c1007bb80653f286566b" in workflows

    test_workflow = (
        ROOT / ".github" / "workflows" / "run_tests.yml"
    ).read_text(encoding="utf-8")
    trigger_block, permissions_block = test_workflow.split("permissions:", 1)
    assert "pull_request:" in trigger_block
    assert "contents: read" in permissions_block


def test_release_publishes_one_zip_with_project_and_third_party_notices():
    release_workflow = (
        ROOT / ".github" / "workflows" / "release.yml"
    ).read_text(encoding="utf-8")
    build_script = (ROOT / "build_exe.bat").read_text(encoding="utf-8")
    notices = (ROOT / "THIRD_PARTY_NOTICES.txt").read_text(encoding="utf-8")

    archive = "release-build/dist/EVETranslator_${{ github.ref_name }}_windows_x64.zip"
    assert f"          {archive}\n" in release_workflow
    assert "Package Windows Release" in release_workflow
    assert "scripts/package_windows_release.py" in release_workflow
    assert "scripts\\package_windows_release.py" in build_script
    assert "_windows_x64.zip" in build_script
    assert "          LICENSE\n" not in release_workflow
    assert "          THIRD_PARTY_NOTICES.txt\n" not in release_workflow

    assert "PySide6" in notices
    assert "LGPLv3" in notices
    assert "Qt Virtual Keyboard" in notices
    assert '© 2014 CCP hf. All rights reserved.' in notices
    assert "LICENSES/LGPL-3.0-only.txt" in notices
    assert "LICENSES/GPL-3.0-only.txt" in notices
    assert "licenses-used-in-qt.html" in notices
    assert (ROOT / "LICENSES" / "LGPL-3.0-only.txt").is_file()
    assert (ROOT / "LICENSES" / "GPL-3.0-only.txt").is_file()


def test_virustotal_release_scan_is_secret_scoped_and_gates_publication():
    release_workflow = (
        ROOT / ".github" / "workflows" / "release.yml"
    ).read_text(encoding="utf-8")

    build_start = release_workflow.index("  build:")
    scan_start = release_workflow.index("  scan:")
    publish_start = release_workflow.index("  publish:")
    build_job = release_workflow[build_start:scan_start]
    scan_job = release_workflow[scan_start:publish_start]
    publish_job = release_workflow[publish_start:]

    assert "permissions: {}" in release_workflow[:build_start]
    assert "contents: read" in build_job
    assert "contents: write" not in build_job
    assert "environment:\n      name: virustotal-release" in scan_job
    assert "VIRUSTOTAL_API_KEY: ${{ secrets.VIRUSTOTAL_API_KEY }}" in scan_job
    assert "VIRUSTOTAL_API_KEY" not in build_job
    assert "VIRUSTOTAL_API_KEY" not in publish_job
    assert "contents: write" not in scan_job
    assert "contents: write" in publish_job
    assert "persist-credentials: false" in build_job
    assert "persist-credentials: false" in scan_job

    assert "scripts/scan_virustotal.py" in scan_job
    assert "--manifest" in scan_job
    assert "--github-output" in scan_job
    assert "--github-summary" in scan_job
    assert "timeout-minutes: 20" in scan_job
    assert "needs: build" in scan_job
    assert "needs: scan" in publish_job
    assert "draft: ${{ needs.scan.outputs.has_detections != 'false' }}" in publish_job

    assert build_start < scan_start < publish_start

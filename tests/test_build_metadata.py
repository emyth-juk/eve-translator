import os
import re
import runpy
import shutil
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

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


def _release_workflow_step(job: str, name: str) -> dict:
    workflow = yaml.safe_load(
        (ROOT / ".github" / "workflows" / "release.yml").read_text(encoding="utf-8")
    )
    for step in workflow["jobs"][job]["steps"]:
        if step.get("name") == name:
            return step
    raise AssertionError(f"release.yml job {job!r} has no step named {name!r}")


def _run_python_step_like_github(
    step: dict, workspace: Path, env: dict[str, str]
) -> subprocess.CompletedProcess[str]:
    """Run a ``shell: python`` step the way GitHub Actions does.

    The runner writes the step body to a temporary file outside the checkout and
    runs ``python <file>`` from the workspace, so the checkout is the working
    directory but is not on ``sys.path``.
    """
    assert step.get("shell") == "python"
    script = workspace.parent / "github_step.py"
    script.write_text(step["run"], encoding="utf-8")
    step_env = {key: value for key, value in os.environ.items() if key != "PYTHONPATH"}
    step_env.update(env)
    return subprocess.run(
        [sys.executable, str(script)],
        cwd=workspace,
        env=step_env,
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )


def test_release_tag_validation_step_runs_the_way_github_executes_it(tmp_path):
    git = shutil.which("git")
    if git is None:
        pytest.skip("git is required to exercise the release ancestry check")

    workspace = tmp_path / "workspace"
    (workspace / "src").mkdir(parents=True)
    (workspace / "src" / "__init__.py").write_text("", encoding="utf-8")
    (workspace / "src" / "version.py").write_text(
        f'__version__ = "{__version__}"\n', encoding="utf-8"
    )
    # Keep the throwaway repository independent of the developer's git config.
    empty_config = tmp_path / "empty.gitconfig"
    empty_config.write_text("", encoding="utf-8")
    git_env = {
        **os.environ,
        "GIT_CONFIG_GLOBAL": str(empty_config),
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_AUTHOR_NAME": "Release Test",
        "GIT_AUTHOR_EMAIL": "release@example.invalid",
        "GIT_COMMITTER_NAME": "Release Test",
        "GIT_COMMITTER_EMAIL": "release@example.invalid",
    }

    def run_git(*args: str) -> str:
        return subprocess.run(
            [git, *args],
            cwd=workspace,
            env=git_env,
            capture_output=True,
            text=True,
            check=True,
        ).stdout.strip()

    run_git("init", "-q")
    run_git("add", ".")
    run_git("commit", "-q", "-m", "release")
    on_main = run_git("rev-parse", "HEAD")
    run_git("update-ref", "refs/remotes/origin/main", on_main)
    run_git("commit", "-q", "--allow-empty", "-m", "not on main")
    off_main = run_git("rev-parse", "HEAD")

    step = _release_workflow_step("build", "Validate Release Tag and Commit")
    expected_tag = f"v{__version__}"

    def validate(tag: str, commit: str) -> subprocess.CompletedProcess[str]:
        return _run_python_step_like_github(
            step, workspace, {"RELEASE_TAG": tag, "RELEASE_COMMIT": commit}
        )

    released = validate(expected_tag, on_main)
    assert released.returncode == 0, released.stderr

    wrong_tag = validate("v0.0.0", on_main)
    assert wrong_tag.returncode != 0
    assert f"does not match app version {expected_tag!r}" in wrong_tag.stderr

    unmerged = validate(expected_tag, off_main)
    assert unmerged.returncode != 0
    assert "reachable from protected main" in unmerged.stderr


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

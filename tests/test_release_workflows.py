"""Release workflows are pinned, least-privilege, and dry-run safe."""

from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).parents[1]
WORKFLOWS = ROOT / ".github" / "workflows"
SHA_PIN = re.compile(r"^\s*uses:\s+[^@\s]+@[0-9a-f]{40}(?:\s+#.*)?$")


def test_every_workflow_action_is_sha_pinned_and_checkout_drops_credentials() -> None:
    """Mutable action tags and persisted checkout credentials never enter CI."""
    for workflow in WORKFLOWS.glob("*.yml"):
        lines = workflow.read_text().splitlines()
        uses = [line for line in lines if line.lstrip().startswith("uses:")]
        assert uses, workflow
        assert all(SHA_PIN.fullmatch(line) for line in uses), workflow
        for index, line in enumerate(lines):
            if "actions/checkout@" in line:
                following = "\n".join(lines[index : index + 6])
                assert "persist-credentials: false" in following, workflow


def test_release_workflow_builds_once_and_cannot_publish_a_manual_run() -> None:
    """Only a protected version-tag run can reach irreversible publication."""
    workflow = (WORKFLOWS / "release.yml").read_text()

    assert "workflow_dispatch:" in workflow
    assert "tags:" in workflow
    assert '- "v*"' in workflow
    assert "uv build --no-sources --sdist" in workflow
    assert '"dist/cqmgr-${VERSION}.tar.gz"' in workflow
    assert "scripts/release_qualification.py prepare" in workflow
    assert "scripts/release_qualification.py verify" in workflow
    assert "github.event_name == 'push'" in workflow
    assert "startsWith(github.ref, 'refs/tags/v')" in workflow
    assert "environment: pypi" in workflow
    assert "id-token: write" in workflow
    assert "attestations: write" in workflow
    assert "pypa/gh-action-pypi-publish@ba38be9e461d3875417946c167d0b5f3d385a247" in (
        workflow
    )
    assert "actions/attest@f7c74d28b9d84cb8768d0b8ca14a4bac6ef463e6" in workflow
    assert "PYPI_TOKEN" not in workflow
    assert "password:" not in workflow


def test_release_workflow_qualifies_resolutions_platforms_and_exact_install() -> None:
    """Cover dependency, Python, OS, architecture, and installed-artifact gates."""
    workflow = (WORKFLOWS / "release.yml").read_text()

    for resolution in ("locked", "lowest-direct", "fresh"):
        assert resolution in workflow
    for python in ("3.12", "3.13", "3.14"):
        assert f'- "{python}"' in workflow
    for runner in (
        "macos-14",
        "macos-15-intel",
        "ubuntu-22.04",
        "ubuntu-24.04",
        "ubuntu-24.04-arm",
        "windows-2022",
        "windows-2025",
    ):
        assert runner in workflow
    assert "scripts/smoke_tool_install.py" in workflow
    assert "pip-audit" in workflow
    assert "mutmut run" in workflow
    assert "SHA256SUMS" in workflow
    assert "uv tool install" in workflow
    assert "cqmgr==${VERSION}" in workflow


def test_release_workflow_qualifies_each_supported_terminal_shell() -> None:
    """Installed artifacts run under the representative native shell per OS."""
    workflow = (WORKFLOWS / "release.yml").read_text()

    assert "name: Smoke macOS artifacts from zsh" in workflow
    assert "if: runner.os == 'macOS'" in workflow
    assert "shell: zsh {0}" in workflow
    assert "name: Smoke Windows artifacts from PowerShell" in workflow
    assert "if: runner.os == 'Windows'" in workflow
    assert "shell: pwsh" in workflow
    assert "name: Smoke Linux artifacts from bash with Secret Service" in workflow
    assert "if: runner.os == 'Linux'" in workflow
    assert "shell: bash" in workflow


def test_live_read_only_workflow_has_separate_identity_and_environment_gate() -> None:
    """Use keyless cloud identity without entering release publication."""
    workflow = (WORKFLOWS / "live-read-only.yml").read_text()

    assert "environment: live-read-only" in workflow
    assert "id-token: write" in workflow
    assert "google-github-actions/auth@7c6bc770dae815cd3e89ee6cdf493a5fab2cc093" in (
        workflow
    )
    assert "scripts/live_read_only_canary.py" in workflow
    assert "GCP_PROJECT_ID" in workflow
    assert "GCP_WORKLOAD_IDENTITY_PROVIDER" in workflow
    assert "GCP_SERVICE_ACCOUNT" in workflow
    assert "create" not in workflow.lower()
    assert "update" not in workflow.lower()
    assert "delete" not in workflow.lower()
    assert "validateonly" not in workflow.lower()


def test_release_publication_requires_the_exact_commit_live_canary() -> None:
    """Publication cannot outpace the protected exact-commit provider evidence."""
    workflow = (WORKFLOWS / "release.yml").read_text()

    assert "live-read-only:" in workflow
    assert "name: Exact-commit bounded provider reads" in workflow
    assert "environment: live-read-only" in workflow
    assert "python scripts/live_read_only_canary.py" in workflow
    assert "name: release-live-read-only-evidence" in workflow
    assert "      - live-read-only\n" in workflow

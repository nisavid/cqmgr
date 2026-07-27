"""Release workflows are pinned, least-privilege, and dry-run safe."""

from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).parents[1]
WORKFLOWS = ROOT / ".github" / "workflows"
SHA_PIN = re.compile(r"^\s*uses:\s+[^@\s]+@[0-9a-f]{40}(?:\s+#.*)?$")
CHECKOUT_USE = re.compile(r"^\s+uses:\s+actions/checkout@")


def _yaml_block(document: str, header: str, *, indent: int) -> str:
    """Return one indentation-delimited mapping block from a workflow."""
    lines = document.splitlines()
    expected = f"{' ' * indent}{header}:"
    starts = [index for index, line in enumerate(lines) if line == expected]
    assert len(starts) == 1, f"expected one {expected!r}, found {len(starts)}"
    start = starts[0]
    end = len(lines)
    for index, line in enumerate(lines[start + 1 :], start=start + 1):
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        line_indent = len(line) - len(line.lstrip())
        if line_indent <= indent:
            end = index
            break
    return "\n".join(lines[start:end])


def _yaml_list_item_blocks(document: str, *, indent: int) -> list[str]:
    """Return list items and their nested content at one indentation level."""
    lines = document.splitlines()
    starts = [
        index
        for index, line in enumerate(lines)
        if len(line) - len(line.lstrip()) == indent and line.lstrip().startswith("- ")
    ]
    blocks: list[str] = []
    for start in starts:
        end = len(lines)
        for index, line in enumerate(lines[start + 1 :], start=start + 1):
            if not line.strip() or line.lstrip().startswith("#"):
                continue
            line_indent = len(line) - len(line.lstrip())
            if line_indent <= indent:
                end = index
                break
        blocks.append("\n".join(lines[start:end]))
    return blocks


def _yaml_list_values(document: str, *, indent: int) -> set[str]:
    """Return scalar values from list items at one indentation level."""
    return {
        block.splitlines()[0].lstrip()[2:]
        for block in _yaml_list_item_blocks(document, indent=indent)
    }


def _workflow_job(workflow: str, name: str) -> str:
    return _yaml_block(workflow, name, indent=2)


def _job_step(job: str, name: str) -> str:
    expected = f"{' ' * 6}- name: {name}"
    matches = [
        block
        for block in _yaml_list_item_blocks(job, indent=6)
        if block.splitlines()[0] == expected
    ]
    assert len(matches) == 1, f"expected one step named {name!r}, found {len(matches)}"
    return matches[0]


def test_every_workflow_action_is_sha_pinned_and_checkout_drops_credentials() -> None:
    """Mutable action tags and persisted checkout credentials never enter CI."""
    workflows = [*WORKFLOWS.glob("*.yml"), *WORKFLOWS.glob("*.yaml")]
    assert workflows
    for workflow in workflows:
        lines = workflow.read_text().splitlines()
        uses = [line for line in lines if line.lstrip().startswith("uses:")]
        assert uses, workflow
        assert all(
            SHA_PIN.fullmatch(line) or "uses: ./.github/workflows/" in line
            for line in uses
        ), workflow
        checkout_steps = [
            block
            for block in _yaml_list_item_blocks(workflow.read_text(), indent=6)
            if any(CHECKOUT_USE.match(line) for line in block.splitlines())
        ]
        assert len(checkout_steps) == sum(
            CHECKOUT_USE.match(line) is not None for line in lines
        ), workflow
        for checkout_step in checkout_steps:
            assert re.search(r"(?m)^\s+with:\s*$", checkout_step), checkout_step
            assert re.search(
                r"(?m)^\s+persist-credentials:\s+false\s*$", checkout_step
            ), checkout_step


def test_release_workflow_builds_once_and_cannot_publish_a_manual_run() -> None:
    """Only a protected version-tag run can reach irreversible publication."""
    workflow = (WORKFLOWS / "release.yml").read_text()
    triggers = _yaml_block(workflow, "on", indent=0)
    build = _workflow_job(workflow, "build")
    publish = _workflow_job(workflow, "publish")

    assert "workflow_dispatch:" in triggers
    assert "tags:" in triggers
    assert '- "v*"' in triggers
    assert workflow.count("uv build --no-sources --sdist") == 1
    assert "uv build --no-sources --sdist" in build
    assert '"dist/cqmgr-${VERSION}.tar.gz"' in build
    assert "scripts/release_qualification.py prepare" in build
    assert "scripts/release_qualification.py verify" in build
    assert "github.event_name == 'push'" in publish
    assert "startsWith(github.ref, 'refs/tags/v')" in publish
    assert "environment: pypi" in publish
    assert "id-token: write" in publish
    assert "attestations: write" in publish
    assert "pypa/gh-action-pypi-publish@ba38be9e461d3875417946c167d0b5f3d385a247" in (
        publish
    )
    assert "actions/attest@f7c74d28b9d84cb8768d0b8ca14a4bac6ef463e6" in publish
    assert "PYPI_TOKEN" not in workflow
    assert "password:" not in workflow
    assert "scripts/publication_preflight.py pypi" in publish
    assert "steps.pypi-preflight.outputs.publish == 'true'" in publish
    assert "scripts/publication_preflight.py github" in publish
    assert "steps.github-preflight.outputs.publish == 'true'" in publish
    assert "skip-existing" not in workflow


def test_release_workflow_qualifies_resolutions_platforms_and_exact_install() -> None:
    """Cover dependency, Python, OS, architecture, and installed-artifact gates."""
    workflow = (WORKFLOWS / "release.yml").read_text()
    build = _workflow_job(workflow, "build")
    resolutions = _workflow_job(workflow, "dependency-resolutions")
    installed = _workflow_job(workflow, "installed-artifacts")
    deep_tests = _workflow_job(workflow, "deep-tests")
    post_publication = _workflow_job(workflow, "post-publication")

    for resolution in ("locked", "lowest-direct", "fresh"):
        assert resolution in resolutions
    for python in ("3.12", "3.13", "3.14"):
        assert f'- "{python}"' in installed
    for runner in (
        "macos-14",
        "macos-15-intel",
        "ubuntu-22.04",
        "ubuntu-24.04",
        "ubuntu-24.04-arm",
        "windows-2022",
        "windows-2025",
    ):
        assert runner in installed
    assert "scripts/smoke_tool_install.py" in installed
    assert "pip-audit" in build
    assert "mutmut run" in deep_tests
    assert "SHA256SUMS" in build
    assert "uv tool install" in post_publication
    assert "cqmgr==${VERSION}" in post_publication


def test_release_workflow_qualifies_each_supported_terminal_shell() -> None:
    """Installed artifacts run under the representative native shell per OS."""
    workflow = (WORKFLOWS / "release.yml").read_text()
    installed = _workflow_job(workflow, "installed-artifacts")
    runner_matrix = _yaml_block(installed, "runner", indent=8)

    assert _yaml_list_values(runner_matrix, indent=10) == {
        "macos-14",
        "macos-15-intel",
        "ubuntu-22.04",
        "ubuntu-24.04",
        "ubuntu-24.04-arm",
        "windows-2022",
        "windows-2025",
    }

    macos = _job_step(installed, "Smoke macOS artifacts from zsh")
    assert "if: runner.os == 'macOS'" in macos
    assert "shell: zsh {0}" in macos
    windows = _job_step(installed, "Smoke Windows artifacts from PowerShell")
    assert "if: runner.os == 'Windows'" in windows
    assert "shell: pwsh" in windows
    linux = _job_step(installed, "Smoke Linux artifacts from bash with Secret Service")
    assert "if: runner.os == 'Linux'" in linux
    assert "shell: bash" in linux


def test_live_read_only_workflow_has_separate_identity_and_environment_gate() -> None:
    """Use keyless cloud identity without entering release publication."""
    workflow = (WORKFLOWS / "live-read-only.yml").read_text()
    triggers = _yaml_block(workflow, "on", indent=0)
    canary = _workflow_job(workflow, "canary")

    assert "workflow_call:" in triggers
    assert "environment: live-read-only" in canary
    assert "id-token: write" in canary
    assert "google-github-actions/auth@7c6bc770dae815cd3e89ee6cdf493a5fab2cc093" in (
        canary
    )
    assert "scripts/live_read_only_canary.py" in canary
    assert "GCP_PROJECT_ID" in canary
    assert "GCP_WORKLOAD_IDENTITY_PROVIDER" in canary
    assert "GCP_SERVICE_ACCOUNT" in canary
    assert "create" not in canary.lower()
    assert "update" not in canary.lower()
    assert "delete" not in canary.lower()
    assert "validateonly" not in canary.lower()


def test_release_publication_requires_the_exact_commit_live_canary() -> None:
    """Publication cannot outpace the protected exact-commit provider evidence."""
    workflow = (WORKFLOWS / "release.yml").read_text()
    live_read_only = _workflow_job(workflow, "live-read-only")
    publish = _workflow_job(workflow, "publish")
    publish_needs = _yaml_block(publish, "needs", indent=4)

    assert "needs: build" in live_read_only
    assert "uses: ./.github/workflows/live-read-only.yml" in live_read_only
    assert "id-token: write" in live_read_only
    assert "live-read-only" in _yaml_list_values(publish_needs, indent=6)

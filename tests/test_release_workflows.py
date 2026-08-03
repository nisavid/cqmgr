"""Release workflows are pinned, least-privilege, and dry-run safe."""

from __future__ import annotations

import re
from pathlib import Path
from typing import cast

import yaml

ROOT = Path(__file__).parents[1]
WORKFLOWS = ROOT / ".github" / "workflows"
SHA_PIN = re.compile(r"[^@\s]+@[0-9a-f]{40}\Z")


def _mapping(value: object, context: str) -> dict[str, object]:
    """Require one string-keyed YAML mapping."""
    assert isinstance(value, dict), context
    assert all(isinstance(key, str) for key in value), context
    return cast("dict[str, object]", value)


def _workflow_actions(
    document: str,
) -> tuple[tuple[str, dict[str, object] | None], ...]:
    """Return only structural reusable-job and step action references."""
    workflow_value = yaml.safe_load(document)
    assert isinstance(workflow_value, dict), "workflow"
    workflow = cast("dict[object, object]", workflow_value)
    jobs = _mapping(workflow.get("jobs"), "jobs")
    actions: list[tuple[str, dict[str, object] | None]] = []
    for job_value in jobs.values():
        job = _mapping(job_value, "job")
        job_use = job.get("uses")
        if job_use is not None:
            assert isinstance(job_use, str), "reusable workflow reference"
            actions.append((job_use, None))
        steps = job.get("steps", [])
        assert isinstance(steps, list), "job steps"
        for step_value in steps:
            step = _mapping(step_value, "step")
            step_use = step.get("uses")
            if step_use is not None:
                assert isinstance(step_use, str), "step action reference"
                actions.append((step_use, step))
    return tuple(actions)


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


def _yaml_mapping_keys(document: str, *, indent: int) -> set[str]:
    """Return mapping keys declared at one indentation level."""
    return {
        line.strip()[:-1]
        for line in document.splitlines()
        if len(line) - len(line.lstrip()) == indent and line.strip().endswith(":")
    }


def _workflow_job(workflow: str, name: str) -> str:
    return _yaml_block(workflow, name, indent=2)


def _workflow_job_mapping(workflow: str, name: str) -> dict[str, object]:
    """Return one parsed workflow job mapping."""
    workflow_value = yaml.safe_load(workflow)
    assert isinstance(workflow_value, dict), "workflow"
    document = cast("dict[object, object]", workflow_value)
    jobs = _mapping(document.get("jobs"), "jobs")
    return _mapping(jobs.get(name), f"job {name}")


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
        actions = _workflow_actions(workflow.read_text(encoding="utf-8"))
        assert actions, workflow
        assert all(
            SHA_PIN.fullmatch(reference) or reference.startswith("./.github/workflows/")
            for reference, _step in actions
        ), workflow
        checkout_steps = [
            step
            for reference, step in actions
            if reference.startswith("actions/checkout@") and step is not None
        ]
        assert checkout_steps, workflow
        for checkout_step in checkout_steps:
            inputs = _mapping(checkout_step.get("with"), "checkout inputs")
            assert inputs.get("persist-credentials") is False, checkout_step


def test_workflow_action_parser_ignores_block_scalar_payload() -> None:
    """Comments and scalar payload cannot masquerade as workflow actions."""
    document = """
# run: |
#   uses: attacker/comment@mutable
jobs:
  example:
    runs-on: ubuntu-22.04
    steps:
      - run: |
          uses: attacker/example@mutable
          echo retained
        env:
          SIBLING: retained
      - run: >-
          uses: attacker/folded@mutable
      - uses: actions/checkout@3d3c42e5aac5ba805825da76410c181273ba90b1
        with:
          persist-credentials: false
""".strip()

    assert _workflow_actions(document) == (
        (
            "actions/checkout@3d3c42e5aac5ba805825da76410c181273ba90b1",
            {
                "uses": ("actions/checkout@3d3c42e5aac5ba805825da76410c181273ba90b1"),
                "with": {"persist-credentials": False},
            },
        ),
    )


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
    assert "pypa/gh-action-pypi-publish@dc37677b2e1c63e2034f94d8a5b11f265b73ba33" in (
        publish
    )
    assert "actions/attest@508db95dd578ae2727ebd6217d5ba78e4fbda05d" in publish
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
    project = (ROOT / "pyproject.toml").read_text()
    build = _workflow_job(workflow, "build")
    resolutions = _workflow_job(workflow, "dependency-resolutions")
    installed = _workflow_job(workflow, "installed-artifacts")
    deep_tests = _workflow_job(workflow, "deep-tests")
    post_publication = _workflow_job(workflow, "post-publication")

    for resolution in ("locked", "lowest-direct", "fresh"):
        assert resolution in resolutions
    lowest_direct = _job_step(resolutions, "Resolve lowest direct dependencies")
    assert "uv lock --resolution lowest-direct" in lowest_direct
    assert "uv sync --locked --resolution lowest-direct --python 3.12" in lowest_direct
    assert '"click>=8.2,<9"' in project
    assert '"pyyaml>=6.0.1,<7"' in project
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

    assert _yaml_mapping_keys(triggers, indent=2) == {
        "schedule",
        "workflow_dispatch",
    }
    assert "environment: live-read-only" in canary
    assert "id-token: write" in canary
    assert "google-github-actions/auth@7c6bc770dae815cd3e89ee6cdf493a5fab2cc093" in (
        canary
    )
    assert "scripts/live_read_only_canary.py" in canary
    assert "timeout-minutes: 25" in canary
    assert "--max-pages 50" in canary
    assert "--max-requests 500" in canary
    assert "--max-seconds 1200" in canary
    assert "--max-locations 125" in canary
    assert "--timeout 10" in canary
    assert "--max-transient-retries 1" in canary
    assert "--retry-backoff-seconds 1" in canary
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
    standalone_workflow = (WORKFLOWS / "live-read-only.yml").read_text()
    live_read_only = _workflow_job(workflow, "live-read-only")
    publish = _workflow_job(workflow, "publish")
    publish_needs = _yaml_block(publish, "needs", indent=4)

    assert "needs: build" in live_read_only
    assert "environment: live-read-only" in live_read_only
    assert "runs-on: ubuntu-22.04" in live_read_only
    assert "id-token: write" in live_read_only
    assert "GCP_WORKLOAD_IDENTITY_PROVIDER" in live_read_only
    assert "GCP_SERVICE_ACCOUNT" in live_read_only
    assert "GCP_PROJECT_ID" in live_read_only
    assert "scripts/live_read_only_canary.py" in live_read_only
    assert "uses: ./.github/workflows/live-read-only.yml" not in live_read_only
    assert "secrets: inherit" not in live_read_only
    release_job = _workflow_job_mapping(workflow, "live-read-only")
    standalone_job = _workflow_job_mapping(standalone_workflow, "canary")
    assert release_job.get("permissions") == {
        "contents": "read",
        "id-token": "write",
    }
    assert "secrets" not in release_job
    release_steps = release_job.get("steps")
    assert isinstance(release_steps, list), "release live-read-only steps"
    checkout_steps = [
        _mapping(step, "release live-read-only step")
        for step in release_steps
        if isinstance(step, dict)
        and str(cast("dict[object, object]", step).get("uses", "")).startswith(
            "actions/checkout@"
        )
    ]
    assert len(checkout_steps) == 1
    checkout_inputs = _mapping(checkout_steps[0].get("with"), "checkout inputs")
    assert checkout_inputs == {"persist-credentials": False}
    assert "ref" not in checkout_inputs
    for field in ("runs-on", "timeout-minutes", "environment", "permissions", "steps"):
        assert release_job.get(field) == standalone_job.get(field), field
    assert "live-read-only" in _yaml_list_values(publish_needs, indent=6)


def test_release_publication_qualifies_installed_live_read_adapters() -> None:
    """Publication requires sanitized live evidence from the exact candidate wheel."""
    workflow = (WORKFLOWS / "release.yml").read_text()
    qualification = _workflow_job(workflow, "installed-live-adapters")
    publish = _workflow_job(workflow, "publish")
    publish_needs = _yaml_block(publish, "needs", indent=4)

    assert "needs: build" in qualification
    assert "environment: live-read-only" in qualification
    assert "runs-on: ubuntu-22.04" in qualification
    assert "id-token: write" in qualification
    assert "GCP_WORKLOAD_IDENTITY_PROVIDER" in qualification
    assert "GCP_SERVICE_ACCOUNT" in qualification
    assert "GCP_PROJECT_ID" in qualification
    assert "scripts/installed_live_adapter_qualification.py" in qualification
    assert "--project-env GCP_PROJECT_ID" in qualification
    assert (
        "candidate/release/cqmgr-${{ needs.build.outputs.version }}-py3-none-any.whl"
        in qualification
    )
    candidate = _job_step(qualification, "Download immutable release candidate")
    assert "uses: actions/download-artifact@" in candidate
    assert "name: release-candidate" in candidate
    assert "path: candidate" in candidate

    evidence = _job_step(
        qualification,
        "Upload sanitized installed-adapter evidence",
    )
    assert "if: always()" in evidence
    assert "uses: actions/upload-artifact@" in evidence
    assert "name: installed-live-adapter-evidence" in evidence
    assert "path: installed-live-adapter-evidence.json" in evidence
    assert "installed-live-adapters" in _yaml_list_values(
        publish_needs,
        indent=6,
    )


def test_release_performance_job_enforces_budgets_and_retains_failure_evidence() -> (
    None
):
    """PR and release callers share one approved performance qualification."""
    release = (WORKFLOWS / "release.yml").read_text()
    python = (WORKFLOWS / "python.yml").read_text()
    workflow = (WORKFLOWS / "performance.yml").read_text()
    performance = _workflow_job(workflow, "performance")
    measure = _job_step(performance, "Measure and enforce performance budgets")
    upload = _job_step(performance, "Upload performance evidence")

    assert "uses: ./.github/workflows/performance.yml" in _workflow_job(
        release,
        "performance",
    )
    assert "uses: ./.github/workflows/performance.yml" in _workflow_job(
        python,
        "performance",
    )
    assert "--budgets docs/release/performance-budgets.json" in measure
    assert "if: always()" in upload


def test_python_workflow_classifies_provider_lanes_fail_closed() -> None:
    """Only an explicit affected-path proof can declare protected live inapplicable."""
    workflow = (WORKFLOWS / "python.yml").read_text()
    classifier = _workflow_job_mapping(workflow, "classify-provider-lanes")
    classifier_text = _workflow_job(workflow, "classify-provider-lanes")
    classify = _job_step(classifier_text, "Classify affected provider lanes")
    non_applicable = _workflow_job_mapping(
        workflow,
        "protected-live-not-applicable",
    )

    assert classifier.get("outputs") == {
        "lanes": "${{ steps.classify.outputs.lanes }}",
    }
    assert classifier.get("if") == "github.event_name == 'pull_request'"
    steps = classifier.get("steps")
    assert isinstance(steps, list), "classifier steps"
    checkout = next(
        _mapping(step, "classifier checkout")
        for step in steps
        if isinstance(step, dict)
        and str(cast("dict[object, object]", step).get("uses", "")).startswith(
            "actions/checkout@"
        )
    )
    assert _mapping(checkout.get("with"), "classifier checkout inputs") == {
        "fetch-depth": 0,
        "persist-credentials": False,
        "ref": "${{ github.event.pull_request.head.sha }}",
    }
    assert "version: 0.11.30" in _job_step(
        classifier_text,
        "Install uv and Python",
    )
    assert "git diff --merge-base --name-only -z" in classify
    assert "uv run --no-project --python 3.14 python -c" in classify
    assert (
        "uv run --no-project --python 3.14 python "
        "scripts/affected_qualification_lanes.py"
    ) in classify
    assert "scripts/affected_qualification_lanes.py" in classify
    assert "printf '[]" not in classify
    assert "continue-on-error" not in classify
    assert non_applicable.get("needs") == "classify-provider-lanes"
    assert non_applicable.get("if") == (
        "needs.classify-provider-lanes.outputs.lanes == '[]'"
    )
    assert "permissions" not in non_applicable


def test_python_workflow_qualifies_same_repo_exact_head_candidate_live() -> None:
    """Affected trusted PRs install and exercise independently verified head bytes."""
    workflow = (WORKFLOWS / "python.yml").read_text()
    candidate = _workflow_job_mapping(workflow, "pull-request-candidate")
    candidate_text = _workflow_job(workflow, "pull-request-candidate")
    live = _workflow_job_mapping(workflow, "google-protected-live")
    live_text = _workflow_job(workflow, "google-protected-live")
    expected_condition = (
        "github.event_name == 'pull_request' && "
        "github.event.pull_request.head.repo.full_name == github.repository && "
        "contains(fromJSON(needs.classify-provider-lanes.outputs.lanes), 'google')"
    )

    assert candidate.get("needs") == "classify-provider-lanes"
    assert candidate.get("if") == expected_condition
    candidate_checkout = _job_step(candidate_text, "Checkout exact pull-request head")
    assert "ref: ${{ github.event.pull_request.head.sha }}" in candidate_checkout
    assert "persist-credentials: false" in candidate_checkout
    assert "version: 0.11.30" in _job_step(
        candidate_text,
        "Install uv and Python",
    )
    identity = _job_step(
        candidate_text,
        "Bind non-publishable candidate identity",
    )
    assert "pull-request-identity" in identity
    assert "^[0-9A-Za-z][0-9A-Za-z._+-]*$" in identity
    assert "--sdist" in _job_step(candidate_text, "Build source distribution")
    wheel = _job_step(candidate_text, "Build sole wheel from source distribution")
    assert "--wheel" in wheel
    assert "sdists=(dist/cqmgr-*.tar.gz)" in wheel
    assert 'test "${#sdists[@]}" -eq 1' in wheel
    assert '"${sdists[0]}"' in wheel
    assert "wheels=(dist/cqmgr-*.whl)" in wheel
    assert 'test "${#wheels[@]}" -eq 1' in wheel
    prepare = _job_step(candidate_text, "Prepare immutable candidate bundle")
    assert "scripts/release_qualification.py prepare" in prepare
    assert "--qualification" in prepare
    assert "verify-pull-request" in _job_step(
        candidate_text,
        "Verify candidate identity and bytes",
    )

    assert live.get("needs") == [
        "classify-provider-lanes",
        "pull-request-candidate",
    ]
    assert live.get("if") == expected_condition
    assert live.get("environment") == "live-read-only"
    assert live.get("permissions") == {
        "contents": "read",
        "id-token": "write",
    }
    live_checkout = _job_step(live_text, "Checkout exact pull-request head")
    assert "ref: ${{ github.event.pull_request.head.sha }}" in live_checkout
    assert "persist-credentials: false" in live_checkout
    assert "verify-pull-request" in _job_step(
        live_text,
        "Independently verify candidate identity and bytes",
    )
    authentication = _job_step(live_text, "Authenticate read-only Google identity")
    assert "google-github-actions/auth@" in authentication
    qualify = _job_step(live_text, "Qualify installed Compute adapters")
    assert "scripts/installed_live_adapter_qualification.py" in qualify
    assert "--project-env GCP_PROJECT_ID" in qualify
    assert "wheels=(candidate/release/cqmgr-*.whl)" in qualify
    assert 'test "${#wheels[@]}" -eq 1' in qualify
    assert '"${wheels[0]}"' in qualify
    live_steps = live.get("steps")
    assert isinstance(live_steps, list), "live steps"
    qualify_mapping = next(
        _mapping(step, "qualify installed Compute adapters")
        for step in live_steps
        if isinstance(step, dict)
        and cast("dict[object, object]", step).get("name")
        == "Qualify installed Compute adapters"
    )
    assert _mapping(qualify_mapping.get("env"), "qualification environment") == {
        "GCP_PROJECT_ID": "${{ secrets.GCP_PROJECT_ID }}",
    }


def test_python_qualification_is_one_fixed_fail_closed_aggregate() -> None:
    """The required status represents every hermetic and applicable live gate."""
    workflow = (WORKFLOWS / "python.yml").read_text()
    aggregate = _workflow_job_mapping(workflow, "python-qualification")
    aggregate_text = _workflow_job(workflow, "python-qualification")
    expected_needs = {
        "classify-provider-lanes",
        "google-protected-live",
        "package-smoke",
        "performance",
        "protected-live-not-applicable",
        "pull-request-candidate",
        "quality",
        "tests",
    }

    assert aggregate.get("name") == "Python qualification"
    assert aggregate.get("if") == "github.event_name == 'pull_request' && always()"
    needs = aggregate.get("needs")
    assert isinstance(needs, list), "aggregate needs"
    assert set(needs) == expected_needs
    assert aggregate.get("permissions") == {}
    assert "environment" not in aggregate
    verify = _job_step(aggregate_text, "Require complete Python qualification")
    assert (
        "SAME_REPOSITORY: "
        "${{ github.event.pull_request.head.repo.full_name == github.repository }}"
    ) in aggregate_text
    assert 'test "${SAME_REPOSITORY}" = "true"' in verify
    for result in (
        "CLASSIFIER_RESULT",
        "GOOGLE_LIVE_RESULT",
        "NON_APPLICABLE_RESULT",
        "PACKAGE_SMOKE_RESULT",
        "PERFORMANCE_RESULT",
        "PULL_REQUEST_CANDIDATE_RESULT",
        "QUALITY_RESULT",
        "TESTS_RESULT",
    ):
        assert result in verify
    assert "\"${LANES}\" == '[]'" in verify
    assert '"${LANES}" == \'["google"]\'' in verify
    assert '"${GOOGLE_LIVE_RESULT}" = "success"' in verify
    assert '"${NON_APPLICABLE_RESULT}" = "success"' in verify
    assert '"${PULL_REQUEST_CANDIDATE_RESULT}" = "success"' in verify
    for result in (
        "CLASSIFIER_RESULT",
        "PACKAGE_SMOKE_RESULT",
        "PERFORMANCE_RESULT",
        "QUALITY_RESULT",
        "TESTS_RESULT",
    ):
        assert f'"${{{result}}}" = "success"' in verify
    assert "exit 1" in verify


def test_dependency_review_accepts_the_reviewed_python_license_expression() -> None:
    """Compound dependency metadata may use SPDX Python-2.0."""
    workflow = (WORKFLOWS / "dependency-review.yml").read_text()
    review = _job_step(
        _workflow_job(workflow, "dependency-review"),
        "Review dependency changes",
    )

    assert "Python-2.0" in review

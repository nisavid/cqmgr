"""Provider qualification lanes are selected fail-closed from affected paths."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).parents[1]
SCRIPT = ROOT / "scripts" / "affected_qualification_lanes.py"
REGISTRY = ROOT / "scripts" / "provider_qualification_lanes.json"


def _run_classifier(
    input_text: str,
    *,
    registry: Path = REGISTRY,
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(  # noqa: S603 - fixed local interpreter and script
        [sys.executable, str(SCRIPT), "--registry", str(registry)],
        input=input_text,
        capture_output=True,
        check=False,
        text=True,
    )


def _classify(
    paths: list[str],
    *,
    registry: Path = REGISTRY,
) -> subprocess.CompletedProcess[str]:
    return _run_classifier(json.dumps(paths), registry=registry)


def test_google_specific_paths_select_the_stable_google_lane() -> None:
    """Google production and test changes require the Google live profile."""
    result = _classify(
        [
            "tests/adapters/test_compute_accelerator_catalog_edges.py",
            "tests/adapters/test_google_catalog_reads.py",
            "src/cqmgr/adapters/google/compute_catalog.py",
        ]
    )

    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout) == ["google"]
    assert result.stdout == '["google"]\n'


def test_shared_qualification_paths_select_every_registered_lane(
    tmp_path: Path,
) -> None:
    """Provider-neutral production and gate changes qualify every provider."""
    registry = tmp_path / "lanes.json"
    registry.write_text(
        json.dumps(
            {
                "lanes": {
                    "google": {"path_prefixes": ["providers/google/"]},
                    "aws": {"path_prefixes": ["providers/aws/"]},
                },
                "schema": "cqmgr.provider-qualification-lanes/v1",
            }
        ),
        encoding="utf-8",
        newline="\n",
    )

    result = _classify(
        [
            "src/cqmgr/bootstrap.py",
            "src/cqmgr/google_read_only.py",
            "src/cqmgr/application/operations/quotas.py",
            "src/cqmgr/application/invocation.py",
            "src/cqmgr/adapters/serialization/results.py",
            "scripts/installed_live_adapter_qualification.py",
            ".github/workflows/python.yml",
            "tests/test_google_read_only_bootstrap.py",
            "tests/test_release_workflows.py",
        ],
        registry=registry,
    )

    assert result.returncode == 0, result.stderr
    assert result.stdout == '["aws","google"]\n'


def test_documentation_and_proved_unrelated_paths_select_no_lane() -> None:
    """Cloud-free repository metadata cannot manufacture a passing live lane."""
    result = _classify(
        [
            "docs/adr/0006-use-layered-operation-failure-containment.md",
            ".github/ISSUE_TEMPLATE/bug.yml",
            "README.md",
            "CONTEXT.md",
            ".gitignore",
        ]
    )

    assert result.returncode == 0, result.stderr
    assert result.stdout == "[]\n"


def test_unknown_path_fails_closed() -> None:
    """A new behavioral surface needs an explicit classification decision."""
    result = _classify(["src/cqmgr/domain/unclassified_behavior.py"])

    assert result.returncode != 0
    assert result.stdout == ""


@pytest.mark.parametrize(
    "input_text",
    [
        "not-json",
        "{}",
        "[]",
        '["src/cqmgr/adapters/google/../unclassified.py"]',
    ],
)
def test_malformed_input_fails_closed(input_text: str) -> None:
    """Invalid path documents cannot become a passing lane selection."""
    result = _run_classifier(input_text)

    assert result.returncode != 0
    assert result.stdout == ""


def test_missing_or_malformed_registry_fails_closed(tmp_path: Path) -> None:
    """The lane authority must exist and parse before any path is classified."""
    missing = _classify(
        ["src/cqmgr/adapters/google/compute_catalog.py"],
        registry=tmp_path / "missing.json",
    )
    malformed_registry = tmp_path / "malformed.json"
    malformed_registry.write_text("{}", encoding="utf-8", newline="\n")
    malformed = _classify(
        ["src/cqmgr/adapters/google/compute_catalog.py"],
        registry=malformed_registry,
    )

    assert missing.returncode != 0
    assert malformed.returncode != 0
    assert missing.stdout == malformed.stdout == ""

"""Select protected provider qualification lanes from affected repository paths."""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence
    from typing import TextIO

REGISTRY_SCHEMA = "cqmgr.provider-qualification-lanes/v1"
LANE_PATTERN = re.compile(r"[a-z][a-z0-9-]*\Z")
MIN_PRINTABLE_CODEPOINT = 32
SHARED_PATHS = {
    "src/cqmgr/adapters/cli/lifecycle.py",
    "src/cqmgr/adapters/cli/read_only.py",
    "src/cqmgr/adapters/tui/app.py",
    "src/cqmgr/application/invocation.py",
    "src/cqmgr/application/operations/lifecycle.py",
    "src/cqmgr/application/operations/lifecycle_requests.py",
    "src/cqmgr/application/operations/obtainability.py",
    "src/cqmgr/application/operations/plans.py",
    "src/cqmgr/application/operations/quotas.py",
    "src/cqmgr/application/operations/read_only.py",
    "src/cqmgr/application/operations/watch.py",
    "src/cqmgr/bootstrap.py",
    "src/cqmgr/cli.py",
    "src/cqmgr/google_read_only.py",
    "src/cqmgr/tui.py",
}
SHARED_PREFIXES = (
    ".github/workflows/",
    "src/cqmgr/adapters/serialization/",
)
SHARED_TEST_PATHS = {
    "tests/adapters/test_operation_result_serialization.py",
    "tests/adapters/test_quota_snapshot_serialization.py",
    "tests/adapters/test_lifecycle_cli.py",
    "tests/adapters/test_watch_resume_serialization.py",
    "tests/adapters/test_workload_result_serialization.py",
    "tests/application/test_invocation.py",
    "tests/application/test_quota_operations.py",
    "tests/application/test_read_only_operations.py",
    "tests/application/test_workload_operation_edges.py",
    "tests/test_affected_qualification_lanes.py",
    "tests/test_google_read_only_bootstrap.py",
    "tests/test_installed_live_adapter_qualification.py",
    "tests/test_lifecycle_cross_surface.py",
    "tests/test_lifecycle_runtime_bootstrap.py",
    "tests/test_mutation_qualification.py",
    "tests/test_performance_qualification.py",
    "tests/test_read_only_bootstrap.py",
    "tests/test_read_only_cli.py",
    "tests/test_release_qualification.py",
    "tests/test_release_workflows.py",
}
SHARED_TEST_PREFIXES = ("tests/serialization/",)
PROVED_UNRELATED_PATHS = {
    ".editorconfig",
    ".gitignore",
    ".markdownlint.json",
    "CONTEXT.md",
    "LICENSE",
    "README.md",
}
PROVED_UNRELATED_PREFIXES = (
    ".github/ISSUE_TEMPLATE/",
    "docs/",
)


def _load_registry(path: Path) -> dict[str, tuple[str, ...]]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict) or value.get("schema") != REGISTRY_SCHEMA:
        msg = "provider qualification lane registry has an unsupported schema"
        raise ValueError(msg)
    lanes_value = value.get("lanes")
    if not isinstance(lanes_value, dict) or not lanes_value:
        msg = "provider qualification lane registry must define lanes"
        raise ValueError(msg)
    lanes: dict[str, tuple[str, ...]] = {}
    for identifier, lane_value in lanes_value.items():
        if not isinstance(identifier, str) or not LANE_PATTERN.fullmatch(identifier):
            msg = "provider qualification lane identifier is malformed"
            raise ValueError(msg)
        if not isinstance(lane_value, dict):
            msg = f"provider qualification lane {identifier} must be an object"
            raise TypeError(msg)
        prefixes = lane_value.get("path_prefixes")
        if (
            not isinstance(prefixes, list)
            or not prefixes
            or any(not isinstance(prefix, str) or not prefix for prefix in prefixes)
        ):
            msg = f"provider qualification lane {identifier} needs path prefixes"
            raise ValueError(msg)
        lanes[identifier] = tuple(prefixes)
    return lanes


def _input_paths(stream: TextIO) -> tuple[str, ...]:
    value = json.load(stream)
    if (
        not isinstance(value, list)
        or not value
        or any(not isinstance(path, str) or not path for path in value)
    ):
        msg = "affected paths must be one non-empty JSON array of strings"
        raise ValueError(msg)
    if any(not _is_repository_path(path) for path in value):
        msg = "affected paths must be canonical repository-relative paths"
        raise ValueError(msg)
    return tuple(value)


def _is_repository_path(path: str) -> bool:
    return (
        not path.startswith("/")
        and "\\" not in path
        and all(part not in {"", ".", ".."} for part in path.split("/"))
        and all(ord(character) >= MIN_PRINTABLE_CODEPOINT for character in path)
    )


def affected_lanes(
    paths: Sequence[str],
    registry: Mapping[str, tuple[str, ...]],
) -> tuple[str, ...]:
    """Return the deterministic provider lanes selected by affected paths."""
    selected: set[str] = set()
    for path in paths:
        if path in PROVED_UNRELATED_PATHS or path.startswith(PROVED_UNRELATED_PREFIXES):
            continue
        if _is_shared_path(path):
            selected.update(registry)
            continue
        matches = {
            identifier
            for identifier, prefixes in registry.items()
            if path.startswith(prefixes)
        }
        if not matches:
            msg = f"affected path is not classified: {path}"
            raise ValueError(msg)
        selected.update(matches)
    return tuple(sorted(selected))


def _is_shared_path(path: str) -> bool:
    if path in SHARED_PATHS or path.startswith(SHARED_PREFIXES):
        return True
    name = Path(path).name
    if path.startswith("scripts/"):
        return "qualification" in name or name == "provider_qualification_lanes.json"
    if path.startswith("tests/"):
        return path in SHARED_TEST_PATHS or path.startswith(SHARED_TEST_PREFIXES)
    return False


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser()
    parser.add_argument("--registry", type=Path, required=True)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    """Read affected paths as JSON and print a deterministic lane JSON array."""
    arguments = _parser().parse_args(argv)
    try:
        paths = _input_paths(sys.stdin)
        lanes = affected_lanes(paths, _load_registry(arguments.registry))
    except (OSError, TypeError, ValueError, json.JSONDecodeError) as error:
        sys.stderr.write(f"qualification lane classification failed: {error}\n")
        return 1
    sys.stdout.write(json.dumps(lanes, separators=(",", ":")) + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

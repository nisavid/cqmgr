"""Validate the exact Cloud Quota Manager distribution artifacts."""

from __future__ import annotations

import argparse
import json
import tarfile
import tomllib
import zipfile
from pathlib import Path, PurePosixPath
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Callable

PACKAGE_PREFIX = PurePosixPath("cqmgr")


def _project_version(project_path: Path) -> str:
    document = tomllib.loads(project_path.read_text(encoding="utf-8"))
    project = document.get("project")
    version = project.get("version") if isinstance(project, dict) else None
    dynamic = project.get("dynamic") if isinstance(project, dict) else None
    if (
        not isinstance(version, str)
        or not version.strip()
        or (isinstance(dynamic, list) and "version" in dynamic)
    ):
        msg = "project version must be one static non-empty string"
        raise ValueError(msg)
    return version


PROJECT_VERSION = _project_version(Path("pyproject.toml"))
SDIST_ROOT = PurePosixPath(f"cqmgr-{PROJECT_VERSION}")
WHEEL_DIST_INFO = PurePosixPath(f"cqmgr-{PROJECT_VERSION}.dist-info")
EXPECTED_PACKAGE_FILES = {
    PurePosixPath("__init__.py"),
    PurePosixPath("__main__.py"),
    PurePosixPath("adapters/__init__.py"),
    PurePosixPath("adapters/clock.py"),
    PurePosixPath("adapters/cli/__init__.py"),
    PurePosixPath("adapters/cli/audit.py"),
    PurePosixPath("adapters/cli/copy_cli.py"),
    PurePosixPath("adapters/cli/group.py"),
    PurePosixPath("adapters/cli/lifecycle.py"),
    PurePosixPath("adapters/cli/local.py"),
    PurePosixPath("adapters/cli/read_only.py"),
    PurePosixPath("adapters/cli/read_only_requests.py"),
    PurePosixPath("adapters/google/__init__.py"),
    PurePosixPath("adapters/google/cloud_quotas.py"),
    PurePosixPath("adapters/google/compute_catalog.py"),
    PurePosixPath("adapters/google/identity.py"),
    PurePosixPath("adapters/google/monitoring.py"),
    PurePosixPath("adapters/google/projects.py"),
    PurePosixPath("adapters/google/quota_preference_writes.py"),
    PurePosixPath("adapters/google/read_policy.py"),
    PurePosixPath("adapters/google/spot_advice.py"),
    PurePosixPath("adapters/google/tpu_catalog.py"),
    PurePosixPath("adapters/persistence/__init__.py"),
    PurePosixPath("adapters/persistence/apply_records.py"),
    PurePosixPath("adapters/persistence/audit.py"),
    PurePosixPath("adapters/persistence/configuration.py"),
    PurePosixPath("adapters/persistence/coordination.py"),
    PurePosixPath("adapters/persistence/installation_trust.py"),
    PurePosixPath("adapters/persistence/locking.py"),
    PurePosixPath("adapters/persistence/native_plan_lock.py"),
    PurePosixPath("adapters/persistence/plans.py"),
    PurePosixPath("adapters/persistence/quota_snapshots.py"),
    PurePosixPath("adapters/persistence/secrets.py"),
    PurePosixPath("adapters/persistence/watch.py"),
    PurePosixPath("adapters/persistence/windows_acl.py"),
    PurePosixPath("adapters/serialization/__init__.py"),
    PurePosixPath("adapters/serialization/plans.py"),
    PurePosixPath("adapters/serialization/quota_snapshots.py"),
    PurePosixPath("adapters/serialization/results.py"),
    PurePosixPath("adapters/serialization/watch.py"),
    PurePosixPath("adapters/tui/__init__.py"),
    PurePosixPath("adapters/tui/app.py"),
    PurePosixPath("application/__init__.py"),
    PurePosixPath("application/configuration.py"),
    PurePosixPath("application/operations/__init__.py"),
    PurePosixPath("application/operations/apply.py"),
    PurePosixPath("application/operations/audit.py"),
    PurePosixPath("application/operations/audited_write.py"),
    PurePosixPath("application/operations/contacts.py"),
    PurePosixPath("application/operations/lifecycle_apply.py"),
    PurePosixPath("application/operations/lifecycle.py"),
    PurePosixPath("application/operations/lifecycle_requests.py"),
    PurePosixPath("application/operations/local.py"),
    PurePosixPath("application/operations/obtainability.py"),
    PurePosixPath("application/operations/plans.py"),
    PurePosixPath("application/operations/quotas.py"),
    PurePosixPath("application/operations/read_only.py"),
    PurePosixPath("application/operations/trust.py"),
    PurePosixPath("application/operations/watch.py"),
    PurePosixPath("application/ports/__init__.py"),
    PurePosixPath("application/ports/apply.py"),
    PurePosixPath("application/ports/apply_records.py"),
    PurePosixPath("application/ports/audit.py"),
    PurePosixPath("application/ports/catalog_reads.py"),
    PurePosixPath("application/ports/clock.py"),
    PurePosixPath("application/ports/configuration.py"),
    PurePosixPath("application/ports/coordination.py"),
    PurePosixPath("application/ports/identity.py"),
    PurePosixPath("application/ports/obtainability.py"),
    PurePosixPath("application/ports/plans.py"),
    PurePosixPath("application/ports/provider_reads.py"),
    PurePosixPath("application/ports/provider_writes.py"),
    PurePosixPath("application/ports/quota_snapshots.py"),
    PurePosixPath("application/ports/secrets.py"),
    PurePosixPath("application/ports/watch.py"),
    PurePosixPath("bootstrap.py"),
    PurePosixPath("cli.py"),
    PurePosixPath("domain/catalog.py"),
    PurePosixPath("domain/accelerator_overlay.py"),
    PurePosixPath("domain/apply_records.py"),
    PurePosixPath("domain/audit.py"),
    PurePosixPath("domain/diagnostics.py"),
    PurePosixPath("domain/identity.py"),
    PurePosixPath("domain/__init__.py"),
    PurePosixPath("domain/obtainability.py"),
    PurePosixPath("domain/plan_consumption.py"),
    PurePosixPath("domain/plans.py"),
    PurePosixPath("domain/projects.py"),
    PurePosixPath("domain/quota_queries.py"),
    PurePosixPath("domain/quotas.py"),
    PurePosixPath("domain/redaction.py"),
    PurePosixPath("domain/results.py"),
    PurePosixPath("domain/schemas.py"),
    PurePosixPath("domain/scopes.py"),
    PurePosixPath("domain/status.py"),
    PurePosixPath("domain/time.py"),
    PurePosixPath("domain/watch.py"),
    PurePosixPath("google_read_only.py"),
    PurePosixPath("py.typed"),
    PurePosixPath("resources/__init__.py"),
    PurePosixPath("resources/accelerator-overlay.json"),
    PurePosixPath("resources/release-evidence.json"),
    PurePosixPath("resources/schemas/catalog.json"),
    PurePosixPath("tui.py"),
}
RELEASE_RESOURCE_FILES = {
    PurePosixPath("resources/accelerator-overlay.json"),
    PurePosixPath("resources/release-evidence.json"),
    PurePosixPath("resources/schemas/catalog.json"),
}
FORBIDDEN_RELEASE_RESOURCE_TEXT = (
    "projects/",
    "private.operator",
    "private-access-token",
    str(Path.home()),
    str(Path.cwd().resolve()),
)


def _regular_files(names: list[str]) -> set[PurePosixPath]:
    return {PurePosixPath(name) for name in names if name and not name.endswith("/")}


def _assert_release_resources(read: Callable[[str], bytes]) -> None:
    for relative_path in RELEASE_RESOURCE_FILES:
        raw = read(str(PACKAGE_PREFIX / relative_path))
        text = raw.decode("utf-8")
        assert all(
            forbidden not in text for forbidden in FORBIDDEN_RELEASE_RESOURCE_TEXT
        )
        document = json.loads(text)
        assert isinstance(document, dict)
    overlay = json.loads(
        read(str(PACKAGE_PREFIX / "resources/accelerator-overlay.json")).decode("utf-8")
    )
    assert overlay["schema"] == "cqmgr.accelerator-catalog/v1"
    assert isinstance(overlay["revision"], str)
    assert overlay["revision"]
    assert isinstance(overlay["mappings"], list)
    assert overlay["mappings"]
    evidence = json.loads(
        read(str(PACKAGE_PREFIX / "resources/release-evidence.json")).decode("utf-8")
    )
    assert evidence["schema"] == "cqmgr.release-evidence/v1"
    assert evidence["claims"] == {
        "physical_capacity": False,
        "universal_availability": False,
    }
    catalog = json.loads(
        read(str(PACKAGE_PREFIX / "resources/schemas/catalog.json")).decode("utf-8")
    )
    assert catalog["schema"] == "cqmgr.schema-catalog/v1"
    assert catalog["quota_request_plan"]["kinds"] == ["bundle", "single"]


def _assert_wheel_contents(wheel: Path) -> set[PurePosixPath]:
    assert wheel.name == f"cqmgr-{PROJECT_VERSION}-py3-none-any.whl"
    with zipfile.ZipFile(wheel) as archive:
        files = _regular_files(archive.namelist())
        allowed_metadata = {
            WHEEL_DIST_INFO / "METADATA",
            WHEEL_DIST_INFO / "RECORD",
            WHEEL_DIST_INFO / "WHEEL",
            WHEEL_DIST_INFO / "entry_points.txt",
            WHEEL_DIST_INFO / "licenses" / "LICENSE",
        }
        assert all(
            PACKAGE_PREFIX in path.parents or path in allowed_metadata for path in files
        )
        assert PACKAGE_PREFIX / "py.typed" in files
        assert WHEEL_DIST_INFO / "licenses" / "LICENSE" in files
        assert archive.read(str(WHEEL_DIST_INFO / "entry_points.txt")).splitlines() == [
            b"[console_scripts]",
            b"cqmgr = cqmgr.cli:main",
            b"",
        ]
        wheel_metadata = archive.read(str(WHEEL_DIST_INFO / "WHEEL"))
        assert b"Root-Is-Purelib: true" in wheel_metadata
        assert b"Tag: py3-none-any" in wheel_metadata
        checkout = str(Path.cwd().resolve()).encode("utf-8")
        assert all(checkout not in archive.read(str(path)) for path in files)
        _assert_release_resources(archive.read)
    package_files = {
        path.relative_to(PACKAGE_PREFIX)
        for path in files
        if PACKAGE_PREFIX in path.parents
    }
    assert package_files == EXPECTED_PACKAGE_FILES
    return package_files


def _assert_sdist_contents(sdist: Path) -> set[PurePosixPath]:
    assert sdist.name == f"cqmgr-{PROJECT_VERSION}.tar.gz"
    with tarfile.open(sdist, "r:gz") as archive:
        files = {
            PurePosixPath(member.name)
            for member in archive.getmembers()
            if member.isfile()
        }
        allowed = {
            SDIST_ROOT / "LICENSE",
            SDIST_ROOT / "PKG-INFO",
            SDIST_ROOT / "README.md",
            SDIST_ROOT / "pyproject.toml",
        }
        package_root = SDIST_ROOT / "src" / PACKAGE_PREFIX
        assert all(path in allowed or package_root in path.parents for path in files)
        assert package_root / "py.typed" in files
        checkout = str(Path.cwd().resolve()).encode("utf-8")
        for path in files:
            extracted = archive.extractfile(str(path))
            assert extracted is not None
            assert checkout not in extracted.read()

        def read_sdist_resource(name: str) -> bytes:
            extracted = archive.extractfile(str(SDIST_ROOT / "src" / name))
            assert extracted is not None
            return extracted.read()

        _assert_release_resources(read_sdist_resource)
    package_files = {
        path.relative_to(package_root) for path in files if package_root in path.parents
    }
    assert package_files == EXPECTED_PACKAGE_FILES
    return package_files


def verify_distribution(dist_dir: Path) -> None:
    """Verify names, contents, metadata, and source-to-wheel agreement."""
    artifacts = sorted(
        path for path in dist_dir.iterdir() if path.name.endswith((".tar.gz", ".whl"))
    )
    assert [path.name for path in artifacts] == [
        f"cqmgr-{PROJECT_VERSION}-py3-none-any.whl",
        f"cqmgr-{PROJECT_VERSION}.tar.gz",
    ]
    wheel_files = _assert_wheel_contents(artifacts[0])
    sdist_files = _assert_sdist_contents(artifacts[1])
    assert wheel_files == sdist_files


def main() -> None:
    """Parse arguments and verify a distribution directory."""
    parser = argparse.ArgumentParser()
    parser.add_argument("dist_dir", type=Path)
    arguments = parser.parse_args()
    verify_distribution(arguments.dist_dir)


if __name__ == "__main__":
    main()

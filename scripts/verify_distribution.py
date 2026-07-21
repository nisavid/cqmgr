"""Validate the exact Cloud Quota Manager distribution artifacts."""

from __future__ import annotations

import argparse
import tarfile
import tomllib
import zipfile
from pathlib import Path, PurePosixPath

PACKAGE_PREFIX = PurePosixPath("cqmgr")
PROJECT_VERSION = tomllib.loads(Path("pyproject.toml").read_text())["project"][
    "version"
]
SDIST_ROOT = PurePosixPath(f"cqmgr-{PROJECT_VERSION}")
WHEEL_DIST_INFO = PurePosixPath(f"cqmgr-{PROJECT_VERSION}.dist-info")
EXPECTED_PACKAGE_FILES = {
    PurePosixPath("__init__.py"),
    PurePosixPath("__main__.py"),
    PurePosixPath("adapters/__init__.py"),
    PurePosixPath("adapters/cli/__init__.py"),
    PurePosixPath("adapters/cli/group.py"),
    PurePosixPath("adapters/google/__init__.py"),
    PurePosixPath("adapters/persistence/__init__.py"),
    PurePosixPath("adapters/serialization/__init__.py"),
    PurePosixPath("adapters/tui/__init__.py"),
    PurePosixPath("application/__init__.py"),
    PurePosixPath("application/operations/__init__.py"),
    PurePosixPath("application/ports/__init__.py"),
    PurePosixPath("bootstrap.py"),
    PurePosixPath("cli.py"),
    PurePosixPath("domain/__init__.py"),
    PurePosixPath("py.typed"),
    PurePosixPath("tui.py"),
}


def _regular_files(names: list[str]) -> set[PurePosixPath]:
    return {PurePosixPath(name) for name in names if name and not name.endswith("/")}


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
        checkout = str(Path.cwd().resolve()).encode()
        assert all(checkout not in archive.read(str(path)) for path in files)
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
        checkout = str(Path.cwd().resolve()).encode()
        for path in files:
            extracted = archive.extractfile(str(path))
            assert extracted is not None
            assert checkout not in extracted.read()
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

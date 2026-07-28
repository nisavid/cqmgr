"""Fail closed when a license-exempt dependency leaves its reviewed version."""

from __future__ import annotations

import argparse
import tomllib
from pathlib import Path

REVIEWED_LICENSE_EXCEPTIONS = {
    "charset-normalizer": frozenset({"3.4.9"}),
    "protobuf": frozenset({"6.33.6", "7.35.1"}),
}


def verify_dependency_license_exceptions(lock_path: Path) -> None:
    """Require every license exception to match exactly one reviewed lock entry."""
    lock = tomllib.loads(lock_path.read_text())
    packages = lock.get("package")
    if not isinstance(packages, list):
        msg = "uv.lock must contain a package list"
        raise TypeError(msg)

    for name, expected_versions in REVIEWED_LICENSE_EXCEPTIONS.items():
        versions = [
            package.get("version")
            for package in packages
            if isinstance(package, dict) and package.get("name") == name
        ]
        if len(versions) != 1 or versions[0] not in expected_versions:
            reviewed = ", ".join(sorted(expected_versions))
            msg = (
                f"license exception for {name} requires exactly one reviewed "
                f"version ({reviewed}); review the new artifact before updating"
            )
            raise ValueError(msg)


def main() -> None:
    """Parse the lock path and enforce the reviewed-version policy."""
    parser = argparse.ArgumentParser()
    parser.add_argument("lock_path", type=Path)
    arguments = parser.parse_args()
    try:
        verify_dependency_license_exceptions(arguments.lock_path)
    except (OSError, tomllib.TOMLDecodeError, TypeError, ValueError) as error:
        raise SystemExit(str(error)) from None


if __name__ == "__main__":
    main()

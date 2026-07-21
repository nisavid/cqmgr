"""Installed package metadata contracts."""

from importlib.metadata import entry_points, metadata, version

import cqmgr


def test_package_metadata_is_authoritative() -> None:
    """Runtime metadata agrees with the installed distribution."""
    package_metadata = metadata("cqmgr")

    assert cqmgr.__version__ == version("cqmgr")
    assert package_metadata["Requires-Python"].replace(" ", "") == ">=3.12,<3.15"
    assert package_metadata["License-Expression"] == "MIT"


def test_console_entry_point_is_declared() -> None:
    """The public executable targets the stable CLI bootstrap."""
    matches = [
        entry_point
        for entry_point in entry_points(group="console_scripts")
        if entry_point.name == "cqmgr"
    ]

    assert len(matches) == 1
    assert matches[0].value == "cqmgr.cli:main"

"""Canonical cloud resource-scope identities."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum


class ResourceScopeKind(StrEnum):
    """Supported canonical resource-container kinds."""

    PROJECT = "project"
    FOLDER = "folder"
    ORGANIZATION = "organization"


@dataclass(frozen=True, slots=True)
class ResourceScope:
    """One canonical project, folder, or organization resource name."""

    kind: ResourceScopeKind
    canonical_name: str

    def __post_init__(self) -> None:
        """Reject names whose collection or identifier is not canonical."""
        if not isinstance(self.kind, ResourceScopeKind):
            msg = "resource scope kind must be a ResourceScopeKind"
            raise TypeError(msg)
        if not isinstance(self.canonical_name, str):
            msg = "canonical resource name must be a string"
            raise TypeError(msg)

        collection = f"{self.kind.value}s"
        prefix = f"{collection}/"
        identifier = self.canonical_name.removeprefix(prefix)
        if (
            not self.canonical_name.startswith(prefix)
            or not identifier
            or "/" in identifier
            or not identifier.isascii()
            or not identifier.isdigit()
        ):
            msg = (
                f"canonical resource name for {self.kind.value} must be "
                f"{collection}/<ASCII numeric identifier>"
            )
            raise ValueError(msg)

"""Canonical resource-scope identity contracts."""

from typing import cast

import pytest

from cqmgr.domain.scopes import ResourceScope, ResourceScopeKind


@pytest.mark.parametrize(
    ("kind", "canonical_name"),
    [
        (ResourceScopeKind.PROJECT, "projects/123456789"),
        (ResourceScopeKind.FOLDER, "folders/987654321"),
        (ResourceScopeKind.ORGANIZATION, "organizations/456789123"),
    ],
)
def test_resource_scope_preserves_canonical_provider_name(
    kind: ResourceScopeKind,
    canonical_name: str,
) -> None:
    """Every supported container kind retains its canonical resource name."""
    scope = ResourceScope(kind=kind, canonical_name=canonical_name)

    assert scope.kind is kind
    assert scope.canonical_name == canonical_name


@pytest.mark.parametrize(
    ("kind", "canonical_name"),
    [
        (ResourceScopeKind.PROJECT, "folders/123"),
        (ResourceScopeKind.FOLDER, "folders/project-name"),
        (ResourceScopeKind.ORGANIZATION, "organizations/12/children/34"),
        (ResourceScopeKind.PROJECT, "projects/١٢٣"),
    ],
)
def test_resource_scope_rejects_noncanonical_name(
    kind: ResourceScopeKind,
    canonical_name: str,
) -> None:
    """Kind, collection, and ASCII numeric identity must agree."""
    with pytest.raises(ValueError, match="canonical resource name"):
        ResourceScope(kind=kind, canonical_name=canonical_name)


def test_resource_scope_rejects_untyped_identity_components() -> None:
    """Closed scope kinds and canonical names require their exact types."""
    with pytest.raises(TypeError, match="ResourceScopeKind"):
        ResourceScope(
            kind=cast("ResourceScopeKind", "project"),
            canonical_name="projects/123",
        )
    with pytest.raises(TypeError, match="must be a string"):
        ResourceScope(
            kind=ResourceScopeKind.PROJECT,
            canonical_name=cast("str", 123),
        )

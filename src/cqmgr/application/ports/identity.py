"""Ports for ADC identity evidence and canonical project resolution."""

from __future__ import annotations

from typing import TYPE_CHECKING, Protocol

if TYPE_CHECKING:
    from cqmgr.domain.identity import ADCIdentityEvidence, ADCQuotaProject
    from cqmgr.domain.projects import ProjectReference, ProjectResolution


class IdentityProvider(Protocol):
    """Resolve refreshed ADC into safe provider-neutral identity evidence."""

    async def resolve(
        self,
        *,
        adc_quota_project: ADCQuotaProject | None = None,
        timeout_seconds: float = 10.0,
    ) -> ADCIdentityEvidence:
        """Resolve identity without switching or brokering credentials."""
        ...


class ProjectResolver(Protocol):
    """Canonicalize one explicit project through Resource Manager."""

    async def resolve(self, reference: ProjectReference) -> ProjectResolution:
        """Return safe canonical evidence or a typed diagnostic failure."""
        ...

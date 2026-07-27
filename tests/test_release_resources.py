"""Checked-in runtime release resources are deterministic and secret-free."""

from __future__ import annotations

import json
import runpy
from pathlib import Path
from typing import Any, cast

from cqmgr.domain.accelerator_overlay import MAINTAINED_ACCELERATOR_OVERLAY

ROOT = Path(__file__).parents[1]
SCRIPT = ROOT / "scripts" / "generate_release_resources.py"
RESOURCES = ROOT / "src" / "cqmgr" / "resources"


def test_checked_in_release_resources_match_the_deterministic_generator(
    tmp_path: Path,
) -> None:
    """Reviewed resource bytes are reproducible from public hermetic fixtures."""
    module = runpy.run_path(str(SCRIPT))
    generate_release_resources = cast("Any", module["generate_release_resources"])

    generate_release_resources(ROOT / "tests" / "fixtures" / "google", tmp_path)

    expected = {
        path.relative_to(RESOURCES): path.read_bytes()
        for path in RESOURCES.rglob("*")
        if path.is_file()
    }
    actual = {
        path.relative_to(tmp_path): path.read_bytes()
        for path in tmp_path.rglob("*")
        if path.is_file()
    }
    assert actual == expected


def test_release_evidence_records_exact_sources_and_honest_limitations() -> None:
    """Hermetic evidence is release-relative and never claims physical capacity."""
    evidence = json.loads((RESOURCES / "release-evidence.json").read_text())

    assert evidence["schema"] == "cqmgr.release-evidence/v1"
    assert evidence["overlay"]["content_digest"] == (
        MAINTAINED_ACCELERATOR_OVERLAY.metadata.content_digest
    )
    assert {source["name"] for source in evidence["sources"]} == {
        "compute-accelerator-types",
        "compute-machine-types",
        "tpu-accelerator-types",
        "tpu-locations",
        "tpu-runtime-versions",
    }
    assert evidence["scenario_coverage"] == {
        "duplicate-and-reordered-pages": True,
        "location-local-failure": True,
        "partial-success": True,
        "provider-unknown-lifecycle": True,
        "terminal-pagination": True,
    }
    assert evidence["claims"]["physical_capacity"] is False
    assert evidence["claims"]["universal_availability"] is False
    encoded = json.dumps(evidence, sort_keys=True)
    assert "projects/" not in encoded
    assert "@" not in encoded


def test_packaged_schema_catalog_closes_single_and_bundle_discriminators() -> None:
    """Published schema identities retain exact lifecycle subject kinds."""
    catalog = json.loads((RESOURCES / "schemas" / "catalog.json").read_text())

    assert catalog["schema"] == "cqmgr.schema-catalog/v1"
    assert catalog["public_record_schemas"] == [
        "cqmgr.accelerator-catalog/v1",
        "cqmgr.audit-record/v1",
        "cqmgr.operation-result/v1",
        "cqmgr.quota-request-plan/v1",
        "cqmgr.watch-event/v1",
    ]
    assert catalog["quota_request_plan"]["kinds"] == ["bundle", "single"]


def test_packaged_overlay_uses_clean_quota_unit_symbols() -> None:
    """Release resources never serialize dataclass representations as unit values."""
    overlay = json.loads((RESOURCES / "accelerator-overlay.json").read_text())

    units = {mapping["selector"]["unit"] for mapping in overlay["mappings"]}
    assert units == {"1", "core"}

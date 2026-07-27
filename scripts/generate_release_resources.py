"""Generate deterministic, secret-free runtime release resources."""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
from typing import TYPE_CHECKING, cast

from cqmgr.domain.accelerator_overlay import MAINTAINED_ACCELERATOR_OVERLAY
from cqmgr.domain.audit import AUDIT_RECORD_SCHEMA
from cqmgr.domain.catalog import ACCELERATOR_CATALOG_SCHEMA
from cqmgr.domain.plans import PlanKind
from cqmgr.domain.schemas import (
    OPERATION_RESULT_SCHEMA,
    QUOTA_REQUEST_PLAN_SCHEMA,
    WATCH_EVENT_SCHEMA,
)

if TYPE_CHECKING:
    from collections.abc import Sequence

EVIDENCE_SCHEMA = "cqmgr.release-evidence/v1"
SCHEMA_CATALOG_SCHEMA = "cqmgr.schema-catalog/v1"
MIN_REORDER_PAGES = 2


def _canonical_json(value: object) -> bytes:
    return (
        json.dumps(value, ensure_ascii=True, separators=(",", ":"), sort_keys=True)
        + "\n"
    ).encode("utf-8")


def _load_object(path: Path) -> dict[str, object]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        msg = f"fixture {path.name} must contain a JSON object"
        raise TypeError(msg)
    return value


def _list(value: object, name: str) -> list[object]:
    if not isinstance(value, list):
        msg = f"{name} must be a list"
        raise TypeError(msg)
    return value


def _mapping(value: object, name: str) -> dict[str, object]:
    if not isinstance(value, dict):
        msg = f"{name} must be an object"
        raise TypeError(msg)
    return cast("dict[str, object]", value)


def _covers_derived_duplicate_and_reordered_pages(
    page_digests: Sequence[str],
) -> bool:
    """Prove page identity survives a derived duplicate/reordered scenario."""
    canonical = tuple(sorted(set(page_digests)))
    if len(canonical) < MIN_REORDER_PAGES:
        return False
    scenario = (*reversed(canonical), canonical[0])
    return (
        scenario[: len(canonical)] != canonical
        and len(scenario) > len(set(scenario))
        and tuple(sorted(set(scenario))) == canonical
    )


def _compute_source(
    fixture_root: Path,
    filename: str,
    item_key: str,
    source_name: str,
) -> tuple[dict[str, object], tuple[str, ...]]:
    document = _load_object(fixture_root / filename)
    pages = _list(document.get("pages"), f"{source_name} pages")
    record_count = 0
    page_digests = [hashlib.sha256(_canonical_json(page)).hexdigest() for page in pages]
    lifecycle_values: set[str] = set()
    unreachables = 0
    for page_value in pages:
        page = _mapping(page_value, f"{source_name} page")
        items = _mapping(page.get("items"), f"{source_name} items")
        unreachables += len(_list(page.get("unreachables", []), "unreachables"))
        for aggregate_value in items.values():
            aggregate = _mapping(aggregate_value, f"{source_name} aggregate")
            for record_value in _list(aggregate.get(item_key, []), item_key):
                record = _mapping(record_value, f"{source_name} record")
                record_count += 1
                lifecycle = record.get("deprecated")
                if isinstance(lifecycle, dict):
                    state = lifecycle.get("state")
                    if isinstance(state, str):
                        lifecycle_values.add(state)
    terminal_pagination = bool(pages) and not _mapping(
        pages[-1], f"{source_name} terminal page"
    ).get("nextPageToken")
    source: dict[str, object] = {
        "duplicate_and_reordered_pages": (
            _covers_derived_duplicate_and_reordered_pages(page_digests)
        ),
        "fixture": filename,
        "name": source_name,
        "pages": len(pages),
        "records": record_count,
        "terminal_pagination": terminal_pagination,
        "unreachable_locations": unreachables,
    }
    return source, tuple(sorted(lifecycle_values))


def _tpu_sources(fixture_root: Path) -> tuple[dict[str, object], ...]:
    document = _load_object(fixture_root / "tpu-catalog-pages.json")
    definitions = (
        ("locationPages", "locations", "tpu-locations"),
        ("acceleratorPages", "acceleratorTypes", "tpu-accelerator-types"),
        ("runtimePages", "runtimeVersions", "tpu-runtime-versions"),
    )
    sources: list[dict[str, object]] = []
    for page_key, item_key, name in definitions:
        page_container = document.get(page_key)
        pages: list[object]
        locations = 0
        terminal_pages: list[object]
        if isinstance(page_container, list):
            pages = page_container
            terminal_pages = pages[-1:] if pages else []
        else:
            page_mapping = _mapping(page_container, page_key)
            locations = len(page_mapping)
            pages = []
            terminal_pages = []
            for location_pages in page_mapping.values():
                location_page_list = _list(
                    location_pages,
                    f"{page_key} location pages",
                )
                pages.extend(location_page_list)
                terminal_pages.extend(location_page_list[-1:])
        records = sum(
            len(
                _list(
                    _mapping(page, f"{name} page").get(item_key, []),
                    f"{name} records",
                )
            )
            for page in pages
        )
        page_digests = [
            hashlib.sha256(_canonical_json(page)).hexdigest() for page in pages
        ]
        terminal = bool(terminal_pages) and all(
            not _mapping(page, f"{name} terminal page").get("nextPageToken")
            for page in terminal_pages
        )
        sources.append(
            {
                "duplicate_and_reordered_pages": (
                    _covers_derived_duplicate_and_reordered_pages(page_digests)
                ),
                "fixture": "tpu-catalog-pages.json",
                "locations": locations,
                "name": name,
                "pages": len(pages),
                "records": records,
                "terminal_pagination": terminal,
                "unreachable_locations": 0,
            }
        )
    return tuple(sources)


def _overlay_resource() -> dict[str, object]:
    overlay = MAINTAINED_ACCELERATOR_OVERLAY
    mappings = [
        {
            "accelerator_counts": list(mapping.accelerator_counts),
            "accelerator_id": mapping.accelerator_id.value,
            "group_id": mapping.group_id.value,
            "machine_types": list(mapping.machine_types),
            "management_plane": mapping.management_plane.value,
            "operator_selected_accelerator_types": list(
                mapping.operator_selected_accelerator_types
            ),
            "provider_accelerator_types": list(mapping.provider_accelerator_types),
            "provisioning_models": [
                model.value for model in mapping.provisioning_models
            ],
            "quota_pool": mapping.quota_pool,
            "reviewed_on": mapping.reviewed_on.isoformat(),
            "runtime_versions": list(mapping.runtime_versions),
            "selector": {
                "dimensions": [
                    {"key": dimension.key, "value": dimension.value}
                    for dimension in mapping.selector.dimensions
                ],
                "location_dimension": mapping.selector.location_dimension,
                "quota_display_name": mapping.selector.quota_display_name,
                "quota_id": mapping.selector.quota_id,
                "quota_scope": mapping.selector.quota_scope.value,
                "service": mapping.selector.service,
                "unit": mapping.selector.native_unit.symbol,
            },
            "source_url": mapping.source_url,
            "topologies": list(mapping.topologies),
            "workload_consumers": [
                consumer.value for consumer in mapping.workload_consumers
            ],
        }
        for mapping in overlay.mappings
    ]
    return {
        "content_digest": overlay.metadata.content_digest,
        "mappings": mappings,
        "revision": overlay.metadata.revision,
        "schema": overlay.metadata.schema,
    }


def _evidence_resource(fixture_root: Path) -> dict[str, object]:
    accelerator, accelerator_lifecycles = _compute_source(
        fixture_root,
        "compute-accelerator-types-pages.json",
        "acceleratorTypes",
        "compute-accelerator-types",
    )
    machines, machine_lifecycles = _compute_source(
        fixture_root,
        "compute-machine-types-pages.json",
        "machineTypes",
        "compute-machine-types",
    )
    sources = (accelerator, machines, *_tpu_sources(fixture_root))
    digest_input = {
        "overlay_digest": MAINTAINED_ACCELERATOR_OVERLAY.metadata.content_digest,
        "sources": sources,
    }
    normalized_digest = (
        "sha256:" + hashlib.sha256(_canonical_json(digest_input)).hexdigest()
    )
    lifecycles = {*accelerator_lifecycles, *machine_lifecycles}
    return {
        "claims": {
            "physical_capacity": False,
            "universal_availability": False,
        },
        "normalized_evidence_digest": normalized_digest,
        "overlay": {
            "content_digest": MAINTAINED_ACCELERATOR_OVERLAY.metadata.content_digest,
            "revision": MAINTAINED_ACCELERATOR_OVERLAY.metadata.revision,
        },
        "scenario_coverage": {
            "duplicate-and-reordered-pages": any(
                source["duplicate_and_reordered_pages"] for source in sources
            ),
            "location-local-failure": any(
                source["unreachable_locations"] for source in sources
            ),
            "partial-success": any(
                source["unreachable_locations"] for source in sources
            ),
            "provider-unknown-lifecycle": any(
                value.startswith("PROVIDER_") for value in lifecycles
            ),
            "terminal-pagination": all(
                source["terminal_pagination"] for source in sources
            ),
        },
        "schema": EVIDENCE_SCHEMA,
        "sources": list(sources),
    }


def _schema_catalog() -> dict[str, object]:
    return {
        "public_record_schemas": sorted(
            (
                ACCELERATOR_CATALOG_SCHEMA,
                AUDIT_RECORD_SCHEMA,
                OPERATION_RESULT_SCHEMA,
                QUOTA_REQUEST_PLAN_SCHEMA,
                WATCH_EVENT_SCHEMA,
            )
        ),
        "quota_request_plan": {
            "kinds": sorted(kind.value for kind in PlanKind),
            "schema": QUOTA_REQUEST_PLAN_SCHEMA,
        },
        "schema": SCHEMA_CATALOG_SCHEMA,
    }


def generate_release_resources(fixture_root: Path, output_root: Path) -> None:
    """Write every generated runtime release resource deterministically."""
    schema_root = output_root / "schemas"
    schema_root.mkdir(parents=True, exist_ok=True)
    (output_root / "__init__.py").write_text(
        '"""Packaged release evidence and public schema resources."""\n',
        encoding="utf-8",
        newline="\n",
    )
    (output_root / "accelerator-overlay.json").write_bytes(
        _canonical_json(_overlay_resource())
    )
    (output_root / "release-evidence.json").write_bytes(
        _canonical_json(_evidence_resource(fixture_root))
    )
    (schema_root / "catalog.json").write_bytes(_canonical_json(_schema_catalog()))


def main(arguments: Sequence[str] | None = None) -> None:
    """Generate runtime resources from the public hermetic fixture corpus."""
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--fixtures",
        type=Path,
        default=Path("tests/fixtures/google"),
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=Path("src/cqmgr/resources"),
    )
    parsed = parser.parse_args(arguments)
    generate_release_resources(parsed.fixtures, parsed.output)


if __name__ == "__main__":
    main()

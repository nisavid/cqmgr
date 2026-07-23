---
status: superseded
superseded_by: 0004-use-a-versioned-federated-provider-inventory
---

# Use versioned accelerator overlays and bounded product snapshots

This historical decision is superseded by ADR 0004. Use ADR 0004 for the
federated provider inventory and bare-query behavior.

Cloud Quota Manager will expose two stable V1 accelerator catalog groups:
`compute-accelerators` for Compute-owned NVIDIA GPU and Compute/GKE TPU
evidence, and `cloud-tpu-legacy` for the legacy Cloud TPU API management
plane. Group and accelerator identifiers are immutable lowercase semantic
identities. Their identifiers do not contain a version.

Catalog content uses schema `cqmgr.accelerator-catalog/v1`. Each maintained
mapping records its first-party source and review date, and each complete
overlay has an immutable content digest. Live provider discovery remains
authoritative. The overlay explains relationships among accelerators, machine
shapes, topologies, consumption modes, quota pools, native units, locations,
lifecycle, restrictions, and exact quota selectors; it never creates a
discovered slice, implies mutability, or claims capacity. GKE remains a
workload-consumer overlay over Compute-owned evidence and adds no Container API
reads.

A quota query has exactly one source: one canonical provider service or one
accelerator catalog group. Repeated filter values are OR alternatives within a
facet, and distinct facets are AND constraints. A successful sorted page is
derived only after the required bounded provider and catalog collection is
complete. Sorting is deterministic, uses an exact slice identity as the final
tie-breaker, keeps missing values last, and never compares numeric values in
different native units. An incomplete scan retains usable evidence and source
gaps but does not claim globally complete ordering.

Continuation uses an opaque, installation-local handle to a bounded immutable
product snapshot rather than exposing or wrapping a provider page token. The
snapshot binds resource scope, query source, normalized filters, sort,
evidence-contract version, catalog schema and content digest, observation
times, completeness, and canonical ordered identities. Provider continuation
tokens remain adapter-internal while the snapshot is collected. An unknown,
expired, or mismatched cursor fails before provider access. Cursor storage
contains only normalized safe evidence and does not depend on a native keyring.

Catalog coverage is explicit per provider source and location. Compute
aggregated machine-type reads use partial-success behavior and retain each
scope warning or failure. Legacy Cloud TPU location, accelerator-type, and
runtime-version reads retain independent per-zone coverage. An explicitly
selected location may resolve when all evidence required for that location and
its quota constraints is complete even if unrelated locations failed; those
failures remain visible. A failed location is never treated as an empty global
catalog, and a successful location never implies global coverage.

These choices preserve exact quota-slice identity and independent regional,
global, zonal, and quota-pool constraints. Workload resolution produces a
native-unit quota requirement and stops on ambiguous compatibility, conversion,
provider identity, or eligibility. It may report that quota permits a request;
it never reports that capacity is available.

---
status: accepted
supersedes: 0003-use-versioned-accelerator-overlays-and-product-snapshots
---

# Use a versioned federated provider inventory

Cloud Quota Manager V1 reads one versioned provider inventory set containing
`compute.googleapis.com` and `tpu.googleapis.com`. Bare quota browsing collects
both sources. Service and accelerator-catalog filters narrow both the provider
read set and the displayed results: `compute` and `tpu` normalize to their
canonical service DNS names, and a catalog group selects only the providers
required by that group. V1 does not enumerate enabled services, accept an
arbitrary service as an inventory source, or claim a cross-provider quota total.

The inventory-set identity binds its evidence-contract version, fixed provider
membership, normalized source-selecting filters, queried provider subset,
accelerator-catalog schema and content digest, observation times, and per-source
coverage. A successful globally sorted page requires complete bounded
collection from every provider selected by the normalized query. An incomplete
collection retains usable evidence, reports each missing or failed selected
source, and does not claim a globally complete ordering for that query.

Continuation uses an opaque, installation-local handle to a bounded immutable
product snapshot rather than exposing or wrapping a provider page token. The
snapshot binds the resource scope, inventory-set identity, normalized filters,
sort, catalog revision, observation times, completeness, and canonical ordered
slice identities. Provider continuation tokens remain adapter-internal while
the snapshot is collected. An unknown, expired, or mismatched cursor fails
before provider access. Cursor storage contains only normalized safe evidence
and does not depend on a native keyring.

The accelerator catalog is release-relative and includes every specialized
hardware identity declared by the two supported provider sources at the
catalog's review point. Catalog presence does not imply guided support.
Accelerator-specific guidance exists only when maintained first-party evidence
binds the provider identity, compatibility, exact quota selectors, native-unit
conversion, provisioning model, quota pool, and required companion constraints.
The V1 guided scope includes A4. Unknown or newly declared provider hardware
remains visible as discovered provider truth, triggers a reviewed catalog
refresh, and never receives guidance without the required exact evidence.

Catalog content uses schema `cqmgr.accelerator-catalog/v1`. Each maintained
mapping records its first-party source and review date, and each complete
overlay has an immutable content digest. Live provider discovery remains
authoritative. The overlay never creates a discovered slice, implies
mutability, or claims capacity. V1 accepts no workload-consumer input because
Compute Engine and GKE resolve to the same Compute-owned quota constraints;
results report supported consumers as metadata. GKE remains a semantic overlay
over Compute-owned evidence and adds no Container API reads.

Workload resolution is workload-first and has two explicit shapes:

- a Compute instance supplies machine type, instance count, provisioning model,
  and either explicit candidate locations or all compatible locations; and
- a Cloud TPU slice supplies accelerator type, topology, runtime version, slice
  count, provisioning model, and either explicit candidate locations or all
  compatible locations.

The resolver derives the applicable accelerator identity, quota pool, native
unit, and exact limiting slices. It returns an independent constraint set for
each compatible requested location. It does not rank locations, select a best
location, infer capacity, or silently broaden the candidate set. Missing,
ambiguous, unsupported, or ineligible compatibility and conversion evidence
stops guidance for the affected location with explicit coverage.

Compute aggregated machine-type reads retain each scope warning or failure.
Cloud TPU location, accelerator-type, and runtime-version reads retain
independent per-location coverage. A requested location may resolve when all
evidence required for that location and its quota constraints is complete even
if unrelated locations fail; those failures remain visible. A failed location
is never treated as an empty inventory, and a successful location never implies
global coverage.

These choices preserve exact quota-slice identity and independent regional,
global, zonal, and quota-pool constraints. Resolution may report that quota
permits a workload; it never reports that physical capacity is available.

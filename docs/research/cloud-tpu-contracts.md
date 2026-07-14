# Current Cloud TPU quota and accelerator contracts

Research date: 2026-07-14

This note records the current first-party Google Cloud contracts that constrain
TPU support in the Cloud Quotas manager. It uses public documentation and API
references only. It does not describe observed private-project state and did
not mutate Google Cloud resources.

## Executive summary

- A guided TPU workflow must choose the **management plane first**. TPU VMs
  managed through Compute Engine or GKE consume Compute Engine quota;
  resources managed through the Cloud TPU API consume a different Cloud TPU
  service quota. The two quota families differ in service owner, scope, unit,
  dimensions, and supported TPU generations. [Cloud TPU quotas][cloud-tpu-quota]
- Compute Engine is the forward-looking surface. Google says the Cloud TPU API
  is no longer under active development, receives only bug and security fixes,
  and does not support TPU7x or later generations. Google recommends Compute
  Engine or GKE for current features. [Manage queued resources][queued-resources]
- The generic Cloud Quotas core can discover and manage both quota families as
  `QuotaInfo` and `QuotaPreference` resources. TPU support still needs a
  domain-specific catalog that maps management plane, TPU version, provisioning
  mode, machine or accelerator shape, chip or core count, and location to the
  owning service's exact quota slice. [Cloud Quotas API overview][quotas-api]
- Quota is only an allocation limit. It does not reserve or guarantee physical
  TPU capacity, even when a regional or zonal limit has room. [Compute Engine
  allocation quotas][compute-quotas]

## Two service-owned quota families

### Compute Engine and GKE-managed TPUs

Compute Engine quota is owned by `compute.googleapis.com`. It limits TPU use
per project and **region**. Running instances and reservations consume it.
Standard and preemptible consumption use separate quota slices. Google tells
operators to request the quota for the intended TPU version in every target
region. [Compute Engine allocation quotas][compute-quotas]

The current Compute Engine and GKE documentation exposes this quota catalog:

| TPU version / machine prefix | Standard quota selector | Preemptible quota selector |
| --- | --- | --- |
| v3 / `ct3-` | dimension `tpu_family=CT3` | not applicable |
| v3 Pod / `ct3p-` | dimension `tpu_family=CT3P` | not applicable |
| v4 / `ct4p-` | `TPU v4 PodSlice chips` | `Preemptible TPU v4 PodSlice chips` |
| v5e / `ct5lp-` | `TPU v5 Lite PodSlice chips` | `Preemptible TPU v5 Lite Podslice chips` |
| v5p / `ct5p-` | `TPU v5p chips` | `Preemptible TPU v5p chips` |
| v6e / `ct6e-` | `TPUs per TPU family`, dimension `tpu_family=CT6E` | `Preemptible TPU slices v6e` |
| TPU7x / `tpu7x-standard-4t` | `TPUs per TPU family`, dimension `tpu_family=tpu7x` | `Preemptible TPU slices tpu7x` |

These are display names and documented dimensions, not stable product keys.
The product must discover the exact `QuotaInfo.quotaId`, metric, dimensions,
unit, effective value, and applicable locations for the selected project. In
particular, the selectors mix dimensions with names and the names mix “TPUs,”
“slices,” and “chips”; the product must not silently reinterpret a quota value
as a different unit. [Compute Engine allocation quotas][compute-quotas], [GKE
TPU troubleshooting][gke-tpu-quota], and [QuotaInfo resource][quota-info]

GKE uses this same Compute Engine quota family for TPU nodes, including older
machine families not listed on the current direct Compute Engine TPU machine
page. A GKE-specific provisioning workflow has additional cluster, node-pool,
accelerator-label, and topology constraints, but it must not switch to Cloud
TPU service quota. [Cloud TPU quotas][cloud-tpu-quota] and [GKE TPU
troubleshooting][gke-tpu-quota]

### Cloud TPU API-managed TPUs

Cloud TPU API quota is owned by `tpu.googleapis.com` and limits TPU **cores**
per project and **zone**. Each TPU generation has separate on-demand and
preemptible/Spot quotas. The current documentation names quota families for
v6e, v5p, v5e, v4, v3 Pod, v3, v2 Pod, and v2. For example, v6e uses `TPU v6e
cores per project per zone` and `Preemptible TPU v6e cores per project per
zone`; v5p uses the equivalent v5p names; and v5e uses `TPU v5 lite pod cores
per project per zone` and its preemptible counterpart. [Cloud TPU
quotas][cloud-tpu-quota]

Default values and automatic-approval thresholds are not catalog constants.
They vary by generation and zone, and increases can roll out gradually before
the new value appears in Cloud Quotas. The product must read the project's
current `QuotaInfo` rather than embedding the documentation tables. [Cloud TPU
quotas][cloud-tpu-quota]

This management plane is a legacy compatibility surface. It covers TPU v2
through v6e in the v2 `AcceleratorConfig` enum, but not TPU7x. New-generation
guided flows must therefore use the Compute Engine/GKE quota family.
[AcceleratorConfig][accelerator-config] and [Manage queued
resources][queued-resources]

## Accelerator and location catalogs

### Compute Engine catalog

For Compute Engine, the deployable object is a TPU machine type. Each TPU
version has one or more machine types, and each machine type fixes a topology
and number of attached TPU chips. Current examples include
`tpu7x-standard-4t`, `ct6e-standard-1t`, `ct6e-standard-4t`,
`ct6e-standard-8t`, and `ct5p-hightpu-4t`. Supported topology and slice sizes
are version- and machine-specific. [TPU machines][tpu-machines]

`machineTypes.aggregatedList` is the live, project-scoped enumeration surface:

```text
GET https://compute.googleapis.com/compute/v1/projects/{project}/aggregated/machineTypes
```

The response groups machine types by zone. A `MachineType` includes its `name`,
`zone`, deprecation state, and assigned accelerator type/count. Google
recommends `returnPartialSuccess=true` for the aggregated request. This API can
confirm which machine types are visible in which zones, but its schema does not
express the full TPU topology table or consumption-option support matrix.
[Machine types aggregated list][machine-types-list] and [MachineType
resource][machine-type]

The versioned TPU catalog therefore needs two inputs:

1. a maintained first-party compatibility table for version, machine type,
   topology, chips per VM/slice, and supported consumption options; and
2. live Compute Engine machine-type enumeration to confirm project-visible
   zone availability and deprecation state.

The product must not derive chip count, topology, or quota unit by parsing a
machine-type string. It should keep the documented mapping as explicit data and
show its source/update date. Current location support changes independently;
Google's regions-and-zones table was updated on 2026-07-10 and includes
specialized AI zones in addition to standard zones. [TPU regions and
zones][tpu-locations]

### Cloud TPU API catalog

The Cloud TPU API has project-and-zone-scoped live discovery endpoints:

```text
GET https://tpu.googleapis.com/v2/projects/{project}/locations/{zone}/acceleratorTypes
GET https://tpu.googleapis.com/v2/projects/{project}/locations/{zone}/runtimeVersions
```

`acceleratorTypes.list` requires `tpu.acceleratortypes.list`, supports
pagination, filtering, and ordering, and returns accelerator type records. An
`AcceleratorType` includes its type string and accelerator configurations; an
`AcceleratorConfig` explicitly records TPU version and topology in chips.
Runtime versions are a separate catalog and require
`tpu.runtimeversions.list`. The generic Google locations list supplies the
service's supported locations. [Accelerator types list][accelerator-list],
[AcceleratorType resource][accelerator-type], [AcceleratorConfig][accelerator-config],
and [Runtime versions list][runtime-list]

A legacy guided flow must select a mutually compatible zone, accelerator type
or configuration, and runtime version. Quota discovery alone cannot establish
that compatibility.

## Provisioning modes and quota selection

The supported consumption options for current Compute Engine TPU machines are
on-demand, Spot, Flex-start, on-demand reservations, future reservations of one
year or longer, and future reservations in calendar mode. Availability varies
by TPU version and zone; some TPU7x options require allowlisting. On-demand is
the default and does not guarantee availability. [TPU machines][tpu-machines]

Their quota behavior is not one-to-one with their marketing names:

- On-demand instances consume standard/on-demand quota. Creating a TPU
  reservation raises both the corresponding quota limit and usage by its chip
  count; a GKE node pool consuming that existing reservation requires no
  additional TPU quota. The product must distinguish creating reserved
  capacity from consuming it. [GKE TPU troubleshooting][gke-tpu-quota]
- Spot consumes preemptible quota.
- Compute Engine documents Flex-start among the VM classes to which
  preemptible CPU, GPU, and local-SSD quotas apply, and the TPU planning guide
  requires either on-demand or preemptible quota for every consumption option.
  The guided flow must verify the relevant TPU `QuotaInfo` instead of assuming
  Flex-start is quota-free. [Compute Engine allocation quotas][compute-quotas]
  and [Plan TPU resources][plan-tpus]
- A granted quota still does not ensure that an on-demand or queued request can
  obtain capacity. Reservations are the separate capacity-assurance mechanism.

The legacy Cloud TPU API defaults to on-demand. Spot or preemptible nodes use
the preemptible quota family. Queued resources distinguish `spot` and
`guaranteed` tiers and can name a reservation. Their lifecycle includes
`WAITING_FOR_RESOURCES`, `PROVISIONING`, `ACTIVE`, `FAILED`, `SUSPENDING`, and
`SUSPENDED`; `WAITING_FOR_RESOURCES` replaced the older `ACCEPTED` waiting
state. This lifecycle is a provisioning contract, not quota-preference
reconciliation. [QueuedResource resource][queued-resource] and [Manage queued
resources][queued-resources]

## TPU quota-preference mutation

TPU quota adjustment uses the generic Cloud Quotas preference contract, not a
TPU-specific mutation endpoint. After catalog resolution, the product creates
or updates a `QuotaPreference` with the exact owning `service`, discovered
`quotaId`, exact dimension map, and an absolute `preferredValue` expressed in
the quota's native unit. The corresponding `QuotaInfo` supplies adjustment
eligibility and the enforced value. A preference can reconcile asynchronously,
so acceptance is not approval and approval is not capacity. [QuotaPreference
resource][quota-preference], [QuotaInfo resource][quota-info], and [Cloud TPU
quotas][cloud-tpu-quota]

The TPU catalog must never manufacture a preference from a display name. If it
cannot match a selected TPU requirement to one unambiguous `QuotaInfo` slice,
or if that slice is ineligible for an increase, the guided workflow must stop
before mutation and explain the discovered state.

## Guided v1 resolution contract

A complete guided lookup proceeds in this order:

1. choose `compute_engine`, `gke`, or `cloud_tpu_legacy`;
2. choose a TPU generation supported by that management plane;
3. choose the management-plane shape: machine type for Compute Engine/GKE, or
   accelerator configuration and runtime version for Cloud TPU API;
4. choose the consumption mode supported by that shape;
5. choose a supported zone and derive its region;
6. derive the quota owner and standard/preemptible family, then match it to the
   project's live `QuotaInfo` by service, quota ID, dimensions, unit, and
   applicable location;
7. show the selected shape, required quota amount in its native unit, current
   effective limit, usage when available, and the capacity caveat;
8. only after explicit mutation confirmation, validate and upsert the exact
   `QuotaPreference`, then observe preference reconciliation separately from
   any later resource-provisioning state.

This ordering prevents a user from selecting a plausible-looking TPU quota
that belongs to the wrong service, scope, unit, or provisioning mode.

## What the generic Cloud Quotas core can own

The existing generic core can own, unchanged:

- project/container identity and Application Default Credentials;
- `QuotaInfo` enumeration under a selected service;
- effective quota slices keyed by service, quota ID, and normalized dimensions;
- `QuotaPreference` validation, deterministic identity, create/update,
  reconciliation, and granted-value observation;
- common eligibility, pending, approved, partially approved, and rollout state;
- the distinction between effective quota, usage, and physical capacity.

Both TPU quota families should enter that core as ordinary discovered quota
slices. Cloud Quotas resource names remain under `locations/global`; the
actual region or zone belongs in the quota dimensions and applicable-location
metadata. [Cloud Quotas API overview][quotas-api] and [QuotaInfo
resource][quota-info]

## What requires TPU-specific catalog semantics

TPU support needs a separate adapter and domain model for:

- `management_plane`: `compute_engine`, `gke`, or `cloud_tpu_legacy`;
- TPU generation and its supported management planes;
- machine type for Compute Engine/GKE versus accelerator type/configuration and
  runtime version for Cloud TPU API;
- topology, slice size, chip count, core count, and the quota's native unit;
- zone availability and its containing region;
- provisioning/consumption mode and the corresponding standard or
  preemptible quota family;
- the exact owning service, discovered quota ID, required dimensions, and
  applicable locations;
- compatibility and access status such as Preview, allowlisted, deprecated, or
  unsupported.

The catalog should produce a **quota requirement** that the generic core can
evaluate, rather than teaching the generic quota model about TPU versions. A
requirement should retain its source unit and explain the conversion from the
selected machine/slice to that unit. If the public contracts do not provide an
unambiguous conversion, the product must stop and show the unresolved mapping
rather than guess.

## Product constraints derived from the contracts

1. Ask for the management plane before showing TPU quota choices. It determines
   the service owner, scope, supported versions, and catalog vocabulary.
2. Default new workflows to Compute Engine or GKE. Label Cloud TPU API support
   as legacy and exclude TPU7x from it.
3. Discover exact quota IDs and values from `QuotaInfo`; use documentation names
   only for recognition and explanation.
4. Keep region and zone distinct. Compute Engine quota is regional while the
   current Cloud TPU API quota is zonal, even though both provision TPU VMs in
   zones.
5. Preserve quota units. Never present chips, cores, slices, hosts, VMs, and
   TensorCores as interchangeable counts.
6. Join the quota result to a TPU catalog entry before claiming that a selected
   shape is covered. A raw quota row cannot answer which topology, machine
   type, runtime, or provisioning mode is valid.
7. Refresh live machine/accelerator/location discovery and retain partial
   failures. Do not treat one unreachable zone as an empty global catalog.
8. Version and date the maintained compatibility table; Google can add zones,
   machine types, generations, or access restrictions without changing the
   generic Cloud Quotas schema.
9. Keep quota-preference reconciliation separate from queued-resource or VM
   provisioning state. They are different asynchronous systems.
10. Say “quota permits this request,” never “capacity is available.”

## Deliberate exclusions

This research does not choose which TPU management planes or generations v1
will support, define a static catalog file format, choose a refresh interval,
or design the operator screens. It does not validate project-specific quota
IDs or values, request quota changes, create TPU resources, or inspect private
Google Cloud state.

[accelerator-config]: https://docs.cloud.google.com/tpu/docs/reference/rest/v2/AcceleratorConfig
[accelerator-list]: https://docs.cloud.google.com/tpu/docs/reference/rest/v2/projects.locations.acceleratorTypes/list
[accelerator-type]: https://docs.cloud.google.com/tpu/docs/reference/rest/v2/projects.locations.acceleratorTypes
[cloud-tpu-quota]: https://docs.cloud.google.com/tpu/docs/quota
[compute-quotas]: https://docs.cloud.google.com/compute/resource-usage#tpu_quota
[gke-tpu-quota]: https://docs.cloud.google.com/kubernetes-engine/docs/troubleshooting/tpus#insufficient_quota
[machine-type]: https://docs.cloud.google.com/compute/docs/reference/rest/v1/machineTypes
[machine-types-list]: https://docs.cloud.google.com/compute/docs/reference/rest/v1/machineTypes/aggregatedList
[plan-tpus]: https://docs.cloud.google.com/tpu/docs/plan-tpus
[quota-info]: https://docs.cloud.google.com/docs/quotas/reference/rest/v1/projects.locations.services.quotaInfos
[quota-preference]: https://docs.cloud.google.com/docs/quotas/reference/rest/v1/projects.locations.quotaPreferences
[quotas-api]: https://docs.cloud.google.com/docs/quotas/api-overview
[queued-resource]: https://docs.cloud.google.com/tpu/docs/reference/rest/v2/projects.locations.queuedResources
[queued-resources]: https://docs.cloud.google.com/tpu/docs/queued-resources
[runtime-list]: https://docs.cloud.google.com/tpu/docs/reference/rest/v2/projects.locations.runtimeVersions/list
[tpu-locations]: https://docs.cloud.google.com/tpu/docs/regions-zones
[tpu-machines]: https://docs.cloud.google.com/compute/docs/tpus/tpu-machines

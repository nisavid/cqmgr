# Spot capacity-advice contracts

Google Cloud's capacity advice is a provider-owned, Preview read surface for
**Spot VMs**. It can inform an operator's workload-placement decision; it is
not an effective quota observation, a quota preference, or a capacity
reservation. Treat every result as decision support rather than a guarantee.

This note records the public contract as of 2026-07-20. All cited sources are
Google Cloud documentation.

## Product boundary

The first Cloud Quotas manager release may show provider advice only alongside
a cataloged, Spot-eligible Compute Engine VM workload. It must keep these
three facts visually and semantically separate:

| Signal | Provider meaning | Must not be presented as |
| --- | --- | --- |
| Effective quota | The currently granted limit for an exact quota slice. | Physical capacity or a successful VM placement. |
| Capacity advice | Best-effort advice about a specific prospective Spot VM request. | A quota result, reservation, or placement promise. |
| Reservation / guaranteed capacity | A separately approved capacity commitment with its own lifecycle. | An implication of advice or quota. |

Google explicitly says that an obtainability result does not guarantee capacity:
resources can become unavailable between the recommendation and VM creation.
Spot VM creation itself is a best-effort provisioning path; approved
reservations have a materially different assurance model. [View Spot VM
availability](https://cloud.google.com/compute/docs/instances/view-vm-availability)
[Provisioning models](https://cloud.google.com/compute/docs/instances/provisioning-models)

The feature is Preview and subject to the Pre-GA Offerings Terms. It can have
limited support and must remain a separately labeled, optional provider-advice
surface rather than a prerequisite for quota browsing or mutation. [Availability
guide](https://cloud.google.com/compute/docs/instances/view-vm-availability)

## `compute.advice.capacity`: current obtainability

`advice.capacity` is a `POST` to:

```text
https://compute.googleapis.com/compute/beta/projects/{project}/regions/{region}/advice/capacity
```

It requires the `compute` or `cloud-platform` OAuth scope and the
`compute.advice.capacity` IAM permission. Compute Viewer (`roles/compute.viewer`)
is the documented predefined read role. [REST
reference](https://cloud.google.com/compute/docs/reference/rest/beta/advice/capacity)
[Availability guide](https://cloud.google.com/compute/docs/instances/view-vm-availability)

### Request identity

A request must preserve the whole requested VM shape, not just an accelerator
name:

| Request field | Product mapping requirement |
| --- | --- |
| `instanceProperties.scheduling.provisioningModel` | First release sends and labels `SPOT`; do not reuse a result for Standard, Flex-start, or reservation-bound workloads. |
| `instanceFlexibilityPolicy.instanceSelections` | Map each selectable workload shape to one or more full Compute Engine machine-type names. Preserve alternatives as alternatives rather than inventing a combined capacity score. |
| `guestAccelerators` | For an N1 VM with attached GPUs, include both accelerator type and card count in its instance selection. The accelerator is part of the request identity. |
| `disks` | Include each requested scratch Local SSD where the machine type does not include it by default. |
| `size` | Preserve the requested VM count; obtainability answers the requested group, not one accelerator card. |
| `distributionPolicy.zones` and `targetShape` | Preserve the candidate zones and `ANY`, `ANY_SINGLE_ZONE`, or `BALANCED` intent. A region endpoint can be constrained to individual zones. |

The API response contains one initial provider-preferred recommendation, its
`scores`, and zero or more uniform placement `shards`. A shard identifies the
zone, machine type, instance count, and provisioning model. The product must
show the request identity with the returned shard allocation; it must not
attribute a regional score to a single zone that the recommendation did not
select. [REST
reference](https://cloud.google.com/compute/docs/reference/rest/beta/advice/capacity)

### Response semantics and freshness

- `obtainability` is a `0.0`–`1.0` score: the likelihood of successfully
  provisioning the requested number of Spot VMs. Google derives it from
  **real-time resource availability** and the success rate of **recent**
  creation requests. Its documented bands are high (`0.7`–`1.0`), medium
  (`0.4`–`0.6`), and low (`0.0`–`0.3`).
- `estimatedUptime` is a duration for how long the majority of the requested
  Spot VMs are expected to run before preemption. It is best effort, based on
  historical data and current conditions. The user guide describes 60-, 10-,
  and 1-minute outcomes; it is not an SLA or an uptime promise.
- The response provides no observation timestamp, validity window, or cache
  TTL. The first release must label the client-observed retrieval time, avoid
  claims such as “fresh for N minutes,” and mark a retained result stale by
  local policy rather than implying a provider guarantee.

[Availability guide](https://cloud.google.com/compute/docs/instances/view-vm-availability)
[REST reference](https://cloud.google.com/compute/docs/reference/rest/beta/advice/capacity)

## `compute.advice.capacityHistory`: historical Spot preemption and price

`advice.capacityHistory` is a `POST` to:

```text
https://compute.googleapis.com/compute/beta/projects/{project}/regions/{region}/advice/capacityHistory
```

It requires the `compute` or `cloud-platform` OAuth scope and the
`compute.advice.capacityHistory` IAM permission. Compute Viewer is the
documented predefined read role. `instanceProperties.machineType` identifies
one machine type, `instanceProperties.scheduling.provisioningModel` **must be
`SPOT`**, `locationPolicy.location` optionally selects a region or a zone, and
`types` selects the requested history categories. [REST
reference](https://cloud.google.com/compute/docs/reference/rest/beta/advice/capacityHistory)
[History guide](https://cloud.google.com/compute/docs/instances/view-spot-preemption-price)

### Allowed location and history combinations

| Scope | Request | Provider response / product use |
| --- | --- | --- |
| Region | Regional endpoint, omit `locationPolicy`; request `PREEMPTION` and optionally `PRICE`. | Present the returned region location as such. Historical price is supported at the region scope. |
| Zone | Keep the regional endpoint but set `locationPolicy.location` to the selected `zones/{zone}`; request `PREEMPTION`. | Present the returned zonal location as such. The public guide documents zonal preemption, not zonal price history. |

The response identifies its `machineType` and `location`. `preemptionHistory`
contains intervals and rates; `priceHistory` contains intervals and USD money
values. A response might omit an unrequested history category. [History
guide](https://cloud.google.com/compute/docs/instances/view-spot-preemption-price)
[REST reference](https://cloud.google.com/compute/docs/reference/rest/beta/advice/capacityHistory)

### Aggregation and temporal meaning

- `preemptionHistory` is daily data for the previous 30 days for the specified
  machine type and zone. Day boundaries are midnight Pacific Time; the current
  day's rate can change. The rate is provider-aggregated: the number of Spot
  VMs preempted that day divided by all Spot VMs of that type/location that
  stopped that day across Google Cloud. The denominator also includes user or
  programmatic suspend, stop, and delete events. It is not this project's
  preemption probability, a hardware-failure rate, or an individual workload's
  observed interruption rate.
- `priceHistory` is regional, USD, and spans the previous year. Each record is
  the hourly price for the interval in which it was active. Price changes occur
  at midnight Pacific Time; unavailable data creates interval gaps.
- Both interval endpoints must remain in the product record. They describe
  provider-defined time buckets: start is inclusive and end is exclusive in the
  REST schema. Do not relabel them as a rolling window or calculate an
  unsupported summary as though Google supplied it.

[History guide](https://cloud.google.com/compute/docs/instances/view-spot-preemption-price)
[REST reference](https://cloud.google.com/compute/docs/reference/rest/beta/advice/capacityHistory)

## Coverage and non-coverage

| Workload shape | Current capacity advice | Capacity history | First-release handling |
| --- | --- | --- | --- |
| Accelerator-optimized GPU VM | Within the documented Spot VM / machine-type flow. | Supported only when the machine type is otherwise supported by history. | Represent the machine type exactly; verify the resulting provider response before displaying advice. |
| N1 VM with attached GPU | Explicitly supported by `advice.capacity` REST through `guestAccelerators` with type and count. | **Not supported**: Google excludes N1 machine types with attached GPUs. | Allow a current-capacity query only when a complete N1+GPU shape is available; show historical preemption as unavailable, not zero or unknown. |
| Custom machine type | The current-advice guide does not state a custom-type exclusion. | **Not supported**: Google excludes custom machine types. | Do not claim current-advice support until the exact provider request is contract-tested; history is unavailable. |
| TPU | **Not supported**: Google says `advice.capacity` cannot view TPU availability. | **Not supported**: Google excludes TPUs from preemption-rate and price trends. | No TPU capacity-advice UI or fallback score in the first release. Keep TPU quota workflows independent. |

Google separately documents that Spot VM provisioning can cover several
accelerator-optimized GPU series and N1 VMs with attached GPUs. That
provisioning eligibility does not widen either advice API beyond the
limitations above. [Provisioning
models](https://cloud.google.com/compute/docs/instances/provisioning-models)
[Availability guide](https://cloud.google.com/compute/docs/instances/view-vm-availability)
[History guide](https://cloud.google.com/compute/docs/instances/view-spot-preemption-price)

## First-release product constraints

1. Advice remains read-only and provider-sourced. It cannot initiate, approve,
   or alter a quota preference, reservation, VM, MIG, or GKE workload.
2. Querying advice requires a separately authorized read capability. If either
   advice permission is absent, show that advice is unavailable without
   degrading effective-quota browsing.
3. Model an **advice query** as a complete, immutable request snapshot:
   project, endpoint region, selected zones, machine shape(s), N1 accelerator
   attachment when applicable, Local SSD attachment when applicable, Spot
   provisioning model, VM count, distribution shape, request time, and raw
   provider result. Never attach a score to an accelerator catalog item without
   that provenance.
4. Surface provider-returned location and shard allocation verbatim enough to
   distinguish a region from a zone. Never derive a per-zone obtainability
   number from a regional recommendation.
5. Show `obtainability`, `estimatedUptime`, and historical `preemptionRate` as
   distinct measures. The latter is a daily provider aggregate; it must not be
   displayed as the inverse of obtainability or as an uptime forecast.
6. Do not synthesize advice for unsupported shapes. For N1 attached GPUs,
   custom types, and TPUs, “unavailable” must carry the documented reason
   instead of a blank graph, zero, or a proxy from a neighboring machine type.
7. Preserve Preview status, best-effort language, observation time, request
   identity, and source links in both CLI and TUI. Advice is a placement hint,
   never an assertion that quota is sufficient or capacity is guaranteed.

# Cloud Quota Manager

Cloud Quota Manager presents effective quota state and manages quota requests,
targets, and grants. Its first complete product workflows cover specialized
accelerator hardware declared by the supported Compute and Cloud TPU provider
inventory while its core domain remains applicable to other quota families.

## Language

**Cloud Quota Manager**:
The product for inspecting effective quotas and managing quota requests, targets, and grants.
_Avoid_: Cloud Quotas manager, GPU capacity manager

**cqmgr**:
The executable, package stem, and repository tool name for Cloud Quota Manager.
_Avoid_: cloud-quotas, quota

**Quota inspector**:
The primary interactive workspace for browsing constraint sets and exact effective quota slices, inspecting their evidence, and entering valid next operations without losing resource-scope context.
_Avoid_: Dashboard, operation launcher

**Resource scope**:
The canonical project, folder, or organization within which the manager reads quota or manages a quota request. It is distinct from a quota's regional, global, or zonal scope.
_Avoid_: Target, default project

**Resource-scope selection**:
An explicit local selection of one resource scope that read operations and Preview may reuse visibly. It never comes from ambient `gcloud` state and never substitutes for Apply's exact resource-scope acknowledgement.
_Avoid_: Default project, ADC quota project

**Quota target**:
The absolute desired limit requested for one exact effective quota slice.
_Avoid_: Quota increment, quota request amount, quota preference

**Quota target strategy**:
The explicit rule that derives one absolute quota target for each selected
constraint. `minimum` requests fresh usage plus the normalized workload
requirement for each deficient slice, `preserve-headroom` requests the current
effective value plus the workload requirement, and `manual` uses an
operator-supplied absolute value for each child. `minimum` never decreases quota
or silently supersedes an existing provider intent. An equal or higher desired
value is preserved; a lower conflicting intent requires an explicit manual
target or a new Preview after settlement. `manual` retains every sufficiency
and dangerous-decrease gate.
_Avoid_: Quota increment, hidden default, automatic optimization

**Quota request**:
The operator and provider lifecycle for one child that asks Google to reconcile
one exact slice toward its quota target and reports the resulting grant.
_Avoid_: Quota preference, request bundle, change request

**Quota request bundle**:
One reviewed operator intent containing one or more independently mutable exact
quota slices and their absolute targets. A bundle is applied in deterministic
accelerator-first order and is deliberately non-atomic.
_Avoid_: Transaction, batch quota, combined quota

**Bundle child disposition**:
The durable Apply outcome for one ordered child: `accepted` when the provider
accepted the bound intent, `failed` when the provider conclusively rejected or
failed the dispatch, `unknown` when acceptance cannot be established safely, or
`unattempted` when an earlier child failed or became unknown.
_Avoid_: Rolled back, partially successful transaction

**Unknown dispatch resolution**:
Append-only authenticated evidence that later proves an `unknown` Apply child
was accepted or failed. It never rewrites the child's durable Apply disposition.
An accepted resolution can add the child to Watch; a failed resolution leaves it
non-watchable.
_Avoid_: Effective disposition, retried dispatch

**Quota preference**:
The Google Cloud `QuotaPreference` resource that stores the provider identity, requested target, granted value, etag, and reconciliation evidence for a quota request. Use this term only for provider-resource detail and structured provenance.
_Avoid_: Product-facing request, quota target

**Quota request plan**:
A time-bounded, single-use authorization to create or amend every ordered child
provider resource in one quota request bundle against freshly validated state.
_Avoid_: Confirmation token, quota request

**Quota contact**:
The verified individual email supplied to Google for a quota request. It is distinct from the authenticated principal that performs the mutation.
_Avoid_: Acting principal, credential identity

**Quota request reconciliation**:
The provider-managed progression from an accepted quota target to a settled grant and enforced effective quota.
_Avoid_: Immediate quota update, synchronous mutation

**Request-settled**:
The provider has ended reconciliation and reported the granted value. Settlement may grant all, some, or none of the quota target.
_Avoid_: Granted, fulfilled

**Quota request status**:
The surface-neutral state of a quota request expressed on separate reconciliation, grant-satisfaction, and effective-confirmation axes. Human headlines are derived from these simultaneous facts.
_Avoid_: Single lifecycle status, provider state detail

**Granted**:
A settled quota request whose granted value equals its quota target. It does not by itself prove that the effective quota enforces that value.
_Avoid_: Request-settled, effective-confirmed

**Fulfilled**:
A granted quota request backed by a fresh effective-quota observation equal to its target and granted values.
_Avoid_: Accepted, request-settled, granted

**Operation success boundary**:
The lifecycle condition an operation promises to reach before reporting
success. Preview may reach a verified no-op. Apply reaches its boundary only
when every non-no-op child is accepted; a failed or unknown child preserves
preceding accepted children and leaves later children unattempted. Only a watch
that requests effective confirmation promises `effective-confirmed`.
_Avoid_: Quota update succeeded, command completed

**Operation result**:
A versioned, surface-neutral record that identifies an operation, resource scope, declared boundary, outcome, completeness, observation times, diagnostics, and operation-specific data whether or not the boundary was reached.
_Avoid_: Raw provider response, rendered command output

**Watch event**:
A versioned, ordered record. Every stream begins with one initial authoritative
subject observation even when no value changed; later records are emitted only
when a material reconciliation, effective-quota, or aggregate bundle
observation changes. Polling ticks and unchanged refreshes after initialization
are not watch events; a terminal event carries the final operation result.
_Avoid_: Poll result, repeated snapshot

**Watch condition**:
The explicitly selected lifecycle observation a watch promises to reach. For one
request, a granted condition requires the grant to equal the quota target and a
fulfilled condition additionally requires fresh effective quota to equal both.
For a bundle, the condition is reached only when every child in the accepted
Watch set reaches it. That set includes children accepted during Apply and
`unknown` children with authenticated accepted resolution evidence. A watched
child fails the condition when its settled grant differs from its target,
including zero when the target is greater than zero. Watch times out at its
caller-controlled deadline when required evidence remains inconclusive.
_Avoid_: Polling duration, success

**Incomplete observation**:
Usable provider evidence returned with one or more required sources, pages, or refreshes missing. It remains visible with source failures and a non-success operation result, and it cannot satisfy a mutation gate.
_Avoid_: Partial success, partial grant

**Effective-confirmed**:
A quota-request outcome backed by a fresh effective-quota observation that matches the settled grant.
_Avoid_: Success, completed

**Effective quota**:
The quota limit currently granted for one quota dimension and scope.
_Avoid_: Available capacity

**Capacity**:
The physical resources that may be provisioned within effective quota. Effective quota does not guarantee capacity.
_Avoid_: Quota

**Spot capacity advice**:
Provider-produced, read-only evidence about the likelihood and expected runtime of obtaining a specified Spot VM configuration in candidate locations. It is Preview guidance, not quota, a reservation, or a capacity guarantee.
_Avoid_: Available capacity, inventory, stock

**Spot advice comparison**:
A read-only comparison that keeps one exact Spot VM configuration fixed while evaluating one or more exact obtainability candidates with per-candidate coverage and evidence.
_Avoid_: Accelerator availability search, global capacity search

**Obtainability candidate**:
One immutable provider-request snapshot identified by its endpoint region,
explicit candidate zones, exact machine configuration, VM quantity, and
distribution shape. A regional provider score belongs to the complete
candidate and is never copied onto an individual zone.
_Avoid_: Location row, regional capacity

**Obtainability workspace**:
The primary interactive workspace for building an exact Spot VM configuration and comparing its current obtainability, estimated uptime, historical preemption, price, and coverage across candidate locations.
_Avoid_: Spot workspace, capacity search

**Obtainability rank**:
A transparent lexicographic ordering of independently attributable, complete
candidate evidence: provider obtainability band descending, exact 30-day p90
preemption rate ascending, then exact current total-request hourly price
ascending. Canonical candidate identity breaks an otherwise exact tie. Each
component and derivation remains visible; the rank is not a capacity score or
guarantee.
_Avoid_: Composite score, best location, availability rank

**Obtainability score**:
The provider's current likelihood score that a specified Spot VM request with an exact machine configuration, quantity, distribution shape, and candidate locations will succeed.
_Avoid_: Availability, capacity probability, success guarantee

**Historical preemption rate**:
The provider's daily aggregate ratio of preempted Spot VMs to all matching Spot VMs that stopped, for one supported machine type and location. It is not the operator's fleet interruption rate.
_Avoid_: Failure rate, uptime, project preemption rate

**Effective quota slice**:
One effective quota identified by its resource scope, service, quota ID, exact dimensions, and applicable quota scope.
_Avoid_: Quota row, accelerator quota

**Accelerator catalog**:
A release-relative view of every specialized accelerator hardware identity
declared by the supported provider inventory at the catalog's review point. It
relates effective quota slices to accelerator, machine, topology, provisioning,
unit, location, lifecycle, and restriction metadata without turning the catalog
into a static allowlist.
_Avoid_: Static hardware list, quota allowlist

**Accelerator constraint set**:
The related effective quota slices that can independently limit one accelerator
workload at one compatible location, such as regional and all-regions GPU
limits. One exact slice can participate in multiple location-anchored sets; a
shared global companion does not combine alternative locations.
_Avoid_: Synthesized quota, combined quota

**Provider inventory set**:
The versioned V1 read boundary that federates the supported
`compute.googleapis.com` and `tpu.googleapis.com` sources. Service and catalog
group filters select the required provider subset and displayed rows; a bare
query selects both providers and every result states its queried-source
coverage.
_Avoid_: Enabled-service discovery, capacity inventory, source selector

**Quota pool**:
A quota limit for one consumption category, such as standard, preemptible, committed, or virtual-workstation use.
_Avoid_: Provisioning model

**Provisioning model**:
A provider-defined allocation and lifecycle mode such as Standard, Spot, Flex-start, or reservation-bound use.
_Avoid_: Quota pool, quota category

**Compatibility**:
Provider-visible evidence that an accelerator, machine shape, topology, provisioning model, and location can be used together. Compatibility does not imply capacity.
_Avoid_: Availability, capacity

**Compute instance requirement**:
A workload-first Compute shape consisting of machine type, instance count,
provisioning model, and explicit candidate locations or all compatible
locations. Applicable accelerator attachment and quota-pool facts are derived
from provider and catalog evidence.
_Avoid_: GPU quota row, capacity request

**Cloud TPU slice requirement**:
A workload-first Cloud TPU shape consisting of accelerator type, topology,
runtime version, slice count, provisioning model, and explicit candidate
locations or all compatible locations. Applicable quota-pool facts are derived
from provider and catalog evidence.
_Avoid_: TPU capacity request, inferred topology

**Discovered**:
Present in authoritative provider data, whether or not the manager recognizes its product semantics.

**Cataloged**:
Recognized by the manager with accelerator-specific semantics and relationships.

**Guided**:
Supported by an accelerator-specific workflow that explains its applicable constraints and choices.

**Mutable**:
Eligible for a quota request after fresh validation of the exact effective quota slice.

**Service owner**:
The Google Cloud service that owns a quota resource.
_Avoid_: Workload service

**Workload consumer**:
A service or workload that consumes quota owned by another service, such as GKE
consuming Compute Engine accelerator quota. It is stated only when it materially
changes resolution or operator understanding.
_Avoid_: Service owner

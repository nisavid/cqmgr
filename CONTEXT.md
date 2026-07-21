# Cloud Quotas Manager

The Cloud Quotas manager presents effective quota state and manages desired quota limits. Its first complete product workflows cover Compute accelerator quotas for NVIDIA GPUs and TPUs while its core domain remains applicable to other quota families.

## Language

**Cloud Quotas manager**:
The product for inspecting effective quotas and managing quota preferences.
_Avoid_: GPU capacity manager

**Quota preference**:
An absolute desired limit for one quota dimension and scope.
_Avoid_: Quota increment, quota request amount

**Quota mutation plan**:
A time-bounded, single-use authorization to create or amend one exact quota preference against freshly validated provider state.
_Avoid_: Confirmation token, change request

**Quota contact**:
The verified individual email supplied to Google for a quota-preference mutation. It is distinct from the authenticated principal that performs the mutation.
_Avoid_: Acting principal, credential identity

**Preference reconciliation**:
The provider-managed progression from accepted desired quota state to a settled grant and enforced effective quota.
_Avoid_: Immediate quota update, synchronous mutation

**Preference-settled**:
The provider has ended reconciliation and reported the granted value. Settlement may grant all, some, or none of the desired value.
_Avoid_: Granted, fulfilled

**Preference status**:
The surface-neutral state of a quota preference expressed on separate reconciliation, grant-satisfaction, and effective-confirmation axes. Human headlines are derived from these simultaneous facts.
_Avoid_: Single lifecycle status, provider state detail

**Fully granted**:
A settled preference whose granted value equals its desired value. It does not by itself prove that the effective quota enforces that value.
_Avoid_: Preference-settled, effective-confirmed

**Fully fulfilled**:
A fully granted preference backed by a fresh effective-quota observation equal to its desired and granted values.
_Avoid_: Accepted, preference-settled, fully granted

**Operation success boundary**:
The lifecycle condition an operation promises to reach before reporting success. Preview may reach a verified no-op; Apply reaches its boundary only when the provider accepts the bound preference. Only a watch that requests effective confirmation promises `effective-confirmed`.
_Avoid_: Quota update succeeded, command completed

**Operation result**:
A versioned, surface-neutral record that identifies an operation, target, declared boundary, outcome, completeness, observation times, diagnostics, and operation-specific data whether or not the boundary was reached.
_Avoid_: Raw provider response, rendered command output

**Watch event**:
A versioned, ordered record emitted when a material reconciliation or effective-quota observation changes. Polling ticks and unchanged refreshes are not watch events; a terminal event carries the final operation result.
_Avoid_: Poll result, repeated snapshot

**Watch condition**:
The explicitly selected lifecycle observation a watch promises to reach. A settled condition accepts any settled grant, a fully granted condition requires granted to equal desired, and a fully fulfilled condition additionally requires fresh effective quota to equal both. A fully granted watch fails on a settled partial grant; a fully fulfilled watch continues until fulfillment or its caller-controlled timeout.
_Avoid_: Polling duration, success

**Incomplete observation**:
Usable provider evidence returned with one or more required sources, pages, or refreshes missing. It remains visible with source failures and a non-success operation result, and it cannot satisfy a mutation gate.
_Avoid_: Partial success, partial grant

**Effective-confirmed**:
A mutation outcome backed by a fresh effective-quota observation that matches the settled granted preference.
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

**Obtainability score**:
The provider's current likelihood score that a specified Spot VM request with an exact machine configuration, quantity, distribution shape, and candidate locations will succeed.
_Avoid_: Availability, capacity probability, success guarantee

**Historical preemption rate**:
The provider's daily aggregate ratio of preempted Spot VMs to all matching Spot VMs that stopped, for one supported machine type and location. It is not the operator's fleet interruption rate.
_Avoid_: Failure rate, uptime, project preemption rate

**Effective quota slice**:
One effective quota identified by its resource container, service, quota ID, exact dimensions, and applicable scope.
_Avoid_: Quota row, accelerator quota

**Accelerator catalog**:
A view that relates effective quota slices to accelerator, machine, topology, provisioning, unit, location, lifecycle, and restriction metadata.
_Avoid_: Static hardware list, quota allowlist

**Accelerator constraint set**:
The related effective quota slices that can independently limit one accelerator workload, such as regional and all-regions GPU limits.
_Avoid_: Synthesized quota, combined quota

**Quota pool**:
A quota limit for one consumption category, such as standard, preemptible, committed, or virtual-workstation use.
_Avoid_: Provisioning model

**Provisioning model**:
A provider-defined allocation and lifecycle mode such as Standard, Spot, Flex-start, or reservation-bound use.
_Avoid_: Quota pool, quota category

**Compatibility**:
Provider-visible evidence that an accelerator, machine shape, topology, provisioning model, and location can be used together. Compatibility does not imply capacity.
_Avoid_: Availability, capacity

**Discovered**:
Present in authoritative provider data, whether or not the manager recognizes its product semantics.

**Cataloged**:
Recognized by the manager with accelerator-specific semantics and relationships.

**Guided**:
Supported by an accelerator-specific workflow that explains its applicable constraints and choices.

**Mutable**:
Eligible for a quota-preference change after fresh validation of the exact effective quota slice.

**Service owner**:
The Google Cloud service that owns a quota resource.
_Avoid_: Workload service

**Workload consumer**:
A service or workload that consumes quota owned by another service, such as GKE consuming Compute Engine accelerator quota.
_Avoid_: Service owner

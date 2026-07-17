# Cloud Quotas Manager

The Cloud Quotas manager presents effective quota state and manages desired quota limits. Its first complete product workflows cover Compute accelerator quotas for NVIDIA GPUs and TPUs while its core domain remains applicable to other quota families.

## Language

**Cloud Quotas manager**:
The product for inspecting effective quotas and managing quota preferences.
_Avoid_: GPU capacity manager

**Quota preference**:
An absolute desired limit for one quota dimension and scope.
_Avoid_: Quota increment, quota request amount

**Effective quota**:
The quota limit currently granted for one quota dimension and scope.
_Avoid_: Available capacity

**Capacity**:
The physical resources that may be provisioned within effective quota. Effective quota does not guarantee capacity.
_Avoid_: Quota

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

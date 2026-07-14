# Cloud Quotas Manager

The Cloud Quotas manager presents effective quota state and manages desired quota limits. Its first product workflows focus on GPU quotas while its core domain remains applicable to other quota families.

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

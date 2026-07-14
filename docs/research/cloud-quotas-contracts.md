# Current Google Cloud quota discovery and mutation contracts

Research date: 2026-07-14

This note records the current public contracts that constrain the Cloud Quotas manager. It uses only first-party Google Cloud documentation and API references. It does not describe observed private-project behavior.

## Executive summary

- Use Cloud Quotas API v1 at `https://cloudquotas.googleapis.com`. Its resource location is always `global`, even when the quota itself has regional or zonal dimensions. [Cloud Quotas API overview](https://docs.cloud.google.com/docs/quotas/api-overview)
- Discover effective quota through read-only `QuotaInfo` resources. The enforced value is `dimensionsInfos[].details.value`; each entry carries its dimension map and applicable locations. [QuotaInfo resource](https://docs.cloud.google.com/docs/quotas/reference/rest/v1/projects.locations.services.quotaInfos)
- Manage desired quota through `QuotaPreference` resources. A preference is unique for a quota and exact dimension set, and its desired absolute limit is `quotaConfig.preferredValue`. [QuotaPreference resource](https://docs.cloud.google.com/docs/quotas/reference/rest/v1/projects.locations.quotaPreferences)
- Treat preference mutation as asynchronous reconciliation, not as an immediate effective-limit write. `reconciling=true` means approval or fulfillment remains pending; `grantedValue` and `stateDetail` provide the resulting value and detail. Quota increases require approval and fulfillment, while decreases are documented as immediate. [QuotaPreference resource](https://docs.cloud.google.com/docs/quotas/reference/rest/v1/projects.locations.quotaPreferences) and [`gcloud beta quotas preferences`](https://docs.cloud.google.com/sdk/gcloud/reference/beta/quotas/preferences)
- Do not equate effective quota with capacity. Compute Engine explicitly states that quota does not guarantee physical resource availability. [Compute Engine allocation quotas](https://docs.cloud.google.com/compute/resource-usage)

## Discovery contract

### Resource identity and enumeration

`QuotaInfo` names have the shape:

```text
projects/{project-number}/locations/global/services/{service}/quotaInfos/{quota-id}
```

The project, folder, and organization variants expose `get` and `list`. Cross-container wildcard listing such as `projects/-` is not allowed. The list operation is scoped to one service and supports `pageSize` and `pageToken`; the REST method does not document a server-side filter. [QuotaInfo `get`](https://docs.cloud.google.com/docs/quotas/reference/rest/v1/projects.locations.services.quotaInfos/get) and [QuotaInfo `list`](https://docs.cloud.google.com/docs/quotas/reference/rest/v1/projects.locations.services.quotaInfos/list)

`quotaId` is unique only within a service, so stable product identity must include at least container, service, and quota ID. Location and service-specific dimensions identify an effective quota slice. [QuotaInfo resource](https://docs.cloud.google.com/docs/quotas/reference/rest/v1/projects.locations.services.quotaInfos)

### Effective value and metadata

`QuotaInfo` supplies both metadata and effective-value slices:

- `dimensions` declares the quota's dimension names.
- `dimensionsInfos` is ordered from more-specific to less-specific dimension combinations.
- `dimensionsInfos[].dimensions` contains the slice's dimension values.
- `dimensionsInfos[].details.value` is the value currently in effect and enforced.
- `dimensionsInfos[].applicableLocations` lists the regions or zones covered; non-regional and non-zonal quotas use `['global']`.
- `quotaIncreaseEligibility` reports whether an increase can be requested and, when not eligible, distinguishes invalid billing, unsupported quota, insufficient usage history, and other reasons.
- `isFixed` distinguishes adjustable quotas from fixed system limits; `isConcurrent`, `refreshInterval`, precision, units, and display names are descriptive metadata.
- `details.rolloutInfo.ongoingRollout` signals that the effective limit will change as a service-config rollout proceeds.

These fields make `QuotaInfo` the authoritative Cloud Quotas API projection for effective quota. Google notes that values can temporarily lag a new default during a gradual rollout. [QuotaInfo resource](https://docs.cloud.google.com/docs/quotas/reference/rest/v1/projects.locations.services.quotaInfos) and [Cloud Quotas known issues](https://docs.cloud.google.com/docs/quotas/known-issues)

Quota usage is separate from effective quota. Google's common-use-case guide reads the limit from `QuotaInfo` and reads usage from Cloud Monitoring's `serviceruntime.googleapis.com/quota/allocation/usage` time series. A product that shows both must preserve that source distinction. [Implement common use cases](https://docs.cloud.google.com/docs/quotas/implement-common-use-cases)

## Quota preference contract

### Resource shape and uniqueness

`QuotaPreference` names have the shape:

```text
projects/{project-number}/locations/global/quotaPreferences/{preference-id}
```

The preference contains `service`, `quotaId`, immutable `dimensions`, and required `quotaConfig.preferredValue`. The preferred value is an absolute desired limit, not an increment; it must be at least `-1`, where `-1` means unlimited. There is only one preference for a quota value targeting a unique dimension set. Missing dimension keys mean the preference applies to all values not covered by a more-specific preference. `user` and `resource` dimensions cannot be set to individual values. [QuotaPreference resource](https://docs.cloud.google.com/docs/quotas/reference/rest/v1/projects.locations.quotaPreferences)

The API accepts project, folder, and organization containers, but most increases must be project-level; only a limited set of products support organization-level increases. Decreases are supported at project, folder, and organization levels. [Cloud Quotas API overview](https://docs.cloud.google.com/docs/quotas/api-overview)

### Create and amend

`create` accepts an optional caller-selected `quotaPreferenceId`; otherwise the service generates one. It also accepts quota-decrease safety-check overrides. It returns the created preference synchronously, not a long-running operation. [QuotaPreference `create`](https://docs.cloud.google.com/docs/quotas/reference/rest/v1/projects.locations.quotaPreferences/create)

`patch` supports:

- an `updateMask`; omitting it overwrites all fields,
- `allowMissing=true` to create when the named preference does not exist,
- `validateOnly=true` to validate without mutation,
- safety-check overrides for a decrease below usage or a decrease exceeding 10 percent.

Validation success does not guarantee fulfillment. Dimensions are immutable, so changing the targeted slice requires a different preference identity rather than a dimension patch. [QuotaPreference `patch`](https://docs.cloud.google.com/docs/quotas/reference/rest/v1/projects.locations.quotaPreferences/patch) and [quota safety checks](https://docs.cloud.google.com/docs/quotas/reference/rest/v1/QuotaSafetyCheck)

The resource exposes create, get, list, and patch, but no delete method. A product cannot promise preference deletion through this API; it must express a new desired limit by amending the preference. [QuotaPreference resource](https://docs.cloud.google.com/docs/quotas/reference/rest/v1/projects.locations.quotaPreferences)

The documented API methods expose no request ID. A deterministic preference ID built from service, quota ID, and dimensions provides a stable retry and lookup key, and Google explicitly recommends such a naming scheme. `patch` with `allowMissing=true` provides an upsert-shaped operation. Before creating a new preference, Google's guide recommends listing reconciling preferences to avoid duplicates. These are the public duplicate-avoidance mechanisms; the docs do not promise general request-level idempotency. [Cloud Quotas API overview](https://docs.cloud.google.com/docs/quotas/api-overview), [QuotaPreference `patch`](https://docs.cloud.google.com/docs/quotas/reference/rest/v1/projects.locations.quotaPreferences/patch), and [Implement common use cases](https://docs.cloud.google.com/docs/quotas/implement-common-use-cases)

For optimistic concurrency, a preference may carry an `etag`. Supplying a stale `etag` on update returns `ABORTED`. [QuotaPreference resource](https://docs.cloud.google.com/docs/quotas/reference/rest/v1/projects.locations.quotaPreferences)

### Reconciliation and status

The preference resource is declarative:

- `reconciling=true` means the preference is pending Google Cloud approval and fulfillment.
- `quotaConfig.grantedValue` is the granted quota value.
- `quotaConfig.stateDetail` provides optional state detail.
- `quotaConfig.traceId` is emitted for increase requests and can identify the request in a support interaction; decreases do not receive a trace ID.
- `createTime` and `updateTime` are server timestamps.

Google's guide says the latest preference is the state the system tries to fulfill. Preference list supports pagination, filter, and ordering; filtering can select reconciling requests and time ranges. A folder or organization list returns preferences on that container, not descendant-project preferences. Polling `get`, or `list` with `reconciling=true`, is therefore the documented reconciliation-observation path. [QuotaPreference resource](https://docs.cloud.google.com/docs/quotas/reference/rest/v1/projects.locations.quotaPreferences), [QuotaPreference `list`](https://docs.cloud.google.com/docs/quotas/reference/rest/v1/projects.locations.quotaPreferences/list), and [Implement common use cases](https://docs.cloud.google.com/docs/quotas/implement-common-use-cases)

Google's current known-issues page says `contactEmail` is required when updating a preference through the API and cannot be a group address. The resource reference further says it is required for increase requests and optional for decreases. The manager should require it for mutations rather than rely on the looser-looking input shape. No contact value belongs in stored research or logs. [Cloud Quotas known issues](https://docs.cloud.google.com/docs/quotas/known-issues) and [QuotaPreference resource](https://docs.cloud.google.com/docs/quotas/reference/rest/v1/projects.locations.quotaPreferences)

The Cloud Quotas API itself is limited to 1,200 reads per minute per project, 60 updates per minute per project, and 300 quota-increase requests per day per project. Discovery pagination, polling, retries, and bulk workflows must stay within those budgets. [Cloud Quotas quotas and system limits](https://docs.cloud.google.com/docs/quotas/quotas)

## Authentication and authorization

All documented Cloud Quotas REST methods require the OAuth scope `https://www.googleapis.com/auth/cloud-platform`. Reads require `cloudquotas.quotas.get`; creates and patches require `cloudquotas.quotas.update` on the target resource. Google's broader quota-permissions guide also lists resource-manager lookup permissions, `monitoring.timeSeries.list`, and `serviceusage.services.list` for the full quota-viewing experience, plus both `serviceusage.quotas.update` and `cloudquotas.quotas.update` for changing quota. [Quota permissions](https://docs.cloud.google.com/docs/quotas/permissions), [QuotaInfo `list`](https://docs.cloud.google.com/docs/quotas/reference/rest/v1/projects.locations.services.quotaInfos/list), and [QuotaPreference `create`](https://docs.cloud.google.com/docs/quotas/reference/rest/v1/projects.locations.quotaPreferences/create)

Application Default Credentials can charge API-client quota to a separate quota project. Using that project requires `serviceusage.services.use`, commonly through `roles/serviceusage.serviceUsageConsumer`. This quota project is about consumption of the Cloud Quotas API itself; it does not change the resource container named in a `QuotaInfo` or `QuotaPreference`. [Set the quota project](https://docs.cloud.google.com/docs/quotas/set-quota-project)

## Regional and global GPU quotas

Compute Engine GPU allocation is constrained by multiple quota slices:

- GPU model or family quotas are regional. Running instances and reservations consume them, and standard, Spot, virtual-workstation, and committed-use variants have distinct quota names.
- New accounts and projects also have a global GPU quota.
- Google instructs users to request the required model quota in each region and an additional `GPUs (all regions)` quota for the total number of GPUs across all regions.

For family-based GPUs, Google's API example uses service `compute.googleapis.com`, quota ID `GPUS-PER-GPU-FAMILY-per-project-region`, and dimensions `region` plus `gpu_family`. The global quota is a separate quota, not the same preference with a missing region. Discovery must therefore rely on `QuotaInfo.quotaId`, `dimensions`, and `applicableLocations` rather than infer scope from the Cloud Quotas resource's always-global location segment. [Compute Engine allocation quotas](https://docs.cloud.google.com/compute/resource-usage), [Cloud Quotas API overview](https://docs.cloud.google.com/docs/quotas/api-overview), and [Implement common use cases](https://docs.cloud.google.com/docs/quotas/implement-common-use-cases)

Even when both regional and all-regions limits are sufficient, provisioning can fail because quota does not reserve or guarantee physical GPU capacity. [Compute Engine allocation quotas](https://docs.cloud.google.com/compute/resource-usage)

## Supported `gcloud` surface

The Cloud Quotas commands are currently beta, with alpha variants also documented:

- `gcloud beta quotas info list|describe` discovers `QuotaInfo` for one service and one project, folder, or organization.
- `gcloud beta quotas preferences create|describe|list|update` manages preferences.
- `preferences list --reconciling-only` selects unresolved preferences.
- `preferences update --allow-missing` exposes the API's create-or-update behavior.
- `preferences update --validate-only` validates without updating.
- create and update expose the two quota-decrease safety overrides.

The CLI's generic `--filter`, `--sort-by`, and `--limit` processing is client-side command behavior and must not be mistaken for REST `QuotaInfo.list` filtering, which exposes only page size and page token. [`gcloud beta quotas`](https://docs.cloud.google.com/sdk/gcloud/reference/beta/quotas), [`gcloud beta quotas info list`](https://docs.cloud.google.com/sdk/gcloud/reference/beta/quotas/info/list), [`gcloud beta quotas preferences create`](https://docs.cloud.google.com/sdk/gcloud/reference/beta/quotas/preferences/create), [`gcloud beta quotas preferences list`](https://docs.cloud.google.com/sdk/gcloud/reference/beta/quotas/preferences/list), and [`gcloud beta quotas preferences update`](https://docs.cloud.google.com/sdk/gcloud/reference/beta/quotas/preferences/update)

## Product constraints derived from the contracts

1. Model an effective quota slice separately from a quota preference. They have different resources, lifecycles, and fields.
2. Key an effective slice by container, service, quota ID, and normalized dimensions. Do not key by display name or metric alone.
3. Represent reconciliation explicitly. A successful mutation response means the preference was accepted, not that the effective quota has changed.
4. Re-read both the preference and `QuotaInfo` after reconciliation: the preference describes requested and granted desired state, while `QuotaInfo` describes the enforced limit.
5. Use caller-controlled deterministic preference IDs and `etag` on amendment. Check for reconciling preferences before creating another request for the same quota slice.
6. Preserve project/folder/organization scope, but reject or explain unsupported increase scopes based on eligibility and product documentation.
7. Require the contact field for mutation, keep it out of diagnostic output, and never place sensitive data in preference annotations.
8. Treat regional GPU-family quota and the global all-regions GPU quota as independent constraints that may both need adjustment.
9. Describe quota as a limit, never as guaranteed capacity.
10. Isolate beta `gcloud` compatibility behind an adapter if the CLI is used; the REST v1 API is the stable contract documented here.

## Documentation skew to guard against

The current v1 REST schema names the enforced effective value `dimensionsInfos[].details.value`. Some guide examples still show older `quotaValue` and `resetValue` fields, and preference prose still refers to `reset_value`. Implementations should bind to the current v1 discovery schema and treat guide payloads as illustrative rather than as a substitute schema. [QuotaInfo resource](https://docs.cloud.google.com/docs/quotas/reference/rest/v1/projects.locations.services.quotaInfos) and [Implement common use cases](https://docs.cloud.google.com/docs/quotas/implement-common-use-cases)

## Deliberate exclusions

This research does not choose a client library, persistence schema, polling interval, retry budget, or user-interface workflow. It also does not validate behavior against a private Google Cloud project or mutate any quota preference.

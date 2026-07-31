# Exact-evidence deadline bottleneck

Research date: 2026-07-31

Code baseline:
[`4958b66e46725567afe2618ced71fee1b1d182b0`](https://github.com/nisavid/cqmgr/commit/4958b66e46725567afe2618ced71fee1b1d182b0)

Tracker question:
[Find the exact-evidence deadline bottleneck](https://github.com/nisavid/cqmgr/issues/96)

## Answer

Exact quota inspection exhausts its deadline because it first materializes and
classifies the entire Compute quota inventory, then starts a second provider
read for the selected exact slice. The first phase spends about 55 seconds
joining 3,324 effective quota slices. Of that time, about 54.9 seconds is spent
recomputing accelerator constraint sets once for every slice. The second
effective-quota, preference, and usage reads therefore begin after the shared
60-second caller deadline and immediately return
`provider-read-deadline-exceeded`.

Explicit no-op Compose shares the failure because lifecycle preparation
delegates exact evidence resolution to the same `ReadOnlyOperations.inspect`
path before pure composition. It does not issue a Plan or perform a provider
mutation.

The roughly 60-second successful inventory scan has the same cause. It performs
the same all-item constraint-set work, but no provider read follows that CPU
phase, so nothing observes that the monotonic provider deadline has expired.
The command returns success after the nominal 60-second deadline.

The provider, Application Default Credentials, and installation-local request
budget are not the bottleneck. A warmed direct exact `QuotaInfo` read completed
in 1.06 seconds. The successful inventory read completed six QuotaInfo pages,
and its isolated budget ledger remained far below the configured 30-request
ceiling.

## Source trace

The CLI gives provider operations one deadline 60 seconds from invocation.
Both `quota inspect` and lifecycle preparation use that value.
[`src/cqmgr/cli.py`](https://github.com/nisavid/cqmgr/blob/4958b66e46725567afe2618ced71fee1b1d182b0/src/cqmgr/cli.py#L93)
[`src/cqmgr/cli.py`](https://github.com/nisavid/cqmgr/blob/4958b66e46725567afe2618ced71fee1b1d182b0/src/cqmgr/cli.py#L258-L260)
[`src/cqmgr/cli.py`](https://github.com/nisavid/cqmgr/blob/4958b66e46725567afe2618ced71fee1b1d182b0/src/cqmgr/cli.py#L944-L949)
[`src/cqmgr/cli.py`](https://github.com/nisavid/cqmgr/blob/4958b66e46725567afe2618ced71fee1b1d182b0/src/cqmgr/cli.py#L286-L300)

`ReadOnlyOperations.inspect` does not begin with an exact provider read. It
constructs a query, calls `QuotaOperations.browse`, walks retained cursor pages
until it finds one unambiguous item, and only then calls
`QuotaOperations.inspect`.
[`src/cqmgr/application/operations/read_only.py`](https://github.com/nisavid/cqmgr/blob/4958b66e46725567afe2618ced71fee1b1d182b0/src/cqmgr/application/operations/read_only.py#L436-L538)

Browse joins every provider-returned evidence item before applying the query
filter. A narrow quota ID and location therefore do not reduce classification
work.
[`src/cqmgr/application/operations/quotas.py`](https://github.com/nisavid/cqmgr/blob/4958b66e46725567afe2618ced71fee1b1d182b0/src/cqmgr/application/operations/quotas.py#L1128-L1166)

Every item join calls `SemanticAcceleratorOverlay.constraint_sets` with the
complete evidence tuple.
[`src/cqmgr/application/operations/quotas.py`](https://github.com/nisavid/cqmgr/blob/4958b66e46725567afe2618ced71fee1b1d182b0/src/cqmgr/application/operations/quotas.py#L1469-L1494)

`constraint_sets` then scans every mapping and every primary evidence for each
item. Matching primaries call `constraint_set`, whose companion lookup scans
the evidence tuple again. This repeated whole-collection traversal is the
measured hot path.
[`src/cqmgr/domain/accelerator_overlay.py`](https://github.com/nisavid/cqmgr/blob/4958b66e46725567afe2618ced71fee1b1d182b0/src/cqmgr/domain/accelerator_overlay.py#L1015-L1026)
[`src/cqmgr/domain/accelerator_overlay.py`](https://github.com/nisavid/cqmgr/blob/4958b66e46725567afe2618ced71fee1b1d182b0/src/cqmgr/domain/accelerator_overlay.py#L1039-L1075)

After that first materialization, `QuotaOperations.inspect` independently
lists effective quota, quota preferences, and Monitoring usage again. These
reads receive the original deadline, not a new one.
[`src/cqmgr/application/operations/quotas.py`](https://github.com/nisavid/cqmgr/blob/4958b66e46725567afe2618ced71fee1b1d182b0/src/cqmgr/application/operations/quotas.py#L1252-L1281)

Compose reaches the same path through
`ReadOnlyLifecycleCompositionReader._read_exact`.
[`src/cqmgr/application/operations/lifecycle_requests.py`](https://github.com/nisavid/cqmgr/blob/4958b66e46725567afe2618ced71fee1b1d182b0/src/cqmgr/application/operations/lifecycle_requests.py#L202-L239)

Google's v1 API exposes both an exact `QuotaInfo.get` method and a paginated
service-wide `QuotaInfo.list` method. `get` accepts the complete QuotaInfo
resource name; `list` accepts only a parent, page size, and page token.
[QuotaInfo `get`](https://docs.cloud.google.com/docs/quotas/reference/rest/v1/projects.locations.services.quotaInfos/get)
[QuotaInfo `list`](https://docs.cloud.google.com/docs/quotas/reference/rest/v1/projects.locations.services.quotaInfos/list)

The repository already has an official exact `quota_info` adapter for watch
observation, but the production read-only graph wires only the paginated
`OfficialCloudQuotasPageClient` into effective quota inspection.
[`src/cqmgr/adapters/google/cloud_quotas.py`](https://github.com/nisavid/cqmgr/blob/4958b66e46725567afe2618ced71fee1b1d182b0/src/cqmgr/adapters/google/cloud_quotas.py#L246-L257)
[`src/cqmgr/bootstrap.py`](https://github.com/nisavid/cqmgr/blob/4958b66e46725567afe2618ced71fee1b1d182b0/src/cqmgr/bootstrap.py#L637-L647)
[`src/cqmgr/bootstrap.py`](https://github.com/nisavid/cqmgr/blob/4958b66e46725567afe2618ced71fee1b1d182b0/src/cqmgr/bootstrap.py#L722-L724)

## Live read-only evidence

All cqmgr paths were redirected to a fresh temporary directory. The probes
used refreshed ambient ADC and the `systalyze-dev` project. No quota preference
was created, amended, or deleted; no Plan or Apply operation was invoked; and
no capacity was provisioned.

| Probe | Exit | Wall | User CPU | Relevant evidence |
| --- | ---: | ---: | ---: | --- |
| Warmed direct `gcloud beta quotas info describe` | 0 | 1.06 s | 0.26 s | Returned the exact global GPU QuotaInfo. |
| `cqmgr quota list` | 0 | 67.11 s | 57.63 s | 3,324 items; six of six QuotaInfo pages complete; 1,000-item first logical page. |
| Instrumented `cqmgr quota list` | 0 | 63.70 s | 56.16 s | 55.10 s in 3,324 `_joined_classification` calls; 54.87 s in 3,324 `constraint_sets` calls; `_complete_browse` took 0.87 s. |
| `cqmgr quota inspect` | 8 | 65.26 s | 57.25 s | Effective quota, preference, and Monitoring reads all reported `provider-read-deadline-exceeded`. |
| Exact no-op `cqmgr request compose` | 1 | 64.23 s | 57.12 s | `exact lifecycle evidence unavailable: operation-deadline-exceeded`. |

The isolated inventory budget ledger recorded seven units on its project axis;
the largest provider-axis entry was five units. Neither approached the
configured limit of 30 requests in 60 seconds.
[`src/cqmgr/bootstrap.py`](https://github.com/nisavid/cqmgr/blob/4958b66e46725567afe2618ced71fee1b1d182b0/src/cqmgr/bootstrap.py#L704-L719)

The uninstrumented successful inventory result started at
`2026-07-31T07:10:19.719272Z`. Its effective-quota read was observed at
`2026-07-31T07:10:25.831561Z`, while the final operation result was not
finished until `2026-07-31T07:11:24.105208Z`. The provider phase therefore
completed near the beginning; local processing consumed the remaining time.

## Reproduction

The following setup preserves ambient ADC while isolating all cqmgr state:

```bash
ROOT="$(mktemp -d "${TMPDIR:-/tmp}/cqmgr-deadline-research.XXXXXX")"
PROJECT_NUMBER="$(
  gcloud projects describe systalyze-dev --format='value(projectNumber)'
)"
export CQMGR_CONFIG_PATH="$ROOT/config.toml"
export CQMGR_SELECTION_STATE_PATH="$ROOT/selection.toml"
export CQMGR_AUDIT_PATH="$ROOT/audit"
export CQMGR_QUOTA_SNAPSHOT_PATH="$ROOT/quota-snapshots"
export CQMGR_BUDGET_PATH="$ROOT/budgets"
export CQMGR_TRUST_PATH="$ROOT/trust.toml"
export CQMGR_PLAN_PATH="$ROOT/plans"
export CQMGR_APPLY_RECORD_PATH="$ROOT/apply-records"
export CQMGR_WATCH_PATH="$ROOT/watch"
TARGET="$(
  gcloud beta quotas info describe GPUS-ALL-REGIONS-per-project \
    --service=compute.googleapis.com \
    --project=systalyze-dev \
    --format='value(dimensionsInfos[0].details.value)'
)"
```

Run the exact provider control twice so the timed run excludes one-time
credential and component startup:

```bash
gcloud beta quotas info describe GPUS-ALL-REGIONS-per-project \
  --service=compute.googleapis.com \
  --project=systalyze-dev \
  --format='value(name)' >/dev/null

/usr/bin/time -p gcloud beta quotas info describe \
  GPUS-ALL-REGIONS-per-project \
  --service=compute.googleapis.com \
  --project=systalyze-dev \
  --format='value(name)' >/dev/null
```

Run the three cqmgr probes with a fresh `ROOT` for each command:

```bash
/usr/bin/time -p cqmgr quota list \
  --resource-scope "projects/$PROJECT_NUMBER" \
  --service compute.googleapis.com \
  --limit 1000 \
  --output json >"$ROOT/list.json"

/usr/bin/time -p cqmgr quota inspect \
  --resource-scope "projects/$PROJECT_NUMBER" \
  --service compute.googleapis.com \
  --quota-id GPUS-ALL-REGIONS-per-project \
  --location global \
  --output json >"$ROOT/inspect.json"

/usr/bin/time -p cqmgr request compose \
  --resource-scope "projects/$PROJECT_NUMBER" \
  --service compute.googleapis.com \
  --quota-id GPUS-ALL-REGIONS-per-project \
  --location global \
  --target "$TARGET" \
  --output json >"$ROOT/compose.json"
```

For source-level timing, wrap
`QuotaOperations._joined_classification`,
`QuotaOperations._complete_browse`, and
`SemanticAcceleratorOverlay.constraint_sets` with `time.perf_counter()` in a
throwaway process, incrementing a call count and elapsed total in `finally`,
then invoke the same `quota list` arguments through
`cqmgr.cli.main(..., standalone_mode=False)`. The wrapper must remain outside
the repository and must not change return values or arguments.

## Planning implications

The implementation handoff should keep two boundaries distinct:

1. Exact evidence resolution must avoid a full classified browse followed by a
   second full source read. The exact QuotaInfo resource and exact dimension
   slice should be resolved once, while preserving exact preference and usage
   joins and all mutation gates.
2. Inventory classification must derive or index constraint-set relationships
   once per provider snapshot rather than recomputing the same whole-inventory
   relation for every item.

The full operation deadline also needs an acceptance contract. Today CPU-side
normalization can run past the 60-second value and still return success; only a
later provider call exposes the expired deadline. Regression coverage should
therefore verify both exact-read latency and end-to-end inventory latency,
including deadline behavior outside provider transport.

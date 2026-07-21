# Status, output, and watch contract

Cloud Quota Manager reports every domain operation through the same surface-neutral
result model. Human CLI output, TUI presentation, JSON automation, audit
evidence, and Watch streams preserve the same resource scope, observations,
outcomes, and diagnostics without treating provider acceptance as effective
quota.

This contract defines behavior and stable machine semantics. The CLI command
tree, option spelling, TUI navigation, and polling implementation belong to
their owning interface and runtime decisions.

## Operation boundaries

Each operation declares the condition it promises before it can report
success. Exit code `0` means that the operation reached that boundary with the
required evidence; it never means that every quota request is fully
fulfilled.

| Domain operation | Success boundary | Required result facts |
| --- | --- | --- |
| Establish resource scope | The canonical project is resolved from explicit input, an explicitly named profile, a direct local selection, or the explicitly selected local profile. V1 rejects folder and organization operations without inferring a project. | Resource-scope type and canonical resource name; acting principal and impersonation chain; resolution source. |
| Browse quota | The requested logical page or bounded query is read with complete required provider evidence. | Canonical resource scope; query and page identity; exact slices; constraint-set relationships; source observation times; continuation identity when present. |
| Inspect slice | One complete exact effective quota slice and its required related evidence are read. | Provider identity; dimensions and quota scope; effective value; usage; provider quota preference; eligibility; related constraints; independent source times and completeness. |
| Resolve requirement | The supplied workload requirement resolves without guessing to a supported constraint set. | Normalized requirement; owning service and management plane; exact slices; compatibility and ambiguity evidence. |
| Assess Spot advice | The exact supported VM request is assessed for the requested evidence. | Full machine configuration, quantity, distribution, locations, provider coverage, Preview status, observation or interval times, and every available advice datum. |
| Compose request | One absolute quota target is validated against one exact mutable slice and current evidence. | Exact slice; quota target and unit; prior desired, granted, effective, and usage values; direction; required warnings and acknowledgements. |
| Preview plan | A locally portable, integrity-protected quota request plan is produced, or an identical quota target against a settled request is freshly verified as a no-op. V1 Apply capability is bound to the issuing installation. | Bound resource scope, slice, evidence, identity, intent, principal, warnings, and acknowledgements; plan expiry, digest, issuing-installation trust, and Apply capability when a plan is produced, otherwise the no-op reason. |
| Review plan | Canonical plan bytes and their digest are verified and all trustworthy bound evidence is presented without applying it. Expiry, foreign issuer, prior consumption, or unresolved acknowledgements remove Apply capability but do not make safe inspection fail. | Every bound plan fact, canonical and digest verification state, expiry, issuer and consumption state, unresolved acknowledgements, Apply capability, and exact incapability reasons. |
| Apply plan | The provider quota preference is proven accepted under the bound intent. A verified no-op has no Apply capability. | Plan digest; resource scope and slice; quota target; provider preference identity, etag, and trace when present; submitted observation; audit reference. |
| Watch request | The explicitly selected Watch condition is reached. | Quota request and provider preference identity; selected condition; orthogonal status; target, granted, and effective values; all material observations and final outcome. |
| Inspect audit | The requested bounded audit query is read completely. | Query and record identities; canonical resource scopes; observation times; continuity metadata. |
| Verify audit | The requested records and rotation checkpoints form a valid chain. | Verified range and checkpoints, or the exact first continuity failure and affected range. |

Apply therefore succeeds at `submitted`. A verified no-op is a successful
Preview result with no Apply capability. Apply does not wait for preference
settlement or effective quota. A timeout or transport failure after the
provider call is reconciled through the deterministic preference identity.
Only a result proven to contain the bound accepted intent reaches the Apply
boundary; unchanged, conflicting, and unknown results require a new preview
and do not report success.

An intentionally bounded page with a continuation identity is complete for
that page. A failed required page, source, or refresh is an incomplete
observation rather than successful pagination.

Plan review separates trustworthy inspection from applicability. Canonical,
digest-valid bytes reach the Review boundary even when the plan is expired,
foreign-issued, already consumed, or has unresolved acknowledgements; the
result sets `apply_capability` to `false` with exact reasons. Invalid canonical
encoding or a digest mismatch prevents trusted review and returns a
non-success result. Apply against a plan that is no longer applicable returns
the appropriate stale, conflicting, authorization, or precondition class.

## Quota request status

Quota request status has three independent axes. Surfaces may derive a concise
headline, but automation reads the axes and values directly.

### Reconciliation

- `submitted`: the bound preference was accepted, but no newer provider
  reconciliation observation is available;
- `reconciling`: the provider reports that approval or fulfillment remains in
  progress;
- `settled`: reconciliation ended and the provider reports a granted value;
- `failed`: the provider reports a terminal failure;
- `superseded`: a later preference replaced this intent; and
- `unknown`: current reconciliation state cannot be established safely.

### Grant satisfaction

- `unknown`: no authoritative granted value is available;
- `none`: the settled grant satisfies none of the requested change;
- `partial`: the settled granted value does not equal the absolute desired
  value but satisfies part of the requested change; and
- `full`: the settled granted value equals the desired value.

The result always carries desired and granted values when known. Callers never
infer grant satisfaction from a headline or warning.

### Effective confirmation

- `unobserved`: no post-submission effective-quota observation is available;
- `stale`: an available effective observation predates the status evidence it
  would need to confirm;
- `mismatch`: a fresh effective observation does not equal the settled granted
  value; and
- `confirmed`: a fresh effective observation equals the settled granted value.

`confirmed` may coexist with a partial grant. `fulfilled` is the stronger
derived condition in which reconciliation is `settled`, grant satisfaction is
`full`, and effective confirmation is `confirmed` for equal desired, granted,
and effective values.

## Versioned operation result

Every non-streaming structured result uses one top-level envelope. Operation-
specific fields stay inside `data`; provider resources never define the public
top-level schema.

```json
{
  "schema": "cqmgr.operation-result/v1",
  "operation": "request.watch",
  "resource_scope": {
    "type": "project",
    "name": "projects/example-project"
  },
  "boundary": {
    "condition": "fulfilled",
    "reached": false
  },
  "outcome": {
    "code": "watch-timeout",
    "exit_class": 8
  },
  "complete": true,
  "started_at": "2026-07-21T02:00:00Z",
  "finished_at": "2026-07-21T02:15:00Z",
  "data": {},
  "diagnostics": [],
  "provenance": []
}
```

The envelope contract is:

- `schema` is required. Breaking field, type, or semantic changes use a new
  schema version. Additive fields may appear within a version, and callers
  ignore unknown fields.
- `operation` is a stable symbolic domain-operation name independent of CLI
  spelling or TUI location.
- `resource_scope` is required for resource-scoped operations and uses the
  canonical provider resource name. It never comes from an unreported ambient
  project.
- `boundary.condition` names the operation's promised condition;
  `boundary.reached` is authoritative for success.
- `outcome.code` is a stable symbolic outcome. `outcome.exit_class` is the
  global numeric process class and remains present even when the result is
  consumed without a process.
- `complete` says whether every source, page, and refresh required by this
  operation is present. It does not describe whether the provider granted the
  desired value.
- timestamps are UTC RFC 3339 values. Each independently sourced observation
  also carries its own observation or provider interval time; the envelope
  timestamp never substitutes for source freshness.
- `data` contains the operation payload. Quota quantities and other provider
  integers use base-10 strings with explicit units so JSON consumers do not
  lose 64-bit precision.
- `diagnostics` contains the ordered typed diagnostics for the operation.
- `provenance` identifies authoritative sources, observation times, coverage,
  lifecycle or Preview status, and request identity where safe.

Stable enum values use lowercase kebab case. Callers treat an unknown outcome
or diagnostic code according to its known exit class and severity rather than
assuming success.

Unavailable optional evidence is explicit. For example, historical Spot
advice excluded by documented provider coverage is `unsupported`, with its
coverage reason, rather than `null`, zero, or an incomplete observation.

Credentials, access or bearer tokens, quota-contact values, sensitive
annotations, and raw provider bodies are excluded. Safe provider metadata may
include HTTP or gRPC status, a documented reason, preference identity, etag, and
trace or request identity. The locally authenticated opaque Watch resume token
is a read-only control artifact and contains none of the excluded values.

## Human-readable output

Human presentation may evolve without a text-layout compatibility promise.
Tables may reorder, wrap, truncate with an explicit marker, or become grouped
views for terminal width and accessibility. Scripts must use the versioned
structured result.

Presentation changes may not remove the facts needed to identify the resource scope,
interpret the operation boundary, or act safely. In particular:

- every resource-scoped result identifies the canonical resource scope;
- quota results preserve exact slice identity, dimensions, scope, native unit,
  source times, and completeness;
- quota request results preserve desired, granted, and effective values as separate
  facts and show all three status axes;
- plan and Apply results preserve principal, plan digest, expiry, warnings,
  acknowledgements, and provider identity without exposing the quota contact;
- Spot advice preserves its exact request configuration, coverage, Preview
  status, and observation or interval time; and
- errors identify the operation, resource scope when known, stable symbolic code, safe
  message, and actionable next step.

State and severity use words and symbols, not color alone. Human output works
without color and keeps a screen-reader-conscious reading order.

Stdout contains only the selected result form. Human warnings, progress, and
errors use stderr. Structured modes carry diagnostics in-band and reserve
stderr for a failure that occurs before a valid structured record can be
formed. A quiet presentation may suppress non-result prose and a partial-grant
warning, but it never changes the result facts, acknowledgements, structured
diagnostics, or exit class.

## Exit classes

Numeric exit classes are global and operation-independent. The structured
outcome supplies the precise symbolic reason.

| Exit | Class | Meaning and representative cases |
| ---: | --- | --- |
| `0` | Success | The selected operation boundary was reached with complete required evidence. |
| `2` | Usage | CLI syntax, option shape, or input decoding is invalid. A structured envelope is returned when the invocation can be decoded far enough to form one. |
| `3` | Rejected precondition | The request is well formed but unsupported, ineligible, ambiguous, missing an acknowledgement, or otherwise barred before execution. |
| `4` | Authorization | Authentication, permission, or allowed principal/contact verification prevents the operation. |
| `5` | Stale or conflicting | An operation that requires current applicability found bound evidence drift, an expired or consumed plan, an etag conflict, ambiguous identity, or a different intent at the deterministic preference identity. Safe Review of trustworthy but non-applicable plan bytes still succeeds with `apply_capability: false`. |
| `6` | Incomplete evidence | Usable observations are returned, but a required source, page, refresh, or local read is missing. |
| `7` | Requested outcome unmet | A conclusive provider or verification outcome cannot satisfy the selected boundary, including a settled grant that differs from the target for a granted or fulfilled Watch, provider failure or supersession, or an invalid audit chain. |
| `8` | Timeout | The caller's deadline arrived before the selected condition. The result retains the last material observation and resume identity. |
| `9` | Operational failure | A provider, transport, serialization, audit persistence, or local internal failure prevents a trustworthy result in another class. |
| `130` | Interrupted | The caller interrupted the operation. No provider quota request is canceled or reversed implicitly. |

Diagnostics do not compete to select a process code. The operation's final
outcome selects exactly one exit class. Quiet mode and output format never
change it.

## Diagnostics and incomplete observations

The result contains an ordered `diagnostics` list. Every diagnostic has:

- a stable symbolic `code`;
- `severity` of `info`, `warning`, `error`, or `critical`;
- the operation `phase` and authoritative or local `source`;
- a retry disposition such as `never`, `after-refresh`, `after-new-preview`,
  `after-backoff`, or `unknown`;
- a concise, safe human message; and
- optional field paths and scrubbed provider metadata.

Messages are for people, not control flow. Automation uses the schema,
operation outcome, exit class, status axes, completeness, and stable diagnostic
codes.

An incomplete observation preserves every usable item and identifies each
failed source, page, or refresh. Its envelope has `complete: false`, an exit
class of `6`, and source-specific diagnostics. It never satisfies a quota request
gate that requires the missing evidence. A fully unavailable operation returns
the more specific authorization, precondition, timeout, conflict, or
operational class when one is known instead of claiming partial data.

Expected provider coverage gaps are not incomplete observations. A supported
Spot request whose live advice succeeds while historical GPU advice is
documented as unsupported is complete when that unsupported coverage is
represented explicitly.

## Watch conditions

A Watch always selects one condition and one deadline explicitly. An
interactive surface may offer deadline presets, but it selects neither input
silently.

| Condition | Reached when | Settled grant differs from target |
| --- | --- | --- |
| `granted` | Reconciliation is `settled` and granted equals the quota target. | Exit `7` when the settled grant differs from the target, including zero when the target is greater than zero. |
| `fulfilled` | `granted` is reached and a fresh effective observation equals the target and granted values. | Exit `7` when the settled grant differs from the target, including zero when the target is greater than zero. |

Provider `failed` or `superseded` state terminates any condition it makes
impossible with exit `7`. A transient or recoverable unknown observation stays
visible and polling continues within the deadline. An irrecoverable local or
provider observation failure exits under its applicable class.

## Watch stream

Structured Watch output is newline-delimited, versioned JSON. It emits one
self-contained record for the initial authoritative observation, each material
status or evidence change, and the terminal result. Unchanged polling ticks do
not produce public events.

Every record carries the same complete request identity. `request.resource_scope`
contains the canonical project name. `request.provider_preference` contains the
canonical preference resource name, service, quota ID, and complete dimension
map. `request.target` and `request.unit` preserve the watched intent even when a
preference is later amended or superseded. `request.intent_id` is the applied
plan digest and must resolve to a durable local cqmgr Apply record for the same
preference, target, and unit. V1 does not adopt an unrelated provider preference
as a watchable intent. A producer may not emit an empty or provider-defined
identity object in place of these fields.

Every event also emits `resume`, an opaque `cqmgr.watch-resume/v1` token
authenticated by the issuing installation. It binds the intent ID, selected
condition, complete request identity, last observed provider etag and trace ID
when present, and checkpoint sequence. It contains no credential or quota
contact. Before an initial event, Watch verifies the current preference target
and trace ID against the Apply record. When no stable trace ID exists, the current
etag must equal the Apply response etag; otherwise lineage is unknown and Watch
returns rejected-precondition rather than treating a same-target amendment as
the original intent. Resume applies the same checks to its authenticated token
and durable checkpoint, rejects any later local Apply for the same preference as
superseded, and treats a changed provider trace ID as superseded. When no stable
trace ID exists, an etag change across the observation gap is unknown lineage and
rejects resume rather than guessing whether reconciliation or a same-target
amendment occurred. Each material event carries a new token for its durable
checkpoint.

```json
{
  "schema": "cqmgr.watch-event/v1",
  "stream_id": "opaque-run-identity",
  "sequence": 4,
  "event": "status-changed",
  "resume": "cqmgr.watch-resume/v1:opaque-authenticated-token",
  "observed_at": "2026-07-21T02:07:00Z",
  "request": {
    "resource_scope": "projects/123456789",
    "condition": "fulfilled",
    "intent_id": "sha256:opaque-applied-plan-digest",
    "target": "8",
    "unit": "1",
    "provider_preference": {
      "name": "projects/123456789/locations/global/quotaPreferences/gpu-region",
      "service": "compute.googleapis.com",
      "quota_id": "GPUS-PER-GPU-FAMILY-per-project-region",
      "dimensions": {
        "gpu_family": "NVIDIA_H100",
        "region": "us-central1"
      }
    }
  },
  "status": {
    "reconciliation": "reconciling",
    "grant_satisfaction": "unknown",
    "effective_confirmation": "unobserved",
    "desired": "8",
    "granted": null,
    "effective": null,
    "unit": "1"
  },
  "diagnostics": []
}
```

`sequence` increases within one stream. A resumed Watch creates a new stream,
starts with the current authoritative observation, and retains the deterministic
preference identity, intent ID, and selected condition from the verified resume
token; it does not pretend that events missed while disconnected were observed.

The terminal event has `event: "terminal"` and carries the complete operation
result. It is emitted when the selected condition is reached, a conclusive
adverse state occurs, the deadline expires, or the stream is interrupted when
there is enough process lifetime to serialize it.

On timeout, the terminal result uses exit `8` and includes the selected
condition, deadline, elapsed duration, last material observation, and
latest resume token. Timeout describes the Watch operation, not the underlying
quota request, and never relabels that request as failed.

On interruption, the manager emits a terminal interrupted event when possible,
exits `130`, and leaves the provider preference unchanged. A later Watch can
resume from the latest verified resume token.

## Polling ownership

The caller controls the deadline, not the polling cadence. The runtime owns an
adaptive schedule that:

- stays within Google Cloud Quotas API read budgets across concurrent observations;
- honors provider retry guidance and throttling;
- applies bounded backoff and jitter to transient failures;
- avoids synchronizing many watches against the same resource scope;
- refreshes preference and effective quota independently at the freshness
  required by the selected condition; and
- emits only material observations.

The exact cadence, backoff constants, coalescing strategy, and client-library
mechanics are runtime architecture decisions. They may change without changing
the Watch contract or caller deadline.

## Audit correspondence

Every preview and Apply result references its append-only audit record without
exposing secret material. Watch observations that are retained in the audit log
use the same operation, resource scope, preference identity, status axes, values,
timestamps, outcome, and diagnostic codes as the public result.

Failure to persist and fsync the pre-Apply intent prevents the provider call and
exits `9`. Failure to persist the result after a possible provider write emits
a critical unknown outcome, exits `9`, and preserves the deterministic
preference identity needed for reconciliation.

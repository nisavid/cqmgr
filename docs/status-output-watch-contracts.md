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
| Establish resource scope | The canonical project selection is resolved offline from explicit input, an explicitly named profile, a direct local selection, or the explicitly selected local profile. V1 rejects folder and organization operations without inferring a project. | Resource-scope type and selected resource name; resolution source; explicit `identity_evidence: deferred-offline`. Provider canonicalization, acting principal, and impersonation chain are deliberately not loaded. |
| Browse quota | The requested logical page or bounded query is read completely from its explicit queried-provider set. A bare query federates Compute and Cloud TPU; service or catalog-group filters prune provider reads and displayed rows. | Canonical resource scope; canonicalized query and page identity; exact slices; constraint-set relationships; independent queried-provider source/location coverage; intentionally unqueried providers; release-relative catalog digests and observation times; continuation identity when present. |
| Inspect slice | One complete exact effective quota slice and its required related evidence are read. | Provider identity; dimensions and quota scope; effective value; usage; provider quota preference; eligibility; related constraints; independent source times and completeness. |
| Resolve requirement | The supplied Compute instance or Cloud TPU slice requirement resolves without guessing for every requested candidate location. | Discriminated normalized requirement; per-location compatibility disposition, native-unit requirement, owning service and management plane, exact constraint set, coverage, and ambiguity or rejection evidence. |
| Assess Spot advice | The exact supported VM request is assessed for the requested evidence. | Full machine configuration, quantity, distribution, locations, provider coverage, Preview status, observation or interval times, and every available advice datum. |
| Compose request | One direct exact-slice target or one selected compatible workload location is converted into absolute targets for every selected independently mutable child. | Request form; resource scope; exact-slice `manual` target or normalized workload, location, and selected strategy; complete ordered child set; exact slices, absolute targets, and units; prior desired, granted, effective, and fresh usage values; no-op children; directions; required warnings and acknowledgements. |
| Preview plan | A locally portable, integrity-protected single or bundle quota request plan is produced, or every child is freshly verified as a no-op. V1 Apply capability is bound to the issuing installation. | Plan subject kind; bound resource scope; exact-slice `manual` input for `single`, or selected location, strategy, normalized workload, and complete constraint set for `bundle`; ordered non-no-op children; per-child evidence, identity, intent, principal, warnings, and acknowledgements; plan expiry, digest, issuing-installation trust, and Apply capability when a plan is produced, otherwise every no-op reason. |
| Review plan | Canonical plan bytes and their digest are verified and all trustworthy bound evidence is presented without applying it. Expiry, foreign issuer, prior consumption, or unresolved acknowledgements remove Apply capability but do not make safe inspection fail. | Every bound plan fact, canonical and digest verification state, expiry, issuer and consumption state, unresolved acknowledgements, Apply capability, and exact incapability reasons. |
| Apply plan | Every non-no-op child in the bound deterministic accelerator-first order is proven accepted. Apply is non-atomic; the first conclusively failed or transport-unknown child stops dispatch and accepted children are never rolled back. A verified all-no-op Preview has no Apply capability. | Plan digest and subject kind; resource scope; bound child order; every child's exact slice, target, unit, disposition, provider preference identity, etag and trace when present, submitted observation, and audit reference; aggregate boundary and outcome. |
| Watch request | The explicitly selected Watch condition is reached for one accepted request or for every accepted child of a bundle. | Single or bundle subject identity; selected condition; durable Apply record; ordered children and dispositions; per-accepted-child preference identity, orthogonal status, target, granted, effective, and lineage values; aggregate state; all material observations and final outcome. |
| Inspect audit | The requested bounded audit query is read completely. | Query and record identities; canonical resource scopes; observation times; continuity metadata. |
| Verify audit | The requested records and rotation checkpoints form a valid chain. | Verified range and checkpoints, or the exact first continuity failure and affected range. |

Apply therefore succeeds at `submitted` only when every non-no-op child is
accepted. A verified all-no-op bundle is a successful Preview result with no
Apply capability. Apply does not wait for preference settlement or effective
quota. A timeout or transport failure after a child provider call is reconciled
through that child's deterministic preference identity before any later child
can be attempted. Only a result proven to contain the bound accepted child
intent receives disposition `accepted`. A conclusive rejection or failure is
`failed`; a dispatched child whose acceptance remains unproven is `unknown`.
Either stops dispatch, preceding accepted children remain accepted, later
children are `unattempted`, and the aggregate Apply boundary is not reached.
An unknown child is consumed and quarantined until read-after-unknown
reconciliation proves its outcome. A verified preflight no-op is a composition
fact, not a dispatch disposition.

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

## Requirement, target, and bundle results

A requirement result is a discriminated Compute instance or Cloud TPU slice
shape. Compute binds machine type, instance count, provisioning model, and
explicit candidate locations or all compatible locations. Cloud TPU binds
accelerator type, topology, runtime version, slice count, provisioning model,
and explicit candidate locations or all compatible locations. Supported
workload consumers are derived and reported as metadata; V1 has no consumer
input because its current Compute and GKE consumers have identical quota
constraints. The result contains an ordered per-location record rather than
one synthesized global answer. Each record is `compatible`, `incompatible`,
`ambiguous`, or `incomplete` and carries the exact native-unit requirement,
constraint set, coverage, and reasons applicable to that location. A
compatible selected location may be complete when unrelated locations are
incomplete; an all-compatible-locations result cannot claim exhaustive
coverage when any required provider location was not enumerated completely.

Request composition records one explicit target strategy:

- `minimum`, the default, sets each deficient child's target to fresh usage
  plus the normalized workload requirement. A child that already permits the
  workload is a no-op; the strategy never silently decreases it or supersedes
  an existing provider intent.
- `preserve-headroom` sets each selected child's target to current effective
  quota plus the normalized workload requirement.
- `manual` requires one explicit absolute target for each selected child and
  applies the ordinary sufficiency, decrease, unlimited-transition, warning,
  and acknowledgement gates.

Targets retain each child's native unit. No strategy adds or compares values
across different units. A bundle result retains its selected location,
normalized workload, strategy, complete constraint set, no-op children, and
the deterministic accelerator-first order of non-no-op children.

Plan schema `cqmgr.quota-request-plan/v1` has required top-level
`kind: "single"|"bundle"` beside `schema`. `single` binds exactly one ordered
child; `bundle` binds one or more ordered children. Exact-slice composition uses
strategy `manual` and kind `single`; workload-derived composition uses kind
`bundle` even when only one non-no-op child remains. Unknown kinds are rejected.
Child records remain ordered and may gain additive fields within v1. Because
v1 is being defined before the first release, these semantics supersede the
earlier single-only planning text without creating v2; after release, an
incompatible change requires a new plan-schema version.

An Apply child disposition is exactly `accepted`, `failed`, `unknown`, or
`unattempted`.
These values describe durable non-atomic dispatch, not a transaction:

- `accepted` proves that provider reconciliation found the bound child intent
  at its deterministic preference identity;
- `failed` identifies the first conclusively rejected or failed child and
  includes its exact provider outcome;
- `unknown` identifies the first dispatched child whose acceptance cannot be
  proven after a transport or persistence ambiguity and requires
  read-after-unknown reconciliation; and
- `unattempted` identifies every later child that was not dispatched because
  dispatch stopped.

An aggregate result never relabels earlier accepted children as rolled back.
It reaches the Apply boundary only when every non-no-op child is `accepted`.
The exact failed or unknown child selects the aggregate nonzero exit class;
accepted children and their reconciliation identities remain available for
Watch and recovery. Verified no-op children remain in composition evidence but
do not receive Apply dispositions.

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
  project. Offline scope-selection results identify their locally validated
  selected project and carry `data.identity_evidence:
  "deferred-offline"`; they do not synthesize a principal or mark the result
  incomplete. Provider-scoped operations replace that deferral with
  Resource Manager canonicalization and explicit acting-principal evidence.
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
- `data` contains the operation payload. Single and bundle plan, Apply, and
  Watch-related results retain their discriminators and ordered child records.
  Quota quantities and other provider integers use base-10 strings with
  explicit units so JSON consumers do not lose 64-bit precision.
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
- offline scope results state that acting-principal evidence was deferred,
  while provider-scoped results show the resolved principal and impersonation
  chain;
- quota results preserve exact slice identity, dimensions, scope, native unit,
  source times, independent provider/location coverage, release-relative
  catalog evidence, and completeness;
- requirement results preserve every requested location and its compatibility,
  native-unit requirement, exact constraints, coverage, and reasons;
- quota request results preserve desired, granted, and effective values as separate
  facts and show all three status axes;
- plan and Apply results preserve plan kind, target strategy, principal, plan
  digest, expiry, warnings, acknowledgements, deterministic child order, child
  dispositions, and every accepted provider identity without exposing the
  quota contact;
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

Inventory results expose coverage for every queried provider independently,
including every provider page, Compute scope warning or failure, Cloud TPU
location and subsource result, catalog digest, and observation time. With no
service or catalog-group filter, the queried-provider set contains both
Compute and Cloud TPU. A filter prunes actual reads and rows and records the
other provider as intentionally unqueried; it does not make that provider a
coverage failure. Input service selectors `compute` and `tpu` normalize to
`compute.googleapis.com` and `tpu.googleapis.com`, and only the full DNS names
appear in query identity, output, persistence, audit, plans, or copied
commands. An explicitly selected location may be complete when all evidence
required for that location and its exact quota constraints is present even if
unrelated locations failed. Conversely, an all-locations or release-relative
exhaustive result is incomplete when any page, scope, location,
accelerator-type list, machine-type list, or runtime-version list required for
its declared queried-provider set was not exhausted. A successful location
never implies global coverage.

Expected provider coverage gaps are not incomplete observations. A supported
Spot request whose live advice succeeds while historical GPU advice is
documented as unsupported is complete when that unsupported coverage is
represented explicitly.

## Watch conditions

A Watch always selects one condition and one deadline explicitly. Its subject
is either one accepted request or one bundle with at least one accepted child.
An interactive surface may offer deadline presets, but it selects neither input
silently.

The initial Watch selects its durable Apply record through the shared
`--intent-id INTENT_ID` input for both single and bundle subjects. There is no
separate plan-ID or bundle-ID selector. `subject.kind` and the durable record
identified by the intent ID determine whether the subject is `single` or
`bundle`; resume uses the authenticated token instead of accepting a second
subject selector.

| Condition | One accepted child reaches it when | Bundle reaches it when | Conclusive mismatch |
| --- | --- | --- | --- |
| `granted` | Reconciliation is `settled` and granted equals the child's target. | Every accepted child reaches `granted`. | Exit `7` when any accepted child's settled grant differs from its target, including zero when the target is greater than zero. |
| `fulfilled` | `granted` is reached and a fresh effective observation equals the child's target and granted values. | Every accepted child reaches `fulfilled`. | Exit `7` when any accepted child's settled grant differs from its target, including zero when the target is greater than zero. |

Provider `failed` or `superseded` state for any accepted child terminates an
aggregate condition it makes impossible with exit `7`. A transient or
recoverable unknown child observation stays visible and polling continues
within the deadline. An irrecoverable local or provider observation failure
exits under its applicable class. Apply children with disposition `failed` or
`unknown` or `unattempted` remain visible in the subject and aggregate summaries
but are not polled as accepted requests. An `unknown` Apply child must first
complete read-after-unknown reconciliation; it is never treated as an accepted
Watch child merely because the target matches. A bundle with no accepted child
is not watchable.

## Watch stream

Structured Watch output is newline-delimited, versioned JSON. It emits one
self-contained record for the initial authoritative subject observation, each
material child or aggregate change, and the terminal result. Unchanged polling
ticks do not produce public events.

`cqmgr.watch-event/v1` has required `subject.kind` of `single` or `bundle`.
Unknown kinds are rejected. `subject` binds the canonical resource scope,
selected condition, shared `intent_id`, and a nonempty ordered `children`
list. A `single` subject has exactly one child. A `bundle` subject retains
every ordered Apply child and disposition and has one or more accepted
children. Each child has a stable `child_id`, bound order, exact effective
quota-slice identity, target, unit, disposition, and deterministic provider
preference identity. Child records may gain additive fields within v1, but
their order and identities are semantic. Because v1 is being defined before
the first release, these semantics supersede the earlier single-request-only
event shape without creating v2; after release, an incompatible subject or
child change requires a new Watch schema version.

The intent ID must resolve to one durable local cqmgr Apply record with the
same subject kind, resource scope, order, child dispositions, preference
identities, targets, and units. V1 does not adopt an unrelated provider
preference or reconstruct a bundle from provider state. A producer may not emit
an empty or provider-defined identity in place of the complete subject.

A material child event names `child_id` and carries that child's complete
orthogonal status. An aggregate event and every terminal result retain ordered
child summaries and the aggregate condition state. Different native-unit
values are never added into an aggregate amount.

Every event also emits `resume`, an opaque `cqmgr.watch-resume/v1` token
authenticated by the issuing installation. It binds the complete subject,
selected condition, ordered accepted-child identities, each child's last
observed provider etag and stable trace ID when present, the durable aggregate
checkpoint, and the stream sequence. It contains no credential or quota
contact. Before an initial event, Watch verifies every accepted child's current
preference target and trace ID against its Apply record. When no stable trace
ID exists, the current etag must equal that child's Apply response etag;
otherwise lineage is unknown and Watch returns rejected-precondition rather
than treating a same-target amendment as the original intent.

Resume applies the same checks to the authenticated token and durable
checkpoint. It rejects a subject when any child identity or disposition differs
from the Apply record, any later local Apply superseded an accepted preference,
or any provider trace ID changed. When a child has no stable trace ID, an etag
change across the observation gap is unknown lineage and rejects the complete
resume rather than guessing whether reconciliation or a same-target amendment
occurred. Each material event carries a new token for its durable checkpoint.

```json
{
  "schema": "cqmgr.watch-event/v1",
  "stream_id": "opaque-run-identity",
  "sequence": 4,
  "event": "child-status-changed",
  "resume": "cqmgr.watch-resume/v1:opaque-authenticated-token",
  "observed_at": "2026-07-21T02:07:00Z",
  "subject": {
    "kind": "bundle",
    "resource_scope": "projects/123456789",
    "condition": "fulfilled",
    "intent_id": "sha256:opaque-applied-plan-digest",
    "children": [
      {
        "child_id": "accelerator-region",
        "order": 0,
        "disposition": "accepted",
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
      }
    ]
  },
  "child_id": "accelerator-region",
  "status": {
    "reconciliation": "reconciling",
    "grant_satisfaction": "unknown",
    "effective_confirmation": "unobserved",
    "desired": "8",
    "granted": null,
    "effective": null,
    "unit": "1"
  },
  "aggregate": {
    "condition_reached": false,
    "accepted_children": 1,
    "children": [
      {
        "child_id": "accelerator-region",
        "reconciliation": "reconciling",
        "grant_satisfaction": "unknown",
        "effective_confirmation": "unobserved"
      }
    ]
  },
  "diagnostics": []
}
```

`sequence` increases within one stream. A resumed Watch creates a new stream,
starts with the current authoritative observation of every accepted child, and
retains the complete subject, intent ID, ordered children, and selected
condition from the verified resume token. It does not pretend that events
missed while disconnected were observed.

The terminal event has `event: "terminal"` and carries the complete operation
result. It is emitted when the selected condition is reached, a conclusive
adverse child or aggregate state occurs, the deadline expires, or the stream is
interrupted when there is enough process lifetime to serialize it.

On timeout, the terminal result uses exit `8` and includes the selected
condition, deadline, elapsed duration, ordered child summaries, aggregate
state, last material observation, and latest resume token. Timeout describes
the Watch operation, not the underlying quota requests, and never relabels
them as failed.

On interruption, the manager emits a terminal interrupted event when possible,
exits `130`, and leaves every provider preference unchanged. A later Watch can
resume the complete subject from the latest verified resume token.

## Polling ownership

The caller controls the deadline, not the polling cadence. The runtime owns an
adaptive schedule that:

- stays within Google Cloud Quotas API read budgets across concurrent observations;
- honors provider retry guidance and throttling;
- applies bounded backoff and jitter to transient failures;
- avoids synchronizing many watches against the same resource scope;
- refreshes every accepted child's preference and effective quota
  independently at the freshness required by the selected condition, then
  recomputes the aggregate condition; and
- emits only material observations.

The exact cadence, backoff constants, coalescing strategy, and client-library
mechanics are runtime architecture decisions. They may change without changing
the Watch contract or caller deadline.

## Audit correspondence

Every Preview and Apply result references its append-only audit record without
exposing secret material. Bundle audit evidence preserves the intent ID,
subject kind, bound order, every no-op or dispatch disposition, each
deterministic preference identity, and the aggregate result. Watch observations
that are retained in the audit log use the same operation, intent ID, resource
scope, ordered child identities, status axes, values, timestamps, aggregate
outcome, and diagnostic codes as the public result.

Failure to persist and fsync the bundle pre-Apply intent prevents every
provider call and exits `9`. Failure to persist a child result after a possible
provider write emits a critical `unknown` outcome, stops later children, exits
`9`, and preserves the deterministic preference identity needed for
read-after-unknown reconciliation.

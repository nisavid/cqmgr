# Status, output, and watch contract

Cloud Quotas reports every domain operation through the same surface-neutral
result model. Human CLI output, TUI presentation, JSON automation, audit
evidence, and Watch streams preserve the same target, observations, outcomes,
and diagnostics without treating provider acceptance as effective quota.

This contract defines behavior and stable machine semantics. The CLI command
tree, option spelling, TUI navigation, and polling implementation belong to
their owning interface and runtime decisions.

## Operation boundaries

Each operation declares the condition it promises before it can report
success. Exit code `0` means that the operation reached that boundary with the
required evidence; it never means that every quota mutation is fully
fulfilled.

| Domain operation | Success boundary | Required result facts |
| --- | --- | --- |
| Establish target | The canonical project, folder, or organization is resolved from explicit input or an explicitly selected profile. | Target type and canonical resource name; acting principal and impersonation chain; resolution source. |
| Browse quota | The requested logical page or bounded query is read with complete required provider evidence. | Canonical target; query and page identity; exact slices; constraint-set relationships; source observation times; continuation identity when present. |
| Inspect slice | One complete exact effective quota slice and its required related evidence are read. | Provider identity; dimensions and scope; effective value; usage; preference; eligibility; related constraints; independent source times and completeness. |
| Resolve requirement | The supplied workload requirement resolves without guessing to a supported constraint set. | Normalized requirement; owning service and management plane; exact slices; compatibility and ambiguity evidence. |
| Assess Spot advice | The exact supported VM request is assessed for the requested evidence. | Full machine configuration, quantity, distribution, locations, provider coverage, Preview status, observation or interval times, and every available advice datum. |
| Compose preference | One absolute desired value is validated against one exact mutable slice and current evidence. | Exact slice; desired value and unit; prior desired, granted, effective, and usage values; direction; required warnings and acknowledgements. |
| Preview plan | A portable, integrity-protected mutation plan is produced, or a settled identical desired value is freshly verified as a no-op. | Bound target, slice, evidence, identity, intent, principal, warnings, acknowledgements, expiry, digest, and apply capability or no-op reason. |
| Review plan | The plan is parsed, its integrity and expiry are checked, and all bound evidence is presented without applying it. | Every bound plan fact, verification state, expiry, and unresolved acknowledgement. |
| Apply plan | The provider preference is proven accepted under the bound intent. A verified no-op has no Apply capability. | Plan digest; target and slice; desired value; provider preference identity, etag, and trace when present; submitted observation; audit reference. |
| Watch preference | The explicitly selected Watch condition is reached. | Preference identity; selected condition; orthogonal status; desired, granted, and effective values; all material observations and final outcome. |
| Inspect audit | The requested bounded audit query is read completely. | Query and record identities; canonical targets; observation times; continuity metadata. |
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

## Preference status

Preference status has three independent axes. Surfaces may derive a concise
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

`confirmed` may coexist with a partial grant. `fully-fulfilled` is the stronger
derived condition in which reconciliation is `settled`, grant satisfaction is
`full`, and effective confirmation is `confirmed` for equal desired, granted,
and effective values.

## Versioned operation result

Every non-streaming structured result uses one top-level envelope. Operation-
specific fields stay inside `data`; provider resources never define the public
top-level schema.

```json
{
  "schema": "cloud-quotas.operation-result/v1",
  "operation": "preference.watch",
  "target": {
    "type": "project",
    "name": "projects/example-project"
  },
  "boundary": {
    "condition": "fully-fulfilled",
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
- `target` is required for target-scoped operations and uses the canonical
  provider resource name. It never comes from an unreported ambient project.
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

Credentials, tokens, quota-contact values, sensitive annotations, and raw
provider bodies are excluded. Safe provider metadata may include HTTP or gRPC
status, a documented reason, preference identity, etag, and trace or request
identity.

## Human-readable output

Human presentation may evolve without a text-layout compatibility promise.
Tables may reorder, wrap, truncate with an explicit marker, or become grouped
views for terminal width and accessibility. Scripts must use the versioned
structured result.

Presentation changes may not remove the facts needed to identify the target,
interpret the operation boundary, or act safely. In particular:

- every target-scoped result identifies the canonical target;
- quota results preserve exact slice identity, dimensions, scope, native unit,
  source times, and completeness;
- mutation results preserve desired, granted, and effective values as separate
  facts and show all three status axes;
- plan and Apply results preserve principal, plan digest, expiry, warnings,
  acknowledgements, and provider identity without exposing the quota contact;
- Spot advice preserves its exact request configuration, coverage, Preview
  status, and observation or interval time; and
- errors identify the operation, target when known, stable symbolic code, safe
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
| `0` | Success | The selected operation boundary was reached with complete required evidence. A settled Watch may return a partial grant successfully because settlement, not full grant, was requested. |
| `2` | Usage | CLI syntax, option shape, or input decoding is invalid. A structured envelope is returned when the invocation can be decoded far enough to form one. |
| `3` | Rejected precondition | The request is well formed but unsupported, ineligible, ambiguous, missing an acknowledgement, or otherwise barred before execution. |
| `4` | Authorization | Authentication, permission, or allowed principal/contact verification prevents the operation. |
| `5` | Stale or conflicting | Bound evidence drifted, a plan expired, an etag conflicted, identity was ambiguous, or a different intent occupies the deterministic preference identity. |
| `6` | Incomplete evidence | Usable observations are returned, but a required source, page, refresh, or local read is missing. |
| `7` | Requested outcome unmet | A conclusive provider or verification outcome cannot satisfy the selected boundary, including a settled partial grant for a fully granted Watch, provider failure or supersession, or an invalid audit chain. |
| `8` | Timeout | The caller's deadline arrived before the selected condition. The result retains the last material observation and resume identity. |
| `9` | Operational failure | A provider, transport, serialization, audit persistence, or local internal failure prevents a trustworthy result in another class. |
| `130` | Interrupted | The caller interrupted the operation. No provider mutation is canceled or reversed implicitly. |

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
class of `6`, and source-specific diagnostics. It never satisfies a mutation
gate that requires the missing evidence. A fully unavailable operation returns
the more specific authorization, precondition, timeout, conflict, or
operational class when one is known instead of claiming partial data.

Expected provider coverage gaps are not incomplete observations. A supported
Spot request whose live advice succeeds while historical GPU advice is
documented as unsupported is complete when that unsupported coverage is
represented explicitly.

## Watch conditions

A noninteractive Watch always selects one condition explicitly. An interactive
surface may offer the same choices, but it makes the selected condition visible
before starting.

| Condition | Reached when | Settled partial grant |
| --- | --- | --- |
| `settled` | Reconciliation is `settled` and a granted value is authoritative. | Exit `0`; retain desired and granted values and emit a warning unless human quiet presentation suppresses it. |
| `fully-granted` | Reconciliation is `settled` and granted equals desired. | Exit `7` as soon as the conclusive partial settlement is observed. |
| `fully-fulfilled` | Reconciliation is `settled`, granted equals desired, and a fresh effective observation equals both. | Keep watching until full fulfillment or the caller's deadline. A partial settlement alone does not terminate this condition. |

Provider `failed` or `superseded` state terminates any condition it makes
impossible with exit `7`. A transient or recoverable unknown observation stays
visible and polling continues within the deadline. An irrecoverable local or
provider observation failure exits under its applicable class.

## Watch stream

Structured Watch output is newline-delimited, versioned JSON. It emits one
self-contained record for the initial authoritative observation, each material
status or evidence change, and the terminal result. Unchanged polling ticks do
not produce public events.

```json
{
  "schema": "cloud-quotas.watch-event/v1",
  "stream_id": "opaque-run-identity",
  "sequence": 4,
  "event": "status-changed",
  "observed_at": "2026-07-21T02:07:00Z",
  "preference": {},
  "status": {
    "reconciliation": "settled",
    "grant_satisfaction": "partial",
    "effective_confirmation": "mismatch"
  },
  "diagnostics": []
}
```

`sequence` increases within one stream. A resumed Watch creates a new stream,
starts with the current authoritative observation, and retains the deterministic
preference identity and selected condition; it does not pretend that events
missed while disconnected were observed.

The terminal event has `event: "terminal"` and carries the complete operation
result. It is emitted when the selected condition is reached, a conclusive
adverse state occurs, the deadline expires, or the stream is interrupted when
there is enough process lifetime to serialize it.

On timeout, the terminal result uses exit `8` and includes the selected
condition, deadline, elapsed duration, last material observation, and
preference identity needed to resume. Timeout describes the Watch operation,
not the underlying mutation, and never relabels that mutation as failed.

On interruption, the manager emits a terminal interrupted event when possible,
exits `130`, and leaves the provider preference unchanged. A later Watch can
resume from the deterministic preference identity.

## Polling ownership

The caller controls the deadline, not the polling cadence. The runtime owns an
adaptive schedule that:

- stays within Cloud Quotas read budgets across concurrent observations;
- honors provider retry guidance and throttling;
- applies bounded backoff and jitter to transient failures;
- avoids synchronizing many watches against the same target;
- refreshes preference and effective quota independently at the freshness
  required by the selected condition; and
- emits only material observations.

The exact cadence, backoff constants, coalescing strategy, and client-library
mechanics are runtime architecture decisions. They may change without changing
the Watch contract or caller deadline.

## Audit correspondence

Every preview and Apply result references its append-only audit record without
exposing secret material. Watch observations that are retained in the audit log
use the same operation, target, preference identity, status axes, values,
timestamps, outcome, and diagnostic codes as the public result.

Failure to persist and fsync the pre-Apply intent prevents the provider call and
exits `9`. Failure to persist the result after a possible provider write emits
a critical unknown outcome, exits `9`, and preserves the deterministic
preference identity needed for reconciliation.

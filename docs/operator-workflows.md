# Operator workflow contract

Cloud Quota Manager presents one domain workflow through an interactive TUI,
scriptable CLI commands, and structured automation. The surfaces may arrange
controls differently, but they operate on the same resource scopes, effective
quota slices, specialized-hardware catalog, per-location constraint sets,
single and bundle quota requests, quota request plans, and reconciliation
observations.

This contract defines operator intent and transitions. The exact surface
structure is defined in [CLI and TUI information
architecture](cli-tui-information-architecture.md). Output schemas remain in
the status contract. The implementation stack and provider boundaries are
defined in the [runtime and integration
architecture](runtime-integration-architecture.md).

## Shared domain operations

Every surface exposes these operations independently:

1. establish or change a resource-scope selection;
2. browse the federated V1 effective-quota inventory and its explicit
   per-source coverage;
3. inspect one exact effective quota slice and its related evidence;
4. resolve a `compute-instance` or `cloud-tpu-slice` workload shape to separate
   exact constraint sets for explicit or all-compatible candidate locations;
5. assess supported Spot capacity advice for an exact VM configuration;
6. compose absolute desired values for one exact mutable slice or one ordered
   multi-slice workload bundle;
7. Preview and review a portable single or bundle quota request plan;
8. Apply one reviewed plan deliberately through an ordered non-atomic
   operation;
9. Watch child request reconciliation and aggregate effective-quota
   confirmation; and
10. inspect and verify local audit evidence.

The TUI invokes these operations interactively. The CLI invokes each operation
directly and supports no-color human-readable and structured noninteractive
results. A plan created on either surface can be reviewed or applied on the
other when its identity, principal, freshness, and integrity requirements still
hold.

## TUI default: quota inspector

The TUI opens on the quota inspector rather than a workload questionnaire or
an operation launcher. It may restore a recent resource-scope selection or use
an explicitly configured selection. It does not require the operator to select
the same resource scope again at every launch.

The active canonical project remains prominent beside every quota view and
detail surface. V1 accepts only project resource scopes and always rejects folder
and organization resource scopes. Inference or provider access for those scopes
is not part of V1; those variants remain reserved for later support. Ambient `gcloud` or
Application Default Credentials settings never
silently replace the project. Switching the resource scope is a deliberate
inspector action. Apply requires the operator to
confirm the exact resource scope again; a noninteractive Apply supplies the
same explicit resource-scope acknowledgement as input.

Showing, selecting, or clearing resource scope remains a local offline
operation. It does not initialize ADC, contact Resource Manager, verify provider
access, or claim an acting principal. Acting-principal and impersonation-chain
evidence appears only after a provider-scoped operation observes it; until then
the TUI says `principal not observed`.

The inspector federates the versioned Compute and legacy Cloud TPU V1 inventory
by default. With no service or catalog-group filters it queries both providers.
Repeatable service and catalog-group filters infer and visibly prune both the
queried provider subset and displayed results; there is no separate source
selector. Short service input `compute` or `tpu` is accepted, while durable
output uses `compute.googleapis.com` or `tpu.googleapis.com`. Per-source pages,
failures, observation times, and coverage stay explicit for every queried
source, without claiming coverage for sources pruned by the filters. Usable
exact slices remain visible when another queried source is incomplete, but the
aggregate does not claim complete global totals or ordering.

The inspector groups related specialized-hardware slices into accelerator
constraint sets. Related regional, global, and zonal slices and separate quota
pools stay independent rows with their exact provider identity, native unit,
effective value, usage source, desired and granted values, eligibility,
timestamps, and lifecycle state visible. The grouping explains which slices
can constrain the same workload; it never synthesizes one combined quota or
implies physical capacity.

Every discovered slice remains browseable. Every specialized-hardware product
authoritatively classified by covered GCP catalog evidence is cataloged.
Unknown or non-accelerator slices appear through a generic provider-truth view
rather than disappearing. Catalog presence does not imply guided resolution,
request eligibility, Spot advice, or capacity; each requires its own complete
evidence.

Guided accelerator constraint sets offer Spot capacity advice when the
catalog can map an exact Spot VM configuration to a provider-supported advice
request. Advice is attached to the machine configuration, quantity,
distribution shape, and candidate region or zones rather than to a quota row
alone.

## Inspect and compose

Selecting a slice opens a detail pane that keeps these facts together:

- canonical resource scope, service, quota ID, dimensions, quota scope, and unit;
- effective value and source timestamp;
- usage and its separate source timestamp when available;
- adjustment eligibility and provider rollout state;
- existing preference identity, desired value, granted value, etag, and
  reconciliation state;
- related accelerator constraints and remaining bottlenecks;
- acting principal and impersonation chain; and
- valid next operations.

A mutable exact slice offers a quota request composer in this pane. The operator
enters an absolute desired value, not an increment. This explicit target uses
strategy `manual` and produces plan kind `single` unless it is a verified
no-op. Creating a new preference and amending a settled or reconciling
preference use the same flow. An amendment also shows the prior desired value,
whether a pending request will be superseded, and both desired-versus-effective
and replacement-versus-existing directions.

Broader inherited preferences remain visible and read-only. The detail pane may
offer a more-specific exact-slice preference after explaining the inherited
value and precedence change. An identical settled desired value is a verified
no-op and offers no apply operation. Unsupported, ambiguous, stale, or
observe-only slices explain why composition cannot continue.

Workload-first composition begins from one resolved `compute-instance` or
`cloud-tpu-slice` location. Its constraint set becomes an ordered bundle of
independently mutable exact slices. The plan remains kind `bundle` when verified
no-ops leave only one mutation child. The operator can omit an optional
companion only by leaving the workload-first flow and composing an explicit
single-slice request. Candidate locations remain separate alternatives and are
never merged into one plan or ranked by quota sufficiency.

The operator chooses one target strategy for the selected constraint set:

- `minimum` is the default. For each deficient child it proposes fresh observed
  usage plus the normalized workload requirement in that slice's native unit.
  A child that already permits the workload is a verified no-op, never an
  automatic decrease.
- `preserve-headroom` proposes current effective quota plus the normalized
  workload requirement for each child.
- `manual` requires one explicit absolute target for every selected child.

`minimum` never silently amends an existing provider intent. It preserves an
equal or higher settled or reconciling desired state; a lower conflicting
intent requires an explicit manual target or a new Preview after settlement.
Preview retains the strategy, fresh input facts, formula, native-unit
conversion, proposed target, and no-op or mutation classification independently
for every child. Missing or ambiguous conversion, incomplete required evidence,
or one ineligible required child prevents plan issuance.

Preview performs no provider mutation and never calls a provider
`validateOnly` mutation path.

## Review and apply

Preview leaves the detail pane or workload resolver for a dedicated plan
review. Every plan declares `kind: single|bundle` and contains an ordered
nonempty collection of non-no-op request children; a single plan has exactly
one child. Preview retains verified no-op children in the complete constraint
evidence, not as dispatchable plan children. Review keeps the selected
constraint set and active resource scope visible while presenting every bound
exact slice, target and derivation, existing preference state, principal,
warnings, acknowledgements, quota-contact source, evidence ages, plan expiry,
and expected consequences. Unknown plan kinds or inconsistent child collections
cannot be reviewed as trustworthy plans.

Single-slice review makes clear that Apply changes only the selected slice.
Bundle review makes clear that Apply is ordered and non-atomic: accepted earlier
children are not rolled back if a later child fails or becomes transport
unknown. No companion slice is changed merely because it was related; only
reviewed children belong to the plan. Dangerous decreases, unlimited-value
transitions, missing evidence, drift, ongoing rollouts, and expert
acknowledgements follow the safety and quota request contract independently for
each child; the workflow does not weaken those gates for convenience.

Apply requires an explicit confirmation of the canonical resource scope. It
revalidates every child and consumes the whole single-use plan before the first
dispatch. Children run in the reviewed deterministic accelerator-first order:
accelerator- or location-specific slices precede broader companion constraints,
and canonical exact-slice identity breaks ties. Each child has one durable
pre-intent and at most one provider dispatch. Apply stops at the first child
whose acceptance cannot be proven, marks later children `unattempted`, and never
attempts rollback. Each plan-child disposition is exactly `accepted`, `failed`,
`unknown`, or `unattempted`. A transport-unknown dispatch is `unknown`, not
`failed`; a failed child preserves its exact unchanged, conflicting, or other
conclusive failure outcome. Verified Preview no-ops remain separate explicit
facts. A stale or drifted plan returns to reviewable evidence instead of
silently rebuilding, reordering, or applying a different intent.

Apply is the sole workflow that may reach a quota-preference write port. This
contract does not authorize a live quota mutation; live execution requires
separate explicit authority for the exact resource scope and operation.

## After apply and reconciliation

After Apply, the TUI returns to the quota inspector with the affected slice or
constraint set selected. Each child shows separate reconciliation,
grant-satisfaction, and effective-confirmation axes plus its Apply outcome.
The aggregate may derive a concise submitted, reconciling, request-settled,
granted, failed, partial, unknown, or fulfilled headline from those simultaneous
facts, but the headline never replaces or flattens child axes. Acceptance never
appears as an effective quota change.

The inspector updates lifecycle observations inline and offers a focused Watch
operation for longer-running reconciliation. A Watch subject declares
`kind: single|bundle` and retains every ordered plan child with its Apply
disposition; a single subject has exactly one child. Watch polls only the
accepted subset. An Apply with no accepted child is not watchable. Material
child events name their `child_id`, and one aggregate terminal event retains
ordered summaries for every subject child.

Aggregate `granted` requires every accepted subject child to settle at its
target. Aggregate `fulfilled` additionally requires fresh effective quota
matching every accepted target and settled grant. Preview no-op children are
not Watch subjects. One accepted child settled below target makes the aggregate
requested outcome unmet without hiding accepted or still-reconciling siblings.
Timeout describes the observation boundary and never relabels child request
state. Quota request reconciliation remains separate from VM, queued-resource,
reservation, workload, or physical-capacity state.

Transport failures enter an explicit reconciliation result. The workflow reads
each deterministic preference identity and classifies it as accepted,
unchanged, conflicting, or unknown; it never offers a blind retry.

## Spot capacity advice

Spot capacity advice is a read-only first-release workflow for supported
Compute Engine machine configurations. The provider contract, coverage limits,
and evidence semantics are recorded in [Spot capacity-advice
contracts](research/spot-capacity-advice-contracts.md).

From one selected location of a resolved `compute-instance` workload, the
operator confirms the inherited shape and supplies or confirms one or more
obtainability candidates. The standalone workflow accepts the same complete
shape explicitly. Each candidate supplies:

- the Spot provisioning model;
- an exact machine type and any required GPU type and count to attach;
- the number of VMs;
- a target distribution shape; and
- one endpoint region with explicit candidate zones when the request is
  zone-constrained.

Every candidate retains its complete immutable request identity. A comparison
may include several regional candidates for the same fixed machine
configuration, quantity, and distribution intent. A provider score for one
regional candidate is never relabeled as evidence for an individual zone.

The result keeps its request configuration visible and presents the provider's
current obtainability score, estimated uptime, recommended zonal shards,
historical daily preemption rate, and historical Spot price where each datum is
available. Operators can compare supported configurations and locations
without changing quota or creating compute resources.

Every datum carries its provider source, observation or interval time, Preview
status, and coverage. Obtainability is a current likelihood, not a capacity
guarantee. Estimated uptime is an advisory minimum for most requested Spot VMs,
not an SLA. Historical preemption rate is the provider's aggregate rate for
matching stopped Spot VMs, not a project-specific fleet failure rate.

The workflow explains unsupported combinations rather than guessing or hiding
them. Live Spot advice does not cover TPUs. Historical advice does not cover
N1 machine types with attached GPUs, custom machine types, or TPUs. A catalog
mapping from accelerator intent to machine configuration is necessary but does
not widen the provider's documented coverage.

The CLI and TUI expose the same advice request and evidence. The TUI presents
it beside the selected constraint set and comparison candidates. The CLI can
run the assessment independently with explicit configuration and stable
structured output. Neither surface probes capacity by attempting resource
creation.

## Workload-first requirement resolver

The resolver is a first-class entry point for an operator who knows the
deployable workload shape but not the owning quota slices. It never begins with
a provider service, quota ID, management plane, or static accelerator allowlist.

`compute-instance` requires an exact machine type, instance count, and
provisioning model. V1 asks for no workload-consumer input because Compute
Engine and GKE resolve to the same quota constraints; supported consumers are
reported as result metadata. The catalog derives accelerator attachment,
service ownership, management plane, accelerator and non-accelerator companion
requirements, native-unit quantities, and applicable quota pools. A machine
shape whose accelerator attachment cannot be proven remains cataloged but is
not guided through this resolver.

`cloud-tpu-slice` requires an accelerator type, topology, runtime version, slice
count, and provisioning model. The catalog derives the legacy Cloud TPU service
ownership, chip and native-unit quantities, companion requirements, and quota
pools. Compute- or GKE-consumed TPU shapes use `compute-instance`; the operator
does not choose a management plane as a substitute for the deployable shape.

Either shape uses explicit candidate locations or an explicit
all-compatible-locations action. The resolver preserves one independent exact
constraint set, source coverage, usage assessment, and quota-sufficiency result
per location. It does not merge locations, choose one silently, rank them, or
claim capacity. Provider-declared specialized hardware remains visible when it
is cataloged but unguided; resolution stops when selectors, compatibility,
conversion, companion constraints, identity, or required source coverage is
missing or ambiguous.

The selected per-location constraint set opens in the same inspector. A
supported Spot `compute-instance` shape can pass its complete configuration and
explicit candidates to the separate obtainability operation without implying
that quota and advice are the same result.

## Surface equivalence

The CLI and TUI share operation inputs, validation, plans, warnings,
acknowledgements, lifecycle vocabulary, and audit records. Surface equivalence
does not require identical navigation:

- the TUI keeps resource scope, constraint context, and lifecycle state visible across
  interactive transitions;
- the CLI requires sufficient explicit input for each standalone operation and
  returns the same evidence in stable human-readable and structured forms;
- canonical full command names identify every durable result, Watch event, and
  audit record even when invocation used an exact explicit alias; and
- automation never depends on terminal rendering or an interactive prompt.

Aliases match only the exact sibling tokens defined by the information
architecture. Neither surface uses fuzzy command matching, prefix expansion,
correction prompts, or interactive disambiguation.

Exact CLI command names, TUI screen boundaries, keyboard behavior, pagination,
filter syntax, and cross-surface plan handoff are defined in [CLI and TUI
information architecture](cli-tui-information-architecture.md). Stable
operation boundaries, status axes, human and structured results, exit classes,
diagnostics, and Watch behavior are defined in the [status, output, and watch
contract](status-output-watch-contracts.md).

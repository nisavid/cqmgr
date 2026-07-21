# Operator workflow contract

Cloud Quotas presents one domain workflow through an interactive TUI,
scriptable CLI commands, and structured automation. The surfaces may arrange
controls differently, but they operate on the same targets, effective quota
slices, accelerator constraint sets, quota preferences, mutation plans, and
reconciliation observations.

This contract defines operator intent and transitions. It does not choose CLI
command spelling, output schemas, keyboard bindings, component layout, or an
implementation stack.

## Shared domain operations

Every surface exposes these operations independently:

1. establish or change a resource-container context;
2. browse effective quota slices and accelerator constraint sets;
3. inspect one exact effective quota slice and its related evidence;
4. resolve an optional accelerator workload requirement to its constraint set;
5. assess supported Spot capacity advice for an exact VM configuration;
6. compose an absolute desired value for one exact mutable slice;
7. preview and review a portable quota mutation plan;
8. apply one reviewed plan deliberately;
9. observe preference reconciliation and effective quota confirmation; and
10. inspect and verify local audit evidence.

The TUI invokes these operations interactively. The CLI invokes each operation
directly and supports no-color human-readable and structured noninteractive
results. A plan created on either surface can be reviewed or applied on the
other when its identity, principal, freshness, and integrity requirements still
hold.

## TUI default: quota inspector

The TUI opens on the quota inspector rather than a workload questionnaire or
an operation launcher. It may restore a recent target or use an explicitly
configured target. It does not require the operator to select the same target
again at every launch.

The active canonical project, folder, or organization remains prominent beside
every quota view and detail surface. Ambient `gcloud` or Application Default
Credentials settings never silently replace it. Switching the target is a
deliberate inspector action. Applying a mutation requires the operator to
confirm the exact target scope again; a noninteractive apply supplies the same
explicit target acknowledgement as input.

The inspector groups recognized GPU and TPU slices into accelerator constraint
sets. Related regional, global, and zonal slices and separate quota pools stay
independent rows with their exact provider identity, native unit, effective
value, usage source, desired and granted values, eligibility, timestamps, and
lifecycle state visible. The grouping explains which slices can constrain the
same workload; it never synthesizes one combined quota or implies physical
capacity.

Every discovered slice remains browseable. Unknown or non-accelerator slices
appear through a generic provider-truth view rather than disappearing or being
misclassified as unsupported.

Recognized accelerator constraint sets offer Spot capacity advice when the
catalog can map an exact Spot VM configuration to a provider-supported advice
request. Advice is attached to the machine configuration, quantity,
distribution shape, and candidate region or zones rather than to a quota row
alone.

## Inspect and compose

Selecting a slice opens a detail pane that keeps these facts together:

- canonical resource container, service, quota ID, dimensions, scope, and unit;
- effective value and source timestamp;
- usage and its separate source timestamp when available;
- adjustment eligibility and provider rollout state;
- existing preference identity, desired value, granted value, etag, and
  reconciliation state;
- related accelerator constraints and remaining bottlenecks;
- acting principal and impersonation chain; and
- valid next operations.

A mutable exact slice offers a preference composer in this pane. The operator
enters an absolute desired value, not an increment. Creating a new preference
and amending a settled or reconciling preference use the same flow. An amendment
also shows the prior desired value, whether a pending request will be
superseded, and both desired-versus-effective and replacement-versus-existing
directions.

Broader inherited preferences remain visible and read-only. The detail pane may
offer a more-specific exact-slice preference after explaining the inherited
value and precedence change. An identical settled desired value is a verified
no-op and offers no apply operation. Unsupported, ambiguous, stale, or
observe-only slices explain why composition cannot continue.

## Review and apply

Preview leaves the detail pane for a dedicated plan review. The review keeps
the selected constraint set and active target visible while presenting the
bound exact slice, current and desired values, existing preference state,
principal, warnings, acknowledgements, quota-contact source, evidence ages,
plan expiry, and expected consequences.

The review makes clear that apply changes only the selected slice. Companion
constraints may warn but are never changed implicitly. Dangerous decreases,
unlimited-value transitions, missing evidence, drift, ongoing rollouts, and
expert acknowledgements follow the safety and mutation contract; the workflow
does not weaken those gates for convenience.

Apply requires an explicit confirmation of the canonical target scope. It then
revalidates and consumes the single-use plan using the same principal. A stale
or drifted plan returns to reviewable evidence instead of silently rebuilding
or applying a different intent.

## After apply and reconciliation

After a provider accepts a preference, the TUI returns to the quota inspector
with the affected slice selected. The row and detail pane show submitted,
reconciling, preference-settled or granted, failed, superseded, unknown, and
effective-confirmed as distinct states. Acceptance never appears as an
effective quota change.

The inspector updates lifecycle observations inline and offers a focused Watch
operation for longer-running reconciliation. Only a fresh effective-quota
observation matching the settled granted preference becomes
effective-confirmed. Preference reconciliation remains separate from VM,
queued-resource, reservation, or physical-capacity state.

Timeouts and transport failures enter an explicit reconciliation result. The
workflow reads the deterministic preference identity and classifies the result
as accepted, unchanged, conflicting, or unknown; it never offers a blind retry.

## Spot capacity advice

Spot capacity advice is a read-only first-release workflow for supported
Compute Engine machine configurations. The provider contract, coverage limits,
and evidence semantics are recorded in [Spot capacity-advice
contracts](research/spot-capacity-advice-contracts.md).

From an accelerator constraint set or a resolved workload requirement, the
operator supplies or confirms:

- the Spot provisioning model;
- an exact machine type and any required attached GPU type and count;
- the number of VMs;
- a target distribution shape; and
- one region with optional candidate zones.

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

## Optional workload requirement resolver

The requirement resolver is a secondary entry point from the inspector. It is
guidance for an operator who knows the accelerator workload but not the owning
quota slices.

For GPUs, it resolves accelerator family, quantity, machine shape,
provisioning mode, and location to the live regional, global, and quota-pool
constraints. For TPUs, it asks for the management plane first, then generation,
shape or topology, consumption mode, and zone before resolving the owning
service, native unit, and exact live slices.

The resolver produces a quota requirement and opens its constraint set in the
same inspector. It stops when compatibility, unit conversion, provider
identity, or eligibility is ambiguous. It says whether quota permits the
request and never claims that capacity is available. When a supported Spot
machine configuration is fully resolved, it can pass that configuration to the
separate capacity-advice operation without implying that quota and advice are
the same result.

## Surface equivalence

The CLI and TUI share operation inputs, validation, plans, warnings,
acknowledgements, lifecycle vocabulary, and audit records. Surface equivalence
does not require identical navigation:

- the TUI keeps target, constraint context, and lifecycle state visible across
  interactive transitions;
- the CLI requires sufficient explicit input for each standalone operation and
  returns the same evidence in stable human-readable and structured forms; and
- automation never depends on terminal rendering or an interactive prompt.

Exact CLI command names, TUI screen boundaries, keyboard behavior, pagination,
filter syntax, structured-output schemas, and exit-status rules belong to the
architecture and interface specification that follows this workflow decision.

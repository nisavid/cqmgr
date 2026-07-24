# Cloud Quota Manager V1 product requirements

Status: accepted by the operator through the Wayfinder handoff gate on
2026-07-21 and revised by explicit operator decisions on 2026-07-23.

Cloud Quota Manager turns authoritative Google Cloud quota evidence into exact,
reviewable operator actions without confusing quota with physical capacity.
This document is the implementation handoff for V1. It consolidates the
accepted product boundary, names the normative detailed contracts, and fixes
an implementation sequence. It does not authorize a live quota mutation,
capacity provisioning, or a package release.

## Authority and interpretation

The following documents are normative parts of this handoff:

- [`CONTEXT.md`](../CONTEXT.md) defines canonical domain language;
- [`PRODUCT.md`](../PRODUCT.md) defines users, purpose, positioning, and design
  principles;
- [`DESIGN.md`](../DESIGN.md) defines the terminal product's visual,
  accessibility, and material-design constraints;
- [`operator-workflows.md`](operator-workflows.md) defines operator intent and
  cross-surface transitions;
- [`cli-tui-information-architecture.md`](cli-tui-information-architecture.md)
  defines the command tree, TUI routes, query semantics, and plan handoff;
- [`status-output-watch-contracts.md`](status-output-watch-contracts.md)
  defines operation boundaries, structured results, diagnostics, exit classes,
  and Watch behavior;
- [`runtime-integration-architecture.md`](runtime-integration-architecture.md)
  and the accepted ADRs define the implementation shape and dependency
  direction; and
- [`verification-distribution-contract.md`](verification-distribution-contract.md)
  defines supported platforms, verification, installation, live-read-only
  canaries, and release gates.

[ADR 0004](adr/0004-use-a-versioned-federated-provider-inventory.md) owns the
federated inventory, catalog coverage, and workload-resolution decisions.
[ADR 0005](adr/0005-use-non-atomic-ordered-request-bundles.md) owns target
strategies, bundle preflight, non-atomic dispatch, and aggregate Watch.

The source-backed notes under [`research/`](research/) are the provider-fact
baseline for Cloud Quotas, Compute, Cloud TPU, Spot advice, and the decision to
omit latency. Adapter work must refresh drift-prone facts against current
official documentation. A provider change is handled within the accepted
product boundary when possible; a change that invalidates a normative
requirement returns to explicit product review.

When a detailed contract is more specific than this handoff, the detailed
contract controls. A later accepted contract controls over an older research
finding or issue comment. Implementation may choose internal names,
algorithms, layouts, and constants only where the normative contracts leave
them open and the choice preserves every externally observable requirement and
safety invariant below.

The accepted handoff and its later explicit revisions supersede conflicting
earlier tracker answers. V1 resource-scoped operations accept projects only;
folder and organization variants remain schema-reserved and return
rejected-precondition without inference or provider access. V1 provider
inventory is exactly `compute.googleapis.com` and `tpu.googleapis.com`. V1
Watch conditions are exactly `granted` and `fulfilled`. A settled child grant
that differs from its target terminates that requested condition as
`requested-outcome-unmet`, including zero when the target is greater than zero.

## Users and outcome

V1 serves two co-primary users:

- a terminal-fluent cloud or platform engineer who inspects and manages
  accelerator quota across Google Cloud projects; and
- an ML or application developer who needs guided accelerator workflows
  without becoming a quota-domain specialist.

A user succeeds when they can establish one explicit project scope, understand
the exact quota slices that constrain an accelerator workload at each selected
location, compare supported Spot evidence without treating it as a guarantee,
derive or enter absolute child targets, preview one ordered request bundle,
apply it deliberately with its non-atomic boundary visible, and observe honest
per-child and aggregate outcomes. When the product cannot complete an operation
safely, it must preserve useful evidence and explain the exact missing, stale,
unsupported, ambiguous, failed, unknown, or unattempted condition.

## V1 scope

### Required capabilities

Every CLI, TUI, and structured-automation surface uses the same typed
application operations to:

1. inspect, select, and clear one canonical project resource scope;
2. inspect and select validated local profiles and interface configuration;
3. browse the versioned Compute and Cloud TPU provider inventory, effective
   quota slices, and accelerator constraint sets;
4. inspect one exact slice with effective value, usage, eligibility, existing
   preference, lifecycle, related constraints, provenance, and completeness;
5. resolve a supported Compute instance or Cloud TPU slice across explicit
   candidate locations or all compatible locations to independent exact
   constraint sets without guessing or ranking locations;
6. compare supported Spot VM obtainability, estimated uptime, historical
   preemption, price, and coverage for one exact configuration;
7. compose absolute child targets with an explicit target strategy and Preview
   an integrity-protected ordered quota request bundle or freshly verified
   child no-ops;
8. review a local or exported plan without applying it;
9. Apply one authenticated, unexpired, unused single or bundle plan after complete
   all-child revalidation and explicit resource-scope acknowledgement;
10. Watch every child in the accepted Watch set to an explicit aggregate `granted` or
    `fulfilled` condition and caller-controlled deadline; and
11. list, inspect, and verify append-only local audit evidence.

The canonical first-release command tree and all explicit stable sibling
aliases are fixed by the information architecture. Aliases are permanently
reserved, never inferred by fuzzy prefix matching, and never appear in durable
output. Bare `cqmgr` is TTY-aware, `cqmgr tui` is explicitly interactive, and
noninteractive invocation never waits for terminal input.

Plan review succeeds when canonical encoding and content digest are trustworthy
enough to present, including for expired, foreign-issued, consumed, or
unacknowledged plans. Those states set `apply_capability` to `false` with exact
reasons. Invalid canonical encoding or a digest mismatch prevents trusted
review and returns a non-success result.

### Provider inventory and guided accelerator coverage

The generic quota core remains service-neutral. V1 inventory is the versioned
federation of `compute.googleapis.com` and `tpu.googleapis.com`. Bare browsing
collects both sources; service and catalog-group filters prune both provider
reads and displayed rows. Results retain coverage for the normalized queried
provider set, identify intentionally unqueried providers, and never claim a
globally complete order when a required queried source is incomplete.

The release-relative accelerator catalog includes every specialized hardware
identity declared by those provider sources at its review point. Catalog
presence and guided support remain independent. Guidance is available only
where maintained first-party evidence binds exact provider identity,
compatibility, quota selectors, unit conversion, provisioning model, quota
pool, and companion constraints. V1 guided coverage includes A4 and supported
Compute and Cloud TPU accelerator mappings backed by that evidence.

Live provider discovery is authoritative. A versioned semantic overlay relates
accelerators, machine shapes, topologies, runtime versions, provisioning
models, native units, locations, lifecycle, restrictions, and companion
constraints. It is not a static hardware allowlist. Every discovered slice and
provider-declared specialized hardware identity remains visible when it is not
guided.

`discovered`, `cataloged`, `guided`, and `mutable` remain independent facts.
Compatibility never implies capacity. Unknown provider values remain visible
and are not coerced into known product categories. Queries and acceptance
fixtures preserve discovered-only, cataloged-but-unguided, guided-but-
currently-immutable, and validated generic-mutable slices.

### Explicitly deferred

V1 does not include:

- folder or organization operations;
- quota preference deletion or reset-to-inherited behavior;
- service-enablement, reservation, machine, workload, or capacity
  provisioning;
- live GKE cluster, node-pool, or workload inventory;
- TPU Spot advice or invented substitutes for unsupported advice;
- latency collection, display, filtering, ranking, or active probing;
- cross-host plan authorization or distributed plan consumption;
- password-manager command adapters or encrypted-file secret stores;
- frozen executables or platform package-manager feeds; or
- a native Textual screen-reader support claim.

## Domain and result contract

An effective quota slice is identified by canonical resource scope, service,
quota ID, normalized dimensions, and applicable quota scope. Friendly names,
accelerator models, machine shapes, and constraint sets are metadata and never
replace that identity. Related regional, global, zonal, and quota-pool slices
may be presented together, but each remains independently observable and
mutable.

A Compute instance requirement supplies machine type, instance count,
provisioning model, and either explicit candidate locations or all compatible
locations. A Cloud TPU slice requirement supplies accelerator type, topology,
runtime version, slice count, provisioning model, and the same explicit
location choice. Resolution derives accelerator identity, quota pool, native
units, and one independent constraint set per compatible requested location. It
does not rank locations or claim capacity. V1 accepts no workload-consumer input
for Compute instances because Compute Engine and GKE resolve to the same quota
constraints; results report supported consumers as metadata.

A quota target is an absolute desired limit for one exact slice. One quota
request bundle contains one or more ordered child requests in one project. Each
child is the operator and provider lifecycle that reconciles its exact slice
toward its absolute target. The Google `QuotaPreference` is provider evidence
rather than product-facing language.

The target strategy is explicit. `minimum` is the default: each deficient
child receives fresh usage plus its normalized workload requirement, while an
already-permitting slice is a no-op. It never auto-decreases quota or silently
supersedes an existing provider intent. An equal or higher desired value is
preserved; a lower conflicting intent requires an explicit manual target or a
new Preview after settlement. `preserve-headroom` requests current effective
quota plus the normalized workload requirement. `manual` requires one explicit
absolute target per selected child and applies the complete sufficiency and
dangerous-decrease gates.

Quota request status uses three orthogonal axes:

- reconciliation: `submitted`, `reconciling`, `settled`, `failed`,
  `superseded`, or `unknown`;
- grant satisfaction: `unknown`, `none`, `partial`, or `full`; and
- effective confirmation: `unobserved`, `stale`, `mismatch`, or `confirmed`.

`granted` requires a settled full grant. `fulfilled` additionally requires a
fresh effective-quota observation equal to both target and grant. Provider
acceptance is not an effective quota change.

An obtainability comparison consists of exact provider-request candidates. A
candidate binds one endpoint region, explicit candidate zones, exact machine
configuration, VM quantity, and distribution shape. Comparable candidates are
ranked lexicographically by provider obtainability band descending, exact
candidate-attributable 30-day p90 preemption rate ascending, exact applicable
total-request hourly price ascending, and canonical candidate identity
ascending as the final tie-breaker. Any missing, unsupported, stale,
incomplete, or non-attributable required component leaves the candidate
unranked with exact reasons. The p90 uses the nearest-rank 27th value after
sorting exactly 30 complete candidate-attributable daily rates. Current price
uses the applicable provider interval containing the retrieval time and covers
the complete machine request before multiplication by VM quantity.

All non-streaming structured operations return
`cqmgr.operation-result/v1`. Structured Watch output is NDJSON using
`cqmgr.watch-event/v1` and ends in exactly one terminal operation result.
Schema versions, operation names, symbolic outcomes, diagnostic codes,
completeness, provenance, and global exit classes are stable contracts. Human
layout is not a parsing contract.

Before the first release, plan and Watch schemas support explicit single and
bundle subjects without changing their V1 identifiers. A
`cqmgr.quota-request-plan/v1` payload has a required top-level `kind` of
`single` or `bundle` beside `schema`; `single` requires exactly one child and
`bundle` contains one or more ordered children. An explicit exact-slice target
uses strategy `manual` and kind `single`; a workload-derived constraint set is
kind `bundle` even when only one non-no-op child remains. A
`cqmgr.watch-event/v1`
record has required `subject.kind` with the same values. Its subject binds
resource scope, condition, plan or intent digest, and an ordered nonempty
`children` array of complete child identities; `single` requires exactly one.
A material child event names `child_id` and carries that child's status.
Aggregate events and results retain ordered child summaries. Unknown kinds
fail closed. A future incompatible change after release requires a new schema
version.

V1 Watch is backed by one durable local cqmgr Apply record and does not adopt
unrelated provider requests. Its subject retains every ordered plan child and
immutable Apply disposition while polling only the accepted Watch set. That set
contains children accepted during Apply plus unknown children with authenticated
accepted resolution evidence. An initial Watch binds the complete subject,
every watched child preference and target, condition, deadline, and
unknown-resolution journal checkpoint. The aggregate condition is reached only
when every watched child reaches it. Every event emits a non-secret, locally
authenticated opaque resume token that binds the subject, watched request
identities, condition, provider lineages, resolution checkpoint, and durable
observation checkpoint. Resume accepts that token plus a new deadline, may
advance through valid later resolution appends, and fails closed before polling
when its installation, Apply record, complete child identities, resolution
chain, durable checkpoint, or applicable etag-or-stable-trace lineage evidence
cannot be verified.

## Mutation safety invariants

These requirements are hard gates rather than interface guidance:

1. **Explicit project and children.** Preview and Apply target one canonical
   project and one or more freshly discovered exact child slices. Ambient
   `gcloud`, ADC, and quota-project state never silently supply the resource
   scope.
2. **Two phases.** Preview produces a plan; Apply consumes it. A bundle whose
   every child is a verified no-op produces no Apply capability. V1 plans
   expire 15 minutes after issuance.
3. **Bound intent.** Canonical plan bytes bind the resource scope, ordered
   children, target strategy and inputs, each slice's effective evidence,
   preference identity and etag, target, principal and impersonation chain,
   quota-contact source binding, warnings, acknowledgements, known constraint
   sets, expiry, schema, and issuing installation.
4. **Local trust and single use.** An allowlisted native OS keyring stores the
   per-installation plan-authentication secret. Apply requires local
   authentication, an unused consumption record, an absent immutable
   native-keyring consumption marker, an exclusive lease, and durable marker
   creation plus the ledger-owned consumption transition before provider
   dispatch. Ambiguous dispatch quarantines the plan; replaying an older
   authentic filesystem record does not restore Apply capability.
5. **Complete preflight.** Preview and Apply validate every child before any
   provider write. Identity, eligibility, effective value, preference, etag,
   rollout, policy, material usage, or companion evidence drift for any child
   invalidates the plan. Apply never silently rebuilds or refreshes a different
   plan.
6. **Identity separation.** Authenticated principal, impersonation chain, ADC
   quota project, resource scope, and quota contact remain distinct. The
   product never elevates, switches, or brokers identity. Preview and Apply
   require an exact stable-principal match.
7. **Contact privacy.** The contact is accepted per operation, by native-keyring
   reference from the selected profile, or from a verified direct-user
   identity. Its value never enters plans, audit records, diagnostics, exports,
   or provider-body retention.
8. **Fail-closed evidence.** Missing, stale, incomplete, unsupported, or
   ambiguous evidence performs zero provider writes. Read-only behavior may
   remain available with typed diagnostics.
9. **Dangerous changes.** Effective-limit decreases require fresh usage.
   Below-usage and greater-than-ten-percent overrides and transitions to or
   from provider unlimited value `-1` require the expert path, explicit named
   acknowledgement before Preview, plan binding, and audit evidence.
10. **Companion independence.** Preview refreshes and shows every selected
    location's complete known constraint set. A companion changes only when it
    is an explicit bundle child; other remaining bottlenecks warn.
11. **Deterministic non-atomic writes.** Existing exact preferences use semantic
    identity and current etag; new preferences use a deterministic identity.
    Preview binds child order using
    `(direct_accelerator_rank, scope_breadth_rank, exact_slice_identity)` and
    fails closed when a child cannot map to one rank pair. Apply uses that order
    unchanged and stops at the first conclusively `failed` child or any
    `unknown` child, preserves preceding `accepted` children, and marks later
    children `unattempted`. Multiple or conflicting matches fail closed.
    Transport or persistence uncertainty is reconciled by reading the child
    identity and is never retried blindly. A proven
    read-after-unknown result is appended once as accepted or failed resolution
    evidence without rewriting the durable `unknown` Apply disposition.
12. **Write-ahead audit.** Preview evidence is appended and fsynced before
    Preview succeeds. After complete revalidation, Apply separately appends and
    fsyncs the complete ordered pre-Apply intent before crossing the consumption
    barrier or making a provider call. Each child dispatch decision and outcome
    and the aggregate terminal result are appended and fsynced. A missing
    durable outcome becomes a critical unknown result with every available child
    reconciliation identity preserved.

The product never exposes a generic provider write port to CLI or TUI code.
Read and mutation ports remain separate even when one generated client backs
both adapters.

## Local data and secret storage

Versioned TOML configuration lives in the platform-native user configuration
directory. Mutable selected-profile and direct-project state is separate and
atomically updated. Profiles may contain a project, ADC quota project, native
keyring reference, and presentation defaults; they never contain credentials,
raw quota-contact values, operation intent, Apply acknowledgement, or plan
authorization.

Mutation-capable secret storage is allowlisted to macOS Keychain, Windows
Credential Locker, and Freedesktop Secret Service. KWallet remains a
secret-store compatibility canary, and Windows arm64 remains a platform
compatibility canary. A missing, locked, null, plaintext, file-backed,
third-party, or unknown backend allows appropriate read-only and local
inspection operations but blocks Preview and Apply.

Plans, config, selection state, and append-only audit records use explicit
storage ports with atomicity, interprocess locking, crash recovery, versioning,
redaction, and fail-closed newer-version handling. Raw provider bodies,
credentials, credential paths, tokens, quota contacts, and sensitive
annotations are never retained.

## Architecture requirements

The implementation uses CPython `>=3.12,<3.15`, one PEP 621
`pyproject.toml`, `uv_build`, a committed `uv.lock`, a `src/` layout, Click,
Textual, Ruff, Pyrefly, and pytest. The published package is `cqmgr`; the
supported user path is `uv tool install cqmgr`, and contributors use
`uv sync --locked`.

Dependencies point inward from CLI and TUI adapters through async application
operations to a framework-free domain. Google clients, credentials,
persistence, serialization, terminal rendering, environment, clocks, and
budgets remain adapters behind typed ports. Generated Google DTOs, pagers,
exceptions, retries, and credentials never cross those ports.

The composition root classifies invocation before initializing Textual, ADC,
providers, or keyring state. Local-only help, profile, configuration, scope,
and audit operations remain offline. Offline scope results defer acting
principal and impersonation evidence until a provider operation initializes
ADC. `cqmgr` never executes `gcloud` or reads its active account or project.

Application operations and provider ports are async-first. Sync-only provider
clients run in bounded workers. One application coordinator owns operation
deadlines, cancellation, bounded inventory fan-out, coalescing, aggregate Watch
schedules, and provider, project, and ADC-quota-project budgets across local
processes. Provider writes do not use generic retry policy.

Official Google Python clients are the default adapters for Resource Manager,
Cloud Quotas, Monitoring, Compute, Cloud TPU, and Preview Spot advice. Direct
REST is permitted only behind the same port when an official client lacks a
required field or method already present in the published provider schema.

## Verification and release acceptance

Implementation is complete only when the named behavior contracts pass at the
domain, application-port, provider-fixture, real-filesystem and subprocess,
CLI, Textual Pilot, cross-surface, artifact, and bounded live-read-only layers.
Provider fixtures come only from public schemas and documented examples; local
stubs test transport mechanics and are never treated as provider truth.

The initial coverage gate is 100 percent branch coverage for plan validation
and consumption, audit integrity, redaction, status and exit classification,
and provider-mutation gates, plus 90 percent across the project. Named safety
scenarios remain mandatory if a reviewed evidence-based change adjusts a
percentage. Tests cover bare two-source federation, filter-pruned provider
reads, queried-source partial coverage, release-relative catalog coverage
including A4 guidance, both workload shapes and location modes,
target-strategy arithmetic, all-child zero-write preflight, deterministic
non-atomic dispatch and child dispositions, and aggregate Watch and resume
behavior. Targeted mutation, property, crash, concurrency, and real-keyring
tests run at the cadence defined by the verification contract.

Supported releases cover CPython 3.12–3.14 on the declared macOS, Ubuntu, and
Windows families. Each release builds one sdist and one `py3-none-any` wheel
once, tests the immutable bytes independently of the checkout, and publishes
the exact artifacts through PyPI Trusted Publishing after protected manual
approval. Attestations, SBOM, checksums, tag, static version, GitHub Release,
PyPI identity, and post-publication exact `uv` installation must agree.

Live gates use a dedicated non-production project and a short-lived
least-privilege identity with no quota-update, service-enablement, or resource
provisioning authority. They perform only the allowlisted bounded reads and the
separate read-only Spot-advice canary. Provisioning this external prerequisite
is not authorized by this handoff.

Before the first release, maintainers must also secure the PyPI project and
trusted publisher, configure the protected publication environment, qualify
native keyrings, record evidence-based performance budgets, and resolve every
release-blocking vulnerability and license finding under the accepted policy.

## Implementation-owned decisions

The following are intentionally evidence-driven implementation choices and do
not reopen the product handoff when they preserve the normative contracts:

- exact Python type, module, and internal port names within the approved
  dependency direction;
- provider page sizes, backoff constants, conservative local budgets,
  coalescing mechanics, and Watch polling cadence;
- exact TUI spacing, material palette values, optional glyphs, and component
  composition within the accepted accessibility and information architecture;
- performance budgets recorded from the first executable baseline;
- promotion or continued canary status for KWallet and Windows arm64; and
- routine dependency versions within declared compatible ranges.

Each choice must be documented where its owning contract requires, verified at
the appropriate layer, and reviewed with the implementation change.

## Implementation sequence

Implementation proceeds through bounded, reviewable tickets. A ticket may
refine low-level design through TDD, but it may not weaken or silently redefine
the handoff.

| Order | Implementation slice | Required outcome | Depends on |
| ---: | --- | --- | --- |
| 0 | Planning closeout | Merge the accepted planning and handoff stack; bind implementation tickets to the accepted handoff commit. | Operator acceptance |
| 1 | Python project and quality baseline | Add `pyproject.toml`, `uv.lock`, package layout, architecture checks, Python CodeQL, Ruff, Pyrefly, pytest, build, and installation smoke without product behavior. | 0 |
| 2 | Domain result and status core | Implement canonical identities, quantities, completeness, diagnostics, operation results, status axes, exit classes, schemas, and property tests. | 1 |
| 3 | Configuration, scope, and bootstrap | Implement versioned config, profiles, selection state, precedence, offline command classification, and local CLI operations. | 2 |
| 4 | Native secrets and plan repository | Implement the allowlisted secret-store port, installation trust, canonical plan encoding, authentication, expiry, leases, consumption, quarantine, crash recovery, and failure injection without provider mutation. | 2 |
| 5 | Audit journal and local coordination | Implement append-only audit continuity, rotation, fsync ordering, interprocess locks, shared budgets, coalescing, cancellation, and failure injection without provider mutation. | 2 |
| 6 | ADC identity and project resolution | Implement direct-user, service-account, impersonated, and federated identity evidence plus Resource Manager canonicalization and safe diagnostics. | 3 |
| 7 | Effective-quota and usage adapters | Implement Cloud Quotas reads, quota-preference reads, Monitoring usage, pagination, completeness, normalization, budgets, and public-schema fixtures. | 5, 6 |
| 8 | Provider inventory, accelerator catalog, and quota operations | Implement the fixed Compute and Cloud TPU inventory set, release-relative specialized-hardware catalog including A4 guidance, exact location-anchored constraint sets, browsing, inspection, bounded federated queries, and both workload-first resolver shapes. | 7 |
| 9 | Read-only CLI vertical slice | Deliver scope, profile, config, federated quota, nested workload resolution, and audit read operations with human and structured output, explicit aliases, TTY behavior, partial-source coverage, and installed-package tests. | 3, 5, 8 |
| 10 | Obtainability vertical slice | Implement supported Spot advice and history adapters, comparison and transparent ranking, coverage handling, and equivalent CLI behavior. | 8, 9 |
| 11 | Compose, Preview, and plan review | Implement `minimum`, `preserve-headroom`, and `manual` target strategies, dangerous-change gates, child no-op behavior, all-child preflight, ordered single or bundle plan issuance/export/review, cross-surface handoff, and zero-write safety proofs. | 4, 5, 7, 8, 9 |
| 12 | Non-atomic Apply and deterministic reconciliation | Implement accelerator-first child preference create/amend, complete fresh revalidation, acknowledgements, durable ordered dispatch, stop after conclusive failure or transport uncertainty, `accepted`/`failed`/`unknown`/`unattempted` outcomes, read-after-unknown reconciliation, audit outcomes, and at-most-one-write-per-child proofs. | 11 |
| 13 | Watch and lifecycle observation | Implement adaptive accepted-Watch-set polling, material child and aggregate event streams, single and bundle Apply-record intent binding, authenticated resume tokens, deadlines, interruption, and single or aggregate `granted` and `fulfilled` conditions. | 12 |
| 14 | Textual shell and quota inspector | Deliver the adaptive shell, scope instrument, Quotas workspace, filters, exact-slice detail, Audit workspace, keyboard behavior, and CLI fallback over read-only operations. | 9 |
| 15 | Textual obtainability workspace | Deliver exact-configuration composition, candidate coverage, transparent ranking, unranked evidence, and Copy CLI behavior. | 10, 14 |
| 16 | Textual mutation and lifecycle routes | Deliver single and bundle compose, Plan Review, non-atomic Apply, and Watch routes with locked scope, child acknowledgements and dispositions, return context, and cross-surface equivalence. | 11, 12, 13, 14 |
| 17 | Release and live-canary prerequisites | Secure the PyPI project and trusted publisher, configure the protected publication environment, and arrange the dedicated least-privilege read-only canary identity under separate operator authorization. | 0 |
| 18 | Release qualification | Complete the supported matrix, deep tests, live-read-only gates, performance budgets, immutable artifact pipeline, provenance, and post-publication verification. | 15, 16, 17 |

Tickets 3, 4, and 5 may proceed in parallel after the domain core. Ticket 17
may proceed after planning closeout but requires separate authority for each
external mutation; it grants no quota-update or capacity-provisioning
authority. Later work may be split further when a ticket proves too large, but
any split must preserve the dependency order, one externally coherent
acceptance boundary per pull request, and the complete requirement
traceability of the parent slice.

## Handoff acceptance

The planning gate may close when the operator confirms that:

- this handoff and its normative documents state the complete V1 product,
  domain, interface, architecture, safety, verification, distribution, and
  implementation-sequence contracts;
- the implementation-owned choices above can be resolved without reopening a
  product decision;
- the implementation tickets reproduce this sequence and trace each slice to
  its owning acceptance contracts; and
- implementation remains prohibited until the accepted planning stack is
  published under repository rules.

After acceptance, this document's status becomes accepted, the Wayfinder map
is resolved, and the bounded implementation tickets become the authoritative
execution frontier.

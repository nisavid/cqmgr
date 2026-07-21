# Cloud Quota Manager V1 product requirements

Status: accepted by the operator through the Wayfinder handoff gate on
2026-07-21.

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

The accepted handoff commit explicitly supersedes conflicting earlier tracker
answers. V1 resource-scoped operations accept projects only; folder and
organization variants remain schema-reserved and return rejected-precondition
without inference or provider access. V1 Watch conditions are exactly
`granted` and `fulfilled`; a settled grant that differs from the target
terminates either as requested-outcome-unmet, including zero when the target is
greater than zero.

## Users and outcome

V1 serves two co-primary users:

- a terminal-fluent cloud or platform engineer who inspects and manages
  accelerator quota across Google Cloud projects; and
- an ML or application developer who needs guided accelerator workflows
  without becoming a quota-domain specialist.

A user succeeds when they can establish one explicit project scope, understand
the exact quota slices that constrain an accelerator workload, compare
supported Spot evidence without treating it as a guarantee, preview one
absolute quota target, apply the reviewed request deliberately, and observe an
honest granted or fulfilled outcome. When the product cannot complete an
operation safely, it must preserve useful evidence and explain the exact
missing, stale, unsupported, ambiguous, or failed condition.

## V1 scope

### Required capabilities

Every CLI, TUI, and structured-automation surface uses the same typed
application operations to:

1. inspect, select, and clear one canonical project resource scope;
2. inspect and select validated local profiles and interface configuration;
3. browse effective quota slices and accelerator constraint sets;
4. inspect one exact slice with effective value, usage, eligibility, existing
   preference, lifecycle, related constraints, provenance, and completeness;
5. resolve supported GPU and TPU workload requirements to exact constraint
   sets without guessing;
6. compare supported Spot VM obtainability, estimated uptime, historical
   preemption, price, and coverage for one exact configuration;
7. compose one absolute quota target and Preview an integrity-protected quota
   request plan or a freshly verified no-op;
8. review a local or exported plan without applying it;
9. Apply one authenticated, unexpired, unused plan after fresh revalidation and
   explicit resource-scope acknowledgement;
10. Watch an accepted request to an explicit `granted` or `fulfilled`
    condition and caller-controlled deadline; and
11. list, inspect, and verify append-only local audit evidence.

The canonical first-release command tree and all stable three-letter aliases
are fixed by the information architecture. Bare `cqmgr` is TTY-aware,
`cqmgr tui` is explicitly interactive, and noninteractive invocation never
waits for terminal input.

Plan review succeeds when canonical encoding and content digest are trustworthy
enough to present, including for expired, foreign-issued, consumed, or
unacknowledged plans. Those states set `apply_capability` to `false` with exact
reasons. Invalid canonical encoding or a digest mismatch prevents trusted
review and returns a non-success result.

### Guided accelerator coverage

The generic quota core remains service-neutral. Complete guided workflows cover:

- NVIDIA GPU quotas owned by Compute Engine;
- Compute Engine and GKE TPU consumption represented through Compute-owned
  quota and catalog evidence; and
- legacy Cloud TPU API quotas for generations supported by that management
  plane.

Live provider discovery is authoritative. A versioned semantic overlay relates
accelerators, machine shapes, topologies, provisioning models, native units,
locations, lifecycle, restrictions, and companion constraints. It is not a
static hardware allowlist. Every discovered slice remains visible through a
generic provider-truth view when it is not cataloged or guided.

`discovered`, `cataloged`, `guided`, and `mutable` remain independent facts.
Compatibility never implies capacity. Unknown provider values remain visible
and are not coerced into known product categories. Queries and acceptance
fixtures preserve discovered-only, cataloged-but-unguided, guided-but-
currently-immutable, and validated generic-mutable slices.

### Explicitly deferred

V1 does not include:

- folder or organization operations;
- quota preference deletion or reset-to-inherited behavior;
- bundled multi-slice Apply;
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

A quota target is an absolute desired limit. A quota request is the operator
and provider lifecycle that reconciles one exact slice toward that target. The
Google `QuotaPreference` is provider evidence rather than product-facing
language.

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

V1 Watch accepts only provider preferences backed by a durable local cqmgr Apply
record and plan digest; it does not adopt unrelated provider requests. An
initial Watch binds the preference, intent ID, condition, and deadline. Every
event emits a non-secret, locally authenticated opaque resume token that binds
the exact request, condition, provider lineage, and durable checkpoint. Resume
accepts that token plus a new deadline and fails closed before polling when its
installation, Apply record, complete request identity, durable checkpoint, or
applicable etag-or-stable-trace lineage evidence cannot be verified.

## Mutation safety invariants

These requirements are hard gates rather than interface guidance:

1. **Explicit project and slice.** Preview and Apply target one canonical
   project and one freshly discovered exact slice. Ambient `gcloud`, ADC, and
   quota-project state never silently supply the resource scope.
2. **Two phases.** Preview produces a plan; Apply consumes it. A verified no-op
   produces no Apply capability. V1 plans expire 15 minutes after issuance.
3. **Bound intent.** Canonical plan bytes bind the resource scope, slice,
   effective evidence, preference identity and etag, target, principal and
   impersonation chain, quota-contact source binding, warnings,
   acknowledgements, known constraint set, expiry, schema, and issuing
   installation.
4. **Local trust and single use.** An allowlisted native OS keyring stores the
   per-installation plan-authentication secret. Apply requires local
   authentication, an unused consumption record, an exclusive lease, and
   durable consumption before provider dispatch. Ambiguous dispatch
   quarantines the plan.
5. **Fresh revalidation.** Identity, eligibility, effective value, preference,
   etag, rollout, policy, material usage, or companion evidence drift
   invalidates the plan. Apply never silently rebuilds or refreshes a different
   plan.
6. **Identity separation.** Acting principal, impersonation chain, ADC quota
   project, resource scope, and quota contact remain distinct. The product
   never elevates, switches, or brokers identity. Preview and Apply require an
   exact stable-principal match.
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
10. **Companion independence.** Preview refreshes and shows the complete known
    constraint set. Remaining bottlenecks warn but do not mutate companion
    slices.
11. **Deterministic writes.** Existing exact preferences use semantic identity
    and current etag; new preferences use a deterministic identity. Multiple or
    conflicting matches fail closed. Transport uncertainty is reconciled by
    reading that identity and is never retried blindly.
12. **Write-ahead audit.** Preview evidence and the pre-Apply intent are
    appended and fsynced before success or provider dispatch. Every terminal
    post-dispatch result is appended and fsynced. A missing durable outcome
    becomes a critical unknown result with reconciliation identity preserved.

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
and audit operations remain offline. `cqmgr` never executes `gcloud` or reads
its active account or project.

Application operations and provider ports are async-first. Sync-only provider
clients run in bounded workers. One application coordinator owns operation
deadlines, cancellation, bounded fan-out, coalescing, Watch schedules, and
provider, project, and ADC-quota-project budgets across local processes.
Provider writes do not use generic retry policy.

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
percentage. Targeted mutation, property, crash, concurrency, and real-keyring
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
| 8 | Accelerator catalog and quota operations | Implement Compute/GKE and Cloud TPU catalog overlays, exact constraint sets, browsing, inspection, bounded queries, and workload resolution. | 7 |
| 9 | Read-only CLI vertical slice | Deliver scope, profile, config, quota, and audit read operations with human and structured output, aliases, TTY behavior, and installed-package tests. | 3, 5, 8 |
| 10 | Obtainability vertical slice | Implement supported Spot advice and history adapters, comparison and transparent ranking, coverage handling, and equivalent CLI behavior. | 8, 9 |
| 11 | Compose, Preview, and plan review | Implement target validation, dangerous-change gates, no-op behavior, plan issuance/export/review, cross-surface handoff, and zero-write safety proofs. | 4, 5, 7, 8, 9 |
| 12 | Apply and deterministic reconciliation | Implement exact preference create/amend, fresh revalidation, acknowledgements, durable dispatch, read-after-unknown reconciliation, audit outcomes, and at-most-one-write proofs. | 11 |
| 13 | Watch and lifecycle observation | Implement adaptive polling, material event streams, Apply-record intent binding, authenticated resume tokens, deadlines, interruption, and `granted` and `fulfilled` conditions. | 12 |
| 14 | Textual shell and quota inspector | Deliver the adaptive shell, scope instrument, Quotas workspace, filters, exact-slice detail, Audit workspace, keyboard behavior, and CLI fallback over read-only operations. | 9 |
| 15 | Textual obtainability workspace | Deliver exact-configuration composition, candidate coverage, transparent ranking, unranked evidence, and Copy CLI behavior. | 10, 14 |
| 16 | Textual mutation and lifecycle routes | Deliver compose, Plan Review, Apply, and Watch routes with locked scope, acknowledgements, return context, and cross-surface equivalence. | 11, 12, 13, 14 |
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

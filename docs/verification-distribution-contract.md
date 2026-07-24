# Verification and distribution contract

This contract defines the evidence required to implement, install, and release
Cloud Quota Manager V1. It refines the [runtime and integration
architecture](runtime-integration-architecture.md); it does not open the
implementation gate, provision test infrastructure, publish a package, or
authorize a live quota mutation.

## Support vocabulary

**Supported** means the platform and dependency combination passes every
applicable release gate and receives fixes under the current release-line
policy.

**Compatibility canary** means CI probes installation and behavior without a
support promise or a release block. A canary becomes supported only through a
reviewed contract change after the complete release matrix passes.

**Live-read-only canary** means an allowlisted, bounded provider operation made
by a short-lived identity that has no quota-update or provisioning authority.
HTTP method alone does not classify an operation: Spot capacity advice is a
read-only `POST`, while Cloud Quotas `validateOnly` remains mutation-shaped and
forbidden.

## Supported runtime and platforms

The package supports CPython `>=3.12,<3.15`. CI tests Python 3.12, 3.13, and
3.14; contributor tooling defaults to 3.14; static typing targets the Python
3.12 language floor.

| Platform family | V1 contract | Release evidence |
| --- | --- | --- |
| macOS | macOS 14 or newer on arm64 and x86_64 | Oldest and latest stable GitHub-hosted images, clean uv-tool installation, native Keychain smoke, terminal and package tests. |
| Linux | Ubuntu 22.04 or newer on glibc x86_64 and arm64 | Oldest and latest stable GitHub-hosted images, clean uv-tool installation, Secret Service smoke, terminal and package tests. |
| Windows | Windows 11 or Windows Server 2022 or newer on x86_64 | Oldest and latest stable GitHub-hosted images, clean uv-tool installation, Credential Locker smoke, PowerShell and package tests. |
| Windows arm64 | Compatibility canary | Promote only after wheel-only dependency resolution and the complete platform, terminal, keyring, and artifact matrix pass. |

Preview runner images are canaries until stable. Musl Linux, 32-bit systems,
BSD, and other operating systems are outside the V1 support promise. CI runner
labels are evidence, not the product contract; adding or retiring a hosted
image does not silently change support.

Supported installation must resolve all runtime dependencies from published
wheels. Cloud Quota Manager does not build, fork, or redistribute `grpcio` to
make Windows arm64 pass, and it does not make a local compiler toolchain an
installation prerequisite.

## Native secret-store contract

[ADR 0002](adr/0002-use-native-keyrings-for-local-secrets.md) selects
allowlisted native keyrings only. The supported mutation-capable backends are
macOS Keychain, Windows Credential Locker, and Freedesktop Secret Service.
KWallet is a non-blocking canary until a real integration spike passes.
Classification uses the concrete classes exported by those trusted backend
modules rather than caller-controlled class-name or module-name strings.

The application-facing secret-store port contains only:

- `probe` with backend identity and capabilities;
- `get(reference)`;
- create-once `create(reference, secret)`; and
- `delete(reference)`.

Plan-consumption markers use a separate create/read-only port. Ordinary secret
operations reject marker references, and marker operations require the plan
repository to own the exact shared cqmgr lock instance. No public operation can
delete or replace a marker.

Outcomes distinguish missing, locked or cancelled, unavailable, unsupported,
and failed. References contain a stable cqmgr service namespace, installation
identity, purpose, and collision-resistant bounded random item identity; they
never contain a secret.

The adapter uses no shell, secret-bearing argument, environment value,
clipboard, inherited standard stream, or plaintext temporary file. A local
interprocess lock serializes cqmgr callers because the portable keyring API has
no compare-and-swap contract. Creation performs exactly one write to a new
immutable random reference, followed by read-after-write verification, with no
automatic write retry or update-in-place. Rotation creates and verifies a new
item before atomically switching the non-secret reference. Observable mismatch
fails closed. Out-of-band writes to cqmgr's private namespace are unsupported
tampering rather than participants in the cqmgr concurrency contract.

A missing, locked, null, plaintext, file-backed, third-party, or unknown
backend cannot issue a mutation-capable plan or Apply one. Read-only provider
operations and local configuration or audit inspection remain available with a
typed diagnostic and exact setup guidance.

## Distribution and installation

The canonical distribution is the `cqmgr` project on PyPI. The supported user
installation and upgrade surface is uv tool installation. Contributors use
`uv sync --locked`. pip, pipx, and other standards-compatible installers may
work but are best-effort and are not part of the release matrix.

Each release publishes exactly:

- one source distribution; and
- one `py3-none-any` wheel.

The release build excludes workspace sources and produces the wheel from the
source-distribution content so the source artifact is complete. Both artifacts
are installed and tested outside the checkout. The cqmgr wheel is pure Python;
that does not imply its dependency closure is platform independent.

An artifact-content audit requires the declared package, license, README,
schemas, accelerator-catalog overlay, release evidence manifest, and runtime
resources. It rejects credentials, local configuration, caches,
development-only assets needed accidentally at runtime, private test data,
absolute machine paths, undeclared executables, or a static hardware allowlist
that could silently replace provider-declared inventory.

## Verification layers

### Static, dependency, and package checks

Every applicable change runs:

- locked dependency and environment validation;
- Ruff formatting and linting;
- Pyrefly at the Python 3.12 language floor;
- an import-boundary check enforcing inward dependency direction;
- package metadata, entry-point, license, and artifact-content validation;
- Python CodeQL in addition to the existing Actions analysis;
- dependency vulnerability and license policy checks; and
- SHA-pinned workflow, least-permission, and non-persisted checkout-credential
  checks.

The repository adds uv dependency updates to Dependabot when `uv.lock` exists.
Tool versions are bounded in project metadata and fixed by the committed lock;
upgrades are deliberate compatibility changes.

### Pure domain and property tests

Pure tests cover:

- resource-scope and profile precedence;
- explicit offline identity deferral without ADC or provider canonicalization;
- exact effective-quota-slice identity and dimension normalization;
- the V1 Compute and Cloud TPU provider inventory set, bare federation, and
  service or catalog-group filtering that prunes both reads and rows;
- `compute` and `tpu` input shorthand canonicalized to
  `compute.googleapis.com` and `tpu.googleapis.com` in every durable shape;
- queried-provider coverage and completeness, including intentionally unqueried
  providers, failed pages, Compute warnings, and Cloud TPU location failures;
- independent discovered, cataloged, guided, and mutable predicates and their
  filter combinations;
- release-relative catalog manifests, overlay and normalized-evidence digests,
  page and location exhaustion, lifecycle, unknown provider-declared hardware,
  and fail-closed exhaustive claims;
- discriminated Compute-instance and Cloud-TPU-slice requirements, exact
  normalization, and independent compatible, incompatible, ambiguous,
  and incomplete per-location results;
- derived supported-workload-consumer metadata, with no V1 consumer input and
  no divergence between current Compute and GKE quota constraints;
- `minimum`, `preserve-headroom`, and `manual` target strategies, native-unit
  isolation, sufficiency and decrease gates, and verified all-no-op results;
- units, integer quantities, unknown provider values, and schema enums;
- status axes, headlines, completeness, operation boundaries, and exit classes;
- cursor binding and pagination completeness;
- deterministic obtainability-candidate identity and lexicographic ranking,
  including exact ties and unranked non-attributable evidence;
- single and bundle plan canonicalization, discriminators, deterministic
  accelerator-first child order, digest stability, authentication, expiry,
  tamper rejection, and single-use state;
- non-atomic child dispositions `accepted`, `failed`, `unknown`, and
  `unattempted`, with verified no-op kept outside dispatch disposition;
- aggregate Watch conditions, ordered child summaries, shared intent identity,
  and complete resume-token subject binding;
- redaction and the exclusion of credentials, quota contacts, raw provider
  bodies, and machine paths; and
- supported version acceptance and fail-closed newer-version rejection.

Hypothesis state machines exercise plan availability, lease, complete-bundle
preflight, ordered child dispatch, accepted/failed/unknown/unattempted
transitions, single consumption, unknown-dispatch quarantine and
read-after-unknown reconciliation; audit append, rotation, tampering and crash
recovery; aggregate Watch progression, per-child lineage, shared-intent resume,
interruption and timeout; configuration precedence; partial provider pages and
locations; and concurrent budget acquisition.

### Hermetic catalog and integration validation

The catalog and integration contract suite is a required hermetic gate. It
makes no provider call, requires no cloud identity, and uses only secret-free
fixtures derived from public schemas and documented examples. The fixture
corpus contains independently pageable Compute accelerator-type and
machine-type reads plus Cloud TPU location, accelerator-type, and
runtime-version reads, including unknown hardware, lifecycle transitions,
partial success, duplicate and reordered pages, and location-local failure.

A deterministic generator normalizes those fixtures into the release evidence
manifest and catalog digest. Tests shuffle page and location order, vary
pagination boundaries, and prove identical complete inputs produce identical
canonical evidence while every omitted, failed, unreachable, or unexhausted
required source changes coverage and blocks an exhaustive claim. The checked-in
snapshot contains no live project identifier or response body and is reviewed
as release-relative evidence, never as physical-capacity evidence.

### Application operation contracts

Scripted in-memory ports, virtual clocks, deterministic jitter, and controlled
cancellation cover every operation and exit class. Required mutation-safety
scenarios include:

- offline scope, profile, config, help, and audit operations never import or
  initialize ADC, provider clients, mutation ports, or keyring-backed plan
  capability;
- bare inventory browsing reads both V1 providers, while service and
  catalog-group filters prune actual reads and returned rows and report
  completeness only for the queried provider set;
- workload resolution returns one typed result per candidate location without
  combining alternative locations or collapsing companion constraints;
- target composition proves each strategy formula, native-unit boundary,
  verified no-op, and deterministic accelerator-first child order;
- a verified no-op Preview returns no plan;
- canonical, digest-valid expired, foreign, consumed, or unacknowledged plans
  remain safely reviewable with no Apply capability;
- stale, missing, incomplete, or ambiguous evidence causes zero provider
  writes;
- acknowledgement, resource scope, principal, contact binding, etag, expiry,
  and installation-trust failures stop before dispatch;
- Apply freshly revalidates the complete bundle before the first write and then
  performs at most one provider write per child in the bound order;
- a crash between consumption-marker creation and the ledger consumption
  transition leaves the plan consumed and quarantined rather than reusable after
  lease expiry;
- recovery resumes only a child without a persisted dispatch intent when every
  prior outcome is durably accepted, while a dispatch intent without a terminal
  outcome becomes durable `unknown` and is never automatically re-dispatched;
- a conclusively failed child stops dispatch, preserves prior children as
  accepted, and marks later children unattempted;
- an ambiguous dispatch is `unknown`, consumes and quarantines the plan, stops
  later dispatch, and requires read-after-unknown reconciliation at the
  deterministic preference identity;
- accepted or failed read-after-unknown proof is appended as authenticated
  single-assignment resolution evidence without rewriting the durable
  `unknown` disposition, and conflicting evidence fails closed;
- every dispatched child has one durable pre-intent and one durable terminal
  outcome or critical unknown record; and
- Watch selects both single and bundle Apply records through the shared
  `intent_id`, emits one initial authoritative subject observation followed only
  by material child or aggregate changes, reaches a bundle condition only when
  every child in the accepted Watch set does, and emits exactly one terminal
  operation result.

### Provider adapter contracts

Provider adapters use secret-free, hand-maintained protobuf or JSON fixtures
derived only from public schemas and documented examples. No response body from
a private or test project is committed, even after attempted redaction.

Fixtures cover Resource Manager, Cloud Quotas `QuotaInfo` and
`QuotaPreference` for both `compute.googleapis.com` and `tpu.googleapis.com`,
Monitoring usage, Compute aggregated accelerator types and machine types,
Compute Spot advice, and Cloud TPU locations, accelerator types, and runtime
versions. They exercise pagination, Compute partial success, scope warnings and
unreachable scopes, Cloud TPU per-location success and failure, unknown
hardware, lifecycle and replacement fields, unknown schema fields and enums,
resource names, 64-bit values, etags, throttling, retry classification,
transport failures, schema skew, and normalized diagnostic evidence.

Adapter orchestration tests prove that a bare inventory query calls both
providers, a service or catalog-group filter calls only providers capable of
satisfying it, and returned coverage names exactly those queried providers.
They also prove that successful results cannot manufacture coverage for an
intentionally skipped provider and that every durable service identity uses
the canonical full DNS name.

Local HTTP or gRPC stubs may verify transport mechanics. They are not provider
emulators and cannot establish Google semantics. Generated-client DTOs,
credentials, pagers, exceptions, and retry objects never cross adapter ports.

### Persistence, concurrency, and failure injection

Real temporary filesystems and subprocesses verify:

- atomic configuration and mutable-selection updates;
- canonical single and bundle plan bytes, ordered children, permissions,
  authentication, foreign-plan review, cross-key Apply rejection, leases,
  single-use across processes, crash windows, and stale-lock recovery;
- bundle pre-intent and per-child terminal fsync ordering, accepted, failed,
  unknown, and unattempted retention, unknown-dispatch quarantine, concurrent
  writers, rotation checkpoints, truncation, deletion, reordering, tampering,
  and exact first-failure reporting;
- append-only audit correspondence for bundle order, every child disposition,
  deterministic preference identity, and aggregate result;
- shared interprocess request budgets, cancellation, coalescing, conservative
  token accounting, and poll deadlines; and
- fake keyring behavior plus real supported-backend smoke tests.

Every injected storage, audit, lock, and keyring failure that occurs before
dispatch proves zero provider writes. Every post-dispatch failure proves a
durable terminal outcome or critical unknown and quarantine record.

### CLI, TUI, and cross-surface contracts

Click runner and installed-package subprocess tests cover canonical commands
and aliases, TTY dispatch, stdout and stderr separation, JSON and NDJSON schema
validity, exit classes, no-color output, secret redaction, and offline commands
that must not initialize ADC, providers, or the keyring.

Interface tests cover bare two-provider browsing; service and catalog-group
read pruning; accepted `compute` and `tpu` shorthand; canonical full-domain
service identities in JSON, cursors, audit, plans, and Copy CLI; typed workload
shapes and independent per-location results; all target strategies; single and
bundle plan discriminators; ordered Apply dispositions; and shared
`--intent-id` Watch selection with subject-kind discrimination. Unknown plan,
Watch-subject, and child schema kinds fail closed.

Textual Pilot is authoritative for keyboard navigation, focus and return
preservation, locked resource scope, worker cancellation and failure, semantic
labels, no-color meaning, Copy CLI placeholders, and the absence of synthesized
Apply acknowledgement. A focused reviewed snapshot set covers wide, medium,
narrow, no-color, provider-error, and confirmation states.

CLI and TUI receive identical typed inputs and scripted ports. Their operation
results, plans, diagnostics, Watch events, and audit facts must be semantically
equal. A plan created by either surface must review and Apply through the other
under the same installation trust boundary.

## Coverage and mutation testing

The initial gate is:

- 100 percent branch coverage for plan validation and consumption, audit-chain
  integrity, redaction, status and exit classification, provider-mutation
  gates, complete-bundle preflight, child dispatch ordering and dispositions,
  unknown-dispatch quarantine, aggregate Watch conditions, and resume lineage;
  and
- 90 percent branch coverage across the project.

Nightly and release workflows run targeted mutation tests against the critical
core. A threshold may change when evidence shows it is rewarding incidental
coupling or low-value tests, but the change must be explicit and reviewed.
Named safety scenarios and externally visible contracts remain mandatory even
when a percentage changes.

## Terminal and accessibility matrix

The support promise is behavioral rather than tied only to emulator brands. An
interactive TUI requires a UTF-8 terminal with keyboard input. Meaning cannot
depend on color or a special glyph. Representative qualification covers:

- macOS Terminal with zsh;
- Windows Terminal with PowerShell;
- an xterm-compatible Linux terminal with bash;
- an SSH pseudo-terminal;
- redirected input and output;
- `NO_COLOR` and low-color operation; and
- non-interactive CI.

Unsupported, incapable, or non-interactive TUI environments route to the
equivalent CLI without pretending that terminal rendering succeeded. V1 does
not claim native Textual screen-reader integration; structured CLI output is
the dependable screen-reader and automation surface.

## CI cadence and dependency compatibility

| Cadence | Required evidence |
| --- | --- |
| Every pull request | Locked sync and build; Ruff; Pyrefly; all Python 3.12–3.14 core contracts on Linux; representative smoke on every stable supported OS; CLI, Pilot, persistence, package, vulnerability, and license checks appropriate to the diff. No cloud identity is available to forked or otherwise untrusted pull requests. |
| Scheduled | Full stable OS/architecture and Python matrix; compatibility canaries; real keyring backends; high-iteration property, mutation, crash, subprocess, and concurrency tests; fresh dependency resolution; bounded live-read-only provider canaries. |
| Release commit | Every supported platform at its oldest and latest stable image; every Python minor; real supported keyrings; source distribution and wheel installation; exact dependency and artifact policy; all deep tests; hermetic release-relative exhaustive catalog evidence; live-read-only canaries; provenance and publication preflight. |
| Post-publication | Exact-version PyPI uv-tool installation and offline smoke on the supported matrix; published hashes, attestations, GitHub Release assets, tag, version, and commit agreement. |

Dependency compatibility gates three resolutions:

1. the committed highest-compatible development lock;
2. the lowest declared direct dependency versions; and
3. a fresh unlocked resolution canary.

Supported release installation disables dependency source builds. Weekly uv
dependency updates begin after the lock exists. A new Python minor joins the
matrix only after dependencies, keyrings, platforms, artifacts, and the full
contract pass. A supported minor leaves through an explicit compatibility
change no earlier than its upstream end of life.

## Live-read-only verification

Live verification uses one explicitly named non-production project and a
short-lived federated identity with a custom least-privilege role. Provisioning
that project, role, and identity is an external prerequisite and is not
authorized by this contract.

The ordinary canary may perform only bounded:

- Resource Manager project lookup;
- `QuotaInfo` get and list for the explicit
  `compute.googleapis.com` and `tpu.googleapis.com` services;
- `QuotaPreference` get and list for the explicit
  `compute.googleapis.com` and `tpu.googleapis.com` services;
- Monitoring time-series list;
- Compute aggregated accelerator-type and machine-type lists; and
- TPU location, accelerator-type, and runtime-version lists.

A separate canary may call the documented read-only Spot capacity advice
operation on a known-supported configuration. Its Preview lifecycle and `POST`
transport are explicit. Both canaries use exact resource and quota projects,
services, regions, page limits, retry limits, and wall-clock deadlines.

The identity deliberately lacks quota-update, service-enablement, and resource
provisioning permissions. The ordinary canary composition contains no create,
update, patch, delete, quota-request Preview, Apply, or `validateOnly` port. The
separate Spot canary exposes only the documented read-only capacity-advice
operation despite its provider Preview lifecycle. Request-path tests assert that
QuotaInfo and QuotaPreference reads name only the two V1 service DNS names. A
permission audit proves all
other mutation-shaped capabilities absent. Adapter contract tests assert the
exact RPC or HTTP method and path template for every allowlisted operation,
including `POST .../advice/capacity` and `POST .../advice/capacityHistory`.
Sanitized live traces retain and verify only those method names and path
templates; any request outside the allowlist fails the canary. The workflow
never enables an API, changes quota, creates capacity, or relies on ambient
`gcloud` state.

Scheduled canaries and the exact release commit's canaries are release gates.
The release canary exhausts every page and every discovered supported Cloud TPU
location required by its declared two-provider evidence set; any warning,
unreachable scope, failed subsource, or unexhausted page is explicit coverage
and prevents an exhaustive live claim. Live evidence retains only normalized
source, coverage, lifecycle, shape, timing, method/path identity, safe digests,
and pass/fail facts; no raw body, credential, principal email, quota contact,
or private identifier is retained. Even complete canary evidence describes
catalog visibility for the named project and observation time, never physical
capacity or universal regional availability.

Direct-user ADC receives hermetic `authorized_user` discovery, refresh,
identity-scope, quota-project, failure, and redaction contracts on every
platform. A bounded real direct-user smoke is advisory rather than a release
gate, and its evidence remains equally redacted. No user refresh credential is
stored in CI.

## Performance qualification

Implementation records cold start, resident and peak memory, first TUI render,
and steady provider-refresh behavior on representative supported platforms.
The first release cannot publish until reviewed budgets are recorded from that
baseline. Later releases block material regressions against those budgets.
This contract does not invent absolute limits before executable evidence
exists.

## Release identity and publication

Early releases use PEP 440 and Semantic Versioning `0.x`. The authoritative
version is static project metadata committed on the release commit. An
annotated `vX.Y.Z` tag must identify that exact protected-main commit, and the
tag, project metadata, wheel metadata, source distribution, PyPI project, and
GitHub Release must agree.

The tag workflow builds the artifacts once, runs every release gate against
those immutable bytes, and prepares publication. A protected `pypi`
environment requires maintainer approval before the irreversible upload.
Publication uses PyPI Trusted Publishing with short-lived OIDC credentials and
no long-lived upload token.

The release produces:

- PyPI PEP 740 attestations;
- GitHub artifact attestations;
- an SBOM for the resolved release environment;
- SHA-256 checksums; and
- an immutable GitHub Release containing the exact tested source distribution
  and wheel.

Post-publication verification compares every hash and identity and performs a
clean exact-version `uv tool install` from PyPI. TestPyPI is an optional
preflight for initial trusted-publisher setup and material release-workflow
changes, not a gate for every production release.

## Vulnerability, license, and compatibility policy

Malware, known-exploited, critical, and high-severity findings block changes
and releases. Medium and low findings remain visible without an automatic
release failure. Any accepted unresolved finding requires an explicit owner,
justification, affected scope, compensating controls, and expiry.

Runtime dependencies must use an approved permissive or weak-copyleft license.
Strong copyleft, network copyleft, source-available or noncommercial terms,
unlicensed code, and unknown licenses block until explicit review. Development
and workflow dependencies are classified separately but never escape the SBOM
and license report.

Before 1.0, only the latest release line receives fixes. Config and mutable
state have forward migrations. Plans, operation results, Watch events, audit
records, and configuration remain independently versioned and reject
unsupported newer versions without guessing. Published artifacts are never
overwritten.

A broken or vulnerable release is yanked and superseded by a higher version.
The GitHub Release is marked, and a security case receives an advisory. The
application never performs an automatic state downgrade, and deleting or
replacing published files is not a recovery strategy.

## Implementation and release prerequisites

Implementation may proceed only after the product and architecture handoff is
approved. Before the first release, the implementation plan must also:

- secure the `cqmgr` PyPI project and configure its exact trusted publisher;
- configure the protected GitHub publication environment;
- arrange the dedicated live-read-only project, custom role, and short-lived
  identity without granting quota mutation or provisioning authority;
- generate and review the hermetic release-relative exhaustive provider
  inventory manifest, overlay digest, normalized evidence digest, and every
  declared coverage limitation;
- qualify every supported native keyring and decide whether the KWallet canary
  may be promoted;
- keep Windows arm64 a canary until upstream wheels and the full matrix pass;
  and
- record reviewed performance budgets from executable evidence.

Frozen executables, Homebrew or other package-manager feeds, password-manager
command adapters, encrypted-file secret stores, native Textual screen-reader
claims, and support outside the declared matrix remain deferred.

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

The application-facing secret-store port contains only:

- `probe` with backend identity and capabilities;
- `get(reference)`;
- create-once `create(reference, secret)`; and
- `delete(reference)`.

Outcomes distinguish missing, locked or cancelled, unavailable, unsupported,
and failed. References contain a stable cqmgr service namespace, installation
identity, purpose, and opaque item identity; they never contain a secret.

The adapter uses no shell, secret-bearing argument, environment value,
clipboard, inherited standard stream, or plaintext temporary file. A local
interprocess lock serializes cqmgr callers because the portable keyring API has
no compare-and-swap contract. Creation uses read-before-write and
read-after-write verification. Another client changing the same native item is
reported as a conflict rather than hidden with last-write-wins semantics.

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
schemas, and runtime resources and rejects credentials, local configuration,
caches, development-only assets needed accidentally at runtime, private test
data, absolute machine paths, or undeclared executables.

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
- exact effective-quota-slice identity and dimension normalization;
- units, integer quantities, unknown provider values, and schema enums;
- status axes, headlines, completeness, operation boundaries, and exit classes;
- cursor binding and pagination completeness;
- plan canonicalization, digest stability, authentication, expiry, tamper
  rejection, and single-use state;
- redaction and the exclusion of credentials, quota contacts, raw provider
  bodies, and machine paths; and
- supported version acceptance and fail-closed newer-version rejection.

Hypothesis state machines exercise plan availability, lease, dispatch,
consumption and quarantine; audit append, rotation, tampering and crash
recovery; Watch progression, resume, interruption and timeout; configuration
precedence; partial provider pages; and concurrent budget acquisition.

### Application operation contracts

Scripted in-memory ports, virtual clocks, deterministic jitter, and controlled
cancellation cover every operation and exit class. Required mutation-safety
scenarios include:

- a verified no-op Preview returns no plan;
- stale, missing, incomplete, or ambiguous evidence causes zero provider
  writes;
- acknowledgement, resource scope, principal, contact binding, etag, expiry,
  and installation-trust failures stop before dispatch;
- a dispatched Apply has one durable pre-intent, at most one provider call, and
  one durable terminal outcome or critical unknown record;
- ambiguous dispatch consumes and quarantines the plan before reconciliation;
  and
- Watch emits only material changes and exactly one terminal operation result.

### Provider adapter contracts

Provider adapters use secret-free, hand-maintained protobuf or JSON fixtures
derived only from public schemas and documented examples. No response body from
a private or test project is committed, even after attempted redaction.

Fixtures cover Resource Manager, Cloud Quotas `QuotaInfo` and
`QuotaPreference`, Monitoring usage, Compute machine types and Spot advice,
and TPU locations, accelerators, and runtime versions. They exercise
pagination, partial locations, unknown fields and enums, resource names,
64-bit values, etags, throttling, retry classification, transport failures,
schema skew, and normalized diagnostic evidence.

Local HTTP or gRPC stubs may verify transport mechanics. They are not provider
emulators and cannot establish Google semantics. Generated-client DTOs,
credentials, pagers, exceptions, and retry objects never cross adapter ports.

### Persistence, concurrency, and failure injection

Real temporary filesystems and subprocesses verify:

- atomic configuration and mutable-selection updates;
- plan permissions, authentication, foreign-plan review, cross-key Apply
  rejection, leases, single-use across processes, crash windows, and stale-lock
  recovery;
- audit pre-intent and terminal-result fsync ordering, concurrent writers,
  rotation checkpoints, truncation, deletion, reordering, tampering, and exact
  first-failure reporting;
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
  integrity, redaction, status and exit classification, and provider-mutation
  gates; and
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
| Every pull request | Locked sync and build; Ruff; Pyrefly; all Python 3.12–3.14 core contracts on Linux; representative smoke on every stable supported OS; CLI, Pilot, persistence, package, vulnerability, and license checks appropriate to the diff. No cloud identity is available to fork or untrusted pull requests. |
| Scheduled | Full stable OS/architecture and Python matrix; compatibility canaries; real keyring backends; high-iteration property, mutation, crash, subprocess, and concurrency tests; fresh dependency resolution; bounded live-read-only provider canaries. |
| Release commit | Every supported platform at its oldest and latest stable image; every Python minor; real supported keyrings; source distribution and wheel installation; exact dependency and artifact policy; all deep tests; live-read-only canaries; provenance and publication preflight. |
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
- `QuotaInfo` get and list;
- `QuotaPreference` get and list;
- Monitoring time-series list;
- Compute machine-type list; and
- TPU location, accelerator-type, and runtime-version lists.

A separate canary may call the documented read-only Spot capacity advice
operation on a known-supported configuration. Its Preview lifecycle and `POST`
transport are explicit. Both canaries use exact resource and quota projects,
services, regions, page limits, retry limits, and wall-clock deadlines.

The identity deliberately lacks quota-update, service-enablement, and resource
provisioning permissions. The canary composition contains no create, update,
patch, delete, Preview, Apply, or `validateOnly` port. A permission audit proves
those capabilities absent. The workflow never enables an API, changes quota,
creates capacity, or relies on ambient `gcloud` state.

Scheduled canaries and the exact release commit's canaries are release gates.
Live evidence retains only normalized source, coverage, shape, timing, and
pass/fail facts; no raw body, credential, principal email, quota contact, or
private identifier is retained.

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
- qualify every supported native keyring and decide whether the KWallet canary
  may be promoted;
- keep Windows arm64 a canary until upstream wheels and the full matrix pass;
  and
- record reviewed performance budgets from executable evidence.

Frozen executables, Homebrew or other package-manager feeds, password-manager
command adapters, encrypted-file secret stores, native Textual screen-reader
claims, and support outside the declared matrix remain deferred.

# Runtime and integration architecture

Cloud Quota Manager uses one provider-neutral domain and one async application
layer through its CLI, TUI, and structured automation. Frameworks, Google SDKs,
credentials, persistence, and wire formats remain adapters. This contract
selects the implementation shape; it does not open the implementation gate or
authorize a live quota request.

## Approved stack

| Concern | Contract |
| --- | --- |
| Runtime | CPython `>=3.12,<3.15`, tested on 3.12, 3.13, and 3.14. Contributor tooling defaults to 3.14; Pyrefly checks the 3.12 language floor. |
| Project metadata | One PEP 621 `pyproject.toml` with the `uv_build` backend and a `src/` package layout. |
| Environment and lock | uv owns Python acquisition, development environments, dependency locking, builds, and tool installation. `uv.lock` is committed. |
| CLI | Click 8.x with an explicit canonical-alias group. |
| TUI | Textual 8.x in full-screen mode. |
| Formatting and linting | Ruff, configured in `pyproject.toml`. |
| Static typing | Pyrefly, configured in `pyproject.toml` and run through `uv run`. |
| Tests | pytest plus operation, adapter, CLI, and Textual Pilot test layers specified by the verification contract. |

The [verification and distribution
contract](verification-distribution-contract.md) owns the exact platform,
terminal, keyring, CI, packaging, and release gates for this stack.

Direct runtime dependencies use bounded compatible ranges. The development
lock records exact versions. Dependency upgrades are deliberate changes that
run the complete required checks. Published package metadata does not attempt
to reproduce the repository lock as exact transitive pins.

`build-system.requires` bounds `uv_build` to the tested current minor series.
Raising that range is a deliberate packaging change with a fresh build and
installation verification.

The package exposes:

```toml
[project.scripts]
cqmgr = "cqmgr.cli:main"
```

## Package boundaries

The intended source shape is:

```text
src/cqmgr/
  domain/
    scopes.py
    quotas.py
    catalog.py
    requests.py
    plans.py
    status.py
    results.py
    diagnostics.py
  application/
    operations/
    ports/
      providers.py
      storage.py
      identity.py
      budgets.py
      clock.py
  adapters/
    cli/
    tui/
    google/
    persistence/
    serialization/
  bootstrap.py
  cli.py
  tui.py
  __main__.py
```

Dependencies point inward:

```text
CLI / TUI -> application operations -> domain
Google adapters -> application provider ports + domain
Persistence adapters -> application storage ports + domain
Serialization adapters -> domain result and event types
bootstrap -> concrete adapters
```

The domain imports no Click, Textual, Google, filesystem, environment,
credential, protobuf, HTTP, JSON, or TOML implementation. Application
operations accept typed inputs and ports and return the same operation results
or Watch event streams to both surfaces. Google resources, pagers, exceptions,
and credential objects never cross an adapter boundary.

`bootstrap.py` is the composition root. It:

- classifies TTY and non-TTY invocation before importing or initializing
  Textual;
- classifies the command before loading ADC, contacting a provider, or opening
  plan-keyring state, so help and local-only profile, configuration, and audit
  operations remain offline;
- loads validated configuration and mutable selection state;
- resolves explicit profiles and the project resource scope;
- consumes ADC, including externally configured impersonated ADC, and
  initializes provider clients, the shared budget coordinator, storage, locks,
  clocks, and serializers;
- injects concrete adapters into application operations; and
- owns client shutdown, cancellation, and Watch lifetime.

It contains composition policy, not domain behavior.

## Async execution

Application operations and provider ports are async-first. Textual awaits them
directly. Click commands enter the same async application boundary without
calling TUI controllers or rendering terminal widgets.

Adapters prefer a native async Google client when the required surface provides
one. A sync-only client runs in a bounded worker pool behind the same async port.
The application owns operation deadlines, cancellation, fan-out, polling, and
shared budgets; an adapter owns only one bounded provider call, its transport
timeout, and documented transient retry behavior.

Watch is an async stream of material events followed by one terminal operation
result. Cancellation never implies cancellation or reversal of a provider quota
request.

## Google Cloud adapters

The default adapters use official Python clients:

| Provider surface | Adapter boundary |
| --- | --- |
| Effective quota and quota preferences | `google-cloud-quotas`, with read and mutation ports kept separate even when one client implements both. |
| Quota usage | `google-cloud-monitoring`; usage observations remain separate from effective quota. |
| Compute accelerator catalog | `google-cloud-compute` v1 with partial-success evidence retained. |
| Legacy Cloud TPU catalog | `google-cloud-tpu` v2 read methods only. |
| Spot capacity advice | `google-cloud-compute-v1beta` behind an independently disableable read-only port. |
| Project canonicalization | The official Resource Manager client behind a project resolver. |

Cloud Quotas and Spot advice packages are pinned to bounded versions behind
ports because their Python package or provider lifecycle is Beta or Preview.
Pure mapping contract tests protect domain semantics from generated-client
changes.

Direct REST is not a parallel default implementation. It is permitted only when
an official client lacks a required field or method already present in the
published provider schema. A REST adapter uses `google-auth`, implements the
same port and evidence contract, and does not expose REST DTOs to application
code.

`cqmgr` never executes `gcloud`, reads its active account or project, or parses
its output. Setup and recovery diagnostics may instruct an operator to run an
exact `gcloud auth application-default` command outside `cqmgr`.

## Authentication and identity

Application Default Credentials are the runtime credential contract. The
credential adapter loads ADC once for a configured auth context, requests the
provider and direct-user identity scopes required by that credential type,
applies an explicit quota-project override when present, and shares the
resulting credential object with provider adapters.

The following concepts stay separate:

- the **acting principal** whose credential signs provider calls;
- an explicit impersonation target and delegate chain;
- the ADC quota project used for transport billing and API quota; and
- the project resource scope named by the operation.

Neither the ADC-discovered project nor the ADC quota project becomes the
resource scope.

Before Preview, the identity adapter produces a verified auth context using the
credential-type-specific authoritative surface:

- direct user ADC is refreshed and resolved through Google's OpenID UserInfo
  contract, retaining the stable subject and verified email;
- service-account credentials retain their canonical service-account
  principal;
- impersonated credentials retain the explicit source identity when verifiable,
  target principal, and delegate chain; and
- supported federated credentials retain their authoritative subject and
  service-account impersonation context.

The plan binds the stable principal identity and complete impersonation chain.
Apply repeats identity resolution and requires an exact match. A typed email is
never proof of identity.

If credentials authorize reads but do not expose a stable verified principal,
read-only operations continue with a visible `principal-unverified` diagnostic.
Preview and Apply fail closed with an exact recovery action. Local-user recovery
points to setup-time ADC login; workload or federation recovery points to an
exact external service-account impersonation or credential-configuration
action. `cqmgr` does not switch, impersonate, broker a more privileged identity,
or own OAuth consent or refresh-token storage.

The quota contact remains distinct from the acting principal. Per-operation
input may supply it. A profile may hold only an OS-keyring reference, never the
email value. Resolution order is protected per-operation input, the reference
in an explicitly named profile, the reference in the explicitly selected
profile, then a verified direct-user ADC identity. A plan binds the exact source
kind, source identity, and keyed value. Apply re-resolves that same source; a
missing keyring item or unavailable fallback fails closed without continuing to
the next source. Plans and audit evidence retain no quota-contact value.

## V1 capability boundaries

V1 resource-scoped operations accept canonical project resource scopes only.
A folder or organization input returns a typed rejected-precondition result; it
is never mapped to a descendant, selected, credential, or quota project. The
domain retains project, folder, and organization variants so later support does
not require redefining resource scope or changing the result schema.

GKE support is a semantic overlay over Compute-owned quota and accelerator
catalog data. V1 does not read clusters, node pools, workloads, or the Container
API.

Accelerator workflows supply first-class Compute and Cloud TPU service groups.
Generic quota browsing accepts an explicit canonical service DNS name. V1 does
not enumerate enabled services through Service Usage or claim a cross-service
total.

## Configuration and profiles

Operator-owned configuration is versioned TOML in the platform-native user
configuration directory. Mutable selected-profile and direct-project selection
state is stored separately and updated atomically. Interactive selection never
rewrites the operator-owned profile file.

Resource-scope precedence remains:

1. explicit invocation input;
2. an explicitly named profile;
3. the direct local project selection; then
4. the explicitly selected profile.

Profiles contain declarative references and validated settings only:

- a project resource scope;
- an ADC quota project;
- an OS-keyring reference for quota contact; and
- interface defaults that do not encode operation intent.

Profiles never contain access tokens, refresh tokens, service-account keys,
credential JSON, raw quota-contact values, Apply acknowledgements, or plan
authorization.

Selecting a profile may select the ADC quota project used by `cqmgr` transport,
but it does not change the credential's acting principal, create impersonated
credentials, mutate ADC, or alter `gcloud` configuration.

`cqmgr`-specific environment variables are bootstrap-only. They may override
configuration and state paths and honor standard presentation conventions such
as `NO_COLOR`. They never set resource scope, quota contact, profile selection,
Apply acknowledgement, quota target, expert override, or other operation
intent. Google-auth environment variables keep their documented ADC meaning.

## Local plans, audit, and evidence

V1 authenticates quota request plan bytes with a per-installation secret stored
in the OS keyring. The digest addresses canonical bytes; the keyed
authentication proves they were issued by the local installation. CLI and TUI
share the same plan repository. An exported plan may be reviewed elsewhere but
cannot be Applied by another installation; cross-host verification requires a
fresh Preview in v1.

If the selected OS-keyring backend or plan-authentication secret is unavailable,
read-only provider operations and local configuration and audit inspection
remain available, and `plan review` may still present a foreign or
unauthenticated plan with no Apply capability. Preview and Apply fail closed
with a typed diagnostic that distinguishes an unsupported or unavailable
keyring from an operational keyring failure. No file-backed secret fallback is
created silently.

One local consumption ledger enforces single use. Apply acquires an exclusive
lease and records consumption before the provider call. A dispatched plan is
never reusable. An interrupted or ambiguous dispatch quarantines the plan and
requires deterministic provider reconciliation or a new Preview.

Storage ports express these behaviors directly:

- `PlanRepository` stores canonical bytes by digest, verifies authentication,
  leases, consumes, and quarantines plans;
- `AuditJournal` appends and fsyncs pre-Apply intent, appends outcomes, queries
  records, and verifies hash continuity;
- `ConfigRepository` validates versioned snapshots and performs atomic updates;
  and
- `SelectionStateRepository` independently persists selected profile and direct
  project selection.

Every Preview, including a verified no-op, appends and fsyncs its audit record
before returning success. Apply revalidates the plan, evidence, and identity;
appends and fsyncs the bound pre-Apply intent; durably leases and marks the plan
as dispatched; and only then calls the provider. A provider outcome is appended
and fsynced before Apply reports success. Failure to persist a post-dispatch
outcome produces a critical unknown result and leaves the plan quarantined for
reconciliation. Every terminal post-dispatch Apply result is appended and
fsynced before it is returned, not only successful results.

Provider adapters normalize responses immediately. Local persistence retains
only the safe fields required for operation results, plans, completeness,
provenance, and audit evidence, plus content hashes and adapter/schema versions.
Raw provider protobuf or JSON bodies are not retained. Credentials, tokens,
quota-contact values, credential paths, and sensitive annotations are excluded.

## Shared budgets and retries

One injected application coordinator with a local interprocess backend owns:

- total operation deadlines and cancellation;
- bounded provider and catalog fan-out;
- coalescing equivalent inspector reads;
- Watch polling cadence and jitter;
- concurrent Watch limits; and
- provider, project, and ADC quota-project budgets.

The coordinator combines equivalent work and budgets concurrent `cqmgr`
processes within one installation. It does not claim a distributed guarantee
across other hosts or tools. Every adapter still handles provider throttling
within the remaining operation deadline, and the configured local budgets stay
conservative relative to documented provider limits.

Read adapters may use bounded exponential backoff with jitter for documented
transient failures and rate limits within the supplied budget. Provider writes
never use blind generic retries. Deterministic preference identity, etag, and
read-after-unknown reconciliation own ambiguous outcomes.

Adapters consume provider pages internally and return domain observations with
page coverage and explicit failures. A pager never crosses a port. A failed page
or location produces an incomplete observation rather than an empty or complete
collection.

## Accessibility boundary

The Textual TUI is fully keyboard-operable, preserves meaning without color,
and is tested at wide, medium, and narrow terminal sizes. The CLI exposes every
domain operation with equivalent structured results and is the dependable
screen-reader and automation surface.

V1 does not claim native Textual screen-reader integration without validation.
The verification contract must test reading order and supported terminal
behavior and state the limitation directly.

## Deferred capabilities

The verification and distribution contract resolves the operating-system,
architecture, terminal, keyring-backend, CI, PyPI publication, and release
matrices. This architecture still defers:

- folder and organization operations;
- cross-host plan signing, trust registration, and distributed consumption;
- Service Usage cross-service discovery;
- live GKE cluster and node-pool inventory;
- frozen executables and platform package-manager feeds; and
- a native TUI screen-reader support claim.

These items do not block an implementation-ready v1.

# CLI and TUI information architecture

Cloud Quota Manager exposes one set of domain operations through the `cqmgr`
CLI, its interactive TUI, and structured automation. Command spelling and TUI
navigation differ, but operation inputs, resource-scope resolution, evidence,
quota request plans, warnings, status, outcomes, and audit records do not.

## Executable entry points

Bare `cqmgr` opens the quota inspector only when attached to an interactive
TTY. `cqmgr tui` is the explicit interactive entry point. Bare `cqmgr` on a
noninteractive input or output stream returns concise help with exit class `2`
and never waits for terminal input. `cqmgr tui` also requires interactive input
and output; without both it returns a usage result with exit class `2` and does
not initialize terminal rendering or prompt.

Commands use the explicit stable aliases in the table below. An alias is
resolved only among siblings at its exact command-tree level. No input is
accepted by prefix, fuzzy match, correction, or interactive disambiguation.
Aliases are invocation conveniences only: help, generated commands,
documentation, diagnostics, operation results, Watch events, and audit records
always use canonical full names.

The first-release command tree is:

```text
cqmgr
cqmgr tui

cqmgr scope show
cqmgr scope select
cqmgr scope clear

cqmgr profile list
cqmgr profile get
cqmgr profile select

cqmgr config get
cqmgr config set

cqmgr quota list
cqmgr quota inspect
cqmgr quota resolve compute-instance
cqmgr quota resolve cloud-tpu-slice

cqmgr obtainability compare

cqmgr request compose
cqmgr request preview
cqmgr request watch

cqmgr plan review
cqmgr plan apply

cqmgr audit list
cqmgr audit inspect
cqmgr audit verify
```

The V1 alias table is a public compatibility surface:

| Canonical siblings | Exact aliases |
| --- | --- |
| `scope`, `profile`, `config`, `quota`, `obtainability`, `request`, `plan`, `audit` | `sc`, `pf`, `cfg`, `q`, `ob`, `req`, `pl`, `aud` |
| `scope show`, `scope select`, `scope clear` | `sc sh`, `sc se`, `sc cl` |
| `profile list`, `profile get`, `profile select` | `pf l`, `pf g`, `pf s` |
| `config get`, `config set` | `cfg g`, `cfg s` |
| `quota list`, `quota inspect`, `quota resolve` | `q l`, `q i`, `q r` |
| `quota resolve compute-instance`, `quota resolve cloud-tpu-slice` | `q r ci`, `q r ct` |
| `obtainability compare` | `ob c` |
| `request compose`, `request preview`, `request watch` | `req c`, `req p`, `req w` |
| `plan review`, `plan apply` | `pl r`, `pl a` |
| `audit list`, `audit inspect`, `audit verify` | `aud l`, `aud i`, `aud v` |

`tui` has no alias. Reusing an alias under a different parent does not make it
ambiguous. Adding a sibling may not change or capture an existing alias.

The groups own these domain operations:

| Group | Responsibility |
| --- | --- |
| `scope` | Inspect, explicitly select, or clear the local resource-scope selection. |
| `profile` | Inspect or explicitly select a named local profile. |
| `config` | Inspect or change validated local interface settings. It never changes `gcloud` configuration. |
| `quota` | Browse the federated V1 quota inventory, inspect one exact slice and its related evidence, or resolve a deployable workload shape to per-location constraint sets. |
| `obtainability` | Compare one exact supported Spot VM configuration across candidate locations. |
| `request` | Compose and Preview one exact-slice request or one ordered workload bundle, then Watch its accepted child requests. |
| `plan` | Review or Apply a portable single-slice or bundle quota request plan. |
| `audit` | Browse exact local audit records or verify audit continuity. |

`quota resolve` owns workload-requirement resolution; there is no separate
requirement namespace. Its shape leaves are `compute-instance` and
`cloud-tpu-slice`. `obtainability` is the product operation name. Google Spot
capacity advice remains provider evidence inside the result.

## Resource-scope resolution

V1 resource-scoped operations accept canonical project resource scopes only.
Folder and organization names return a typed rejected-precondition result and
never cause a descendant or ambient project to be inferred. The domain and
structured resource-scope shape retain all three variants for future support.

A resource-scoped operation resolves its resource scope in this order:

1. an explicit `--resource-scope` input;
2. the resource scope in an explicitly named `--profile`;
3. the explicit local selection made by `scope select`; then
4. the resource scope in the explicitly selected local profile.

An explicitly named profile that exists but has no resource scope returns a
typed rejected-precondition result. It does not fall through to a direct local
selection or to the explicitly selected profile. An unknown named profile also
returns rejected-precondition. This preserves the caller's explicit profile
intent without classifying a well-formed lookup miss as invalid syntax.

`scope clear` removes only the direct resource-scope selection and reveals the
selected profile's resource scope when one exists. `profile select` never
overrides a direct selection made by `scope select`.

Ambient `gcloud` project state and the Application Default Credentials quota
project never become the resource scope. Every resource-scoped human and
structured result shows the canonical resource name and the resolution source.

`scope`, `profile`, and `config` operations are local and offline. They do not
load or refresh ADC, resolve an acting principal, call Resource Manager or a
quota provider, or claim that a selected project is accessible. Acting
principal and impersonation-chain evidence belongs only to the provider-scoped
operation that observed it. Until such an operation runs, the TUI says
`principal not observed`; it does not turn the selected resource scope into
identity evidence.

Apply has an independent acknowledgement gate. `plan apply` requires the full
canonical resource name, such as `projects/123456789`, through
`--acknowledge-resource-scope`. The TUI presents the bound canonical name beside
an empty confirmation field. Neither surface prefills, abbreviates, or derives
the acknowledgement.

Selecting a profile or resource scope is local Cloud Quota Manager state. It
does not switch credentials, alter impersonation, or write `gcloud`
configuration. Generated equivalent commands always include the resolved
canonical `--resource-scope` and never depend on a selected profile or scope.

## Shared CLI behavior

Human-readable and structured modes use the operation-result and Watch
contracts. Stdout contains only the selected result form. Human progress and
diagnostics use stderr; structured diagnostics stay in the result whenever a
valid result can be formed. Color is optional, and no-color output retains the
same words, ordering, and safety facts.

A TUI-generated equivalent command uses canonical full command names and
includes every non-secret operation input. It never inserts a quota-contact
placeholder as an argument value. When protected per-operation contact input is
required, the copied command includes `--quota-contact-stdin` and the TUI renders
a separate, non-copyable instruction to provide exactly one line on stdin;
the option reads exactly one UTF-8 line, strips one trailing LF and optional CR,
and rejects empty, invalid, multiline, NUL-containing, or trailing input.
Missing or invalid input fails closed. Credentials and other secret material
are excluded. The command never supplies an Apply acknowledgement on the
operator's behalf.

The TUI names this affordance **Copy CLI**. It is available on a fully specified
operation input, an operation result, and Plan Review whenever a safe equivalent
command can be formed. It is unavailable for an incomplete draft. An Apply
command retains a visibly incomplete acknowledgement placeholder rather than a
value copied from the bound plan or active resource scope.

### Public option vocabulary

V1 option names are public compatibility surface. Options have no abbreviated
forms; the explicit alias table applies only to commands and subcommands. Every
operational option follows the leaf command it configures, including shared
options; root or intermediate-group placement is rejected as usage input.
Informational `--help` is valid on the root, every group, and every leaf command;
`--version` is valid on the root only. The following shared options are
canonical:

| Purpose | Canonical option |
| --- | --- |
| Explicit project or named profile | `--resource-scope RESOURCE_SCOPE`; `--profile NAME` |
| Result presentation | `--output human` or `--output json`; one-shot commands default to `human` regardless of TTY state. `request watch` accepts only `--output human` or `--output jsonl`, defaulting to `human` on a TTY and `jsonl` otherwise. |
| Terminal presentation | `--no-color`; `--quiet` suppresses human progress and non-result prose, including suppressible warnings, but never result facts, acknowledgements, structured diagnostics, pre-result failures, or the exit class. |
| Bounded continuation | `--limit COUNT`; `--cursor CURSOR` |
| Help and version | `--help` on any command or group; root-only `--version` |

Quota queries use `--text TEXT` and repeatable `--service SERVICE`,
`--catalog-group GROUP`, `--accelerator ACCELERATOR`, `--location LOCATION`,
`--quota-scope SCOPE`, `--quota-pool POOL`, `--cataloged true|false`,
`--guided true|false`, `--mutable true|false`, `--reconciliation STATE`,
`--grant-satisfaction STATE`, and `--effective-confirmation STATE`. Neither a
service nor a catalog group is required: an unfiltered list reads the complete
versioned V1 source inventory. `--service` accepts `compute`, `tpu`, or the
corresponding canonical service DNS name; durable output always uses
`compute.googleapis.com` or `tpu.googleapis.com`. Service and catalog-group
filters infer and prune the provider source set as well as filtering displayed
results. Repeatable `--sort FIELD[:asc|desc]` defines sort priority. Public sort
fields are `quota-id`, `display-name`, `service`,
`accelerator`, `location`, `quota-scope`, `quota-pool`, `effective`, `usage`,
`desired`, `granted`, `reconciliation`, `grant-satisfaction`,
`effective-confirmation`, and `evidence-age`; an inapplicable field is rejected
instead of ignored. Repetition within one facet is OR; distinct facets are AND,
as defined by the quota query contract.

Commands that identify one exact quota slice use `--service SERVICE`,
`--quota-id QUOTA_ID`, `--location LOCATION`, and repeatable
`--dimension KEY=VALUE`.

`quota resolve compute-instance` requires `--machine-type MACHINE_TYPE`,
`--instance-count COUNT`, and `--provisioning-model MODEL`. V1 has no
`--workload-consumer` input because Compute Engine and GKE resolve to the same
quota constraints; supported consumers appear as result metadata. Accelerator
attachment and native-unit quantities are derived from the authoritative
machine-shape catalog, not supplied as competing selectors.
`quota resolve cloud-tpu-slice` requires
`--accelerator-type ACCELERATOR_TYPE`, `--topology TOPOLOGY`,
`--runtime-version RUNTIME_VERSION`, `--slice-count COUNT`, and
`--provisioning-model MODEL`. For either shape, exactly one location mode is
required: repeatable `--candidate LOCATION` or
`--all-compatible-locations`. Candidate mode preserves one independent result
per explicit region or zone. All-compatible mode enumerates every location
proven compatible by the covered provider catalog; it is not a rank, capacity
search, or permission to infer unsupported locations.

The resolver derives service ownership, management plane, native units, quota
pools, companion requirements, and normalized deployable-resource quantities.
Callers do not supply those derived facts as competing flags. A missing,
ambiguous, or contradictory provider mapping stops resolution instead of
guessing.

`obtainability compare` uses `--machine-type MACHINE_TYPE`, optional
`--gpu-type GPU_TYPE` plus `--gpu-count COUNT`, `--vm-count COUNT`,
`--distribution-shape SHAPE`, and either repeatable
`--candidate REGION[=ZONE[,ZONE...]]` or `--all-compatible-locations`. The two
candidate forms are mutually exclusive. Each `--candidate` value defines one
complete endpoint-region and explicit-zone component of an immutable candidate;
no zone is inferred from a different candidate.

`request compose` and `request preview` operate on either one selected exact
slice or one resolved per-location constraint set. The exact-slice form uses
`--service`, `--quota-id`, `--location`, repeatable `--dimension`, and one
absolute `--target VALUE`; it uses target strategy `manual` and produces plan
kind `single` when the target is not a verified no-op. The workload form repeats
the complete applicable shape and location inputs from its `quota resolve` leaf
and adds
`--target-strategy minimum|preserve-headroom|manual`. `minimum` is the default.
It produces plan kind `bundle` whenever any child requires mutation, including
when exactly one non-no-op child remains.
Supplying both shape vocabularies or an incomplete shape is usage input and is
rejected before provider access.
It proposes, for every deficient child, fresh observed usage plus the normalized
workload requirement in that slice's native unit. A child that already permits
the workload is a verified no-op, never an automatic decrease.
`preserve-headroom` proposes current effective quota plus the normalized
workload requirement. `manual` requires repeatable
`--target CHILD_ID=VALUE`, with one absolute target for every selected child.
`minimum` never silently amends an existing provider intent: an equal or higher
settled or reconciling desired state is preserved, while a lower conflicting
intent requires an explicit manual target or a new Preview after settlement.
Preview records the strategy, source observations, normalized requirement,
formula, proposed target, and no-op or mutation classification for every child.

Both request commands accept repeatable `--acknowledge CODE` and optional
`--quota-contact-stdin`; no contact value is accepted in argv. This protected
per-operation input precedes any reference from an explicitly named or selected
profile and the verified direct-user identity, using the normative
contact-resolution order. Stable acknowledgement codes are
`decrease-below-usage`, `decrease-over-ten-percent`, and
`unlimited-transition`. A rejected-precondition result lists every required
code, and an unknown code is rejected as usage input. `--quota-contact-stdin`
reads exactly one UTF-8 line, removes one trailing LF and optional preceding CR,
and rejects an empty value, NUL, embedded line break, invalid UTF-8, or
remaining bytes. `request preview` alone accepts `--plan-out PATH`.

An initial `request watch` accepts one `--intent-id INTENT_ID`,
`--condition granted|fulfilled`, and an absolute RFC 3339
`--deadline TIMESTAMP`. The durable local intent and plan discriminators
identify a single or bundle subject; the public CLI does not duplicate that
fact in another flag. A resumed Watch uses `--resume TOKEN` instead of the
initial identity and condition options and requires a new absolute `--deadline`;
shared presentation options remain available. `plan review` and `plan apply`
accept exactly one of
`--plan DIGEST` or `--plan-file PATH`; Apply additionally requires
`--acknowledge-resource-scope RESOURCE_SCOPE`.

Named local objects and audit records use positional identifiers: `profile get
NAME`, `profile select NAME`, `config get KEY`, `config set KEY VALUE`, and
`audit inspect RECORD_ID`. `audit list` uses repeatable `--operation OPERATION`
and `--outcome OUTCOME`, plus `--since TIMESTAMP`, `--until TIMESTAMP`,
`--limit`, and `--cursor`. `audit verify` uses optional `--from RECORD_ID` and
`--through RECORD_ID`; omitting both verifies the complete retained chain.

## Quota query contract

`quota list` and the quota inspector operate on a bounded logical query. The
query identifies one resource scope and begins from the versioned V1 provider
inventory spanning `compute.googleapis.com` and `tpu.googleapis.com`. Bare
`quota list` queries both. Repeatable service and catalog-group filters infer
the required provider subset and prune both provider reads and displayed
results; there is no separate source-selector option. A catalog-group query
contacts only the providers required by that group. Cloud Quota Manager scans
the required provider pages and applies remaining product filters locally; it
never describes those local filters as provider-side filtering.

Continuation uses an opaque product cursor bound to the resource scope,
inventory-set revision, inferred provider subset, catalog digest, normalized
filter set, sort, evidence contract, queried-source coverage, observation
times, completeness, and canonical ordered snapshot slice identities. A result
exposes coverage, scanned pages, continuation state, observation times, and
failures independently for every queried source; it makes no coverage claim
about sources pruned by the filters.
Usable exact slices from one source remain visible when another source is
incomplete, but the aggregate result is incomplete and does not claim a
complete global total or ordering.

Supplying a cursor without query options resumes its bound query. Supplying a
cursor with query options is valid only when every supplied option matches the
cursor's bound resource scope, inventory revision, filters, sort, catalog
digest, and evidence contract. A mismatch returns rejected-precondition with
exit class `3` before provider access; it never substitutes either query.

The first-release shared facets and canonical values are:

| Facet | Type and normalization |
| --- | --- |
| Text | A non-empty Unicode string normalized to NFC and matched case-insensitively against quota ID, provider display name, and normalized dimension keys and values. |
| Service | Input accepts `compute`, `tpu`, or the lowercase canonical DNS name. Durable output uses `compute.googleapis.com` or `tpu.googleapis.com`. |
| Catalog group | A stable provider-neutral group identifier. It infers the providers required by the group and filters their matching inventory items. |
| Accelerator | A stable accelerator-catalog identifier, not a display label. |
| Location | A lowercase canonical Google Cloud region, zone, or `global`. |
| Quota scope | One of `global`, `regional`, or `zonal`. |
| Quota pool | A stable lowercase catalog identifier such as `standard`, `preemptible`, `committed`, or `virtual-workstation`. |
| Cataloged | Boolean `true` or `false`; provider presence never implies recognized product semantics. |
| Guided | Boolean `true` or `false`; a guided workflow may be currently immutable. |
| Mutability | Boolean `true` or `false`, derived only after fresh exact-slice validation. |
| Reconciliation | One of `submitted`, `reconciling`, `settled`, `failed`, `superseded`, or `unknown`. |
| Grant satisfaction | One of `unknown`, `none`, `partial`, or `full`. |
| Effective confirmation | One of `unobserved`, `stale`, `mismatch`, or `confirmed`. |

Repeated values within one facet are OR alternatives. Different facets combine
with AND. TUI filter controls and CLI flags use the same typed values and
combination rules. Invalid or noncanonical CLI values fail as usage input rather
than being guessed or normalized across semantic categories. Unrecognized
provider values remain visible as provider evidence and are never coerced into
a known semantic category; product status axes use their explicit `unknown`
values when authoritative classification is unavailable. The first release has
no general boolean expression language.

Provider discovery, specialized-hardware catalog classification,
guided-workflow coverage, and current mutability are independent facts.
Provider discovery is presence and provenance, not an exclusive maturity enum.
Every specialized-hardware product authoritatively classified by covered GCP
catalog evidence is cataloged, even when cqmgr cannot guide it. Guidance
requires proven selectors, conversions, compatibility, companion constraints,
and request eligibility; it is never a static allowlist that hides a new
provider product. The model and fixtures preserve discovered-only,
cataloged-but-unguided, guided-but-currently-immutable, and validated
generic-mutable slices.

## TUI shell

The TUI has three sibling workspaces:

1. **Quotas** — the default quota inspector;
2. **Obtainability** — exact Spot VM location comparison; and
3. **Audit** — local record inspection and continuity verification.

A persistent instrument bar shows the active canonical resource scope, its
resolution source, provider identity evidence when a provider-scoped operation
has observed it, and evidence freshness or completeness. Before that
observation it shows `principal not observed`; opening the TUI or changing local
scope never initializes ADC merely to populate the bar. These are authoritative
words and values; color and optional glyphs only reinforce them.

Requirement resolution, request composition, Plan Review, Apply, and Watch are
focused routes within their owning workspace. They replace the workspace body
while retaining the instrument bar, breadcrumb, and return context. The
command palette, help, and small selectors may use overlays; consequential
flows do not.

Plan Review, Apply, and Watch lock the bound resource scope until the route is
left. Changing resource scope while a requirement or request draft is unsaved
requires explicit discard confirmation.

## Quota inspector

The quota inspector is an adaptive workbench:

- wide terminals show a scope/filter rail, quota ledger, and selected-slice
  detail pane;
- medium terminals collapse the rail behind an explicit selector and retain
  ledger plus detail; and
- narrow terminals use one-pane routes with breadcrumbs and Back while
  preserving the resource scope, query, filters, selection, and return focus.

The default ledger is federated across the complete V1 inventory. The rail
filters by service, catalog group, and other query facets. Service and catalog
group filters visibly prune the queried providers and displayed results; there
is no independent source selector. The ledger preserves coverage for every
queried source alongside exact slice identity, quota scope, effective and usage
values, desired and granted values, independent cataloged, guided, and mutable
facts, eligibility, evidence age, and completeness.

Quota request status appears as one adaptive cell with labeled reconciliation,
grant-satisfaction, and effective-confirmation axes. A narrow ledger may show a
derived headline only when it also exposes an explicit path to the full axes.
Exact-slice detail always shows all three axes and their values.

Selecting a slice opens its full identity, source evidence, preference
provenance, related constraint set, acting principal, and valid next
operations. Single-slice request composition begins there. Workload-first
composition begins with `quota resolve compute-instance` or
`quota resolve cloud-tpu-slice`, presents separate constraint sets for every
explicit or all-compatible candidate location, and returns the selected
location's exact ordered children to the same inspector. It never selects a
location merely because its quota is sufficient.

## Obtainability workspace

An obtainability comparison fixes one exact supported Spot VM configuration
while varying exact obtainability candidates. Each candidate is one immutable
provider-request snapshot: endpoint region, explicit candidate zones, machine
configuration, VM quantity, and distribution shape. One comparison may contain
multiple regional candidates, but a provider's regional score belongs to its
complete candidate and is never duplicated onto a zone row. The configuration must be cataloged,
Spot-eligible, and owned by the Compute Engine management plane. Unsupported
TPU, non-Spot, or uncataloged configurations remain visible with an exact
coverage reason and cannot start a provider advice query. A comparison can start
from:

- one selected location from a resolved `compute-instance` workload, with the
  complete shape and candidate inherited visibly and still requiring operator
  confirmation; or
- the standalone workspace or `obtainability compare`, with the complete
  configuration supplied explicitly.

A contextual entry starts with the resolver's explicit candidates only; an
all-compatible resolver result makes that expansion visible before comparison.
A standalone entry requires explicit candidate locations. A prominent
**Compare all compatible locations** action may expand the query to every
catalog-compatible, provider-supported location; no flow broadens the candidate
set silently.

Comparable candidates use the transparent obtainability rank: provider
obtainability band descending, exact 30-day p90 preemption rate ascending, then
the exact current total-request hourly price ascending. The provider's
documented high, medium, and low bands define the first component. The p90 is
derived only when the provider returns one candidate-attributable rate for each
of the previous 30 provider-defined daily buckets: sort the rates ascending and
select the nearest-rank value at `ceil(0.90 * 30)`, the 27th value. Current
total-request price is the provider hourly-price interval containing the
candidate retrieval time, multiplied by the requested VM quantity, only when
that price represents the complete machine request. Canonical candidate
identity is the final ascending tie-breaker.

Every ranked component and derivation remains visible. A candidate whose
history or price cannot be attributed to the complete request, including an N1
attached-GPU request without supported history, appears in a separate unranked
section with per-component reasons. Unsupported, unavailable, stale, or
incomplete required evidence is likewise unranked and is never coerced to a
worst value.

The first release does not collect, import, display, filter, or rank latency
evidence. It does not probe regional endpoints or provision measurement
targets.

## Quota request plan handoff

When Preview produces an integrity-protected quota request plan, it writes the
plan to a local, content-addressed plan store and returns its digest handle in
the operation result. An optional `--plan-out` writes the same portable plan
atomically to an explicit path. Plan bytes never share stdout with the operation
result.

Every plan payload has `kind: single|bundle` and an ordered nonempty `children`
collection of non-no-op request children. A single plan has exactly one child.
A bundle has one or more independently mutable exact-slice children selected
from one resolved constraint set. Each child binds its stable child ID, exact
slice, absolute target, target strategy and derivation, prior
desired/granted/effective/usage facts, warnings, and acknowledgements. An
unknown kind or an empty or structurally inconsistent child collection is
rejected.

A wholly verified no-op produces no plan, durable handle, export, or Apply
capability; its operation result carries each exact no-op reason and bound
evidence. A bundle containing both no-op and mutation children retains the
no-op children in its Preview result and complete bound constraint evidence so
review and final outcomes account for the complete workload constraint set.
They are not plan children and Apply never dispatches them.

Preview performs no provider mutation and never calls a provider
`validateOnly` mutation path. `plan apply` is the only V1 command that may reach
a quota-preference write port. Describing that production path does not
authorize a live quota mutation; live execution requires separate explicit
authority for the exact resource scope and operation.

In v1, portable means transferable between CLI and TUI and exportable for
review. A per-installation key authenticates the plan. Another installation may
inspect its canonical contents but cannot Apply it; cross-host Apply requires a
fresh Preview.

`plan review` and `plan apply` accept either a local digest handle or an
explicit plan file. Review always validates canonical encoding and the content
digest. When the issuing installation key is available, it also authenticates
the issuer; a foreign plan is displayed explicitly as unauthenticated with no
Apply capability. Apply requires canonical and digest validation, issuer
authentication by the local installation, an unused local consumption record,
and unexpired evidence. Review shows the complete bound resource scope, exact
plan kind, ordered children, targets and derivations, current evidence,
principal, warnings, acknowledgements, expiry, authentication state,
non-atomicity, and Apply capability. Apply never rebuilds, reorders, splits, or
refreshes a different plan silently.

Apply consumes the whole single-use plan before dispatch and processes children
in the reviewed deterministic order: accelerator- or location-specific children
precede broader companion constraints, with canonical exact-slice identity as
the final tie-breaker. Each child has at most one provider dispatch and its own
durable pre-intent and terminal outcome. Apply stops at the first child whose
acceptance cannot be proven, marks every later child `unattempted`, and never
attempts rollback. Each plan-child disposition is exactly `accepted`, `failed`,
`unknown`, or `unattempted`. A transport-unknown dispatch is `unknown`, not
`failed`; a failed child preserves its exact unchanged, conflicting, or other
conclusive failure outcome. Verified Preview no-ops remain separate explicit
facts. The aggregate result succeeds only when every plan child is accepted.

After Apply, the TUI returns to the quota inspector with the affected constraint
set and each child outcome visible. Watch is available as a separate focused
route; Apply does not force the operator into it.

## Watch interaction

Watch requires both an explicit condition and an explicit deadline. The only
conditions are:

- `granted`: reconciliation is settled and the granted value equals the quota
  target; and
- `fulfilled`: `granted` is reached and a fresh effective-quota observation
  equals the target and granted values.

An initial `request watch` identifies one durable local single or bundle Apply
record, supplies `--condition granted|fulfilled`, and supplies an absolute
`--deadline TIMESTAMP`. V1 does not adopt unrelated provider preferences. The
Watch event subject has `kind: single|bundle` and every ordered plan child plus
its Apply disposition; a single subject has exactly one child. Watch polls and
evaluates conditions only for the accepted subset. An Apply with no accepted
child is not watchable. Unknown kinds or inconsistent children fail before
polling.

A material child event names its `child_id`. Aggregate events change only when
the bundle-level condition or outcome changes. Every event emits the locally
authenticated opaque resume token defined by the Watch stream contract. A
resumed invocation supplies `--resume TOKEN` instead of the initial identity
and condition options, because the token binds them, and requires a new absolute
`--deadline`. Shared presentation options remain available. Invalid,
foreign-installation, unknown-lineage, structurally inconsistent, or superseded
tokens return rejected-precondition before polling. Omitting `--resume` starts
a new observation stream rather than claiming a resume.

A single or bundle Watch reaches `granted` only when every accepted subject
child has settled at its target. It reaches `fulfilled` only when those same
accepted children also have fresh matching effective quota. Preview no-op
children are not Watch subjects. An accepted child's settled grant that
differs from its target conclusively makes the aggregate requested outcome
unmet; accepted or still-reconciling siblings remain visible. A zero grant
therefore succeeds when that child's target is zero and fails when its target
is greater than zero. Timeout describes the aggregate observation boundary and
never relabels an underlying child request. The one aggregate terminal event
retains ordered summaries for every subject child.
Interactive deadline presets are conveniences only; none is selected silently.

## Keyboard and glyphs

The default keyboard contract is:

- Tab and Shift-Tab move between focus regions and controls;
- arrow keys move within lists, tables, trees, and selectors;
- Enter opens or activates the focused item;
- Escape returns to the prior route or cancels the current transient control;
- `/` focuses the current workspace's filters;
- `?` opens contextual help; and
- Ctrl-K opens the command palette outside text editing; the palette provides
  searchable navigation and actions without
  becoming the primary route model.

Visible, optional mnemonic chords may accelerate workspace and action
navigation. A validated setting may enable Vim navigation. Another setting may
enable Nerd Font glyph enrichment. Neither setting changes semantics or hides
labels, and neither Vim knowledge nor Nerd Font availability is required.

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

Every canonical command and subcommand has an explicit, stable alias made from
its first three ASCII letters. The aliases are reserved even when later
commands are added. For example, `cqmgr quota inspect` may be invoked as
`cqmgr quo ins`. Aliases are invocation conveniences only: help, generated
commands, documentation, diagnostics, operation results, and audit records use
canonical full names.

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
cqmgr quota resolve

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

The groups own these domain operations:

| Group | Responsibility |
| --- | --- |
| `scope` | Inspect, explicitly select, or clear the local resource-scope selection. |
| `profile` | Inspect or explicitly select a named local profile. |
| `config` | Inspect or change validated local interface settings. It never changes `gcloud` configuration. |
| `quota` | Browse exact effective quota slices, inspect one slice and its related evidence, or resolve a workload requirement to a constraint set. |
| `obtainability` | Compare one exact supported Spot VM configuration across candidate locations. |
| `request` | Validate a quota target, Preview its quota request plan, or Watch an accepted quota request. |
| `plan` | Review or Apply a portable quota request plan. |
| `audit` | Browse exact local audit records or verify audit continuity. |

`quota resolve` owns workload-requirement resolution; there is no separate
requirement namespace. `obtainability` is the product operation name. Google
Spot capacity advice remains provider evidence inside the result.

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

## Quota query contract

`quota list` and the quota inspector operate on a bounded logical query. The
query identifies one resource scope plus a service or accelerator catalog
group. Cloud Quota Manager scans the required provider pages and applies
product filters locally; it never describes those filters as provider-side
filtering.

Continuation uses an opaque product cursor bound to the resource scope,
service or catalog group, filter set, sort, and evidence contract. A result
does not claim a global total before the bounded collection is exhausted.
Coverage, scanned provider pages, continuation state, observation times, and
incomplete sources remain visible.

Supplying a cursor without query options resumes its bound query. Supplying a
cursor with query options is valid only when every supplied option matches the
cursor's bound resource scope, service or catalog group, filters, sort, and
evidence contract. A mismatch returns rejected-precondition with exit class `3`
before provider access; it never substitutes either query.

The first-release shared facets and canonical values are:

| Facet | Type and normalization |
| --- | --- |
| Text | A non-empty Unicode string normalized to NFC and matched case-insensitively against quota ID, provider display name, and normalized dimension keys and values. |
| Service | A lowercase canonical service DNS name such as `compute.googleapis.com`. |
| Accelerator | A stable accelerator-catalog identifier, not a display label. |
| Location | A lowercase canonical Google Cloud region, zone, or `global`. |
| Quota scope | One of `global`, `regional`, or `zonal`. |
| Quota pool | A stable lowercase catalog identifier such as `standard`, `preemptible`, `committed`, or `virtual-workstation`. |
| Catalog state | One of `discovered`, `cataloged`, or `guided`. |
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

## TUI shell

The TUI has three sibling workspaces:

1. **Quotas** — the default quota inspector;
2. **Obtainability** — exact Spot VM location comparison; and
3. **Audit** — local record inspection and continuity verification.

A persistent instrument bar shows the active canonical resource scope, its
resolution source, acting principal and impersonation chain, and evidence
freshness or completeness. These are authoritative words and values; color and
optional glyphs only reinforce them.

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

- wide terminals show a scope/service rail, quota ledger, and selected-slice
  detail pane;
- medium terminals collapse the rail behind an explicit selector and retain
  ledger plus detail; and
- narrow terminals use one-pane routes with breadcrumbs and Back while
  preserving the resource scope, query, filters, selection, and return focus.

The rail selects a service or accelerator catalog group; it does not hide
discovered generic slices. The ledger preserves exact slice identity, quota
scope, effective and usage values, desired and granted values, catalog state,
eligibility, evidence age, and completeness.

Quota request status appears as one adaptive cell with labeled reconciliation,
grant-satisfaction, and effective-confirmation axes. A narrow ledger may show a
derived headline only when it also exposes an explicit path to the full axes.
Exact-slice detail always shows all three axes and their values.

Selecting a slice opens its full identity, source evidence, preference
provenance, related constraint set, acting principal, and valid next
operations. Request composition begins there. `quota resolve` is also available
as a secondary route and returns its resolved constraint set to the same
inspector.

## Obtainability workspace

An obtainability comparison fixes one exact supported Spot VM configuration
while varying candidate locations. The configuration must be cataloged,
Spot-eligible, and owned by the Compute Engine management plane. Unsupported
TPU, non-Spot, or uncataloged configurations remain visible with an exact
coverage reason and cannot start a provider advice query. A comparison can start
from:

- a resolved workload or compatible accelerator constraint set, with inherited
  fields visible and still requiring operator confirmation; or
- the standalone workspace or `obtainability compare`, with the complete
  configuration supplied explicitly.

A contextual entry starts with its inherited or currently filtered location
only. A standalone entry requires explicit candidate locations. A prominent
**Compare all compatible locations** action may expand the query to every
catalog-compatible, provider-supported location; no flow broadens the candidate
set silently.

Comparable candidates use the transparent obtainability rank: provider
obtainability band descending, product-defined 30-day p90 preemption band
ascending, then current total-request price quartile ascending. Candidates
with unsupported, unavailable, incomplete, or otherwise noncomparable required
evidence appear in a separate unranked section with per-component reasons.
Missing evidence is never coerced to a worst value.

The first release does not collect, import, display, filter, or rank latency
evidence. It does not probe regional endpoints or provision measurement
targets.

## Quota request plan handoff

When Preview produces an integrity-protected quota request plan, it writes the
plan to a local, content-addressed plan store and returns its digest handle in
the operation result. An optional `--plan-out` writes the same portable plan
atomically to an explicit path. Plan bytes never share stdout with the operation
result. A freshly verified no-op produces no plan, durable handle, export, or
Apply capability; its operation result carries the exact no-op reason and bound
evidence.

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
slice, quota target, current evidence, principal, warnings, acknowledgements,
expiry, authentication state, and Apply capability. Apply never rebuilds or
refreshes a different plan silently.

After provider acceptance, the TUI returns to the quota inspector with the
affected slice selected. Watch is available as a separate focused route; Apply
does not force the operator into it.

## Watch interaction

Watch requires both an explicit condition and an explicit deadline. The only
conditions are:

- `granted`: reconciliation is settled and the granted value equals the quota
  target; and
- `fulfilled`: `granted` is reached and a fresh effective-quota observation
  equals the target and granted values.

An initial `request watch` supplies `--preference PREFERENCE`,
`--intent-id INTENT_ID`, `--condition granted|fulfilled`, and an absolute
`--deadline TIMESTAMP`. V1 accepts only an intent ID backed by a durable local
cqmgr Apply record; it does not adopt an unrelated provider preference as a
watchable intent. Every event emits the locally authenticated opaque resume token
defined by the Watch stream contract.

A resumed invocation supplies `--resume TOKEN` and a new absolute `--deadline`
only. `--resume` is mutually exclusive with `--preference`, `--intent-id`, and
`--condition` because the token binds them. Invalid, foreign-installation,
unknown-lineage, or superseded tokens return rejected-precondition before
polling. Omitting `--resume` starts a new observation stream rather than claiming
a resume.

A settled partial or zero grant conclusively fails either requested condition.
The underlying request still remains visibly request-settled with its exact
grant. Interactive deadline presets are conveniences only; none is selected
silently.

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

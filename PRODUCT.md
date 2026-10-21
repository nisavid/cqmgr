# Product

<!-- impeccable:product-schema 1 -->

## Platform

web

## Users

Cloud Quota Manager serves two co-primary users: a terminal-fluent cloud or
platform engineer who inspects and manages accelerator quota across Google
Cloud resource scopes, and an ML or application developer who needs guided
workflows to diagnose and request the exact constraints for a workload without
becoming a quota-domain specialist.

The product's first-release surfaces are CLI and TUI. The platform marker
selects Impeccable's non-mobile product rulebook; it does not promise a browser
interface.

## Product Purpose

Cloud Quota Manager turns authoritative provider state into a safe operator
workflow: understand the effective constraints on an accelerator workload, compare
supported Spot capacity advice without treating it as a guarantee, preview an
exact quota target or workload-derived quota request bundle with its evidence
and consequences, apply the resulting quota request plan deliberately, and
follow reconciliation to an honest outcome.

Success means an operator can move from a quota or accelerator question to an
effective-confirmed change, or to a precise explanation of what remains
pending or constrained, without confusing quota with physical capacity.

## Positioning

Cloud Quota Manager turns live provider truth into an exact, evidenced quota
action without confusing quota with capacity.

## Operating Context

The first-release surfaces are a Click CLI and Textual TUI running in the
operator's terminal. Quotas, Obtainability, and Audit are sibling interactive
workspaces. Composition, Review, Apply, and Watch retain their selected
resource-scope and evidence context; Copy CLI exposes equivalent explicit
commands for repeatable terminal workflows.

Local resource-scope selection, profiles, and configuration are distinct from
provider authentication. A saved project selection may support read operations
and Preview; Apply still requires exact resource-scope acknowledgement.
Provider operations expose the acting identity and preserve their evidence,
observation times, coverage, and diagnostic outcomes. They never silently
select a project from ambient `gcloud` state.

## Capabilities and Constraints

- **V1 scope:** resource-scoped operations accept canonical Google Cloud
  projects. Folder and organization variants remain reserved and are rejected
  before provider access. The fixed provider inventory federates Compute and
  Cloud TPU; bare browsing queries both, while supported filters narrow the
  selected sources. Incomplete observations remain visible and cannot satisfy
  a mutation gate.
- **Workload-first resolution:** Compute instance and Cloud TPU slice
  requirements resolve to independently evidenced constraint sets for each
  compatible location. Discovered, cataloged, guided, and mutable are distinct
  capabilities; recognition does not imply eligibility for a quota request.
- **Plan before mutation:** Preview validates every selected child before
  issuing an authenticated, expiring, single-use quota request plan. The
  `minimum`, `preserve-headroom`, and `manual` strategies derive absolute
  targets in native units. Verified no-ops make no provider write and do not
  issue an all-no-op Apply plan.
- **Non-atomic Apply:** freshly validated children dispatch in a deterministic
  order. Accepted work remains accepted when a later child fails; failed or
  unknown dispatch stops later children, leaving them unattempted. There is
  no automatic rollback or blind retry of uncertain writes.
- **Honest reconciliation:** Watch has explicit `granted` and `fulfilled`
  conditions and preserves per-child evidence. Provider acceptance, settlement,
  grant satisfaction, and effective confirmation are separate facts. Unknown
  dispatch resolution adds authenticated evidence without rewriting the
  original Apply disposition.
- **Spot capacity advice:** Obtainability compares one fixed exact Spot VM
  configuration across immutable candidates. Complete evidence can be ranked
  transparently by provider obtainability band, exact 30-day p90 preemption
  rate, then current total-request hourly price. Missing or unsupported
  evidence remains unranked. Advice is Preview guidance, not quota or a
  capacity guarantee; V1 does not offer TPU obtainability advice or latency
  comparisons.
- **Local trust:** Preview and Apply require a qualifying in-process native
  keyring and valid installation trust. Missing, locked, or unsupported secret
  storage leaves read-only operations available and blocks mutation workflows
  with setup guidance. Audit and local plan evidence support later inspection.

Detailed contracts remain authoritative in `docs/product-requirements.md`,
`docs/operator-workflows.md`, and `docs/adr/`. This record does not establish a
browser product, a release qualification, or authorization to mutate cloud
resources.

## Brand Commitments

Calm, exact, and accountable. The product should make consequential quota work
legible and controlled without becoming clinical, decorative, or hesitant.

The product name is Cloud Quota Manager; `cqmgr` is its executable, package
stem, and repository tool name. Preserve the canonical terms in `CONTEXT.md`.
The Mineral Instrument visual direction and its confirmed anti-references
remain recorded in `DESIGN.md`.

## Evidence on Hand

- Accepted product and interaction contracts: `docs/product-requirements.md`,
  `docs/operator-workflows.md`, and
  `docs/cli-tui-information-architecture.md`.
- Accepted implementation decisions: `docs/adr/`, including native keyrings,
  federated provider inventory, and ordered non-atomic request bundles.
- Implemented terminal surfaces: `src/cqmgr/adapters/cli/` and
  `src/cqmgr/adapters/tui/app.py`; semantic TUI captures in
  `tests/snapshots/tui/` describe state and layout contracts, not pixel goldens.
- Hermetic provider fixtures in `tests/fixtures/google/` and cross-surface
  contracts in `tests/test_lifecycle_cross_surface.py` and
  `tests/adapters/tui/test_app.py`.
- Support, accessibility, live qualification, and release criteria:
  `docs/verification-distribution-contract.md`. Fixtures and passing local
  checks alone do not prove current live-provider behavior or publication.

## Product Principles

- **Provider truth before guidance.** Present authoritative identity and state
  first, then add accelerator-specific explanation without replacing the
  source facts.
- **Advice is evidence, not a promise.** Present Spot obtainability, estimated
  uptime, and preemption history with their exact configuration, source,
  freshness, Preview status, and coverage limits. Never relabel them as quota
  or guaranteed capacity.
- **Exact target, deliberate mutation.** Keep each selected effective quota
  slice, related constraint set, acting identity, and consequences visible.
  Separate Preview from Apply, renew review when material evidence changes,
  and never let convenience erase a safety gate.
- **State stays honest.** Keep desired, granted, reconciling, effective,
  failed, superseded, and unknown outcomes distinct.
- **Equivalent operator surfaces.** CLI, TUI, and automation expose the same
  domain operations, evidence, warnings, and outcomes.

## Accessibility & Inclusion

Every operation is keyboard-accessible. State and severity use authoritative
words and symbols rather than color alone, and structured no-color output
remains useful. Dense tables preserve a meaningful reading order and adapt to
narrow and low-color terminals. The structured CLI is the dependable
screen-reader and automation surface; V1 does not claim native Textual
screen-reader integration. Motion must be brief, state-driven, and removable.
Text and state colors target WCAG 2.2 AA contrast where the rendering surface
permits; renderer and terminal limitations remain explicit in the verification
contract.

---
status: accepted
---

# Use non-atomic ordered quota request bundles

Cloud Quota Manager V1 composes one quota request bundle from one or more exact
mutable slices in one canonical project. Each child retains its complete slice
identity, native unit, absolute target, effective and usage evidence, preference
identity and etag, warnings, acknowledgements, and known companion constraints.
The bundle does not synthesize a combined quota value.

The operator selects one target strategy for the bundle:

- `minimum` is the default and derives each deficient child's target as fresh
  usage plus the normalized workload requirement;
- `preserve-headroom` derives each target as the current effective value plus
  the normalized workload requirement; and
- `manual` requires an explicit absolute target for every child.

Every derivation uses exact native-unit arithmetic. Under `minimum`, a slice
that already permits the workload is a no-op; the strategy never auto-decreases
quota or silently supersedes an existing provider intent. An equal or higher
desired value is preserved; a lower conflicting intent requires an explicit
manual target or a new Preview after settlement. `manual` remains subject to
complete sufficiency and dangerous-decrease gates. It accepts one target for
every selected constraint child before no-op classification; a target equal to
the settled desired value is retained as a verified no-op and never becomes a
mutation child.
Missing, stale, ambiguous, or incompatible usage, workload, conversion, or
slice evidence prevents Preview. A target equal to the existing settled desired
value is a verified child no-op and has no provider write.

Preview performs complete preflight for every child before issuing an
integrity-protected plan. It validates one resource scope, acting principal,
impersonation chain, quota-contact binding, plan lifetime, every exact slice,
target, preference identity and etag, eligibility, usage, acknowledgements, and
companion evidence. Any failed child preflight produces zero provider writes and
no Apply-capable plan. Canonical plan bytes bind the ordered children, target
strategy and inputs, evidence, warnings, acknowledgements, issuing installation,
and expiry. The unreleased `cqmgr.quota-request-plan/v1` payload has a required
top-level `kind` beside `schema`: `single` requires exactly one child and
`bundle` permits one or more ordered children. Unknown kinds fail closed.
An explicit exact-slice target is represented as strategy `manual` and kind
`single`. A workload-derived constraint set is kind `bundle` even when exactly
one child remains after verified no-ops are removed. When no child remains,
Preview returns the complete verified-no-op result without issuing a plan;
`single` remains reserved for an explicit exact-slice target and Apply has no
all-no-op input to consume.

Apply is deliberately non-atomic. It freshly revalidates every child before the
first provider write. It then dispatches non-no-op children in deterministic
accelerator-first order, using canonical exact-slice identity as the final
tie-breaker. It stops at the first conclusively failed or transport-unknown
child and never attempts later children. Each dispatched child receives one
durable disposition:

- `accepted` when the provider is proven to have accepted the bound intent;
- `failed` when the provider conclusively rejected or failed that dispatch;
- `unknown` when provider acceptance cannot be established safely; or
- `unattempted` when an earlier child failed or became unknown.

An accepted child is never described as rolled back when a later child fails.
The aggregate Apply succeeds only when every dispatchable child is accepted.
The result preserves verified no-ops and every accepted, failed, unknown, and
unattempted child with every available provider reconciliation identity.

The bundle plan is one authenticated, expiring, single-use authorization. Apply
acquires one exclusive bundle lease, fsyncs the ordered pre-Apply intent, and
crosses a fail-closed consumption barrier under the plan lock before dispatch:
it creates the immutable consumption marker and commits the ledger consumption
transition. Either half of a partially persisted barrier keeps the plan consumed
and quarantined, so lease expiry never makes it reusable. Every consumed plan
retains a durable Apply record. Each child dispatch intent is fsynced before the
provider call and its terminal outcome is fsynced before the next child begins.
Recovery may resume only a child with no persisted dispatch intent after every
prior outcome is terminal. A dispatch intent without a terminal outcome becomes
a durable `unknown`, stops later dispatch, and requires read-after-unknown
reconciliation; it is never dispatched again automatically. Interruption,
transport uncertainty, or persistence failure never causes a blind retry.
Deterministic child preference identity preserves accepted work and classifies
uncertainty.

Watch observes the accepted children of one applied bundle. It retains each
child's preference lineage, target, status axes, effective evidence, and resume
checkpoint. The aggregate `granted` or `fulfilled` condition is reached only
when every accepted child reaches that condition. A conclusive unmet child
terminates the aggregate condition without flattening other child states.
Timeout and interruption preserve the latest material observation and a locally
authenticated resume token bound to the bundle and all accepted children.
Every unreleased `cqmgr.watch-event/v1` record has a required `subject.kind` of
`single` or `bundle`. The subject binds resource scope, condition, plan or
intent digest, and an ordered nonempty array of complete child identities;
`single` requires exactly one child. A material child event names its
`child_id` and carries that child's status. Aggregate events and terminal
results retain ordered child summaries. Unknown subject kinds fail closed.

No bundle operation creates capacity, enables a service, mutates a companion
slice that was not an explicit child, or weakens the requirement for explicit
resource-scope acknowledgement.

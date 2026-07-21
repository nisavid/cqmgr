# Cloud Quota Manager

Cloud Quota Manager (`cqmgr`) is being designed to turn live cloud-provider
quota evidence into exact, reviewable operator actions without confusing quota
with physical capacity.

Its first complete workflows cover Google Cloud accelerator quota for NVIDIA
GPUs and TPUs. The CLI, TUI, and structured automation surfaces share the same
domain operations, evidence, warnings, and outcomes.

## Planned capabilities

- inspect exact effective quota slices and their related constraint sets;
- compare current Spot obtainability advice, historical preemption, price, and
  explicitly sourced latency evidence across compatible locations;
- preview one exact quota target with its identity, consequences, and fresh
  provider evidence;
- apply a time-bounded quota request plan deliberately;
- follow reconciliation through granted and effective-confirmed outcomes;
- preserve honest unknown, incomplete, partial, and unsupported states.

## Project status

This repository contains product planning, provider-contract research, and an
interaction prototype. It does not yet ship a `cqmgr` executable, and the
planning work does not authorize live quota mutations.

Start with:

- [the product context](PRODUCT.md) for users, purpose, and design principles;
- [the domain glossary](CONTEXT.md) for canonical language;
- [operator workflows](docs/operator-workflows.md) for the shared interaction
  contract;
- [provider research](docs/research/) for source-backed constraints;
- [GitHub Issues](https://github.com/nisavid/cqmgr/issues) for the live
  Wayfinder frontier.

The preserved interactive prototype lives on the
[`prototype/operator-workflows`](https://github.com/nisavid/cqmgr/tree/prototype/operator-workflows/prototypes/operator-workflows)
branch.

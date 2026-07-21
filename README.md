# Cloud Quota Manager

Cloud Quota Manager (`cqmgr`) is being designed to turn live cloud-provider
quota evidence into exact, reviewable operator actions without confusing quota
with physical capacity.

Its first complete workflows cover Google Cloud accelerator quota for NVIDIA
GPUs and TPUs. The CLI, TUI, and structured automation surfaces share the same
domain operations, evidence, warnings, and outcomes.

## Planned capabilities

- inspect exact effective quota slices and their related constraint sets;
- compare current Spot obtainability advice, historical preemption, and price
  across exact candidates while preserving candidate identity and evidence;
- preview one exact quota target with its identity, consequences, and fresh
  provider evidence;
- apply a time-bounded quota request plan deliberately;
- follow reconciliation through explicit `granted` and `fulfilled` outcomes;
- preserve honest unknown, incomplete, partial, and unsupported states.

## Project status

This repository contains the installable Python and quality baseline, product
planning, provider-contract research, and an interaction prototype. The
`cqmgr` executable currently exposes package help and version metadata; product
workflows are not implemented, and this work does not authorize live quota
mutations.

The approved implementation baseline is CPython 3.12–3.14, `pyproject.toml`,
uv, Ruff, Pyrefly, Click, and Textual. Once a package is published, the primary
installation path will be `uv tool install cqmgr`. The runtime and provider
boundaries are defined in the [runtime and integration
architecture](docs/runtime-integration-architecture.md). The supported platform,
test, installation, and release gates are defined in the [verification and
distribution contract](docs/verification-distribution-contract.md).

Start with:

- [the V1 product requirements](docs/product-requirements.md) for the complete
  implementation handoff, safety invariants, acceptance gates, and execution
  sequence;
- [the product context](PRODUCT.md) for users, purpose, and design principles;
- [the domain glossary](CONTEXT.md) for canonical language;
- [operator workflows](docs/operator-workflows.md) for the shared interaction
  contract;
- [CLI and TUI information architecture](docs/cli-tui-information-architecture.md)
  for command, navigation, query, and plan-handoff behavior;
- [runtime and integration architecture](docs/runtime-integration-architecture.md)
  for the Python stack, dependency direction, configuration, authentication,
  provider adapters, and local plan trust boundary;
- [verification and distribution contract](docs/verification-distribution-contract.md)
  for supported platforms, test layers, live-read-only canaries, package
  installation, and release gates;
- [provider research](docs/research/) for source-backed constraints;
- [GitHub Issues](https://github.com/nisavid/cqmgr/issues) for the live
  Wayfinder frontier.

The preserved interactive prototype lives on the
[`prototype/operator-workflows`](https://github.com/nisavid/cqmgr/tree/prototype/operator-workflows/prototypes/operator-workflows)
branch.

## Install and develop

Until `cqmgr` is published on PyPI, install the local checkout with uv:

```console
uv tool install .
cqmgr --help
```

Contributors use the committed lock and run the complete local baseline with:

```console
uv sync --locked
uv run ruff format --check .
uv run ruff check .
uv run pyrefly check
uv run lint-imports --no-cache
uv run pip-licenses --from mixed --allow-only "Apache-2.0;Apache-2.0 OR BSD-2-Clause;Apache Software License;BSD-2-Clause;BSD-3-Clause;BSD License;MIT;MIT License;MPL-2.0;PSF-2.0"
uv run pytest
uv run coverage report --include="src/cqmgr/domain/status.py,src/cqmgr/domain/results.py,src/cqmgr/domain/redaction.py" --fail-under=100
uv build --clear --no-sources
uv run python scripts/verify_distribution.py dist
uv run python scripts/smoke_tool_install.py dist --python 3.14
```

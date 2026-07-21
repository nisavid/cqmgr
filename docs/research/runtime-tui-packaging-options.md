# Runtime, TUI, and packaging options

Research date: 2026-07-14

Selection and platform revalidation date: 2026-07-21

This note compares credible implementation and distribution stacks for a small
cross-platform Cloud Quota Manager CLI/TUI. It records constraints and
trade-offs; it does not select a stack.

The selected Python, Textual, Click, uv, and Google client architecture is
defined in the [runtime and integration
architecture](../runtime-integration-architecture.md) and [ADR
0001](../adr/0001-python-runtime-and-google-cloud-integration.md).

## Constraints shared by every stack

- Google publishes first-party Cloud Quotas clients for C++, C#, Go, Java,
  Node.js, PHP, Python, and Ruby. The Go, Node.js, and Python clients all expose
  the v1 operations needed to list effective quota metadata and to create,
  update, get, and list quota preferences. The API is declarative: the latest
  quota preference is what Google Cloud tries to fulfill, and callers should
  list existing preferences before creating another one to avoid duplicates.
  [Cloud Quotas client libraries][cloud-quotas-libraries]
  [Cloud Quotas API overview][cloud-quotas-overview]
  [Common use cases][cloud-quotas-use-cases]
- A quota preference is not an immediate capacity allocation. Increases are
  subject to Google Cloud approval; `grantedValue`, `stateDetail`, and
  `reconciling` describe the resulting state. Preferences cannot be deleted.
  The product therefore needs an explicit pending/reconciled state regardless
  of UI framework or language. [Cloud Quotas API overview][cloud-quotas-overview]
- All three first-party clients use Application Default Credentials (ADC).
  Local user ADC is created separately from the active `gcloud` CLI identity,
  so installation and diagnostics must explain that distinction. Google also
  supports local ADC through service-account impersonation for Go, Node.js,
  and Python. [How ADC works][adc]
  [Local ADC setup][adc-local]
- The API is global even when a quota carries regional dimensions. Resource
  names use `locations/global`; the quota dimension carries a region such as
  `us-central1`. [Cloud Quotas API overview][cloud-quotas-overview]

## Coherent stack options

- **Go and Bubble Tea v2:** a typed first-party client, an Elm-style TUI,
  and per-platform binaries. This has the smallest destination runtime burden,
  while the application must model asynchronous work deliberately.
- **Python and Textual:** a first-party Python 3.10+ client and a higher-level
  widget/reactive UI. Distribution can use isolated Python tools or frozen
  executables, each with a distinct support lifecycle.
- **TypeScript and Ink:** a stable first-party typed client and a React TUI.
  Distribution can require supported Node.js or use newer single-executable
  tooling that remains in active development.

### Go and Bubble Tea

Google's current Go client is a stable module and exposes the required v1
methods directly. Its client methods may be called concurrently, and its list
operations provide iterators, which fits a TUI that streams or incrementally
loads quota rows. [Go Cloud Quotas client][go-cloud-quotas]

Bubble Tea v2 is current (v2.0.6 as of this research) and moved to the
`charm.land/bubbletea/v2` import path. It provides a single event loop with
commands for external work; the companion Bubbles project includes table,
list, text input, spinner, progress, and viewport components. The repository
contains separate Unix and Windows terminal implementations, while enhanced
key events require compatible terminals and therefore need fallbacks.
[Bubble Tea][bubble-tea] [Bubble Tea releases][bubble-tea-releases]

Go can target Darwin, Linux, and Windows across amd64 and arm64 without
requiring a language runtime on the destination. GoReleaser's Go builder
generates a configured `GOOS`/`GOARCH` matrix, archives the artifacts, and can
publish a GitHub release; its defaults cover Darwin, Linux, and Windows. This
path stays simplest when dependencies do not require CGo. macOS signing and
notarization and Windows signing remain separate release-policy decisions.
[Go target ports][go-ports] [GoReleaser Go builder][goreleaser-build]
[GoReleaser quick start][goreleaser-quick-start]

Decision-relevant consequences:

- Direct release archives provide the smallest support surface: one native
  binary per OS/architecture plus checksums.
- `go install <module>@<version>` is useful for developers but transfers the Go
  toolchain requirement to users and is not a general desktop installation
  story.
- Homebrew, WinGet/Scoop, and Linux packages can improve upgrades and discovery,
  but each adds a feed, review, or repository lifecycle beyond building the
  binary.
- The UI's asynchronous model must make API cancellation, retry, pagination,
  and pending quota-preference state explicit rather than hiding them inside
  widgets.

### Python and Textual

The current `google-cloud-quotas` distribution is marked Beta on PyPI and
requires Python 3.10 or newer, including Python 3.14. Its v1 client covers the
same list/get/create/update operations as the Go client. Textual 8.2.8 is
marked Production/Stable, supports Python 3.9 through 3.14, and declares
macOS, Linux, Windows 10, and Windows 11 support. The Cloud client therefore
sets a dependency floor of Python 3.10; the selected product floor is Python
3.12. [Python Cloud Quotas package][python-cloud-quotas]
[Python Cloud Quotas client][python-cloud-quotas-client]
[Textual package][textual]

Textual supplies widgets, reactive state, CSS-like layout, async workers, and a
testing framework. It can also serve an application in a browser. Terminal
behavior is not identical on every platform: for example, Textual documents
that inline mode is not currently supported on Windows. A full-screen TUI
should therefore be the portable baseline unless Windows-specific testing
proves another mode. [Textual getting started][textual-start]
[Textual application guide][textual-app]

There are two distinct delivery models:

- Publish a normal Python package and recommend `uv tool install` or `pipx`.
  `uv tool install` creates an isolated persistent environment and exposes the
  package's executable on `PATH`. uv can manage the required Python, but uv
  itself becomes an installation prerequisite. uv's Tier 1 platforms are
  macOS arm64/x86_64, Linux x86_64, and Windows x86_64; Linux arm64 and Windows
  arm64 are Tier 2. That uv classification is not a product support promise:
  the current `grpcio` dependency has no Windows arm64 wheel, so the v1 matrix
  must exclude or separately resolve that target. [uv tools][uv-tools]
  [uv platform policy][uv-platforms] [grpcio package][grpcio]
- Freeze one executable per target with PyInstaller. Users then do not need
  Python installed, but PyInstaller is not a cross-compiler: artifacts are
  specific to the build OS, Python version, and word size. Linux artifacts also
  retain host compatibility constraints because PyInstaller does not bundle
  `libc`. A release matrix needs native runners and target-specific validation.
  [PyInstaller operating model][pyinstaller]

Decision-relevant consequences:

- This stack provides the richest built-in UI system and the shortest path to
  forms, tables, styling, workers, and UI tests.
- Package installation keeps releases simple but makes Python/uv environment
  failures part of support and startup diagnostics.
- Frozen binaries improve the first-run experience at the cost of larger
  artifacts, native build jobs, hidden-import/resource configuration, and
  target-by-target smoke tests.

### TypeScript and Ink

Google marks `@google-cloud/cloudquotas` stable. The current package is 2.3.1,
ships TypeScript declarations, exposes v1 list/get/create/update operations,
and follows the supported Node.js release schedule. [Node Cloud Quotas package][node-cloud-quotas]
[Node Cloud Quotas client][node-cloud-quotas-client]

Ink renders React components in the terminal and uses Yoga for Flexbox layout.
This makes it credible when React and TypeScript familiarity materially lowers
the UI learning cost. Ink v7 requires Node.js 22 and React 19.2 or newer.
[Ink][ink] [Ink v7 release][ink-v7]

The straightforward distribution is an npm package installed globally or run
through a package runner, which requires a supported Node.js runtime. Node 22
and Node 24 are LTS lines as of this research. Node's single-executable
application (SEA) support can produce an executable for a machine without Node
installed, but the API is still marked active development. Cross-platform SEA
builds must disable code cache and snapshots, and macOS/Windows signing remains
part of the artifact workflow. [Node.js release status][node-releases]
[Node single-executable applications][node-sea]

Decision-relevant consequences:

- Ink is compelling when React reuse is more valuable than having a
  batteries-included terminal widget framework.
- npm installation is portable but exposes Node version management and package
  installation to users.
- SEA removes the runtime prerequisite but currently carries more packaging
  uncertainty than Go binaries or Python package installation.

## Boundary case: Rust and Ratatui

Ratatui is a credible native TUI framework and Cargo provides a strong build
and package ecosystem. Google does not, however, list Rust among the Cloud
Quotas client-library languages. A Rust implementation would need to own the
REST/protobuf transport, ADC integration, retries, pagination, and generated
API compatibility directly or depend on non-Google client libraries. That is a
material scope increase for this product, not merely a language preference.
[Ratatui installation][ratatui] [Cloud Quotas client libraries][cloud-quotas-libraries]

## Selected verification and support contract

The [verification and distribution
contract](../verification-distribution-contract.md) resolves the supported
platform and terminal matrix, the Windows arm64 dependency gate, native
keyring support, layered tests, live-read-only canaries, PyPI artifacts,
provenance, and release policy.

## Required implementation validation

- Authenticate through short-lived workload identity and externally configured
  service-account impersonation, then call `GetQuotaInfo` with an explicitly
  configured, stable GPU quota name using the v1 client. Direct-user ADC receives
  hermetic contract coverage and an advisory live smoke.
- Render the approved Textual inspector, request composer, lifecycle status,
  narrow layout, and provider error, then verify keyboard and cancellation
  behavior with Pilot.
- Build an sdist and pure-Python wheel, resolve it as an external uv tool on
  each candidate platform, and exercise redirected and non-interactive CLI
  behavior separately from the TUI.
- Measure cold start, resident memory, reading order, and low-color behavior on
  the supported terminal matrix.

[adc]: https://cloud.google.com/docs/authentication/application-default-credentials
[adc-local]: https://cloud.google.com/docs/authentication/set-up-adc-local-dev-environment
[bubble-tea]: https://github.com/charmbracelet/bubbletea
[bubble-tea-releases]: https://github.com/charmbracelet/bubbletea/releases
[cloud-quotas-libraries]: https://cloud.google.com/docs/quotas/reference/libraries
[cloud-quotas-overview]: https://cloud.google.com/docs/quotas/api-overview
[cloud-quotas-use-cases]: https://cloud.google.com/docs/quotas/implement-common-use-cases
[go-cloud-quotas]: https://pkg.go.dev/cloud.google.com/go/cloudquotas/apiv1
[go-ports]: https://go.dev/doc/install/source#environment
[goreleaser-build]: https://goreleaser.com/customization/builds/builders/go/
[goreleaser-quick-start]: https://goreleaser.com/getting-started/quick-start/
[grpcio]: https://pypi.org/project/grpcio/
[ink]: https://github.com/vadimdemedes/ink
[ink-v7]: https://github.com/vadimdemedes/ink/releases/tag/v7.0.0
[node-cloud-quotas]: https://www.npmjs.com/package/@google-cloud/cloudquotas
[node-cloud-quotas-client]: https://cloud.google.com/nodejs/docs/reference/cloudquotas/latest/cloudquotas/v1.cloudquotasclient
[node-releases]: https://nodejs.org/en/about/previous-releases
[node-sea]: https://nodejs.org/api/single-executable-applications.html
[pyinstaller]: https://pyinstaller.org/en/stable/operating-mode.html
[python-cloud-quotas]: https://pypi.org/project/google-cloud-quotas/
[python-cloud-quotas-client]: https://cloud.google.com/python/docs/reference/google-cloud-cloudquotas/latest/google.cloud.cloudquotas_v1.services.cloud_quotas.CloudQuotasClient
[ratatui]: https://ratatui.rs/installation/
[textual]: https://pypi.org/project/textual/
[textual-app]: https://textual.textualize.io/guide/app/
[textual-start]: https://textual.textualize.io/getting_started/
[uv-platforms]: https://docs.astral.sh/uv/reference/policies/platforms/
[uv-tools]: https://docs.astral.sh/uv/concepts/tools/

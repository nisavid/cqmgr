# Credential-storage backend research

Date: 2026-07-21

Status: supporting research for the verification and distribution contract.
This note records verified facts and non-binding recommendations. The selected
V1 policy is defined in [ADR 0002](../adr/0002-use-native-keyrings-for-local-secrets.md).

## Scope and evidence standard

This note compares Python `keyring`, password-store `pass`, Proton Pass CLI
(`pass-cli`), Bitwarden CLI (`bw`), KDE Wallet, GnuPG (`gpg`), Sequoia (`sq`),
`age`, and `rage` for:

- GitHub-hosted macOS, Linux, and Windows x86-64 runners;
- GitHub-hosted macOS and Linux arm64 runners;
- noninteractive use and plaintext exposure through argv, stdout, environment
  variables, and temporary files;
- item identity, update atomicity, and concurrent callers;
- unlock and user-presence behavior;
- size, licensing, bundling, maintenance, and update ownership; and
- Python integration.

Only project-owned documentation, source, release metadata, and GitHub's
runner documentation are used. “Available” below means that the program or
Python package can be installed or built for the runner architecture. It does
not mean that a fresh hosted runner supplies a representative logged-in desktop
session, persistent vault, interactive unlock path, or production-grade secret.
GitHub documents hosted Ubuntu, Windows, and macOS runners and x64/arm64 larger
runner images, while the linked runner-image manifests are updated weekly.
([GitHub-hosted runners](https://docs.github.com/actions/concepts/runners/github-hosted-runners),
[larger-runner images](https://docs.github.com/actions/reference/runners/larger-runners))

No credentials or live cloud providers were used for this research.

## Verified facts

### Target-platform availability

| Backend | macOS x64 | Linux x64 | Windows x64 | macOS arm64 | Linux arm64 | Hosted-runner qualification |
| --- | --- | --- | --- | --- | --- | --- |
| Python `keyring` | Yes: Keychain | Yes: Secret Service or KWallet | Yes: Credential Locker | Yes: Keychain | Yes: Secret Service or KWallet | The wheel is portable, but Linux needs D-Bus plus the selected desktop service; KWallet also needs `dbus-python`. |
| password-store `pass` | Installable | Installable | No official native path | Installable | Installable | Architecture-neutral shell program, but requires Bash, GnuPG, and Unix utilities. |
| Proton `pass-cli` | Official build | Official build | Official build | Official build | Official build | Exact requested matrix is in the official installation table. |
| Bitwarden `bw` | Native x64 | Native x64 | Native x64 | npm/Node path | npm/Node path | Official native binaries are x64; the npm package is the documented ARM path. |
| KDE `kwallet-query` | No supported backend | Install/build | No supported backend | No supported backend | Install/build | Needs a KWallet-enabled D-Bus desktop session; a fresh headless Linux runner is not representative. |
| GnuPG `gpg` | Supported | Supported | Supported | Supported | Supported | Official platform support covers macOS, GNU/Linux amd64/arm64, and 64-bit Windows. |
| Sequoia `sq` | Package/build dependent | Package/build dependent | No native parity established | Package/build dependent | Package/build dependent | Official Windows instructions recommend WSL; a native Windows source build is a separate undertaking. |
| `age` | Official asset | Official asset | Official asset | Official asset | Official asset | The v1.3.1 release publishes the exact five requested artifacts. |
| `rage` | Prebuilt OS support; exact asset unverified | Prebuilt OS support; exact asset unverified | Prebuilt OS support; exact asset unverified | Exact asset unverified | Exact asset unverified | The project promises prebuilt binaries for the three operating systems, but its current release architecture assets still need a release-gate audit. |

Availability evidence: Python `keyring` documents the recommended native
backends and their Linux dependencies
([keyring docs](https://keyring.readthedocs.io/en/latest/)); password-store is a
Unix shell program built around GnuPG
([official site](https://www.passwordstore.org/),
[source](https://git.zx2c4.com/password-store/tree/src/password-store.sh));
Proton publishes an exact OS/architecture table
([installation](https://protonpass.github.io/pass-cli/get-started/installation/));
Bitwarden distinguishes native x64 executables from its cross-platform npm
package ([CLI installation](https://bitwarden.com/help/cli/)); KDE builds
`kwallet-query` in the KWallet Framework source
([KDE source](https://github.com/KDE/kwallet/tree/master/src/runtime/kwallet-query));
GnuPG publishes its supported-system matrix
([GnuPG systems](https://gnupg.org/download/supported_systems.html)); Sequoia
documents its platform installation paths
([Sequoia installation](https://book.sequoia-pgp.org/installation.html)); and
the age release lists architecture-specific assets
([age v1.3.1](https://github.com/FiloSottile/age/releases/tag/v1.3.1)). `rage`
documents Windows, Linux, and macOS prebuilt installation without making the
exact five-architecture release contract equally explicit
([rage source](https://github.com/str4d/rage)).

### Python `keyring` native backends

`keyring` provides direct Python `get_password`, `set_password`,
`delete_password`, and `get_credential` APIs. Its recommended backends include
macOS Keychain, Freedesktop Secret Service, KDE KWallet, and Windows Credential
Locker. The current PyPI distribution is an MIT-licensed, platform-independent
wheel; native and Linux desktop dependencies remain separate.
([keyring API and backends](https://keyring.readthedocs.io/en/latest/),
[PyPI metadata](https://pypi.org/project/keyring/))

The native mappings are not identical:

- macOS uses a generic-password service and username through the Security API.
  The backend converts denied access into a locked-keyring error. The project
  warns that scripts run by the same Python executable may access a
  keyring-created item without another OS prompt unless its access control is
  changed. ([macOS backend](https://github.com/jaraco/keyring/blob/main/keyring/backends/macOS/__init__.py),
  [macOS security note](https://keyring.readthedocs.io/en/latest/#security-considerations))
- Windows uses generic credentials. Its compatibility path may first move an
  existing same-service/different-user credential and then write the new
  credential, so that path is a multi-step update rather than a portable
  transaction. ([Windows backend](https://github.com/jaraco/keyring/blob/main/keyring/backends/Windows.py))
- Secret Service identifies items with service, username, and application
  attributes and creates with replacement enabled. It may need to unlock the
  collection or item and can report a dismissed prompt. Headless use requires
  a D-Bus session and a keyring daemon in the same session.
  ([Secret Service backend](https://github.com/jaraco/keyring/blob/main/keyring/backends/SecretService.py),
  [headless Linux instructions](https://keyring.readthedocs.io/en/latest/#using-keyring-on-headless-linux-systems))
- KWallet maps service to folder and username to entry over D-Bus. Opening the
  wallet may prompt or be cancelled; reads and writes use KWallet's password
  calls. ([KWallet backend](https://github.com/jaraco/keyring/blob/main/keyring/backends/kwallet.py))

The portable API defines no compare-and-swap, create-only write, multi-item
transaction, or cross-process lock. Its in-process boundary avoids putting a
secret in child-process argv, environment, stdout, or a temporary file, though
Python and OS process memory remain in scope.

### password-store `pass` and Proton `pass-cli` are different tools

Password-store `pass` is a GPL-2.0-or-later shell program that stores one GPG
file per hierarchical path under a password-store directory. A read decrypts
to stdout. Multiline insertion can read the value from stdin. Its `edit`
workflow creates a protected plaintext temporary file, so that operation has a
materially larger exposure boundary and is not equivalent to stdin insertion.
Some source paths use a temporary sibling and `mv`, but ordinary insertion does
not establish a general atomic, conditional-update, or cross-process-locking
contract. GnuPG agent and pinentry control unlock and caching. There is no
official Python API; integration is by subprocess.
([password-store site](https://www.passwordstore.org/),
[password-store source](https://git.zx2c4.com/password-store/tree/src/password-store.sh))

Proton Pass CLI is a separate GPL-3.0-or-later Rust client for the Proton Pass
service. It requires a Proton account, network access, authentication, and a
retained local session. Interactive login may use a browser or terminal;
personal access tokens are intended for automation. Environment-provided
tokens are visible within the process environment, and token arguments are an
argv exposure. The session store itself uses the platform key store where
available. ([authentication](https://protonpass.github.io/pass-cli/commands/login/),
[source and license](https://github.com/protonpass/pass-cli))

Its stable remote identity is a share or vault plus item ID and optional field.
Creation can read a JSON template from stdin, but the documented item-update
form places updated field values in command arguments. That makes the
documented password-update path unsuitable for a no-secret-in-argv adapter
without another verified input form. The CLI documents no conditional update
or multi-item transaction. There is no official Python SDK for this CLI;
integration is by subprocess.
([item commands](https://protonpass.github.io/pass-cli/commands/item/),
[developer resources](https://protonpass.github.io/pass-cli/developer-resources/))

### Bitwarden CLI `bw`

`bw get password <id>` emits the password on stdout. An exact item UUID avoids
the ambiguity of search strings. Create and edit operations accept encoded JSON
through stdin; edit replaces the full object and returns the updated object as
JSON. The public CLI contract does not expose compare-and-swap. Changes push to
the server, while `sync` updates the local vault, so concurrent devices can
still race around a read/modify/write adapter. Deletes move items to trash
unless permanent deletion is requested.
([Bitwarden CLI item operations](https://bitwarden.com/help/cli/))

Login and unlock produce a `BW_SESSION` value. Passing it with `--session`
exposes it in argv; exporting it exposes it to the environment inherited by
children and to same-user process inspection permitted by the OS. Password
environment and password-file options move, rather than remove, the exposure.
Piped JSON avoids a plaintext payload file, and Bitwarden states that decrypted
vault data is held in memory rather than written decrypted to disk.
([authentication and session handling](https://bitwarden.com/help/cli/))

Python integration is subprocess/JSON; no official Python SDK is identified.
Using `bw` makes the user responsible for account and vault configuration, CLI
installation and updates, unlock/logout lifecycle, and network availability.
The source tree is GPL-3.0 except for separately identified Bitwarden-licensed
components, so bundling would require an exact artifact and license audit.
([Bitwarden client source and license](https://github.com/bitwarden/clients))

### KDE Wallet command-line tools

`kwallet-query` is the KDE-provided command, built in the KDE Frameworks
KWallet repository. `kwalletcli` or `kwallet-cli` is a separate legacy
third-party utility and is not the canonical KDE command for this evaluation.
The KDE command identifies an item by wallet, folder, and entry. It reads a
secret from stdin for writes, returns a secret on stdout for reads, and warns
that a write overwrites the existing entry. Its documented exit codes
distinguish a missing wallet, open failure, missing folder, and read/write
failure. No conditional update, cross-process lock, or multi-entry transaction
is documented.
([KDE `kwallet-query` manual source](https://github.com/KDE/kwallet/blob/master/docs/kwallet-query/man-kwallet-query.1.docbook),
[KDE command source](https://github.com/KDE/kwallet/tree/master/src/runtime/kwallet-query))

The command requires the Qt/KDE/D-Bus runtime and an available KWallet daemon;
opening a wallet may cause an unlock prompt. The command reports GPL licensing
while the surrounding framework contains LGPL-licensed code, so any bundled
artifact needs an aggregate license review. Direct use of Python `keyring`'s
KWallet backend reaches the same service without a secret-bearing subprocess
channel and is the more direct Python integration.

### GnuPG `gpg`

For unattended operation, the official manual specifies `--batch`, `--no-tty`,
machine-readable status output, and colon-delimited listings. Passphrase argv
and passphrase-file options are explicitly questionable; a separate file
descriptor plus loopback pinentry can avoid argv and a plaintext file, but the
caller must keep passphrase input separate from plaintext input. Normal agent
and pinentry behavior may prompt and cache authorization.
([GnuPG manual](https://gnupg.org/documentation/manuals/gnupg26/gpg.1.html))

GnuPG is an encryption and key-management suite, not an item store. cqmgr would
own file identity, locking, crash-safe temporary writes, filesystem syncing,
atomic replacement, deletion, recovery, and key selection. The download is a
multi-component native stack including agent, pinentry, and libraries. Official
downloads list an approximately 8 MB compressed source archive and an
approximately 15 MB archive with required libraries; installed and packaged
sizes are larger and platform-specific. GnuPG is GPLv3-or-later, while bundled
components use their own licenses, including Libgcrypt under LGPLv2.1-or-later;
every distributed artifact and component therefore requires separate license
review.
([GnuPG downloads](https://gnupg.org/download/index.html),
[Libgcrypt licensing](https://www.gnupg.org/software/libgcrypt/index.html))

The GnuPG project recommends GPGME for programmatic use, and GPGME has official
Python 3 bindings. That avoids parsing human CLI output but retains the native
GnuPG/GPGME stack and does not add credential-store semantics.
([GPGME introduction](https://gnupg.org/documentation/manuals/gpgme/Introduction.html),
[GPGME Python FAQ](https://www.gnupg.org/faq/gpgme-faq.html))

### Sequoia `sq`

`sq` is Sequoia's LGPL-2.0-or-later OpenPGP CLI. It can encrypt and decrypt files
or stdin/stdout streams. The official installation guide covers packages and
source builds on Linux and macOS; for Windows it recommends WSL, while a native
Windows build requires a separate source toolchain. Password automation is
command/version-specific, and this research did not establish a clean,
documented separate-fd secret input equivalent across the target matrix.
([Sequoia installation](https://book.sequoia-pgp.org/installation.html),
[encrypt/decrypt guide](https://book.sequoia-pgp.org/encrypt_decrypt.html),
[`sq` source and license](https://gitlab.com/sequoia-pgp/sequoia-sq))

Like `gpg`, `sq` supplies encryption and OpenPGP key operations rather than
item identity, conditional updates, locking, or crash-safe credential-file
replacement. No first-party Python binding for `sq` was identified; the direct
integration is a subprocess, while adopting Sequoia's native library would be a
separate FFI and packaging project.

### `age`

`age` is a BSD-3-Clause Go implementation and library. Version 1.3.1 publishes
darwin-amd64, darwin-arm64, linux-amd64, linux-arm64, and windows-amd64 release
assets. The compressed archives are approximately 9–10 MB. It streams
plaintext and ciphertext through stdin/stdout, and an identity can be read from
stdin; an identity path, rather than identity contents, may be placed in argv.
([age source and license](https://github.com/FiloSottile/age),
[age v1.3.1 assets](https://github.com/FiloSottile/age/releases/tag/v1.3.1))

The CLI has deliberately kept ordinary passphrase input interactive. Version
1.3.0 added noninteractive passphrase input through the separate `batchpass`
plugin and asks users to read its warning; it did not turn the built-in
passphrase prompt into a general stdin, argv, or environment interface.
Automation therefore needs an identity or plugin whose custody is itself
solved. A plaintext age identity beside its ciphertext is not equivalent to an
OS keyring.
([age v1.3.0 release notes](https://github.com/FiloSottile/age/releases/tag/v1.3.0),
[age v1.1.0 notes](https://github.com/FiloSottile/age/releases/tag/v1.1.0))

`age` is an encryption format and tool, not an item store. cqmgr would own item
identity, directories, permissions, locks, temporary files, fsync and atomic
rename, deletion, corruption recovery, backup, and root-key custody. There is
no official Python API; subprocess integration is the narrow direct path.

### `rage`

`rage` is a Rust implementation of the age format, dual-licensed Apache-2.0 and
MIT. The project documents prebuilt binaries for Windows, Linux, and macOS and
installation through package managers or Cargo. This research did not verify
that its current release assets make all five requested architectures a stable
distribution contract, so that remains an acceptance gate rather than an
assumed fact.
([rage source and license](https://github.com/str4d/rage),
[rage v0.12.1](https://github.com/str4d/rage/releases/tag/v0.12.1))

Its CLI uses stdin/stdout for content, accepts identity sources, and uses
interactive terminal or pinentry prompting for protected identities. No
documented noninteractive passphrase channel, credential-store item model,
compare-and-swap, or atomic-file contract was identified. There is no official
Python API. As with `age`, cqmgr would own storage and key custody; bundling both
implementations would add update and provenance work without adding a new
storage model.

## Cross-cutting exposure and concurrency findings

These are verified consequences of the documented interfaces:

- A process argument may be observable in process listings, diagnostics, crash
  reports, and command-history tooling. No adapter should place a secret or
  session credential in argv.
- An environment value is inherited by child processes unless scrubbed and may
  be readable by same-user inspection allowed by the operating system. It is
  not an equivalent replacement for a keyring.
- CLI reads that emit plaintext on stdout require a bounded pipe captured in
  memory. stdout and stderr must never be inherited, logged, or included in an
  exception containing payload bytes.
- stdin is the least-broad CLI payload channel in this comparison, but the
  child process still receives plaintext. Direct execution without a shell is
  required.
- A plaintext temporary file adds filesystem, backup, indexing, crash-remnant,
  and deletion-recovery exposure. Mode `0600` and best-effort deletion do not
  make it equivalent to never writing plaintext.
- None of the compared portable interfaces supplies a shared compare-and-swap
  or multi-item transaction. A caller-side lock can serialize cqmgr processes
  on one machine, but cannot eliminate races with another cloud client or an
  external tool.

## Non-binding recommendations

The following are recommendations derived from the verified facts. They are
not final operator decisions.

1. Evaluate allowlisted Python `keyring` native backends as the built-in V1
   path. They have the narrowest Python integration and plaintext-transfer
   boundary. Reject null, plaintext, file-backed, third-party, or unknown
   backends unless separately authorized.
2. Give every reference a deterministic cqmgr service namespace, installation
   or profile identifier, and purpose label. Keep the secret out of the
   reference. Add a cqmgr interprocess lock, create-only behavior, and
   read-after-write verification because the portable API has no CAS contract.
3. Treat `pass`, `kwallet-query`, `bw`, and Proton `pass-cli` as explicit,
   operator-installed adapters only if a later decision admits them. Do not
   autodetect one as a silent fallback. `pass` is Unix-only; `kwallet-query` is
   redundant with the direct KWallet backend; cloud CLIs add account, session,
   network, remote-concurrency, and product-policy dependencies.
4. Do not treat `gpg`, `sq`, `age`, or `rage` as storage backends by themselves.
   They become encrypted-file backends only after cqmgr owns a complete storage,
   locking, recovery, and root-key-custody design.
5. If an encrypted-file design is authorized for a later release, evaluate
   `age` first. Its exact artifact matrix, small format surface, permissive
   license, and roughly 9–10 MB archives make its redistribution question more
   bounded than GnuPG or Sequoia. This does not solve identity custody and is
   not a recommendation to bundle it in V1.
6. Do not support Proton item updates until a documented, tested update form can
   carry the new secret without argv, environment, or plaintext files. Do not
   support any CLI operation that requires those transports.

## Acceptance gates before admitting a later backend

Run the following spike before enabling or approving any deferred backend:

1. Install and smoke-test the exact supported version on GitHub-hosted macOS,
   Linux, and Windows x86-64 and macOS/Linux arm64. Record when CI exercises
   only installation or mocked service behavior rather than a real unlock.
2. Round-trip the maximum planned secret size using exact item identity. Test
   missing, existing, locked, cancelled, unavailable, corrupt, and deleted
   states and map them to typed cqmgr outcomes.
3. Prove that writes use no secret argv, environment value, shell, clipboard,
   or plaintext temporary file and that reads cannot reach logs or inherited
   stdout/stderr.
4. Race two cqmgr processes through create, read, rotation, and deletion. For
   cloud providers, also race a second client. Verify conflict behavior rather
   than assuming last-write-wins is safe.
5. Test login, unlock, lock, logout, session expiry, desktop logout, and user-
   cancelled prompts. Confirm that headless CI limitations do not become a
   production availability claim.
6. Record executable and dependency sizes, exact artifact licenses, signatures
   or provenance, vulnerability response owner, update mechanism, supported-
   version window, and uninstall behavior.
7. Decide separately whether cqmgr owns a dependency, bundles it, or requires
   an operator-managed executable. Bundling authorization must name the exact
   artifact and license set.

V1 selects allowlisted native `keyring` stores only. External password-manager
and encrypted-file adapters remain deferred; this research does not authorize
or specify one.

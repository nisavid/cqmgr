# Use native keyrings for local secrets

Cloud Quota Manager V1 stores quota-contact values and the per-installation
plan-authentication secret only through allowlisted in-process native keyring
backends:

- macOS Keychain;
- Windows Credential Locker; and
- Freedesktop Secret Service.

KWallet remains a compatibility canary until its D-Bus dependencies, unlock
behavior, and failure mapping pass the complete backend contract. A missing,
locked, null, plaintext, file-backed, third-party, or unknown backend permits
read-only provider operations but blocks Preview and Apply with setup guidance.
The allowlist matches concrete backend class objects loaded from the selected
keyring package; mutable class-name or module-name strings cannot grant
capability.

V1 does not invoke password-manager command-line tools, provide an encrypted
file fallback, bundle an encryption binary, accept an arbitrary secret command,
or programmatically unlock a vault. The backend port exposes only capability
probing and exact-reference create, get, and delete operations with typed
missing, locked, unavailable, and failure outcomes.

Every cqmgr-managed secret uses a collision-resistant, bounded random item
identity in cqmgr's private service namespace. Item identities are immutable:
creation performs one native write followed by one verification read, never an
update-in-place or automatic write retry. Rotation creates a new random item,
verifies it, and atomically switches the non-secret reference before the old
item is eligible for explicit cleanup.

Each plan also has a non-secret, digest-derived consumption-marker identity in
that private namespace. Immediately before provider dispatch, cqmgr creates the
marker once with a keyed digest-bound value and verifies the write. The marker
is never replaced or deleted. Filesystem ledger recovery checks it before
issuing authority, so replaying an older authentic ledger snapshot cannot make
a dispatched plan reusable.

A local interprocess lock serializes cqmgr callers. The portable keyring API
does not provide compare-and-swap, so arbitrary external writers are not
participants in this concurrency protocol. An observable verification mismatch
fails closed. A client that writes cqmgr's private namespace out of band is
unsupported tampering; cqmgr does not claim to preserve an invisible external
write racing its single create operation.

Native keyrings provide the narrowest plaintext-transfer boundary for the
selected Python package. Command-line password managers expose secrets or
session material through subprocess input, output, arguments, or environment
state and add their own account, unlock, synchronization, and concurrency
contracts. File encryptors such as age, rage, GnuPG, and Sequoia protect bytes
but do not solve key custody, item identity, locking, crash-safe replacement,
or recovery. Bundling one would also replace the selected universal
pure-Python artifact with a platform-binary release matrix.

This choice accepts native desktop-service availability as a mutation
prerequisite. Headless systems without a qualifying backend retain useful
read-only and local inspection behavior, but they cannot issue or apply a
quota request plan.

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

V1 does not invoke password-manager command-line tools, provide an encrypted
file fallback, bundle an encryption binary, accept an arbitrary secret command,
or programmatically unlock a vault. The backend port exposes only capability
probing and exact-reference create, get, and delete operations with typed
missing, locked, unavailable, and failure outcomes.

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

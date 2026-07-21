---
status: accepted
---

# Use Python adapters around a provider-neutral async application

Cloud Quota Manager will use CPython `>=3.12,<3.15`, Textual, and Click,
packaged from a `pyproject.toml` with the bounded `uv_build` backend, uv, Ruff,
and Pyrefly. This stack supplies the required interactive widgets, async work,
test seams, and isolated tool installation without making terminal rendering
part of the domain. CLI and TUI code remain thin inbound adapters over async
application operations and provider-neutral domain types; Google Cloud clients,
persistence, serialization, and credential inspection are outbound adapters
composed at startup.

Official Google Python clients are the default integration boundary. Direct
REST is permitted only behind the same ports when an official client lacks a
required published schema, and `cqmgr` never executes `gcloud` or reads its
active account or project at runtime. Local development may use `gcloud auth
application-default login` to create or repair Application Default Credentials,
which `cqmgr` consumes through `google-auth`.

This choice accepts bounded Python and Google client dependencies in exchange
for a smaller implementation and verification surface. V1 accepts project
resource scopes, authenticates and consumes plans within one local installation,
retains normalized safe evidence rather than raw provider bodies, and defers
folder and organization operations, cross-host plan trust, cross-service
discovery, and live GKE inventory.

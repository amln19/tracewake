# Tracewake contract set

These files define the boundary between the local Python semantics and a hosted
control plane. Python remains authoritative for bundles, event interpretation,
analysis profiles, and semantic results. The hosted control plane owns tenant
authorization and lifecycle transitions.

Version 1 contains:

* `bundle-v1.md`: deterministic transport bytes and validation limits;
* `align-v1.md`: the dependency-free hosted alignment profile;
* `public-api-v1.md`: tenant-facing HTTP resources and errors;
* `worker-protocol-v1.md`: attempt-scoped worker messages and authentication;
* `lifecycle-v1.json` and `lifecycle-v1.md`: executable state-machine rules;
* `persistence-v1.md` and `postgres/0001_*.sql`: authoritative relational state;
* `threat-model-v1.md`: tenant, bundle, worker, and browser boundaries;
* `compatibility-v1.md`: version evolution and shared-fixture workflow;
* `schemas/v1`: canonical Pydantic-generated JSON Schemas.

`divergence.md` sits alongside these but is not one of them. It documents the
local rule that locates where a failing run went irrecoverably wrong, which no
external party depends on and which is expected to change as evidence changes.
It carries no version and promises no compatibility. The hosted plane accepts
`align-v1` only.

Regenerate or check schemas without an additional dependency:

```bash
python -m tracewake.contracts --output contracts/schemas/v1
python -m tracewake.contracts --output contracts/schemas/v1 --check
```

Contract fixtures live under `contracttest/fixtures/v1`. Python validates the
semantic content. Go validates only the transport shape, declared versions,
limits, and lifecycle protocol that the control plane owns.

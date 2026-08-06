# Locus contract set

These files define the boundary between the local Python semantics and a hosted
control plane. Python remains authoritative for bundles, event interpretation,
analysis profiles, and semantic results. The hosted control plane owns tenant
authorization and lifecycle transitions.

Version 1 contains:

* `bundle-v1.md`: deterministic transport bytes and validation limits;
* `lexical-v1.md`: the dependency-free hosted alignment profile;
* `public-api-v1.md`: tenant-facing HTTP resources and errors;
* `worker-protocol-v1.md`: attempt-scoped worker messages and authentication;
* `lifecycle-v1.json` and `lifecycle-v1.md`: executable state-machine rules;
* `persistence-v1.md` and `postgres/0001_*.sql`: authoritative relational state;
* `threat-model-v1.md`: tenant, bundle, worker, and browser boundaries;
* `compatibility-v1.md`: version evolution and shared-fixture workflow;
* `schemas/v1`: canonical Pydantic-generated JSON Schemas.

Regenerate or check schemas without an additional dependency:

```bash
python -m locus.contracts --output contracts/schemas/v1
python -m locus.contracts --output contracts/schemas/v1 --check
```

Contract fixtures live under `contracttest/fixtures/v1`. Python validates the
semantic content. Go validates only the transport shape, declared versions,
limits, and lifecycle protocol that the control plane owns.

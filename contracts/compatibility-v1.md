# Contract compatibility

Persistent contracts evolve independently. Bundle format, event schema,
cassette format, worker protocol, public API, semantic results, analysis
profiles, and PostgreSQL migrations never share a version merely for
convenience.

Version 1 behavior is:

* an absent, unknown, or future major version is rejected before semantic use;
* extra fields in worker and semantic messages are rejected;
* bounded enums and failure codes are closed sets;
* a stored version is never reinterpreted after release;
* a compatible additive public API change may retain `/v1` only when existing
  requests and responses preserve their meaning;
* semantic, archive-byte, required-field, state-machine, or profile changes
  require a new version or profile name;
* database migrations are ordered and never edited after deployment.

Pydantic models generate canonical schemas under `schemas/v1`. Regeneration is
a test gate. Shared fixtures have a manifest that names each payload, expected
acceptance, expected bounded rejection code, and exact SHA-256 digest. Python
and Go run the same manifest. Python additionally checks semantic results and
bundle contents; Go checks the transport subset it is permitted to understand.

Accepted fixtures include every result and worker envelope plus a deterministic
bundle. Rejected fixtures cover unknown versions, unknown fields, invalid
digests, invalid enum values, outcome-shape conflicts, noncanonical archives,
and bundle limit boundaries. A fixture cannot be changed in place after its
contract version is released; a changed case gets a new fixture identity.

# `localize-v1`

`localize-v1` is the hosted form of the single-trace divergence rule. It takes
one run, not two, and answers where that run went irrecoverably wrong. No
reference run, no alignment, no inference, no model call.

It is the same rule `tracewake localize` runs locally and the same rule
[`align-v2`](align-v2.md) reports in its `divergence` field. The definition, the
measured accuracy, and the limits live in [`divergence.md`](divergence.md); this
document fixes only what the hosted contract commits to.

## Operation

One ready run in. `run_ids` has exactly one entry, and `profile` must be
`localize-v1`. The control plane rejects any other shape before a job exists.

## The rule

The run became unrecoverable at the first step that writes a file which is not
its own scratch file. The scratch file is the first path the run writes that it
has never read. A run that writes nothing outside its scratch file falls back to
step `min(12, step_count)`.

Parameters, pinned by `tests/test_profiles.py` and published as
`schemas/v1/localize-profile.schema.json`:

| Parameter | Value |
| --- | --- |
| `rule` | `first-nonscratch-write` |
| `create_markers` | `create`, `touch`, `new_file`, `write_file` |
| `scratch_fallback` | 12 |
| `long_trace` | 18 |

`scratch_fallback` is the only fitted number and is inert: sweeping it from 6 to
20 moves held-out exact match between 29.4% and 29.8%. `long_trace` is reused
from the alignment profile's long/short split rather than refitted.

## Result

`step` is one-based and never exceeds `step_count`. `reliability` is one of five
classes and `confidence` is the band it maps to:

| `reliability` | `confidence` |
| --- | --- |
| `commit-short` | high |
| `commit-long-single` | moderate |
| `commit-long-many` | low |
| `silent-short` | low |
| `silent-long` | very low |

The class is decided without labels, from whether the run committed at all and
whether the trace exceeds `long_trace`. It is part of the answer, not
decoration: `silent-long` is right about a tenth of the time and should be read
as "cannot localize" rather than as a step. Consumers that need to abstain drop
`silent-long`, which is about 14% of observed runs and carries most of the
error.

Bands are ordered, not calibrated. The ordering survives re-measurement; the
underlying percentages do not, and are deliberately not on the wire.

## Artifacts

One result artifact, `localize_result_json`, carrying the envelope, and one
companion, `localize_json`, carrying the same result plus the action text of the
reported step. A run with no steps is a permanent failure, not a retryable one.

## Versioning

The rule carries a version here because the hosted plane and every stored
artifact name it, and a stored result must keep meaning what it meant when it
was committed. Locally the same rule is unversioned: `tracewake localize` is
free to improve, and `contracts/divergence.md` is not a compatibility boundary.
A materially different rule gets `localize-v2`; this one does not change.

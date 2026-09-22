# `align-v2`

`align-v2` is the hosted alignment profile. Python implements it; other
languages carry its name and results without recreating its semantics. Any
behavior change requires another profile name.

The alignment profile couples global sequence alignment with single-trace divergence localization:
- **Pairwise Alignment**: Aligns execution traces using Gotoh dynamic programming with affine gap penalties. Aligned columns identify where runs match and where they diverge in behavior.
- **Divergence Localization**: The `divergence` field reports where the failing run went irrecoverably wrong using the single-trace commitment rule (defined in [`localize-v1.md`](localize-v1.md)), decoupling alignment column agreement from failure attribution.

The single-run form of the rule is a separate operation: see [`localize-v1.md`](localize-v1.md). `tests/test_profiles.py` pins all profile parameters, golden alignments, and divergence outputs against regression.

## Steps

Events are reduced to tool steps in insertion order. Tool calls with the same
parent model call form one partial-order batch and are sorted by `batch_index`.
A step contains tool name or batch names, target paths or queries, canonical
arguments, normalized model reasoning, and the cumulative set of written
`(path, content digest)` pairs.

Reasoning normalization replaces every whitespace run with one ASCII space and
strips the ends. Lexical tokenization uses `[A-Za-z0-9_./-]+` after lowercase
conversion. A blank reasoning string is represented by the token `.`. A shared
vocabulary is built across both runs, vectors contain token counts, and
reasoning similarity is cosine clamped to `[0, 1]`.

## Similarity

Step similarity is:

```text
0.45 tool + 0.25 arguments + 0.20 reasoning + 0.10 changed-files
```

Tool similarity is exact equality of the set of tool names. Changed-file
similarity is Jaccard similarity over cumulative `(path, digest)` pairs; two
empty sets score one.

Argument similarity is `0.70 target + 0.30 remaining arguments`.

* Identical values score one.
* Path targets compare common trailing path components divided by the larger
  component count.
* Query targets compare lowercase token-set Jaccard similarity.
* `old` and `new` values use Python `difflib.SequenceMatcher.ratio`.
* `around`, `at`, and numeric pairs score
  `max(0, 1 - abs(left-right)/50)`.
* Other values use `SequenceMatcher.ratio` on their string forms.
* A key missing on either side scores zero; remaining keys are averaged in
  lexical key order; two absent remaining sets score one.
* Batch targets use set Jaccard similarity for both target and remaining
  portions because there is no stable cross-batch argument pairing.

## Alignment

Similarity `s` becomes column score `2*s-1`. Global Gotoh alignment uses gap
open `-1.0` and gap extension `-0.2`.

The implementation's tie rules are part of the profile: the terminal matrix
prefers a match state; a match predecessor tie follows `Y`, then `X`, then `M`;
gap traceback stays in the same gap state on equality when another element
remains. Tool batches and input sequences retain their defined order.

## Divergence

The `divergence` field is the single-trace rule's answer, defined in
[`localize-v1.md`](localize-v1.md) and identical to what `tracewake localize`
reports for the failing run alone. It does not read the alignment. An empty
failing run has no step to report and the field is absent; the alignment is
still meaningful without one.

`reliability` and `confidence` carry that step's class and band, with the same
values and meaning as [`localize-v1.md`](localize-v1.md) gives them. All three
fields are present together or absent together: a step is not reportable
without the class that says how far to trust it, since the same rule lands
within two steps about nine times in ten on `commit-short` and one in ten on
`silent-long`. The HTML companion shows the class beside the step and warns
explicitly on `silent-long`.

Column agreement is defined strictly: aligned columns agree only when tool-name
sets and target sets are both equal. Aligned columns highlight where runs match
or diverge in behavior, while the reported divergence step is selected independently
by the commitment rule.

Length ratio is the longer step count divided by the shorter. A zero-length
side gives infinity unless both sides are empty. Ratios above four are reported
as low-confidence context but do not alter alignment.

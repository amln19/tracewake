# `lexical-v1`

`lexical-v1` is the sole initial hosted alignment profile. Python implements
it; other languages carry its name and results without recreating its
semantics. Any behavior change requires another profile name.

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

Aligned columns agree only when tool-name sets and target sets are both equal.
The reported divergence is the first one-based failing-run step after the last
agreeing column. If no column agrees, it is step one. If the runs agree again
through the end, there is no standing divergence.

A trailing run of at least two identical `(name, arguments)` failing steps is a
loop. Agreements inside that loop do not count as recovery. An empty failing
run is invalid for divergence selection.

Length ratio is the longer step count divided by the shorter. A zero-length
side gives infinity unless both sides are empty. Ratios above four are reported
as low-confidence context but do not alter alignment.

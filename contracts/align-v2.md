# `align-v2`

`align-v2` is the hosted alignment profile. Python implements it; other
languages carry its name and results without recreating its semantics. Any
behavior change requires another profile name.

**The alignment is frozen. The divergence readout is not the alignment's.**
Every parameter below — tokenization, weights, gap penalties, tie rules — is
unchanged from `align-v1` and does not change. What changed is which question
the `divergence` field answers.

`align-v1` read divergence off the last aligned column that agreed. That
answers where two runs stopped agreeing, which is not where the failing run
went wrong, and it is far weaker at the second question: within two steps of a
human label on 45 of 178 pairs, against 96 for the single-trace rule. The
`divergence` field now carries the single-trace rule's answer, which is what
`tracewake diff` already led with locally. The alignment columns still report
where the runs stopped agreeing; that is what they are for.

Because the field's meaning changed, the profile name changed with it. A stored
diff names the profile that produced it, so keeping the name would silently
reinterpret any result committed under it. The superseded readout is retained as
an evaluation baseline in `bench/`, under the name `last-target-agreement`, and
`contracts/divergence.md` records the comparison that justifies the swap.

`tests/test_profiles.py` pins these parameters, the golden alignment, and the
new readout against regression.

This profile was called `lexical-v1`, then `align-v1`, before first release.
Neither name was ever published, so each was corrected rather than aliased.

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
[`divergence.md`](divergence.md) and identical to what `tracewake localize`
reports for the failing run alone. It does not read the alignment. An empty
failing run has no step to report and the field is absent; the alignment is
still meaningful without one.

Column agreement remains defined — aligned columns agree only when tool-name
sets and target sets are both equal — because it is what the alignment display
uses to mark where the runs parted. It no longer selects the reported step.

The superseded readout, retained as `last-target-agreement` in `bench/`, was:
the first one-based failing-run step after the last agreeing column; step one if
no column agrees; absent if the runs agree through the end; and a trailing run
of at least two identical `(name, arguments)` failing steps counted as a loop
whose internal agreements were not recovery.

Length ratio is the longer step count divided by the shorter. A zero-length
side gives infinity unless both sides are empty. Ratios above four are reported
as low-confidence context but do not alter alignment.

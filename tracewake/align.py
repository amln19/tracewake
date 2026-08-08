"""Trajectory alignment: find where two agent runs stopped agreeing.

Gotoh affine-gap alignment over a weighted step distance. The distance weights
and the sub-constants below are frozen a priori — chosen before any hand label
was scored against the aligner — so measured accuracy is not a product of
tuning on the evaluation set.
"""

from __future__ import annotations

import math
import re
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from difflib import SequenceMatcher
from typing import Any

from .events import (
    FsWriteEvent,
    ModelCallEvent,
    StoredEvent,
    ToolCallEvent,
)
from .patches import TracewakeError

# ---------------------------------------------------------------------------
# Frozen a priori. Do not change after evaluation numbers are published.
# ---------------------------------------------------------------------------

WEIGHT_TOOL = 0.45
WEIGHT_ARGS = 0.25
WEIGHT_REASONING = 0.20
WEIGHT_FILES = 0.10

# Inside the argument component: the file or pattern the action aimed at carries
# most of the weight. Runs of the same task mostly part on *how* they touch a
# file, not on *which* file, so a comparison that treats the whole argument blob
# as equal/not-equal collapses into a first-difference check and has nothing to
# beat a positional baseline with.
WEIGHT_TARGET = 0.70
WEIGHT_ARG_REST = 0.30

# Column score from similarity s ∈ [0, 1]. Maps identity to +1 and unrelated
# steps to −1 with no extra offset to choose.
def score_from_similarity(s: float) -> float:
    return 2.0 * s - 1.0


GAP_OPEN = -1.0
GAP_EXTEND = -0.2

# A read fifty lines away is a different region of the file.
LINE_FALLOFF = 50.0

EMBEDDING_MODEL = "mlx-community/bge-small-en-v1.5-bf16"
EMBEDDING_REVISION = "0e415031434cdf5f1b89d584e11be33b82abfc8d"

# Divergence readout uses target-width agreement on aligned columns, not a
# threshold on the total similarity. Name-plus-target agreement and
# name-with-different-target can both land above any single cut on the weighted
# sum; the unit the run-to-run measurement validated is the predicate below.

_TOKEN = re.compile(r"[A-Za-z0-9_./-]+")


@dataclass(frozen=True)
class AlignConfig:
    """Knobs for ablations. Defaults are the frozen evaluation settings."""

    weight_tool: float = WEIGHT_TOOL
    weight_args: float = WEIGHT_ARGS
    weight_reasoning: float = WEIGHT_REASONING
    weight_files: float = WEIGHT_FILES
    weight_target: float = WEIGHT_TARGET
    weight_arg_rest: float = WEIGHT_ARG_REST
    gap_open: float = GAP_OPEN
    gap_extend: float = GAP_EXTEND
    # "weighted" = frozen target/rest split; "target_only" = ignore non-target
    # args; "strict" = binary equality on the full args dict.
    arg_mode: str = "weighted"

    def __post_init__(self) -> None:
        if self.arg_mode not in ("weighted", "target_only", "strict"):
            raise ValueError(
                f"unknown arg_mode {self.arg_mode!r}; use weighted, target_only, or strict"
            )


DEFAULT_CONFIG = AlignConfig()


def needleman_wunsch_config() -> AlignConfig:
    # Linear gaps: every gap cell costs the open penalty. Affine collapses to
    # this when open == extend.
    return AlignConfig(gap_open=GAP_OPEN, gap_extend=GAP_OPEN)


def drop_reasoning_config() -> AlignConfig:
    rest = WEIGHT_TOOL + WEIGHT_ARGS + WEIGHT_FILES
    return AlignConfig(
        weight_tool=WEIGHT_TOOL / rest,
        weight_args=WEIGHT_ARGS / rest,
        weight_reasoning=0.0,
        weight_files=WEIGHT_FILES / rest,
    )


def target_only_args_config() -> AlignConfig:
    return AlignConfig(arg_mode="target_only")


def strict_args_config() -> AlignConfig:
    return AlignConfig(arg_mode="strict")


@dataclass(frozen=True)
class Step:
    name: str
    args: dict[str, Any]
    target: str = ""
    reasoning: str = ""
    # (path, content-digest) pairs written up to and including this step.
    changed_files: frozenset[tuple[str, str]] = field(default_factory=frozenset)
    # Parallel batches are one step; names/targets are then multi-valued.
    batch_names: tuple[str, ...] = ()
    batch_targets: tuple[str, ...] = ()

    @property
    def names(self) -> frozenset[str]:
        return frozenset(self.batch_names) if self.batch_names else frozenset({self.name})

    @property
    def targets(self) -> frozenset[str]:
        if self.batch_targets:
            return frozenset(t for t in self.batch_targets if t)
        return frozenset({self.target}) if self.target else frozenset()


def target_of(args: dict[str, Any]) -> str:
    for field_name in ("path", "query"):
        if field_name in args:
            return str(args[field_name])
    return ""


def target_agree(a: Step, b: Step) -> bool:
    return a.names == b.names and a.targets == b.targets


@dataclass(frozen=True)
class StepTrace:
    """A step with the events it came from, for anything that reports on steps."""

    step: Step
    # Empty for a synthesized submit, which no model call produced.
    parent_call_id: str
    tools: tuple[ToolCallEvent, ...]


def extract_steps(
    events: Sequence[StoredEvent],
    *,
    append_submit: bool = False,
) -> list[Step]:
    return [t.step for t in extract_traces(events, append_submit=append_submit)]


def extract_traces(
    events: Sequence[StoredEvent],
    *,
    append_submit: bool = False,
) -> list[StepTrace]:
    """Build the alignment sequence from a run's event log.

    Insertion order, not `canonical_order`: the latter sorts by string
    `tool_call_id`, which puts step10 before step2. Parallel tool calls that
    share a parent model call collapse into one step — a batch is a partial
    order, and treating completion order as sequence would invent divergence.
    """
    models = {
        e.event.call_id: e.event
        for e in events
        if isinstance(e.event, ModelCallEvent)
    }
    writes_by_tool: dict[str, list[tuple[str, str]]] = {}
    tool_groups: dict[str, list[ToolCallEvent]] = {}
    group_order: list[str] = []

    for stored in events:
        event = stored.event
        if isinstance(event, FsWriteEvent) and event.tool_call_id:
            writes_by_tool.setdefault(event.tool_call_id, []).append(
                (event.path, event.content.digest)
            )
        elif isinstance(event, ToolCallEvent):
            key = event.parent_call_id
            if key not in tool_groups:
                tool_groups[key] = []
                group_order.append(key)
            tool_groups[key].append(event)

    changed: set[tuple[str, str]] = set()
    out: list[StepTrace] = []
    for parent_id in group_order:
        tools = sorted(tool_groups[parent_id], key=lambda t: t.batch_index)
        for tool in tools:
            changed.update(writes_by_tool.get(tool.tool_call_id, ()))
        parent = models.get(parent_id)
        reason = ""
        if parent is not None and parent.response.text:
            reason = " ".join(parent.response.text.split())
        files = frozenset(changed)
        if len(tools) == 1:
            tool = tools[0]
            args = dict(tool.args)
            step = Step(
                name=tool.name,
                args=args,
                target=target_of(args),
                reasoning=reason,
                changed_files=files,
            )
        else:
            names = tuple(t.name for t in tools)
            targets = tuple(target_of(dict(t.args)) for t in tools)
            step = Step(
                name="+".join(sorted(set(names))),
                args={"batch": [dict(t.args) for t in tools]},
                target="",
                reasoning=reason,
                changed_files=files,
                batch_names=names,
                batch_targets=targets,
            )
        out.append(StepTrace(step=step, parent_call_id=parent_id, tools=tuple(tools)))

    if append_submit:
        out.append(
            StepTrace(
                step=Step(
                    name="submit",
                    args={},
                    reasoning="",
                    changed_files=frozenset(changed),
                ),
                parent_call_id="",
                tools=(),
            )
        )
    return out


# ---------------------------------------------------------------------------
# Distance components
# ---------------------------------------------------------------------------


def path_similarity(a: str, b: str) -> float:
    if a == b:
        return 1.0
    if not a or not b:
        return 0.0
    pa = [p for p in a.split("/") if p]
    pb = [p for p in b.split("/") if p]
    if not pa or not pb:
        return 1.0 if a == b else 0.0
    common = 0
    for left, right in zip(reversed(pa), reversed(pb)):
        if left != right:
            break
        common += 1
    return common / max(len(pa), len(pb))


def token_jaccard(a: str, b: str) -> float:
    if a == b:
        return 1.0
    ta = set(_TOKEN.findall(a.lower()))
    tb = set(_TOKEN.findall(b.lower()))
    if not ta and not tb:
        return 1.0
    if not ta or not tb:
        return 0.0
    return len(ta & tb) / len(ta | tb)


def text_similarity(a: str, b: str) -> float:
    if a == b:
        return 1.0
    if not a or not b:
        return 0.0
    return SequenceMatcher(None, a, b).ratio()


def number_similarity(a: Any, b: Any) -> float:
    try:
        x = float(a)
        y = float(b)
    except (TypeError, ValueError):
        return text_similarity(str(a), str(b))
    return max(0.0, 1.0 - abs(x - y) / LINE_FALLOFF)


def jaccard_files(a: frozenset[tuple[str, str]], b: frozenset[tuple[str, str]]) -> float:
    # Two runs that have written nothing are in the same state.
    if not a and not b:
        return 1.0
    return len(a & b) / len(a | b)


def _value_similarity(key: str, a: Any, b: Any) -> float:
    if a == b:
        return 1.0
    if key in ("path",):
        return path_similarity(str(a), str(b))
    if key in ("query", "old", "new"):
        return token_jaccard(str(a), str(b)) if key == "query" else text_similarity(str(a), str(b))
    if key in ("around", "at"):
        return number_similarity(a, b)
    if isinstance(a, (int, float)) and isinstance(b, (int, float)):
        return number_similarity(a, b)
    return text_similarity(str(a), str(b))


def argument_similarity(
    a: Step, b: Step, *, config: AlignConfig = DEFAULT_CONFIG
) -> float:
    if config.arg_mode == "strict":
        return 1.0 if a.args == b.args else 0.0

    if a.batch_names or b.batch_names:
        # Set-valued steps: compare targets by Jaccard; remaining args ignored.
        ta, tb = a.targets, b.targets
        if not ta and not tb:
            target_sim = 1.0
        elif not ta or not tb:
            target_sim = 0.0
        else:
            target_sim = len(ta & tb) / len(ta | tb)
        if config.arg_mode == "target_only":
            return target_sim
        return config.weight_target * target_sim + config.weight_arg_rest * target_sim

    ta, tb = a.target, b.target
    if ta or tb:
        if "path" in a.args or "path" in b.args:
            target_sim = path_similarity(ta, tb)
        elif "query" in a.args or "query" in b.args:
            target_sim = token_jaccard(ta, tb)
        else:
            target_sim = 1.0 if ta == tb else 0.0
    else:
        target_sim = 1.0

    if config.arg_mode == "target_only":
        return target_sim

    keys = (set(a.args) | set(b.args)) - {"path", "query"}
    if not keys:
        rest_sim = 1.0
    else:
        parts = []
        for key in sorted(keys):
            if key not in a.args or key not in b.args:
                parts.append(0.0)
            else:
                parts.append(_value_similarity(key, a.args[key], b.args[key]))
        rest_sim = sum(parts) / len(parts)
    return config.weight_target * target_sim + config.weight_arg_rest * rest_sim


def tool_similarity(a: Step, b: Step) -> float:
    return 1.0 if a.names == b.names else 0.0


def cosine(u: Sequence[float], v: Sequence[float]) -> float:
    if len(u) != len(v):
        raise ValueError(
            f"embedding lengths differ ({len(u)} vs {len(v)}). The embedder must "
            f"return a fixed-width vector for every text."
        )
    dot = sum(x * y for x, y in zip(u, v))
    nu = math.sqrt(sum(x * x for x in u))
    nv = math.sqrt(sum(y * y for y in v))
    if nu == 0.0 or nv == 0.0:
        return 0.0
    return max(0.0, min(1.0, dot / (nu * nv)))


EmbedFn = Callable[[Sequence[str]], list[list[float]]]


def step_similarity(
    a: Step,
    b: Step,
    *,
    reasoning_vectors: tuple[Sequence[float], Sequence[float]] | None = None,
    config: AlignConfig = DEFAULT_CONFIG,
) -> float:
    parts = [
        config.weight_tool * tool_similarity(a, b),
        config.weight_args * argument_similarity(a, b, config=config),
        config.weight_files * jaccard_files(a.changed_files, b.changed_files),
    ]
    if config.weight_reasoning:
        if reasoning_vectors is None:
            # Empty↔empty needs no embedder. Anything else must supply vectors so
            # the frozen weight is real — SequenceMatcher is not a silent stand-in.
            if not a.reasoning and not b.reasoning:
                parts.append(config.weight_reasoning * 1.0)
            else:
                raise TracewakeError(
                    "alignment needs an embedder when steps carry reasoning text. "
                    "Pass embed=... (MlxEmbedder or LexicalEmbedder), or set "
                    "weight_reasoning=0 for an ablation."
                )
        else:
            parts.append(config.weight_reasoning * cosine(*reasoning_vectors))
    return sum(parts)


# ---------------------------------------------------------------------------
# Embeddings
# ---------------------------------------------------------------------------


class LexicalEmbedder:
    """Bag-of-words vectors for tests. Not the pinned evaluation embedder."""

    def __call__(self, texts: Sequence[str]) -> list[list[float]]:
        # Match MlxEmbedder: blank strings still need a stable non-zero vector so
        # empty↔empty reasoning scores 1.0 instead of cosine(0, 0) → 0.
        filled = [t if t.strip() else "." for t in texts]
        vocab: dict[str, int] = {}
        tokenized = [_TOKEN.findall(t.lower()) for t in filled]
        for tokens in tokenized:
            for tok in tokens:
                if tok not in vocab:
                    vocab[tok] = len(vocab)
        dim = max(len(vocab), 1)
        out: list[list[float]] = []
        for tokens in tokenized:
            vec = [0.0] * dim
            for tok in tokens:
                vec[vocab[tok]] += 1.0
            out.append(vec)
        return out


class MlxEmbedder:
    """Pinned local embedding model. Records model id + revision for the run."""

    def __init__(
        self,
        model_id: str = EMBEDDING_MODEL,
        revision: str = EMBEDDING_REVISION,
    ) -> None:
        try:
            from huggingface_hub import snapshot_download
            from mlx_embeddings import generate, load
        except ImportError as exc:
            # TracewakeError rather than ImportError so the CLI prints the one line
            # that says what to install instead of a traceback through it.
            raise TracewakeError(
                "Tracewake alignment needs the embeddings extra. Install with "
                "`uv sync --extra embeddings` (or `pip install 'tracewake[embeddings]'`), "
                "or pass --lexical to skip the model."
            ) from exc

        path = snapshot_download(model_id, revision=revision)
        self.model_id = model_id
        self.revision = revision
        self._model, self._tokenizer = load(path)
        self._generate = generate

    def __call__(self, texts: Sequence[str]) -> list[list[float]]:
        if not texts:
            return []
        # Empty strings still need a vector so index alignment stays 1:1.
        filled = [t if t.strip() else "." for t in texts]
        out = self._generate(self._model, self._tokenizer, list(filled))
        embeds = out.text_embeds
        return [list(map(float, row)) for row in embeds.tolist()]


# ---------------------------------------------------------------------------
# Gotoh (affine gaps)
# ---------------------------------------------------------------------------

Aligned = list[tuple[int | None, int | None]]


def gotoh(
    scores: list[list[float]],
    *,
    gap_open: float = GAP_OPEN,
    gap_extend: float = GAP_EXTEND,
) -> tuple[float, Aligned]:
    """Global alignment with affine gap costs.

    `scores[i][j]` is the substitution score for A[i] vs B[j]. Returns the
    total score and an alignment as pairs of indices (None = gap).
    """
    n = len(scores)
    m = len(scores[0]) if n else 0
    if n == 0 and m == 0:
        return (0.0, [])
    if n == 0:
        return (gap_open + (m - 1) * gap_extend, [(None, j) for j in range(m)])
    if m == 0:
        return (gap_open + (n - 1) * gap_extend, [(i, None) for i in range(n)])

    neg = -1e30
    # M: match/mismatch; X: gap in B (consume A); Y: gap in A (consume B).
    M = [[neg] * (m + 1) for _ in range(n + 1)]
    X = [[neg] * (m + 1) for _ in range(n + 1)]
    Y = [[neg] * (m + 1) for _ in range(n + 1)]
    M[0][0] = 0.0
    for i in range(1, n + 1):
        X[i][0] = gap_open + (i - 1) * gap_extend
    for j in range(1, m + 1):
        Y[0][j] = gap_open + (j - 1) * gap_extend

    for i in range(1, n + 1):
        for j in range(1, m + 1):
            s = scores[i - 1][j - 1]
            M[i][j] = max(M[i - 1][j - 1], X[i - 1][j - 1], Y[i - 1][j - 1]) + s
            X[i][j] = max(M[i - 1][j] + gap_open, X[i - 1][j] + gap_extend)
            Y[i][j] = max(M[i][j - 1] + gap_open, Y[i][j - 1] + gap_extend)

    # Traceback from the best of the three terminals.
    i, j = n, m
    best = max(M[i][j], X[i][j], Y[i][j])
    state = "M" if M[i][j] == best else ("X" if X[i][j] == best else "Y")
    pairs: Aligned = []
    while i > 0 or j > 0:
        if i > 0 and j > 0 and state == "M":
            pairs.append((i - 1, j - 1))
            prev = max(
                (M[i - 1][j - 1], "M"),
                (X[i - 1][j - 1], "X"),
                (Y[i - 1][j - 1], "Y"),
            )[1]
            i -= 1
            j -= 1
            state = prev
        elif i > 0 and state == "X":
            pairs.append((i - 1, None))
            # Prefer staying in X when scores tie, so an excursion is one run.
            if X[i - 1][j] + gap_extend >= M[i - 1][j] + gap_open and i > 1:
                state = "X"
            else:
                state = "M"
            i -= 1
            if i == 0:
                state = "Y" if j > 0 else state
        elif j > 0:
            pairs.append((None, j - 1))
            if j > 1 and Y[i][j - 1] + gap_extend >= M[i][j - 1] + gap_open:
                state = "Y"
            else:
                state = "M"
            j -= 1
            if j == 0 and i > 0:
                state = "X"
        else:
            break
    pairs.reverse()
    return (best, pairs)


def _score_matrix(
    a: Sequence[Step],
    b: Sequence[Step],
    embed: EmbedFn | None,
    config: AlignConfig = DEFAULT_CONFIG,
) -> list[list[float]]:
    if not a or not b:
        return [[0.0] for _ in a] if a and not b else []

    reasons_a = [s.reasoning for s in a]
    reasons_b = [s.reasoning for s in b]
    vecs_a: list[list[float]] | None = None
    vecs_b: list[list[float]] | None = None
    if embed is not None and config.weight_reasoning:
        # One call for both sides so a bag-of-words embedder shares a vocabulary
        # and a real model keeps a single batch.
        combined = embed([*reasons_a, *reasons_b])
        if len(combined) != len(reasons_a) + len(reasons_b):
            raise ValueError(
                f"embedder returned {len(combined)} vectors for "
                f"{len(reasons_a) + len(reasons_b)} texts"
            )
        vecs_a = combined[: len(reasons_a)]
        vecs_b = combined[len(reasons_a) :]

    matrix: list[list[float]] = []
    for i, left in enumerate(a):
        row: list[float] = []
        for j, right in enumerate(b):
            vectors = None
            if vecs_a is not None and vecs_b is not None:
                vectors = (vecs_a[i], vecs_b[j])
            sim = step_similarity(
                left, right, reasoning_vectors=vectors, config=config
            )
            row.append(score_from_similarity(sim))
        matrix.append(row)
    return matrix


def align(
    a: Sequence[Step],
    b: Sequence[Step],
    *,
    embed: EmbedFn | None = None,
    config: AlignConfig = DEFAULT_CONFIG,
) -> tuple[float, Aligned, list[list[float]]]:
    if not a or not b:
        # Same gap accounting as gotoh's empty-side branches; skip the matrix.
        if not a and not b:
            return (0.0, [], [])
        if not a:
            n = len(b)
            score = config.gap_open + (n - 1) * config.gap_extend
            return (score, [(None, j) for j in range(n)], [])
        n = len(a)
        score = config.gap_open + (n - 1) * config.gap_extend
        return (score, [(i, None) for i in range(n)], [])
    scores = _score_matrix(a, b, embed, config=config)
    total, pairs = gotoh(
        scores, gap_open=config.gap_open, gap_extend=config.gap_extend
    )
    return (total, pairs, scores)


def _trailing_identical_loop_start(steps: Sequence[Step]) -> int | None:
    """0-based index of a trailing run of 2+ identical (name, args) steps, else None.

    Matches the labeling rule: a run that ends by repeating an action with the
    same arguments is looping, and the loop is not recovery.
    """
    if len(steps) < 2:
        return None
    last = steps[-1]
    start = len(steps) - 1
    while start > 0 and (
        steps[start - 1].name == last.name and steps[start - 1].args == last.args
    ):
        start -= 1
    if start == len(steps) - 1:
        return None
    return start


def divergence_step(
    alignment: Aligned,
    good: Sequence[Step],
    bad: Sequence[Step],
) -> int | None:
    """1-based index on the failure (`bad`) side, or None if they re-align through the end.

    Walk the alignment to the last column that agrees at target width, then take
    the first failure-side step after it. Never agreed → step 1. Trailing region
    empty because they recovered → None (the product reports no standing
    divergence; evaluation maps that to the last failure step, matching the
    labeling rule for a run that was only doomed at the end).

    Agreements whose failure-side step sits inside a trailing identical-arg loop
    do not count as recovery: a stuck `run_tests()` repeated against several
    separate real tests on the other side would otherwise look like sustained
    re-alignment. A single shared terminal action (no loop) is unchanged — that
    case still needs the ending stripped before diffing. Requiring 2+ agreeing
    *columns* was tried and reverted; it broke real one-column recoveries and
    did not fix the loop case.
    """
    if not bad:
        raise ValueError("the failure run has no steps to locate a divergence in")

    loop_start = _trailing_identical_loop_start(bad)
    last_agree = -1
    for k, (i, j) in enumerate(alignment):
        if i is None or j is None:
            continue
        if loop_start is not None and j >= loop_start:
            continue
        if target_agree(good[i], bad[j]):
            last_agree = k

    for k in range(last_agree + 1, len(alignment)):
        _, j = alignment[k]
        if j is not None:
            return j + 1

    if last_agree < 0:
        return 1
    return None


@dataclass(frozen=True)
class DiffResult:
    good_steps: list[Step]
    bad_steps: list[Step]
    alignment: Aligned
    score: float
    # None when the traces re-align through the end at target width.
    divergence: int | None
    length_ratio: float
    embedding_model: str | None = None
    embedding_revision: str | None = None
    # scores[i][j] for the aligned columns, so a report can show how strongly a
    # column matched rather than only whether it agreed at target width.
    scores: list[list[float]] = field(default_factory=list)

    def column_similarity(self, i: int | None, j: int | None) -> float | None:
        if i is None or j is None or not self.scores:
            return None
        return (self.scores[i][j] + 1.0) / 2.0

    @property
    def excluded_by_length(self) -> bool:
        return self.length_ratio > 4.0

    @property
    def divergence_or_end(self) -> int:
        """Index used for scoring against hand labels that always name a step."""
        return self.divergence if self.divergence is not None else len(self.bad_steps)


def length_ratio(a: Sequence[Step], b: Sequence[Step]) -> float:
    shorter = min(len(a), len(b))
    if shorter == 0:
        return float("inf") if max(len(a), len(b)) else 1.0
    return max(len(a), len(b)) / shorter


def diff_runs(
    good_events: Sequence[StoredEvent],
    bad_events: Sequence[StoredEvent],
    *,
    embed: EmbedFn | None = None,
    append_submit_good: bool = False,
    append_submit_bad: bool = False,
    embedding_model: str | None = None,
    embedding_revision: str | None = None,
) -> DiffResult:
    good = extract_steps(good_events, append_submit=append_submit_good)
    bad = extract_steps(bad_events, append_submit=append_submit_bad)
    total, pairs, scores = align(good, bad, embed=embed)
    return DiffResult(
        good_steps=good,
        bad_steps=bad,
        alignment=pairs,
        score=total,
        divergence=divergence_step(pairs, good, bad),
        length_ratio=length_ratio(good, bad),
        embedding_model=embedding_model,
        embedding_revision=embedding_revision,
        scores=scores,
    )


def format_diff(result: DiffResult, *, good_label: str = "GOOD", bad_label: str = "BAD") -> str:
    if result.divergence is None:
        head = f"no standing divergence — traces re-align through the end ({bad_label})"
    else:
        head = f"divergence at {bad_label} step {result.divergence}"
    lines = [
        head,
        f"alignment score {result.score:.3f}  length ratio {result.length_ratio:.2f}",
    ]
    if result.embedding_model:
        lines.append(
            f"embeddings {result.embedding_model}@{result.embedding_revision or 'unpinned'}"
        )
    if result.excluded_by_length:
        lines.append(
            "warning: length ratio exceeds 4:1; gap cost dominates and the "
            "divergence index may be meaningless"
        )
    lines.append("")
    header = f"{'':>4}  {good_label:<40}  {bad_label}"
    lines.append(header)
    lines.append("-" * len(header))

    for i, j in result.alignment:
        mark = " "
        if i is not None and j is not None and target_agree(
            result.good_steps[i], result.bad_steps[j]
        ):
            mark = "="
        elif i is not None and j is not None:
            mark = "|"
        else:
            mark = " "

        def cell(idx: int | None, steps: Sequence[Step], is_div: bool) -> str:
            if idx is None:
                return "—"
            step = steps[idx]
            target = f" → {step.target}" if step.target else ""
            text = f"{idx + 1}. {step.name}{target}"
            if is_div:
                text = f">>> {text}"
            return text[:40]

        bad_div = (
            result.divergence is not None
            and j is not None
            and (j + 1) == result.divergence
        )
        left = cell(i, result.good_steps, False)
        right = cell(j, result.bad_steps, bad_div)
        lines.append(f"{mark:>4}  {left:<40}  {right}")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Baselines (no DP)
# ---------------------------------------------------------------------------


def first_target_difference(good: Sequence[Step], bad: Sequence[Step]) -> int:
    """Baseline (a): first positional target-width disagreement, 1-based on bad."""
    n = min(len(good), len(bad))
    for i in range(n):
        if not target_agree(good[i], bad[i]):
            return i + 1
    if len(good) != len(bad):
        return n + 1 if n < len(bad) else max(len(bad), 1)
    return len(bad)


def last_common_prefix(good: Sequence[Step], bad: Sequence[Step]) -> int:
    """Baseline (b): last failure step of the agreeing prefix, or 1 if none."""
    n = min(len(good), len(bad))
    agreed = 0
    for i in range(n):
        if not target_agree(good[i], bad[i]):
            break
        agreed = i + 1
    if agreed == 0:
        return 1
    return agreed

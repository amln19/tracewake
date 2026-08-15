"""Score divergence methods on the partitioned OpenHands transfer set.

Development and held-out halves are scored by the same code and written to
separate sheets. The two constant competitors are fitted on the development
half and frozen to a file before the held-out half is read, so that the number
they earn on it is a prediction rather than a fit.

An oracle constant is also printed. It is fitted on whichever labels are being
scored and is a ceiling diagnostic, never a baseline.
"""

from __future__ import annotations

import json
import statistics
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path

from tracewake.align import (
    LexicalEmbedder,
    Step,
    align,
    divergence_step,
    first_target_difference,
    last_common_prefix,
)
from tracewake.diverge import (
    RELIABILITY_ACCURACY,
    commitment_steps,
    earliest_bound,
    first_commitment,
    reliability,
)

from .aligneval import mcnemar, median_abs_error, oracle_constant, within_tol
from .external import (
    EXTERNAL_LABEL_ROOT,
    OPENHANDS_DATASET,
    iter_openhands_rows,
    strip_terminal,
    to_steps,
)
from .partition import load_split, read_partition
from .repos import CORPUS_ROOT, corpus_metadata_path

FITTED_PATH = CORPUS_ROOT / "alignment" / "dev-fitted.json"
SHEET_DIR = CORPUS_ROOT / "alignment"
LENGTH_CUT = 18

Method = Callable[[Sequence[Step], Sequence[Step]], int]


@dataclass(frozen=True)
class Prediction:
    packet_id: str
    instance_id: str
    label: int
    failure_steps: int
    values: dict[str, int]
    reliability: str
    n_commitments: int


def _embedder() -> LexicalEmbedder:
    return LexicalEmbedder()


def m_lexical_v1(good: Sequence[Step], bad: Sequence[Step]) -> int:
    _, aligned, _ = align(list(good), list(bad), embed=_embedder())
    found = divergence_step(aligned, good, bad)
    return found if found is not None else len(bad)


def m_earliest_bound(good: Sequence[Step], bad: Sequence[Step]) -> int:
    return earliest_bound(bad)


def m_first_commitment(good: Sequence[Step], bad: Sequence[Step]) -> int:
    found = first_commitment(bad)
    return found if found is not None else len(bad)


def m_first_difference(good: Sequence[Step], bad: Sequence[Step]) -> int:
    return first_target_difference(good, bad)


def m_last_common_prefix(good: Sequence[Step], bad: Sequence[Step]) -> int:
    return last_common_prefix(good, bad)


def constant_method(k: int) -> Method:
    def f(good: Sequence[Step], bad: Sequence[Step]) -> int:
        return max(1, min(len(bad), k))

    return f


def proportional_method(c: float) -> Method:
    """Failure length times a fitted fraction.

    Worth beating rather than assuming: on development data the hand label
    correlates 0.84 with the failing run's length, so a method that only tracks
    length can look strong without localising anything.
    """

    def f(good: Sequence[Step], bad: Sequence[Step]) -> int:
        return max(1, min(len(bad), round(c * len(bad))))

    return f


# ---------------------------------------------------------------------------
# pairs
# ---------------------------------------------------------------------------


def load_pairs(packet_ids: Sequence[str], *, rows=None) -> list[tuple[dict, list[Step], list[Step]]]:
    keys = [
        json.loads(line)
        for line in (EXTERNAL_LABEL_ROOT / "key.jsonl").read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    wanted = {k["packet_id"]: k for k in keys if k["packet_id"] in set(packet_ids)}
    by_id: dict[tuple[str, str], dict] = {}
    for row in rows if rows is not None else iter_openhands_rows():
        by_id[(row["instance_id"], row["run_id"])] = row
    out = []
    for packet_id in sorted(wanted):
        key = wanted[packet_id]
        win = by_id.get((key["instance_id"], key["good_run_id"]))
        loss = by_id.get((key["instance_id"], key["bad_run_id"]))
        if win is None or loss is None:
            raise KeyError(f"packet {packet_id} names runs not in {OPENHANDS_DATASET}")
        out.append(
            (
                key,
                strip_terminal(to_steps(win["messages"], shell_verbs=True)),
                strip_terminal(to_steps(loss["messages"], shell_verbs=True)),
            )
        )
    return out


# ---------------------------------------------------------------------------
# fitting the constants (development half only)
# ---------------------------------------------------------------------------


def fit_constants(rows=None) -> dict:
    labels = load_split()
    pairs = load_pairs(sorted(labels), rows=rows)
    ys = [labels[key["packet_id"]] for key, _, _ in pairs]
    lens = [len(bad) for _, _, bad in pairs]

    def best(candidates, predict):
        scored = []
        for value in candidates:
            preds = [predict(value, n) for n in lens]
            hits = sum(1 for p, y in zip(preds, ys, strict=True) if within_tol(p, y))
            mae = statistics.mean(abs(p - y) for p, y in zip(preds, ys, strict=True))
            scored.append((hits, -mae, value))
        return max(scored)[2]

    k = best(range(1, max(lens) + 1), lambda v, n: max(1, min(n, v)))
    c = best(
        [round(0.02 * i, 2) for i in range(5, 51)],
        lambda v, n: max(1, min(n, round(v * n))),
    )
    return {
        "constant_k": int(k),
        "proportional_c": float(c),
        "fitted_on": "development half of the OpenHands transfer partition",
        "n": len(ys),
    }


def write_fitted(path: Path = FITTED_PATH, rows=None) -> Path:
    if path.exists():
        raise FileExistsError(
            f"{path} already exists. The competing constants are fitted once, on "
            f"development data, and frozen; refitting them after seeing held-out "
            f"results would make them oracles."
        )
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(fit_constants(rows=rows), indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    return path


def read_fitted(path: Path = FITTED_PATH) -> dict:
    if not path.is_file():
        raise FileNotFoundError(
            f"no fitted constants at {path}. Run `python -m bench diverge-eval "
            f"--fit` on the development half first."
        )
    return json.loads(path.read_text(encoding="utf-8"))


# ---------------------------------------------------------------------------
# scoring
# ---------------------------------------------------------------------------


def methods(fitted: dict) -> dict[str, Method]:
    return {
        "earliest_bound": m_earliest_bound,
        "first_commitment": m_first_commitment,
        "lexical-v1": m_lexical_v1,
        "first-difference": m_first_difference,
        "last-common-prefix": m_last_common_prefix,
        f"constant-{fitted['constant_k']}": constant_method(fitted["constant_k"]),
        f"proportional-{fitted['proportional_c']}": proportional_method(
            fitted["proportional_c"]
        ),
    }


def predict(split_final: bool, *, rows=None) -> tuple[list[Prediction], dict]:
    labels = load_split(final=split_final)
    fitted = read_fitted()
    fns = methods(fitted)
    out: list[Prediction] = []
    for key, good, bad in load_pairs(sorted(labels), rows=rows):
        out.append(
            Prediction(
                packet_id=key["packet_id"],
                instance_id=key["instance_id"],
                label=labels[key["packet_id"]],
                failure_steps=len(bad),
                values={name: fn(good, bad) for name, fn in fns.items()},
                reliability=reliability(bad),
                n_commitments=len(commitment_steps(bad)),
            )
        )
    return out, fitted


def _group(preds: Sequence[Prediction], name: str, primary: str) -> list[str]:
    if not preds:
        return [f"\n{name}: (empty)"]
    labels = [p.label for p in preds]
    n = len(preds)
    lines = [f"\n{name}: n={n}"]
    lines.append(
        f"  {'method':<24}{'exact':>7}{'±1':>6}{'±2':>6}{'meanAE':>9}{'medAE':>8}"
    )
    hits_by: dict[str, list[bool]] = {}
    for method in preds[0].values:
        values = [p.values[method] for p in preds]
        hits = [within_tol(v, y) for v, y in zip(values, labels, strict=True)]
        hits_by[method] = hits
        lines.append(
            f"  {method:<24}"
            f"{sum(1 for v, y in zip(values, labels, strict=True) if v == y):>7}"
            f"{sum(1 for v, y in zip(values, labels, strict=True) if abs(v - y) <= 1):>6}"
            f"{sum(hits):>6}"
            f"{statistics.mean(abs(v - y) for v, y in zip(values, labels, strict=True)):>9.2f}"
            f"{median_abs_error(values, labels):>8.1f}"
        )
    k = oracle_constant(labels)
    k_hits = sum(1 for y in labels if within_tol(k, y))
    lines.append(
        f"  {'oracle-constant':<24}{sum(1 for y in labels if y == k):>7}"
        f"{sum(1 for y in labels if abs(k - y) <= 1):>6}{k_hits:>6}"
        f"{statistics.mean(abs(k - y) for y in labels):>9.2f}"
        f"{median_abs_error([k] * n, labels):>8.1f}   (k={k}; fitted on these "
        f"labels — ceiling diagnostic, not a baseline)"
    )
    for method, other_hits in hits_by.items():
        if method == primary:
            continue
        b_only, a_only, p = mcnemar(hits_by[primary], other_hits)
        p_txt = f"{p:.3f}" if p is not None else "n/a"
        lines.append(
            f"  McNemar {primary} vs {method}: {primary}-only {a_only}, "
            f"other-only {b_only}, n_discordant {a_only + b_only}, p={p_txt}"
        )
    return lines


def bootstrap_gap(
    preds: Sequence[Prediction], a: str, b: str, *, draws: int = 2000, seed: int = 7
) -> tuple[float, float]:
    """Percentile interval for the ±2 accuracy gap between two methods."""
    import random

    rng = random.Random(seed)
    n = len(preds)
    gaps = []
    for _ in range(draws):
        sample = [preds[rng.randrange(n)] for _ in range(n)]
        ha = sum(1 for p in sample if within_tol(p.values[a], p.label))
        hb = sum(1 for p in sample if within_tol(p.values[b], p.label))
        gaps.append((ha - hb) / n)
    gaps.sort()
    lo = gaps[int(0.025 * draws)]
    hi = gaps[min(draws - 1, int(0.975 * draws))]
    return (lo, hi)


def report(preds: Sequence[Prediction], fitted: dict, title: str) -> str:
    primary = "earliest_bound"
    lines = [
        title,
        f"source {OPENHANDS_DATASET}",
        (
            f"competing constants fitted on {fitted['n']} development pairs: "
            f"k={fitted['constant_k']}, c={fitted['proportional_c']}"
        ),
    ]
    lines += _group(preds, "all pairs", primary)
    lines += _group(
        [p for p in preds if p.failure_steps <= LENGTH_CUT],
        f"short failures (<= {LENGTH_CUT} steps)",
        primary,
    )
    lines += _group(
        [p for p in preds if p.failure_steps > LENGTH_CUT],
        f"long failures (> {LENGTH_CUT} steps)",
        primary,
    )
    lines.append("")
    lines.append("earliest_bound by the reliability class it reports:")
    for level in RELIABILITY_ACCURACY:
        group = [p for p in preds if p.reliability == level]
        if not group:
            lines.append(f"  {level:<22} n=0")
            continue
        hits = sum(1 for p in group if within_tol(p.values[primary], p.label))
        lines.append(
            f"  {level:<22} n={len(group):>3}  within±2 {hits}/{len(group)} "
            f"({hits / len(group):.0%})  meanAE "
            f"{statistics.mean(abs(p.values[primary] - p.label) for p in group):.2f}"
        )
    lines.append("")
    for other in ("lexical-v1", "first-difference"):
        lo, hi = bootstrap_gap(preds, primary, other)
        lines.append(
            f"bootstrap 95% interval for the ±2 gap over {other}: "
            f"[{lo:+.1%}, {hi:+.1%}]"
        )
    return "\n".join(lines)


def write_sheet(preds: Sequence[Prediction], fitted: dict, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as fh:
        fh.write(
            json.dumps(
                {
                    "_meta": {
                        "source": OPENHANDS_DATASET,
                        "n": len(preds),
                        "partition": corpus_metadata_path(
                            CORPUS_ROOT / "alignment" / "partition.json"
                        ),
                        "fitted": fitted,
                    }
                },
                sort_keys=True,
            )
            + "\n"
        )
        for p in preds:
            fh.write(
                json.dumps(
                    {
                        "packet_id": p.packet_id,
                        "instance_id": p.instance_id,
                        "label": p.label,
                        "failure_steps": p.failure_steps,
                        "reliability": p.reliability,
                        "n_commitments": p.n_commitments,
                        **p.values,
                    },
                    sort_keys=True,
                )
                + "\n"
            )


def evaluate(*, final: bool = False, rows=None) -> str:
    split = read_partition()
    which = "held-out" if final else "development"
    preds, fitted = predict(final, rows=rows)
    path = SHEET_DIR / f"predictions-{'final' if final else 'dev'}.jsonl"
    write_sheet(preds, fitted, path)
    title = (
        f"divergence evaluation — {which} half "
        f"({len(split.final if final else split.dev)} packets), sheet {path.name}"
    )
    return report(preds, fitted, title)



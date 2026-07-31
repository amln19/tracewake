"""Evaluate the aligner against hand labels and the named baselines.

Pair selection is independent of every method score. Weights are frozen a priori.
Per-pair predictions are written to a sheet; aggregate metrics are what you print.
Do not open the per-pair sheet during a labeling pass.
"""

from __future__ import annotations

import json
import math
import statistics
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

from locus import Store
from locus.align import (
    EMBEDDING_MODEL,
    EMBEDDING_REVISION,
    LexicalEmbedder,
    MlxEmbedder,
    Step,
    align,
    divergence_step,
    extract_steps,
    first_target_difference,
    last_common_prefix,
)

from .fidelity import ledger_rows
from .label import LABEL_ROOT, SELECT_SEED, SelectedPair, select_pairs
from .repos import CORPUS_ROOT
from .runner import LEDGER, STORE

PRED_ROOT = CORPUS_ROOT / "alignment"
PRED_SHEET = PRED_ROOT / "predictions.jsonl"


@dataclass(frozen=True)
class PairPred:
    task_id: str
    good_run: str
    bad_run: str
    failure_steps: int
    length_ratio: float
    under_3_to_1: bool
    aligner: int
    baseline_a: int
    baseline_b: int
    baseline_llm: int | None
    label: int | None
    note: str = ""

    def row(self) -> dict:
        return {
            "task_id": self.task_id,
            "good_run": self.good_run,
            "bad_run": self.bad_run,
            "failure_steps": self.failure_steps,
            "length_ratio": self.length_ratio,
            "under_3_to_1": self.under_3_to_1,
            "aligner": self.aligner,
            "baseline_a": self.baseline_a,
            "baseline_b": self.baseline_b,
            "baseline_llm": self.baseline_llm,
            "label": self.label,
            "note": self.note,
        }


def _load_labels(path: Path) -> dict[str, int]:
    if not path.exists():
        return {}
    out: dict[str, int] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        row = json.loads(line)
        if row.get("label") is None:
            continue
        out[row["packet_id"]] = int(row["label"])
    return out


def _key_by_packet(path: Path = LABEL_ROOT / "key.jsonl") -> dict[str, dict]:
    rows = [
        json.loads(line)
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    return {r["packet_id"]: r for r in rows}


def trajectory_for(
    store: Store,
    run_id: str,
    stop_reason: str | None,
) -> list[Step]:
    return extract_steps(
        store.events(run_id),
        append_submit=stop_reason == "submitted",
    )


def predict_pair(
    store: Store,
    pair: SelectedPair,
    stops: dict[str, str],
    embed,
) -> tuple[list[Step], list[Step], int, int, int]:
    good = trajectory_for(store, pair.good_run, stops[pair.good_run])
    bad = trajectory_for(store, pair.bad_run, stops[pair.bad_run])
    if len(bad) != pair.bad_actions:
        # key.jsonl records failure_steps after anonymize; bad_actions is the
        # pre-export count from select_pairs and must match extraction here.
        pass
    _, pairs, _ = align(good, bad, embed=embed)
    aligned = divergence_step(pairs, good, bad)
    pred = aligned if aligned is not None else len(bad)
    return (
        good,
        bad,
        pred,
        first_target_difference(good, bad),
        last_common_prefix(good, bad),
    )


def run_predictions(
    store: Path = STORE,
    ledger: Path = LEDGER,
    out: Path = PRED_SHEET,
    *,
    lexical: bool = False,
    labels_path: Path | None = None,
) -> list[PairPred]:
    selected = select_pairs(store, ledger, SELECT_SEED)
    rows = {r["run_id"]: r for r in ledger_rows(ledger)}
    stops = {rid: r["stop_reason"] for rid, r in rows.items()}
    key = _key_by_packet()
    packet_for_task = {v["task_id"]: k for k, v in key.items()}
    labels = _load_labels(labels_path) if labels_path else {}

    if lexical:
        embed = LexicalEmbedder()
    else:
        embed = MlxEmbedder(EMBEDDING_MODEL, EMBEDDING_REVISION)

    db = Store(store)
    preds: list[PairPred] = []
    try:
        for pair in selected:
            good, bad, aligner, base_a, base_b = predict_pair(store=db, pair=pair, stops=stops, embed=embed)
            packet_id = packet_for_task.get(pair.task_id)
            key_row = key.get(packet_id or "", {})
            failure_steps = key_row.get("failure_steps", len(bad))
            if len(bad) != failure_steps:
                raise RuntimeError(
                    f"{pair.task_id}: extracted {len(bad)} failure steps but the "
                    f"labeling packet had {failure_steps}. Index agreement with "
                    f"hand labels is impossible until extraction matches export."
                )
            preds.append(
                PairPred(
                    task_id=pair.task_id,
                    good_run=pair.good_run,
                    bad_run=pair.bad_run,
                    failure_steps=failure_steps,
                    length_ratio=pair.length_ratio,
                    under_3_to_1=pair.length_ratio <= 3.0,
                    aligner=aligner,
                    baseline_a=base_a,
                    baseline_b=base_b,
                    baseline_llm=None,
                    label=labels.get(packet_id) if packet_id else None,
                )
            )
    finally:
        db.close()

    out.parent.mkdir(parents=True, exist_ok=True)
    with out.open("w", encoding="utf-8") as fh:
        meta = {
            "embedding_model": None if lexical else EMBEDDING_MODEL,
            "embedding_revision": None if lexical else EMBEDDING_REVISION,
            "lexical": lexical,
            "n_pairs": len(preds),
        }
        fh.write(json.dumps({"_meta": meta}, sort_keys=True) + "\n")
        for pred in preds:
            fh.write(json.dumps(pred.row(), sort_keys=True) + "\n")
    return preds


def within_tol(pred: int, label: int, tol: int = 2) -> bool:
    return abs(pred - label) <= tol


def median_abs_error(preds: Sequence[int], labels: Sequence[int]) -> float:
    return float(statistics.median(abs(p - y) for p, y in zip(preds, labels)))


def mcnemar(a_hits: Sequence[bool], b_hits: Sequence[bool]) -> tuple[int, int, float | None]:
    """Return (b_only, a_only, two-sided exact p) for discordant pairs.

    `a_hits` is the aligner; `b_hits` is the baseline. b_only = baseline right,
    aligner wrong; a_only = aligner right, baseline wrong.
    """
    a_only = b_only = 0
    for a, b in zip(a_hits, b_hits):
        if a and not b:
            a_only += 1
        elif b and not a:
            b_only += 1
    n = a_only + b_only
    if n == 0:
        return (b_only, a_only, None)
    # Exact binomial two-sided: P(X <= min) * 2, capped at 1.
    k = min(a_only, b_only)
    p = sum(math.comb(n, i) for i in range(k + 1)) / (2**n)
    p = min(1.0, 2.0 * p)
    return (b_only, a_only, p)


def oracle_constant(labels: Sequence[int]) -> int:
    """Best single index on this label set — a ceiling diagnostic, not a baseline."""
    if not labels:
        raise ValueError("no labels")
    best = labels[0]
    best_hits = -1
    for candidate in range(1, max(labels) + 1):
        hits = sum(1 for y in labels if within_tol(candidate, y))
        if hits > best_hits:
            best_hits = hits
            best = candidate
    return best


def score(
    preds: Sequence[PairPred],
    *,
    require_labels: bool = True,
) -> str:
    labeled = [p for p in preds if p.label is not None]
    if require_labels and not labeled:
        return (
            f"no labels on {len(preds)} predictions. Pass a label sheet or run "
            f"labeling first. Per-pair sheet is safe to keep closed during a pass."
        )

    def metrics(group: Sequence[PairPred], name: str) -> list[str]:
        if not group:
            return [f"{name}: (empty)"]
        labels = [p.label for p in group if p.label is not None]
        assert len(labels) == len(group)
        methods: dict[str, list[int]] = {
            "aligner": [p.aligner for p in group],
            "baseline_a": [p.baseline_a for p in group],
            "baseline_b": [p.baseline_b for p in group],
        }
        if any(p.baseline_llm is not None for p in group):
            methods["baseline_llm"] = [
                p.baseline_llm if p.baseline_llm is not None else -10**9 for p in group
            ]
        lines = [f"{name}: n={len(group)}"]
        hits_by: dict[str, list[bool]] = {}
        for method, values in methods.items():
            hits = [within_tol(v, y) for v, y in zip(values, labels)]
            hits_by[method] = hits
            mae = median_abs_error(values, labels)
            n_hit = sum(hits)
            lines.append(
                f"  {method:<12} within±2 {n_hit}/{len(group)} "
                f"({n_hit / len(group):.1%})  median |err| {mae:.1f}"
            )
        oracle = oracle_constant(labels)
        oracle_hits = sum(1 for y in labels if within_tol(oracle, y))
        lines.append(
            f"  {'oracle_k':<12} within±2 {oracle_hits}/{len(group)} "
            f"({oracle_hits / len(group):.1%})  (best constant k={oracle}; "
            f"ceiling diagnostic, fit on these labels)"
        )
        for baseline in ("baseline_a", "baseline_b", "baseline_llm"):
            if baseline not in hits_by:
                continue
            b_only, a_only, p = mcnemar(hits_by["aligner"], hits_by[baseline])
            p_txt = f"{p:.3f}" if p is not None else "n/a"
            lines.append(
                f"  McNemar vs {baseline}: aligner-only {a_only}, "
                f"baseline-only {b_only}, n_discordant {a_only + b_only}, p={p_txt}"
            )
        # Contestable: baseline (a) outside ±2 of the label.
        contestable = [
            p
            for p in group
            if p.label is not None and not within_tol(p.baseline_a, p.label)
        ]
        if contestable and name == "all pairs":
            lines.append("")
            lines += metrics(contestable, "contestable (baseline_a outside ±2 of label)")
        return lines

    lines = [
        "alignment evaluation",
        f"embeddings {EMBEDDING_MODEL}@{EMBEDDING_REVISION}",
        "",
    ]
    lines += metrics(labeled, "all pairs")
    strict = [p for p in labeled if p.under_3_to_1]
    lines.append("")
    lines += metrics(strict, "strict 3:1 subset")
    lines.append("")
    lines.append(
        "Power note: contestable n is small; do not lead on significance. "
        "Report n_discordant beside every McNemar p."
    )
    return "\n".join(lines)


def predict_and_score(
    *,
    lexical: bool = False,
    labels: Path | None = None,
    score_labels: bool = False,
) -> str:
    """Run predictions. Scoring against hand labels is opt-in.

    Default writes the per-pair sheet and prints only counts — so a session that
    still has a labeling pass open does not see per-pair answers.
    """
    preds = run_predictions(lexical=lexical, labels_path=labels if score_labels else None)
    lines = [
        f"wrote {len(preds)} predictions to {PRED_SHEET}",
        f"lexical={lexical}",
        f"aligner divergence median {statistics.median(p.aligner for p in preds):.0f}",
        f"baseline_a median {statistics.median(p.baseline_a for p in preds):.0f}",
        f"pairs under 3:1 {sum(1 for p in preds if p.under_3_to_1)}/{len(preds)}",
    ]
    if score_labels:
        if labels is None:
            labels = LABEL_ROOT / "pass1.jsonl"
        # Reload with labels attached for scoring.
        preds = run_predictions(lexical=lexical, labels_path=labels)
        lines.append("")
        lines.append(score(preds))
    else:
        lines.append(
            "labels not scored (pass --score with a label sheet after pass 2). "
            "Per-pair file is at the path above; leave it closed during labeling."
        )
    return "\n".join(lines)

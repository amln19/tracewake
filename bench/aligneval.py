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

from tracewake import Store
from tracewake.align import (
    DEFAULT_CONFIG,
    EMBEDDING_MODEL,
    EMBEDDING_REVISION,
    AlignConfig,
    LexicalEmbedder,
    MlxEmbedder,
    Step,
    align,
    divergence_step,
    drop_reasoning_config,
    extract_steps,
    first_target_difference,
    last_common_prefix,
    needleman_wunsch_config,
    strict_args_config,
    target_only_args_config,
)

from .fidelity import ledger_rows
from .label import LABELS_FILE, LABEL_ROOT, SELECT_SEED, SelectedPair, select_pairs
from .repos import CORPUS_ROOT
from .runner import LEDGER, STORE

PRED_ROOT = CORPUS_ROOT / "alignment"
PRED_SHEET = PRED_ROOT / "predictions.jsonl"
LLM_SHEET = PRED_ROOT / "llm_judge.jsonl"
ABLATION_SHEET = PRED_ROOT / "ablations.jsonl"
JUDGE_SEED = 20260730
JUDGE_MAX_TOKENS = 32

# Pre-specified before any ablation number was read. Report every arm.
ABLATION_ARMS: tuple[tuple[str, str], ...] = (
    ("full", "Gotoh + frozen weights + BGE embeddings"),
    ("needleman_wunsch", "linear gaps (open == extend)"),
    ("no_reasoning", "drop reasoning weight; renormalize the rest"),
    ("lexical_reasoning", "bag-of-words instead of BGE"),
    ("target_only_args", "argument component = target similarity only"),
    ("strict_args", "argument component = full-args equality"),
)


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
    *,
    config: AlignConfig = DEFAULT_CONFIG,
) -> tuple[list[Step], list[Step], int, int, int]:
    good = trajectory_for(store, pair.good_run, stops[pair.good_run])
    bad = trajectory_for(store, pair.bad_run, stops[pair.bad_run])
    if len(bad) != pair.bad_actions:
        # key.jsonl records failure_steps after anonymize; bad_actions is the
        # pre-export count from select_pairs and must match extraction here.
        pass
    _, pairs, _ = align(good, bad, embed=embed, config=config)
    aligned = divergence_step(pairs, good, bad)
    pred = aligned if aligned is not None else len(bad)
    return (
        good,
        bad,
        pred,
        first_target_difference(good, bad),
        last_common_prefix(good, bad),
    )


def _load_llm(path: Path = LLM_SHEET) -> dict[str, int]:
    """packet_id → predicted step. Task ids are joined via the label key."""
    if not path.exists():
        return {}
    by_packet: dict[str, int] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        row = json.loads(line)
        if row.get("_meta") or row.get("label") is None:
            continue
        by_packet[row["packet_id"]] = int(row["label"])
    return by_packet


def run_predictions(
    store: Path = STORE,
    ledger: Path = LEDGER,
    out: Path = PRED_SHEET,
    *,
    lexical: bool = False,
    labels_path: Path | None = None,
    llm_path: Path | None = LLM_SHEET,
) -> list[PairPred]:
    selected = select_pairs(store, ledger, SELECT_SEED)
    rows = {r["run_id"]: r for r in ledger_rows(ledger)}
    stops = {rid: r["stop_reason"] for rid, r in rows.items()}
    key = _key_by_packet()
    packet_for_task = {v["task_id"]: k for k, v in key.items()}
    labels = _load_labels(labels_path) if labels_path else {}
    llm_by_packet = _load_llm(llm_path) if llm_path else {}

    if lexical:
        embed = LexicalEmbedder()
    else:
        embed = MlxEmbedder(EMBEDDING_MODEL, EMBEDDING_REVISION)

    db = Store(store)
    preds: list[PairPred] = []
    try:
        for pair in selected:
            good, bad, aligner, base_a, base_b = predict_pair(
                store=db, pair=pair, stops=stops, embed=embed
            )
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
                    baseline_llm=llm_by_packet.get(packet_id) if packet_id else None,
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
            "llm_sheet": str(llm_path) if llm_path and llm_path.exists() else None,
        }
        fh.write(json.dumps({"_meta": meta}, sort_keys=True) + "\n")
        for pred in preds:
            fh.write(json.dumps(pred.row(), sort_keys=True) + "\n")
    return preds


def within_tol(pred: int, label: int, tol: int = 2) -> bool:
    return abs(pred - label) <= tol


def median_abs_error(preds: Sequence[int], labels: Sequence[int]) -> float:
    return float(statistics.median(abs(p - y) for p, y in zip(preds, labels, strict=True)))


def mcnemar(a_hits: Sequence[bool], b_hits: Sequence[bool]) -> tuple[int, int, float | None]:
    """Return (b_only, a_only, two-sided exact p) for discordant pairs.

    `a_hits` is the aligner; `b_hits` is the baseline. b_only = baseline right,
    aligner wrong; a_only = aligner right, baseline wrong.
    """
    a_only = b_only = 0
    for a, b in zip(a_hits, b_hits, strict=True):
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
    median = statistics.median(labels)
    best = int(median)
    best_hits = -1
    best_dist = float("inf")
    for candidate in range(1, max(labels) + 1):
        hits = sum(1 for y in labels if within_tol(candidate, y))
        dist = abs(candidate - median)
        if hits > best_hits or (hits == best_hits and dist < best_dist):
            best_hits = hits
            best_dist = dist
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
            hits = [within_tol(v, y) for v, y in zip(values, labels, strict=True)]
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


def _parse_judge_label(text: str, lo: int, hi: int) -> int | None:
    import re

    for match in re.finditer(r"\b(\d{1,3})\b", text):
        value = int(match.group(1))
        if lo <= value <= hi:
            return value
    return None


def run_llm_judge(
    packets_dir: Path = LABEL_ROOT / "packets",
    key_path: Path = LABEL_ROOT / "key.jsonl",
    out: Path = LLM_SHEET,
    *,
    model_id: str | None = None,
    seed: int = JUDGE_SEED,
) -> str:
    """Baseline (c): same packets the annotator saw, one integer back."""
    from tracewake import DecodeParams, Message

    from .backend import DEFAULT_MODEL, LocalModel

    key = _key_by_packet(key_path)
    model = LocalModel(
        model_id=model_id or DEFAULT_MODEL,
        temperature=0.0,
        max_tokens=JUDGE_MAX_TOKENS,
        seed=seed,
    )
    model.warm()
    out.parent.mkdir(parents=True, exist_ok=True)
    rows: list[dict] = []
    packet_ids = sorted(key)
    print(f"llm-judge: {len(packet_ids)} packets, model {model.model_id}", flush=True)
    for i, packet_id in enumerate(packet_ids, start=1):
        packet = (packets_dir / f"{packet_id}.md").read_text(encoding="utf-8")
        hi = int(key[packet_id]["failure_steps"])
        prompt = (
            f"{packet}\n\n"
            f"Reply with a single integer between 1 and {hi} inclusive. "
            f"No words, no punctuation, just the number."
        )
        stream = model.stream(
            model.model_id,
            [Message(role="user", content=prompt)],
            DecodeParams(temperature=0.0, max_tokens=JUDGE_MAX_TOKENS),
        )
        try:
            while True:
                next(stream)
        except StopIteration as stop:
            response = stop.value
        assert isinstance(response, object)
        text = getattr(response, "text", "") or ""
        label = _parse_judge_label(text, 1, hi)
        rows.append(
            {
                "packet_id": packet_id,
                "label": label,
                "raw": text.strip()[:200],
                "failure_steps": hi,
                "model_id": model.model_id,
                "seed": seed,
            }
        )
        flag = str(label) if label is not None else f"PARSE:{text.strip()[:40]!r}"
        print(f"[{i}/{len(packet_ids)}] {packet_id} → {flag}", flush=True)

    with out.open("w", encoding="utf-8") as fh:
        fh.write(
            json.dumps(
                {
                    "_meta": {
                        "model_id": model.model_id,
                        "seed": seed,
                        "n": len(rows),
                        "parsed": sum(1 for r in rows if r["label"] is not None),
                    }
                },
                sort_keys=True,
            )
            + "\n"
        )
        for row in rows:
            fh.write(json.dumps(row, sort_keys=True) + "\n")
    parsed = sum(1 for r in rows if r["label"] is not None)
    return (
        f"llm-judge wrote {parsed}/{len(rows)} parsed labels to {out} "
        f"(model {model.model_id}, seed {seed})"
    )


def _arm_config(name: str) -> AlignConfig:
    if name == "full" or name == "lexical_reasoning":
        return DEFAULT_CONFIG
    if name == "needleman_wunsch":
        return needleman_wunsch_config()
    if name == "no_reasoning":
        return drop_reasoning_config()
    if name == "target_only_args":
        return target_only_args_config()
    if name == "strict_args":
        return strict_args_config()
    raise ValueError(f"unknown ablation arm {name!r}")


def run_ablations(
    store: Path = STORE,
    ledger: Path = LEDGER,
    labels_path: Path = LABEL_ROOT / LABELS_FILE,
    out: Path = ABLATION_SHEET,
) -> str:
    """Score every pre-specified arm. Writes the sheet; returns the table."""
    selected = select_pairs(store, ledger, SELECT_SEED)
    rows = {r["run_id"]: r for r in ledger_rows(ledger)}
    stops = {rid: r["stop_reason"] for rid, r in rows.items()}
    key = _key_by_packet()
    packet_for_task = {v["task_id"]: k for k, v in key.items()}
    labels = _load_labels(labels_path)
    if len(labels) != len(selected):
        raise RuntimeError(
            f"ablations need labels for every selected pair; have {len(labels)} "
            f"labels and {len(selected)} pairs"
        )

    bge: MlxEmbedder | None = None
    lexical = LexicalEmbedder()

    def embedder_for(name: str):
        nonlocal bge
        if name in ("no_reasoning", "lexical_reasoning"):
            return lexical
        if bge is None:
            bge = MlxEmbedder(EMBEDDING_MODEL, EMBEDDING_REVISION)
        return bge

    db = Store(store)
    arm_preds: dict[str, list[int]] = {}
    label_list: list[int] = []
    task_order: list[str] = []
    try:
        for pair in selected:
            packet_id = packet_for_task[pair.task_id]
            label_list.append(labels[packet_id])
            task_order.append(pair.task_id)

        for arm_name, _ in ABLATION_ARMS:
            config = _arm_config(arm_name)
            embed = embedder_for(arm_name)
            preds: list[int] = []
            print(f"ablation: {arm_name}", flush=True)
            for pair in selected:
                _, _, pred, _, _ = predict_pair(
                    db, pair, stops, embed, config=config
                )
                preds.append(pred)
            arm_preds[arm_name] = preds
    finally:
        db.close()

    out.parent.mkdir(parents=True, exist_ok=True)
    with out.open("w", encoding="utf-8") as fh:
        fh.write(
            json.dumps(
                {
                    "_meta": {
                        "arms": [a for a, _ in ABLATION_ARMS],
                        "n": len(selected),
                        "labels": str(labels_path),
                    }
                },
                sort_keys=True,
            )
            + "\n"
        )
        for i, task_id in enumerate(task_order):
            row = {"task_id": task_id, "label": label_list[i]}
            for arm_name, _ in ABLATION_ARMS:
                row[arm_name] = arm_preds[arm_name][i]
            fh.write(json.dumps(row, sort_keys=True) + "\n")

    lines = [
        "alignment ablations (pre-specified; all arms reported)",
        f"n={len(selected)}  labels={labels_path.name}",
        "",
        f"{'arm':<22} {'within±2':>10} {'median|err|':>12}  note",
    ]
    full_hits = [
        within_tol(p, y) for p, y in zip(arm_preds["full"], label_list, strict=True)
    ]
    for arm_name, note in ABLATION_ARMS:
        preds = arm_preds[arm_name]
        hits = [within_tol(p, y) for p, y in zip(preds, label_list, strict=True)]
        n_hit = sum(hits)
        mae = median_abs_error(preds, label_list)
        extra = ""
        if arm_name != "full":
            b_only, a_only, p = mcnemar(full_hits, hits)
            # Here full_hits is "a", arm hits is "b" in mcnemar(aligner, baseline)
            # We want full vs arm: pass full as a, arm as b → a_only = full right arm wrong
            p_txt = f"{p:.3f}" if p is not None else "n/a"
            extra = f"  vs full: full-only {a_only}, arm-only {b_only}, n_disc {a_only + b_only}, p={p_txt}"
        lines.append(
            f"{arm_name:<22} {n_hit:>4}/{len(selected)} ({n_hit / len(selected):>5.1%})  "
            f"{mae:>10.1f}  {note}{extra}"
        )
    lines.append("")
    lines.append(f"per-pair sheet → {out}")
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
    label_path = labels
    if score_labels and label_path is None:
        label_path = LABEL_ROOT / LABELS_FILE
    default_labels = LABEL_ROOT / LABELS_FILE
    out = PRED_SHEET
    if label_path is not None and label_path.resolve() != default_labels.resolve():
        out = PRED_ROOT / f"predictions-{label_path.stem}.jsonl"
    preds = run_predictions(
        out=out,
        lexical=lexical,
        labels_path=label_path if score_labels else None,
    )
    lines = [
        f"wrote {len(preds)} predictions to {out}",
        f"lexical={lexical}",
        f"aligner divergence median {statistics.median(p.aligner for p in preds):.0f}",
        f"baseline_a median {statistics.median(p.baseline_a for p in preds):.0f}",
        f"pairs under 3:1 {sum(1 for p in preds if p.under_3_to_1)}/{len(preds)}",
        f"llm-judge filled {sum(1 for p in preds if p.baseline_llm is not None)}/{len(preds)}",
    ]
    if score_labels:
        lines.append("")
        lines.append(score(preds))
    else:
        lines.append(
            "labels not scored (pass --score after labeling is complete). "
            "Per-pair file is at the path above; leave it closed during labeling."
        )
    return "\n".join(lines)

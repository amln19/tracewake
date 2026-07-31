from __future__ import annotations

import json
from pathlib import Path

from bench.aligneval import (
    _parse_judge_label,
    init_pass_sheet,
    mcnemar,
    oracle_constant,
    self_agreement,
    within_tol,
)


def test_parse_judge_label_takes_first_in_range():
    assert _parse_judge_label("4", 1, 16) == 4
    assert _parse_judge_label("The step is 7.", 1, 16) == 7
    assert _parse_judge_label("99", 1, 16) is None
    assert _parse_judge_label("no number", 1, 16) is None


def test_mcnemar_counts_discordant_pairs():
    a = [True, True, False, False, True]
    b = [True, False, True, False, False]
    b_only, a_only, p = mcnemar(a, b)
    assert a_only == 2
    assert b_only == 1
    assert p is not None


def test_oracle_constant_prefers_the_mode_band():
    labels = [4, 4, 4, 5, 5, 16]
    assert oracle_constant(labels) == 4
    assert within_tol(4, 5)


def test_init_pass_sheet_and_self_agreement(tmp_path: Path):
    key = tmp_path / "key.jsonl"
    with key.open("w", encoding="utf-8") as fh:
        for i in range(1, 4):
            fh.write(
                json.dumps(
                    {
                        "packet_id": f"P{i:02d}",
                        "task_id": f"t{i}",
                        "failure_steps": 6,
                    },
                    sort_keys=True,
                )
                + "\n"
            )
    # Point LABEL_ROOT helpers by writing beside key the way init expects.
    sheet = init_pass_sheet("pass2", dest=tmp_path)
    assert sheet.exists()
    rows = [json.loads(line) for line in sheet.read_text().splitlines() if line.strip()]
    assert len(rows) == 3
    assert all(r["label"] is None for r in rows)

    pass1 = tmp_path / "pass1.jsonl"
    pass2 = tmp_path / "pass2.jsonl"
    with pass1.open("w", encoding="utf-8") as fh:
        for i, label in enumerate([4, 5, 6], start=1):
            fh.write(
                json.dumps({"packet_id": f"P{i:02d}", "label": label}, sort_keys=True) + "\n"
            )
    with pass2.open("w", encoding="utf-8") as fh:
        for i, label in enumerate([4, 7, 6], start=1):
            fh.write(
                json.dumps({"packet_id": f"P{i:02d}", "label": label}, sort_keys=True) + "\n"
            )
    report = self_agreement(pass1, pass2)
    assert "exact agreement            2/3" in report
    assert "within±2                   3/3" in report

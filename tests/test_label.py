from __future__ import annotations

import json

from bench.label import _anonymize, _paths_in, select_pairs


def test_paths_are_collected_from_messy_tokens():
    assert _paths_in('see "pkg/mod.py", please') == ["pkg/mod.py"]
    assert _paths_in("pkg/mod.py:12") == ["pkg/mod.py"]
    assert _paths_in("open test_schema.py next") == ["test_schema.py"]


def test_anonymize_rewrites_paths_before_they_can_leak():
    good = [
        {
            "step": 1,
            "name": "read_file",
            "target": "parse/__init__.py",
            "args": json.dumps({"path": "parse/__init__.py", "around": 1}),
            "status": "ok",
            "reason": "Looking at parse/__init__.py for the bug.",
        }
    ]
    bad = [
        {
            "step": 1,
            "name": "edit_file",
            "target": "parse/__init__.py",
            "args": json.dumps(
                {
                    "path": "parse/__init__.py",
                    "old": "x",
                    "new": "y" * 200,
                }
            ),
            "status": "ok",
            "reason": "",
        }
    ]
    good_out, bad_out = _anonymize(good, bad)
    blob = json.dumps(good_out) + json.dumps(bad_out)
    assert "parse/" not in blob
    assert "file_a" in good_out[0]["target"]
    assert good_out[0]["target"] == bad_out[0]["target"]


def test_select_pairs_is_one_per_task_and_coverage_mixed():
    # Against the closed corpus. Selection is method-blind: it only reads
    # coverage labels and trajectory lengths.
    selected = select_pairs()
    assert len(selected) >= 30
    assert len({p.task_id for p in selected}) == len(selected)
    for pair in selected:
        assert pair.good_actions >= 1
        assert pair.bad_actions >= 1
        assert pair.length_ratio <= 4

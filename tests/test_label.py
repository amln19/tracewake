from __future__ import annotations

import json
from pathlib import Path

import pytest

from bench.fidelity import extract
from bench.label import _anonymize, _paths_in, select_pairs
from bench.runner import STORE
from locus import Store


def _corpus_is_present() -> bool:
    # The labels ship but the recorded runs do not, and opening a Store creates
    # an empty one, so the presence of the directory proves nothing.
    if not (STORE / "locus.db").exists():
        return False
    db = Store(STORE)
    try:
        return bool(db.runs())
    finally:
        db.close()


def test_a_ledger_without_its_store_fails_instead_of_reading_as_empty(tmp_path: Path) -> None:
    ledger = tmp_path / "runs.jsonl"
    ledger.write_text(
        json.dumps({"run_id": "a" * 32, "task_id": "t", "stop_reason": "submitted"}) + "\n",
        encoding="utf-8",
    )
    with pytest.raises(FileNotFoundError, match="holds events for none of them"):
        extract(store=tmp_path / "absent", ledger=ledger)


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
    if not _corpus_is_present():
        pytest.skip("the recorded corpus store is not committed; `bench run` rebuilds it")
    selected = select_pairs()
    assert len(selected) >= 30
    assert len({p.task_id for p in selected}) == len(selected)
    for pair in selected:
        assert pair.good_actions >= 1
        assert pair.bad_actions >= 1
        assert pair.length_ratio <= 4

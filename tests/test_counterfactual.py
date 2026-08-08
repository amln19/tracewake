"""Forking a recorded attempt: run indexing, and diffing a fork against its source.

`fork()` itself needs live inference and is exercised against the corpus rather
than here. What is testable without a model is the part that decides *which*
seed a fork continues from and the part that pairs a fork back to its source.
"""

from __future__ import annotations

from pathlib import Path

import pytest

import tracewake
from bench.counterfactual import _run_index, fork_diff

from mock_agent import MockBackend, Transcript, run_agent


def _record(store: Path) -> str:
    backend = MockBackend()
    with tracewake.record("bidict-deleted_guard-3#1", store=store, mode="all") as s:
        model = s.model(
            provider="mock",
            model_id="mock-1",
            create_fn=backend.create,
            stream_fn=backend.stream,
        )
        run_agent(model, s.tools(backend.dispatch), s.clock, Transcript())
        s.outcome(status="ok")
        return s.run_id


def _fork(source: str, store: Path, fork_store: Path) -> str:
    live = MockBackend()
    with tracewake.intervene(
        source,
        drop_tags=["tool_output"],
        from_turn=1,
        store=fork_store,
        source_store=store,
    ) as s:
        model = s.model(
            provider="mock", model_id="mock-1", create_fn=live.create, stream_fn=live.stream
        )
        run_agent(model, s.tools(live.dispatch), s.clock, Transcript())
        s.outcome(status="ok")
        return s.run_id


def test_the_run_index_comes_off_the_cassette_name():
    # The seed a corpus attempt used is derived from this, so reading it wrong
    # would silently continue a fork from a different sampler than the source.
    assert _run_index("bidict-deleted_guard-3#1") == 1
    assert _run_index("tabulate-operator_swap-10#0") == 0
    assert _run_index("semver-off_by_one-5#12") == 12


def test_a_name_without_a_run_index_falls_back_to_zero():
    assert _run_index("agent") == 0
    assert _run_index("weird#name") == 0


def test_fork_diff_pairs_a_fork_back_to_its_source_across_stores(tmp_path: Path):
    store, fork_store = tmp_path / "corpus", tmp_path / "forks"
    source = _record(store)
    forked = _fork(source, store, fork_store)

    out = fork_diff(forked, store=store, fork_store=fork_store, lexical=True)

    assert source[:12] in out
    assert forked[:12] in out
    assert "dropped tool_output from turn 1" in out
    assert "SOURCE" in out and "FORK" in out


def test_fork_diff_refuses_a_run_that_is_not_a_fork(tmp_path: Path):
    store = tmp_path / "corpus"
    source = _record(store)

    # A plain recording carries no record of a source, and diffing it against
    # nothing would need a source id invented from somewhere.
    with pytest.raises(ValueError, match="not a fork"):
        fork_diff(source, store=store, fork_store=store, lexical=True)

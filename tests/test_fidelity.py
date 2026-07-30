from __future__ import annotations

import json

import pytest

import locus
from bench.fidelity import (
    Step,
    assert_fresh,
    chance_agreement,
    compare,
    comparisons,
    fresh_seed,
    name_key,
    recorded_seed,
    report,
    steps,
    target_key,
)
from locus import (
    BlobRef,
    DecodeParams,
    EventMeta,
    Message,
    ModelCallEvent,
    ModelResponse,
    StoredEvent,
    ToolCallEvent,
    ToolCallRequest,
    ToolOutcome,
    Usage,
    hash_args,
)


def tool_event(index: int, name: str, args: dict) -> StoredEvent:
    return StoredEvent(
        run_id="r",
        seq=index,
        event=ToolCallEvent(
            parent_call_id="c",
            tool_call_id=f"step{index}-{name}",
            batch_index=0,
            name=name,
            args=args,
            args_hash=hash_args(args),
            result=BlobRef(digest="0" * 64, size=0),
            status="ok",
            meta=EventMeta(recorded_at=0.0),
        ),
    )


def sequence(*pairs: tuple[str, dict]) -> list[StoredEvent]:
    return [tool_event(i, name, args) for i, (name, args) in enumerate(pairs)]


def test_a_seed_matching_a_recording_is_refused():
    """The single easiest way to get a false 100%.

    The sampler is seeded from the run index, so an arm that reuses a recorded
    seed reproduces the recording token for token and reports perfect agreement
    without observing any.
    """
    recorded = [recorded_seed(i) for i in range(3)]
    with pytest.raises(ValueError, match="would reproduce it"):
        assert_fresh(recorded_seed(1), "toolz-off_by_one-1", recorded)


def test_a_seed_inside_a_recorded_call_window_is_refused():
    # A run advances the seed by one per model call, so landing a few past a
    # recorded seed still collides partway through the trajectory.
    recorded = [recorded_seed(i) for i in range(3)]
    with pytest.raises(ValueError, match="lands within"):
        assert_fresh(recorded_seed(2) + 7, "toolz-off_by_one-1", recorded)


def test_fresh_seeds_clear_every_recorded_window():
    recorded = [recorded_seed(i) for i in range(3)]
    for replicate in range(3):
        for index in range(3):
            assert_fresh(fresh_seed(index, replicate), "t", recorded)


def test_fresh_seeds_do_not_collide_with_each_other():
    seen = [fresh_seed(i, r) for r in range(3) for i in range(3)]
    assert len(set(seen)) == len(seen)
    for seed in seen:
        assert_fresh(seed, "t", [s for s in seen if s != seed])


def test_identical_runs_never_part():
    left = right = [Step("read_file", "h1"), Step("edit_file", "h2")]
    result = compare("t", "a", "b", left, right)
    assert result.divergence is None
    assert result.agree == (True, True)
    assert result.survived == 2
    assert result.holds_to(2)


def test_parting_is_the_first_differing_step():
    left = [Step("read_file", "h1"), Step("edit_file", "h2"), Step("run_tests", "h3")]
    right = [Step("read_file", "h1"), Step("search", "h9"), Step("run_tests", "h3")]
    result = compare("t", "a", "b", left, right)
    assert result.divergence == 1
    assert result.agree == (True, False, True)
    assert result.holds_to(1)
    assert not result.holds_to(2)


def test_the_same_tool_with_different_arguments_is_a_parting():
    left = [Step("read_file", "h1")]
    right = [Step("read_file", "h2")]
    result = compare("t", "a", "b", left, right)
    assert result.divergence == 0
    assert result.names == (True,)
    assert result.agree == (False,)


def test_one_run_stopping_early_is_a_parting():
    left = [Step("read_file", "h1")]
    right = [Step("read_file", "h1"), Step("edit_file", "h2")]
    result = compare("t", "a", "b", left, right)
    assert result.divergence == 1
    assert result.compared == 1
    assert result.holds_to(1)


def test_steps_come_out_in_dispatch_order_not_id_order():
    # Sorting tool_call_ids as strings puts step10 before step2, which would
    # silently shift every comparison from step ten onwards.
    events = sequence(*[("read_file", {"path": f"f{i}.py"}) for i in range(12)])
    got = steps(events)
    assert len(got) == 12
    assert [s.args_hash for s in got] == [
        hash_args({"path": f"f{i}.py"}) for i in range(12)
    ]


def test_a_gap_in_step_numbering_raises():
    events = sequence(("read_file", {"path": "a.py"}), ("edit_file", {"path": "a.py"}))
    events[1].event.tool_call_id = "step5-edit_file"
    with pytest.raises(ValueError, match="names step 5"):
        steps(events)


def test_a_submitted_run_carries_a_terminal_step():
    events = sequence(("read_file", {"path": "a.py"}))
    assert [s.name for s in steps(events, "submitted")] == ["read_file", "submit"]
    assert [s.name for s in steps(events, "stuck")] == ["read_file"]


def test_a_run_that_submits_parts_from_one_that_does_not():
    events = sequence(("read_file", {"path": "a.py"}))
    result = compare("t", "a", "b", steps(events, "submitted"), steps(events, "stuck"))
    assert result.divergence == 1


def test_a_wider_comparison_sees_agreement_a_strict_one_misses():
    # Same file, different window: strict equality calls this a parting, and a
    # comparison of what the action was aimed at does not.
    left = [Step("read_file", "h1", "core.py"), Step("edit_file", "h2", "core.py")]
    right = [Step("read_file", "h9", "core.py"), Step("edit_file", "h2", "core.py")]
    assert compare("t", "a", "b", left, right).divergence == 0
    wide = compare("t", "a", "b", left, right, key=target_key)
    assert wide.divergence is None
    assert compare("t", "a", "b", left, right, key=name_key).divergence is None


def test_realignment_is_agreement_after_the_first_parting():
    parts_and_returns = compare(
        "t",
        "a",
        "b",
        [Step("read_file", "h1"), Step("search", "h2"), Step("run_tests", "h3")],
        [Step("read_file", "h1"), Step("edit_file", "h9"), Step("run_tests", "h3")],
    )
    assert parts_and_returns.divergence == 1
    assert parts_and_returns.realigns

    never_returns = compare(
        "t",
        "a",
        "b",
        [Step("read_file", "h1"), Step("search", "h2")],
        [Step("read_file", "h1"), Step("edit_file", "h9")],
    )
    assert never_returns.divergence == 1
    assert not never_returns.realigns


def test_a_pair_is_mixed_when_its_runs_disagree_on_coverage():
    steps_ = [Step("read_file", "h1")]
    assert compare("t", "a", "b", steps_, steps_, coverage=(True, False)).mixed
    assert not compare("t", "a", "b", steps_, steps_, coverage=(True, True)).mixed


def test_length_ratio_uses_the_shorter_run():
    long_run = [Step("read_file", f"h{i}") for i in range(16)]
    short_run = [Step("read_file", "h0"), Step("search", "h1")]
    assert compare("t", "a", "b", long_run, short_run).length_ratio == 8.0
    assert compare("t", "a", "b", short_run, short_run).length_ratio == 1.0


def test_the_target_is_read_off_whichever_argument_names_it():
    read = sequence(("read_file", {"path": "core.py", "around": 12}))
    assert steps(read)[0].target == "core.py"
    searched = sequence(("search", {"query": "def parse"}))
    assert steps(searched)[0].target == "def parse"
    tested = sequence(("run_tests", {}))
    assert steps(tested)[0].target == ""


def test_chance_agreement_reflects_the_marginal_distribution():
    even = {"r": [Step("a", ""), Step("b", "")]}
    assert chance_agreement(even) == pytest.approx(0.5)
    skewed = {"r": [Step("a", ""), Step("a", ""), Step("a", ""), Step("b", "")]}
    assert chance_agreement(skewed) == pytest.approx(0.625)
    assert chance_agreement({}) == 0.0


def recorded_run(store, name, task_id, actions):
    with locus.record(name, store=store, task_id=task_id, block_network=False) as session:
        model = session.model(
            provider="p",
            model_id="m",
            create_fn=lambda mid, msgs, params: ModelResponse(
                text="ok", finish_reason="end_turn", usage=Usage()
            ),
        )
        tools = session.tools(lambda tool, args: ToolOutcome(status="ok", content="done"))
        completion = model.create(messages=[Message(role="user", content="go")])
        for index, (tool, args) in enumerate(actions):
            tools.call(
                completion.call_id,
                ToolCallRequest(id=f"step{index}-{tool}", name=tool, args=args, batch_index=0),
            )
        return session.run_id


def test_the_report_pairs_every_run_of_a_task(tmp_path):
    store = tmp_path / "store"
    ledger = tmp_path / "runs.jsonl"
    rows = []
    plans = [
        [("read_file", {"path": "a.py"}), ("edit_file", {"path": "a.py"})],
        [("read_file", {"path": "a.py"}), ("search", {"query": "x"})],
        [("search", {"query": "y"})],
    ]
    for index, actions in enumerate(plans):
        run_id = recorded_run(store, f"t#{index}", "toolz-off_by_one-1", actions)
        rows.append(
            {
                "task_id": "toolz-off_by_one-1",
                "run_index": index,
                "run_id": run_id,
                "stop_reason": "stuck",
                "coverage": index == 0,
                "resolve": False,
            }
        )
    ledger.write_text("\n".join(json.dumps(r) for r in rows) + "\n", encoding="utf-8")

    found, extracted = comparisons(store, ledger)
    assert len(found) == 3
    assert {c.divergence for c in found} == {0, 1}
    assert sorted(len(v) for v in extracted.values()) == [1, 2, 2]
    # Run 0 is the only one with coverage, so it pairs mixed with each other run.
    assert sum(1 for c in found if c.mixed) == 2

    text = report(store, ledger, tmp_path / "pairs.jsonl")
    assert "3 over 1 tasks" in text
    written = [
        json.loads(line)
        for line in (tmp_path / "pairs.jsonl").read_text(encoding="utf-8").splitlines()
    ]
    assert len(written) == 3
    assert all(row["task_id"] == "toolz-off_by_one-1" for row in written)


def test_a_missing_ledger_says_what_is_missing(tmp_path):
    with pytest.raises(FileNotFoundError, match="no attempt ledger"):
        comparisons(tmp_path / "store", tmp_path / "absent.jsonl")


def test_a_ledger_without_outcomes_is_refused(tmp_path):
    # Defaulting a missing coverage flag would mark every pair same-outcome and
    # report a clean result for the comparison the phase turns on.
    store = tmp_path / "store"
    ledger = tmp_path / "runs.jsonl"
    rows = [
        {
            "task_id": "toolz-off_by_one-1",
            "run_index": index,
            "run_id": recorded_run(
                store, f"t#{index}", "toolz-off_by_one-1", [("read_file", {"path": "a.py"})]
            ),
            "stop_reason": "stuck",
        }
        for index in range(2)
    ]
    ledger.write_text("\n".join(json.dumps(r) for r in rows) + "\n", encoding="utf-8")
    with pytest.raises(KeyError, match="coverage"):
        comparisons(store, ledger)


def test_model_calls_are_not_steps():
    events = [
        StoredEvent(
            run_id="r",
            seq=0,
            event=ModelCallEvent(
                call_id="c",
                provider="p",
                model_id="m",
                params=DecodeParams(),
                messages=[Message(role="user", content="go")],
                messages_hash="h",
                response=ModelResponse(text="ok", finish_reason="end_turn", usage=Usage()),
                meta=EventMeta(recorded_at=0.0),
            ),
        ),
        tool_event(0, "read_file", {"path": "a.py"}),
    ]
    assert [s.name for s in steps(events)] == ["read_file"]

"""nebius adapter: SWE-agent's stateful editor, and pair selection."""

from __future__ import annotations

from bench.nebius import build_pairs, to_steps


def turn(text: str, role: str = "ai") -> dict:
    return {"role": role, "text": text}


def action(reasoning: str, command: str) -> dict:
    return turn(f"{reasoning}\n\n```\n{command}\n```")


def test_the_model_turn_is_tagged_ai_in_this_dump():
    # Not `assistant`; reading only that role yields an empty trajectory and,
    # silently, zero usable pairs.
    assert len(to_steps([action("look", "ls -F")])) == 1
    assert len(to_steps([turn("ls -F", role="user")])) == 0


def test_edit_writes_to_whatever_open_selected():
    """SWE-agent's `edit` names no path — the ACI has a current file."""
    steps = to_steps(
        [
            action("find it", 'find_file "memset.py" lexicon'),
            action("open it", "open lexicon/memset.py"),
            action("fix it", "edit 42:44"),
        ]
    )
    assert [s.name for s in steps] == ["find_file", "open", "edit"]
    assert steps[2].target == "lexicon/memset.py"
    assert steps[2].writes == frozenset({"lexicon/memset.py"})


def test_an_edit_with_nothing_open_claims_no_write():
    steps = to_steps([action("edit blind", "edit 1:1")])
    assert steps[0].writes == frozenset()


def test_create_then_edit_is_not_a_commitment():
    from tracewake.diverge import commitment_steps

    # The run makes reproduce.py itself, so editing it is still exploration.
    steps = to_steps(
        [
            action("scratch", "create reproduce.py"),
            action("fill it", "edit 1:1"),
            action("run", "python reproduce.py"),
        ]
    )
    assert commitment_steps(steps) == []


def test_editing_a_file_the_run_opened_is_a_commitment():
    from tracewake.diverge import commitment_steps

    steps = to_steps(
        [
            action("open source", "open src/thing.py"),
            action("change it", "edit 10:12"),
        ]
    )
    assert commitment_steps(steps) == [2]


def test_a_language_tag_on_the_fence_is_not_the_command():
    steps = to_steps([turn("run it\n\n```bash\ngrep -rn foo src\n```")])
    assert steps[0].name == "grep"


def test_turns_without_a_command_are_skipped():
    assert to_steps([turn("just thinking out loud, no block here")]) == []


def test_pairs_are_same_model_mixed_outcome_only():
    rows = [
        {"instance_id": "a", "model_name": "m1", "target": True,
         "trajectory": [action("x", "open f.py"), action("y", "edit 1:1")]},
        {"instance_id": "a", "model_name": "m1", "target": False,
         "trajectory": [action("x", "open g.py"), action("y", "edit 2:2")]},
        # same instance, different model — must not be paired across
        {"instance_id": "a", "model_name": "m2", "target": True,
         "trajectory": [action("x", "ls")]},
        # all-fail group yields nothing
        {"instance_id": "b", "model_name": "m1", "target": False,
         "trajectory": [action("x", "ls")]},
    ]
    pairs = build_pairs(rows)
    assert len(pairs) == 1
    assert (pairs[0].instance_id, pairs[0].model) == ("a", "m1")
    assert pairs[0].good[1].writes == frozenset({"f.py"})
    assert pairs[0].bad[1].writes == frozenset({"g.py"})

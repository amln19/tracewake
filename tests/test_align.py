"""Alignment gate: affine gaps, target-width re-alignment, frozen distance."""

from __future__ import annotations

import pytest

from tracewake import (
    BlobRef,
    EventMeta,
    FsWriteEvent,
    ModelCallEvent,
    ModelResponse,
    StoredEvent,
    ToolCallEvent,
    Usage,
    hash_args,
)
from tracewake.align import (
    GAP_EXTEND,
    GAP_OPEN,
    WEIGHT_ARGS,
    WEIGHT_FILES,
    WEIGHT_REASONING,
    WEIGHT_TOOL,
    LexicalEmbedder,
    Step,
    align,
    argument_similarity,
    diff_runs,
    divergence_step,
    extract_steps,
    first_target_difference,
    format_diff,
    gotoh,
    jaccard_files,
    last_common_prefix,
    path_similarity,
    score_from_similarity,
    step_similarity,
    target_agree,
)


def test_frozen_weights_sum_to_one():
    assert abs(WEIGHT_TOOL + WEIGHT_ARGS + WEIGHT_REASONING + WEIGHT_FILES - 1.0) < 1e-12


def test_score_maps_identity_and_unrelated():
    assert score_from_similarity(1.0) == 1.0
    assert score_from_similarity(0.0) == -1.0
    assert score_from_similarity(0.5) == 0.0


def test_empty_changed_sets_are_identical():
    assert jaccard_files(frozenset(), frozenset()) == 1.0


def test_path_similarity_uses_trailing_components():
    assert path_similarity("pkg/mod.py", "pkg/mod.py") == 1.0
    assert path_similarity("a/b/mod.py", "x/y/mod.py") == 1 / 3
    assert path_similarity("a.py", "b.py") == 0.0


def test_argument_similarity_survives_a_different_edit_body():
    left = Step(
        "edit_file",
        {"path": "a.py", "old": "x", "new": "y", "at": 10},
        target="a.py",
    )
    right = Step(
        "edit_file",
        {"path": "a.py", "old": "x", "new": "zzzz", "at": 10},
        target="a.py",
    )
    # Same target, different body — must stay well above a different-file edit.
    same = argument_similarity(left, right)
    other = argument_similarity(
        left,
        Step("edit_file", {"path": "b.py", "old": "x", "new": "y"}, target="b.py"),
    )
    assert same > 0.7
    assert same > other


def test_gotoh_charges_an_excursion_once():
    # Five gaps as one open + four extends must beat five independent opens.
    one_excursion = GAP_OPEN + 4 * GAP_EXTEND
    five_opens = 5 * GAP_OPEN
    assert one_excursion > five_opens

    # Insert a 5-step excursion into B between A[0] and A[1] by aligning
    # A=abc against B=a....bc with high mismatch cost for '.'.
    a = "abc"
    b = "a.....bc"
    matrix = [
        [1.0 if a[i] == b[j] else -1.0 for j in range(len(b))] for i in range(len(a))
    ]
    total, pairs = gotoh(matrix)
    gap_cols = sum(1 for i, j in pairs if i is None)
    assert gap_cols == 5
    # One open + four extends plus three matches: 3 - 1.0 - 0.8 = 1.2
    assert abs(total - (3.0 + one_excursion)) < 1e-9


def test_identical_traces_have_no_divergence():
    steps = [
        Step("read_file", {"path": "a.py"}, target="a.py"),
        Step("edit_file", {"path": "a.py", "old": "1", "new": "2"}, target="a.py"),
    ]
    _, pairs, _ = align(steps, steps, embed=LexicalEmbedder())
    assert divergence_step(pairs, steps, steps) is None


def test_a_shared_final_step_hides_divergence_everywhere_before_it():
    """The backward rule needs the two runs to end differently.

    A harness that finishes every run with the same terminal action makes the
    last column agree by construction, so there is no trailing mismatch region
    and the runs read as re-aligning however far apart they actually went.
    Strip a fixed ending before diffing. (A single shared terminal is not a
    trailing identical-arg loop, so the loop rule does not apply here.)
    """
    good = [
        Step("read_file", {"path": "a.py"}, target="a.py"),
        Step("edit_file", {"path": "a.py"}, target="a.py"),
        Step("submit", {}),
    ]
    bad = [
        Step("read_file", {"path": "b.py"}, target="b.py"),
        Step("search", {"query": "z"}, target="z"),
        Step("submit", {}),
    ]
    embed = LexicalEmbedder()
    _, pairs, _ = align(good, bad, embed=embed)
    assert divergence_step(pairs, good, bad) is None

    trimmed_good, trimmed_bad = good[:-1], bad[:-1]
    _, pairs, _ = align(trimmed_good, trimmed_bad, embed=embed)
    assert divergence_step(pairs, trimmed_good, trimmed_bad) == 1


def test_a_trailing_identical_arg_loop_is_not_recovery():
    """Stuck repeats of the same action must not pad the agreeing suffix.

    The failing side loops on bare `run_tests()`; Gotoh pairs each copy against
    a separate real `run_tests()` on the good side. Counting those as recovery
    hid the divergence (corpus pair cachetools-deleted_guard-7). Ignoring
    agreements inside the trailing loop surfaces it; a genuine one-column
    recovery (no loop) is unchanged.
    """
    good = [
        Step("read_file", {"path": "a.py"}, target="a.py"),
        Step("edit_file", {"path": "a.py", "old": "x", "new": "y"}, target="a.py"),
        Step("run_tests", {}),
        Step("edit_file", {"path": "a.py", "old": "y", "new": "z"}, target="a.py"),
        Step("run_tests", {}),
        Step("edit_file", {"path": "a.py", "old": "z", "new": "w"}, target="a.py"),
        Step("run_tests", {}),
        Step("run_tests", {}),  # genuine second test, different position — not a bad-side loop
    ]
    bad = [
        Step("read_file", {"path": "a.py"}, target="a.py"),
        Step("search", {"query": "wrong"}, target="wrong"),
        Step("run_tests", {}),
        Step("run_tests", {}),
        Step("run_tests", {}),
        Step("run_tests", {}),
    ]
    embed = LexicalEmbedder()
    _, pairs, _ = align(good, bad, embed=embed)
    # Loop starts at bad index 2; those agreements are ignored, so the last
    # counted agreement is the shared read and divergence is the search.
    assert divergence_step(pairs, good, bad) == 2

    # One-column recovery at the end still counts when there is no loop.
    recovered_good = [
        Step("read_file", {"path": "a.py"}, target="a.py"),
        Step("search", {"query": "x"}, target="x"),
        Step("run_tests", {}),
    ]
    recovered_bad = [
        Step("read_file", {"path": "b.py"}, target="b.py"),
        Step("search", {"query": "y"}, target="y"),
        Step("run_tests", {}),
    ]
    _, pairs, _ = align(recovered_good, recovered_bad, embed=embed)
    assert divergence_step(pairs, recovered_good, recovered_bad) is None


def test_backward_definition_beats_first_difference_after_realignment():
    """The step-unit trap: target-width re-alignment is where the aligner wins.

    Under strict equality the edit bodies differ and the pair never re-aligns, so
    the backward definition and baseline (a) coincide. At target width they
    re-align on the edit, and the aligner reports a later divergence than the
    first positional difference.
    """
    good = [
        Step("read_file", {"path": "a.py", "around": 10}, target="a.py"),
        Step("edit_file", {"path": "a.py", "old": "x", "new": "y"}, target="a.py"),
        Step("run_tests", {}),
        Step("submit", {}),
    ]
    bad = [
        Step("read_file", {"path": "a.py", "around": 10}, target="a.py"),
        Step("search", {"query": "hook"}, target="hook"),
        Step("search", {"query": "schema"}, target="schema"),
        Step(
            "edit_file",
            {"path": "a.py", "old": "x", "new": "WRONG"},
            target="a.py",
        ),
        Step("run_tests", {}),
        Step("search", {"query": "again"}, target="again"),
    ]
    # Strict-style: different new= means edits would not match as equal args.
    assert good[1].args["new"] != bad[3].args["new"]
    assert target_agree(good[1], bad[3])

    _, pairs, _ = align(good, bad, embed=LexicalEmbedder())
    aligned = divergence_step(pairs, good, bad)
    baseline_a = first_target_difference(good, bad)
    baseline_b = last_common_prefix(good, bad)

    assert baseline_a == 2
    assert baseline_b == 1
    assert aligned == 6
    assert aligned != baseline_a


def test_extract_steps_folds_a_parallel_batch():
    events = [
        StoredEvent(
            run_id="r",
            seq=0,
            event=ModelCallEvent(
                call_id="c1",
                provider="t",
                model_id="m",
                params={},
                messages=[],
                messages_hash="0" * 64,
                response=ModelResponse(
                    text="batch",
                    finish_reason="end_turn",
                    usage=Usage(),
                    tool_calls=[],
                ),
                meta=EventMeta(recorded_at=0.0),
            ),
        ),
        StoredEvent(
            run_id="r",
            seq=1,
            event=ToolCallEvent(
                parent_call_id="c1",
                tool_call_id="t0",
                batch_index=0,
                name="read_file",
                args={"path": "a.py"},
                args_hash=hash_args({"path": "a.py"}),
                result=BlobRef(digest="0" * 64, size=0),
                status="ok",
                meta=EventMeta(recorded_at=0.0),
            ),
        ),
        StoredEvent(
            run_id="r",
            seq=2,
            event=ToolCallEvent(
                parent_call_id="c1",
                tool_call_id="t1",
                batch_index=1,
                name="read_file",
                args={"path": "b.py"},
                args_hash=hash_args({"path": "b.py"}),
                result=BlobRef(digest="0" * 64, size=0),
                status="ok",
                meta=EventMeta(recorded_at=0.0),
            ),
        ),
    ]
    steps = extract_steps(events)
    assert len(steps) == 1
    assert steps[0].names == frozenset({"read_file"})
    assert steps[0].targets == frozenset({"a.py", "b.py"})


def test_extract_steps_accumulates_writes_under_the_tool():
    digest_a = "a" * 64
    digest_b = "b" * 64
    events = [
        StoredEvent(
            run_id="r",
            seq=0,
            event=ModelCallEvent(
                call_id="c1",
                provider="t",
                model_id="m",
                params={},
                messages=[],
                messages_hash="0" * 64,
                response=ModelResponse(text="edit", finish_reason="end_turn"),
                meta=EventMeta(recorded_at=0.0),
            ),
        ),
        StoredEvent(
            run_id="r",
            seq=1,
            event=FsWriteEvent(
                path="a.py",
                content=BlobRef(digest=digest_a, size=1),
                tool_call_id="t0",
                parent_call_id="c1",
                meta=EventMeta(recorded_at=0.0),
            ),
        ),
        StoredEvent(
            run_id="r",
            seq=2,
            event=ToolCallEvent(
                parent_call_id="c1",
                tool_call_id="t0",
                batch_index=0,
                name="edit_file",
                args={"path": "a.py", "old": "1", "new": "2"},
                args_hash=hash_args({"path": "a.py", "old": "1", "new": "2"}),
                result=BlobRef(digest="0" * 64, size=0),
                status="ok",
                meta=EventMeta(recorded_at=0.0),
            ),
        ),
        StoredEvent(
            run_id="r",
            seq=3,
            event=ModelCallEvent(
                call_id="c2",
                provider="t",
                model_id="m",
                params={},
                messages=[],
                messages_hash="1" * 64,
                response=ModelResponse(text="edit2", finish_reason="end_turn"),
                meta=EventMeta(recorded_at=0.0),
            ),
        ),
        StoredEvent(
            run_id="r",
            seq=4,
            event=FsWriteEvent(
                path="b.py",
                content=BlobRef(digest=digest_b, size=1),
                tool_call_id="t1",
                parent_call_id="c2",
                meta=EventMeta(recorded_at=0.0),
            ),
        ),
        StoredEvent(
            run_id="r",
            seq=5,
            event=ToolCallEvent(
                parent_call_id="c2",
                tool_call_id="t1",
                batch_index=0,
                name="edit_file",
                args={"path": "b.py", "old": "1", "new": "2"},
                args_hash=hash_args({"path": "b.py", "old": "1", "new": "2"}),
                result=BlobRef(digest="0" * 64, size=0),
                status="ok",
                meta=EventMeta(recorded_at=0.0),
            ),
        ),
    ]
    steps = extract_steps(events)
    assert steps[0].changed_files == frozenset({("a.py", digest_a)})
    assert steps[1].changed_files == frozenset(
        {("a.py", digest_a), ("b.py", digest_b)}
    )
    # `changed_files` accumulates; `writes` names what each step alone wrote.
    assert steps[0].writes == frozenset({"a.py"})
    assert steps[1].writes == frozenset({"b.py"})


def test_writes_do_not_enter_the_frozen_lexical_distance():
    plain = Step(name="edit", args={"path": "a.py"}, target="a.py")
    annotated = Step(
        name="edit", args={"path": "a.py"}, target="a.py", writes=frozenset({"a.py"})
    )
    other = Step(name="edit", args={"path": "b.py"}, target="b.py")

    assert step_similarity(plain, other, reasoning_vectors=None) == step_similarity(
        annotated, other, reasoning_vectors=None
    )
    assert target_agree(plain, annotated)


def test_diff_runs_and_format():
    def call(seq: int, call_id: str, text: str, tool_id: str, name: str, args: dict):
        return [
            StoredEvent(
                run_id="r",
                seq=seq,
                event=ModelCallEvent(
                    call_id=call_id,
                    provider="t",
                    model_id="m",
                    params={},
                    messages=[],
                    messages_hash=f"{seq:064d}"[-64:],
                    response=ModelResponse(text=text, finish_reason="end_turn"),
                    meta=EventMeta(recorded_at=0.0),
                ),
            ),
            StoredEvent(
                run_id="r",
                seq=seq + 1,
                event=ToolCallEvent(
                    parent_call_id=call_id,
                    tool_call_id=tool_id,
                    batch_index=0,
                    name=name,
                    args=args,
                    args_hash=hash_args(args),
                    result=BlobRef(digest="0" * 64, size=0),
                    status="ok",
                    meta=EventMeta(recorded_at=0.0),
                ),
            ),
        ]

    good = call(0, "c0", "read", "t0", "read_file", {"path": "a.py"}) + call(
        2, "c1", "edit", "t1", "edit_file", {"path": "a.py", "old": "1", "new": "2"}
    )
    bad = call(0, "c0", "read", "t0", "read_file", {"path": "a.py"}) + call(
        2, "c1", "search", "t1", "search", {"query": "nope"}
    )
    result = diff_runs(good, bad, embed=LexicalEmbedder())
    assert result.divergence == 2
    text = format_diff(result)
    assert "divergence at BAD step 2" in text
    assert ">>>" in text


def test_step_similarity_uses_all_four_components():
    a = Step(
        "read_file",
        {"path": "a.py"},
        target="a.py",
        reasoning="look at the helper",
        changed_files=frozenset({("a.py", "x" * 64)}),
    )
    b = Step(
        "read_file",
        {"path": "a.py"},
        target="a.py",
        reasoning="look at the helper",
        changed_files=frozenset({("a.py", "x" * 64)}),
    )
    vecs = LexicalEmbedder()([a.reasoning, b.reasoning])
    assert step_similarity(a, b, reasoning_vectors=(vecs[0], vecs[1])) == 1.0


def test_lexical_embedder_scores_empty_reasoning_as_identity():
    a = Step("bash", {"cmd": "ls"}, target="ls", reasoning="")
    b = Step("bash", {"cmd": "ls"}, target="ls", reasoning="")
    vecs = LexicalEmbedder()([a.reasoning, b.reasoning])
    assert step_similarity(a, b, reasoning_vectors=(vecs[0], vecs[1])) == 1.0


def test_align_refuses_reasoning_without_an_embedder():
    from tracewake.patches import TracewakeError

    a = Step("read_file", {"path": "a.py"}, target="a.py", reasoning="why")
    b = Step("read_file", {"path": "a.py"}, target="a.py", reasoning="why")
    with pytest.raises(TracewakeError, match="needs an embedder"):
        align([a], [b])


def test_align_empty_side_charges_affine_gaps():
    steps = [Step("read_file", {"path": "a.py"}, target="a.py")]
    score, pairs, _ = align(steps, [])
    assert pairs == [(0, None)]
    assert score == GAP_OPEN
    score, pairs, _ = align([], steps)
    assert pairs == [(None, 0)]
    assert score == GAP_OPEN

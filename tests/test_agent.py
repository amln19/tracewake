"""The agent, driven by a scripted model so the test costs nothing and is stable."""

from __future__ import annotations

from collections.abc import Generator
from pathlib import Path

import pytest

import locus
from locus import DecodeParams, Message, ModelCallEvent, ModelResponse, Store, StreamChunk, Usage

from bench import agent


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    root = tmp_path / "repo"
    (root / "pkg").mkdir(parents=True)
    (root / "pkg" / "window.py").write_text(
        "def slice_window(xs, i, n):\n    return xs[i : i + n + 1]\n", encoding="utf-8"
    )
    (root / "pkg" / "test_window.py").write_text("def test_x():\n    pass\n", encoding="utf-8")
    return root


def scripted(replies: list[str]):
    sent: list[int] = []

    def stream(
        model_id: str, messages: list[Message], params: DecodeParams
    ) -> Generator[StreamChunk, None, ModelResponse]:
        # Holds the last reply once the script runs out, so a test can assert
        # where the loop stops without having to predict the turn count.
        text = replies[min(len(sent), len(replies) - 1)]
        sent.append(1)
        for index, piece in enumerate([text[i : i + 9] for i in range(0, len(text), 9)]):
            yield StreamChunk(index=index, text_delta=piece)
        return ModelResponse(
            text=text, finish_reason="end_turn", usage=Usage(input_tokens=10, output_tokens=5)
        )

    return stream


def block(payload: str) -> str:
    return f"Looking at it now.\n\n```json\n{payload}\n```"


def drive(
    store: Path,
    repo: Path,
    replies: list[str],
    green: bool = True,
    max_steps: int | None = None,
) -> tuple:
    calls: list[int] = []

    def run_tests() -> tuple[str, bool]:
        calls.append(1)
        return ("1 failed, 3 passed", green)

    with locus.record("agent", store=store) as session:
        tools = agent.Tools(session, repo, ("pkg",), run_tests)
        model = session.model(provider="test", model_id="test-1", stream_fn=scripted(replies))
        trace = agent.run(
            session,
            model,
            "it is broken",
            tools,
            max_steps=len(replies) if max_steps is None else max_steps,
        )
        session.outcome(status="ok")
        return trace, session.run_id, len(calls)


def test_a_full_turn_reads_edits_and_submits(tmp_path: Path, repo: Path) -> None:
    trace, run_id, test_calls = drive(
        tmp_path / "store",
        repo,
        [
            block('{"action": "read_file", "path": "pkg/window.py"}'),
            block(
                '{"action": "edit_file", "path": "pkg/window.py", '
                '"old": "i + n + 1", "new": "i + n"}'
            ),
            block('{"action": "run_tests"}'),
            block('{"action": "submit"}'),
        ],
    )

    assert trace.actions == ["read_file", "edit_file", "run_tests", "submit"]
    assert (trace.edits, trace.test_runs, trace.submitted) == (1, 1, True)
    assert trace.stop_reason == "submitted"
    assert test_calls == 1
    assert (repo / "pkg" / "window.py").read_text() == (
        "def slice_window(xs, i, n):\n    return xs[i : i + n]\n"
    )


def test_every_context_block_carries_a_provenance_tag(tmp_path: Path, repo: Path) -> None:
    _, run_id, _ = drive(
        tmp_path / "store",
        repo,
        [
            block('{"action": "read_file", "path": "pkg/window.py"}'),
            block('{"action": "run_tests"}'),
            "no action at all in this reply",
            block('{"action": "submit"}'),
        ],
    )

    store = Store(tmp_path / "store")
    calls = [e.event for e in store.events(run_id) if isinstance(e.event, ModelCallEvent)]
    store.close()

    seen = {m.provenance for call in calls for m in call.messages}
    assert None not in seen, "a context block reached the model without an origin label"
    assert seen <= set(agent.PROVENANCE)
    # The four that only appear if the run actually exercised them.
    assert {
        agent.SYSTEM_PROMPT,
        agent.TASK_ISSUE,
        agent.REPO_MAP,
        agent.FILE_READ,
        agent.TEST_OUTPUT,
        agent.ERROR_FEEDBACK,
    } <= seen


def test_a_reply_without_an_action_is_fed_back_rather_than_raising(
    tmp_path: Path, repo: Path
) -> None:
    trace, _, _ = drive(
        tmp_path / "store",
        repo,
        ["I think the problem is somewhere in the window code.", block('{"action": "submit"}')],
    )
    assert trace.parse_failures == 1
    assert trace.actions == ["submit"]


def test_running_out_of_actions_is_recorded_not_raised(tmp_path: Path, repo: Path) -> None:
    for n in range(3):
        (repo / "pkg" / f"n{n}.py").write_text(f"x = {n}\n", encoding="utf-8")
    trace, _, _ = drive(
        tmp_path / "store",
        repo,
        [block('{"action": "read_file", "path": "pkg/n%d.py"}' % n) for n in range(3)],
        max_steps=3,
    )
    assert trace.stop_reason == "step_budget"
    assert trace.actions_taken == 3
    assert not trace.submitted


def test_editing_a_test_file_is_refused(tmp_path: Path, repo: Path) -> None:
    original = (repo / "pkg" / "test_window.py").read_text()
    drive(
        tmp_path / "store",
        repo,
        [
            block(
                '{"action": "edit_file", "path": "pkg/test_window.py", '
                '"old": "pass", "new": "assert False"}'
            ),
            block('{"action": "submit"}'),
        ],
    )
    assert (repo / "pkg" / "test_window.py").read_text() == original


def test_an_ambiguous_edit_is_refused_rather_than_guessing(tmp_path: Path, repo: Path) -> None:
    (repo / "pkg" / "dup.py").write_text("a = 1\nb = 1\n", encoding="utf-8")
    trace, run_id, _ = drive(
        tmp_path / "store",
        repo,
        [
            block('{"action": "edit_file", "path": "pkg/dup.py", "old": "1", "new": "2"}'),
            block('{"action": "submit"}'),
        ],
    )
    assert trace.edits == 0
    assert (repo / "pkg" / "dup.py").read_text() == "a = 1\nb = 1\n"


def test_reading_outside_the_repo_is_refused(tmp_path: Path, repo: Path) -> None:
    secret = tmp_path / "secret.txt"
    secret.write_text("not the agent's business")
    trace, run_id, _ = drive(
        tmp_path / "store",
        repo,
        [
            block('{"action": "read_file", "path": "../secret.txt"}'),
            block('{"action": "submit"}'),
        ],
    )
    store = Store(tmp_path / "store")
    blobs = [
        store.blobs.get(e.event.result.digest).decode()
        for e in store.events(run_id)
        if e.event.type == "tool_call"
    ]
    store.close()
    assert any("cannot read" in b for b in blobs)
    assert not any("not the agent's business" in b for b in blobs)


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ('```json\n{"action": "submit"}\n```', "submit"),
        ('```\n{"action": "run_tests"}\n```', "run_tests"),
        ('some words {"action": "list_files"} and more', "list_files"),
        ('```json\n{"action": "read_file"}\n```\nthen ```json\n{"action": "submit"}\n```', "submit"),
    ],
)
def test_action_parsing_accepts_the_shapes_a_small_model_produces(
    text: str, expected: str
) -> None:
    parsed = agent.parse_action(text)
    assert parsed is not None and parsed["action"] == expected


@pytest.mark.parametrize(
    "text",
    ["no block here", "```json\n{not json at all}\n```", '```json\n{"path": "a.py"}\n```'],
)
def test_action_parsing_rejects_what_it_cannot_use(text: str) -> None:
    assert agent.parse_action(text) is None


def test_the_run_replays_from_the_log_without_the_model(tmp_path: Path, repo: Path) -> None:
    store = tmp_path / "store"
    replies = [
        block('{"action": "read_file", "path": "pkg/window.py"}'),
        block('{"action": "run_tests"}'),
        block('{"action": "submit"}'),
    ]
    recorded, run_id, _ = drive(store, repo, replies)

    def forbidden(*args: object, **kwargs: object) -> None:
        raise AssertionError("replay reached the model backend")

    def forbidden_tests() -> tuple[str, bool]:
        raise AssertionError("replay reached the test runner")

    with locus.replay(run_id, store=store) as session:
        tools = agent.Tools(session, repo, ("pkg",), forbidden_tests)
        model = session.model(provider="test", model_id="test-1", stream_fn=forbidden)
        replayed = agent.run(session, model, "it is broken", tools, max_steps=len(replies))

    assert replayed.actions == recorded.actions
    assert replayed.usage == recorded.usage


def test_the_agent_is_told_which_files_exist_before_its_first_move(
    tmp_path: Path, repo: Path
) -> None:
    """Without the listing a small model invents paths out of its own instructions."""
    _, run_id, _ = drive(tmp_path / "store", repo, [block('{"action": "submit"}')])

    store = Store(tmp_path / "store")
    first = next(
        e.event for e in store.events(run_id) if isinstance(e.event, ModelCallEvent)
    )
    store.close()

    listing = [m for m in first.messages if m.provenance == agent.REPO_MAP]
    assert len(listing) == 1
    assert "pkg/window.py" in listing[0].content

    # Nothing in the prompt may look like a path the agent could copy instead.
    schema = "".join(m.content for m in first.messages if m.provenance == agent.TOOL_SCHEMA)
    assert ".py" not in schema


def test_an_identical_repeated_action_is_refused(tmp_path: Path, repo: Path) -> None:
    """A small model that learns nothing from an action will take it again forever."""
    read = block('{"action": "read_file", "path": "pkg/window.py"}')
    trace, run_id, _ = drive(
        tmp_path / "store", repo, [read, read, read, block('{"action": "submit"}')]
    )

    assert trace.repeats == 2
    assert trace.actions == ["read_file", "read_file", "read_file", "submit"]
    assert trace.actions_taken == 1, "a refused repeat must not spend the action budget"

    store = Store(tmp_path / "store")
    reads = [
        e for e in store.events(run_id)
        if e.event.type == "tool_call" and e.event.name == "read_file"
    ]
    store.close()
    assert len(reads) == 1, "a refused repeat still reached the tool"


def test_the_same_tool_with_different_arguments_is_not_a_repeat(
    tmp_path: Path, repo: Path
) -> None:
    (repo / "pkg" / "other.py").write_text("y = 2\n", encoding="utf-8")
    trace, _, _ = drive(
        tmp_path / "store",
        repo,
        [
            block('{"action": "read_file", "path": "pkg/window.py"}'),
            block('{"action": "read_file", "path": "pkg/other.py"}'),
            block('{"action": "submit"}'),
        ],
    )
    assert trace.repeats == 0


def test_the_remaining_budget_is_shown_near_the_end(tmp_path: Path, repo: Path) -> None:
    for n in range(8):
        (repo / "pkg" / f"m{n}.py").write_text(f"x = {n}\n", encoding="utf-8")
    replies = [
        block('{"action": "read_file", "path": "pkg/m%d.py"}' % n) for n in range(8)
    ]
    _, run_id, _ = drive(tmp_path / "store", repo, replies)

    store = Store(tmp_path / "store")
    calls = [e.event for e in store.events(run_id) if isinstance(e.event, ModelCallEvent)]
    store.close()

    text = "".join(m.content for m in calls[-1].messages)
    assert "step left]" in text or "steps left]" in text
    early = "".join(m.content for m in calls[1].messages)
    assert "steps left]" not in early, "the budget notice should not crowd every turn"


def test_a_stuck_agent_ends_early_instead_of_grinding_out_the_budget(
    tmp_path: Path, repo: Path
) -> None:
    """Refusing a repeat and charging a step is worse than not refusing at all.

    A model that repeats a refused action will keep repeating it, so charging
    the step spends the whole run on refusals and the agent never edits.
    """
    same = block('{"action": "read_file", "path": "pkg/window.py"}')
    trace, _, _ = drive(tmp_path / "store", repo, [same] * 20, max_steps=12)

    assert trace.stop_reason == "stuck"
    assert trace.actions_taken == 1
    assert trace.turns <= agent.MAX_CONSECUTIVE_STALLS + 1, (
        f"a stuck run cost {trace.turns} model calls; it should stop within "
        f"{agent.MAX_CONSECUTIVE_STALLS + 1}"
    )


def test_recovering_from_a_stall_restores_the_full_budget(
    tmp_path: Path, repo: Path
) -> None:
    """Four stalls in total, never three in a row: a model that comes back lives."""
    (repo / "pkg" / "third.py").write_text("z = 3\n", encoding="utf-8")
    same = block('{"action": "read_file", "path": "pkg/window.py"}')
    other = block('{"action": "run_tests"}')
    third = block('{"action": "read_file", "path": "pkg/third.py"}')
    trace, _, _ = drive(
        tmp_path / "store",
        repo,
        [same, same, same, other, same, same, third, block('{"action": "submit"}')],
        max_steps=12,
    )

    assert trace.stop_reason == "submitted"
    assert trace.actions_taken == 3
    assert trace.repeats == 4


def test_turns_are_bounded_even_when_nothing_ever_parses(
    tmp_path: Path, repo: Path
) -> None:
    trace, _, _ = drive(tmp_path / "store", repo, ["no action here"] * 30, max_steps=12)
    assert trace.stop_reason == "stuck"
    assert trace.turns <= agent.MAX_CONSECUTIVE_STALLS + 1


def test_a_large_file_is_windowed_rather_than_gutted(tmp_path: Path, repo: Path) -> None:
    """Clipping the middle hands the agent a file that provably lacks its bug."""
    lines = [f"line_{n} = {n}" for n in range(1, 1201)]
    lines[599] = "BUG_IS_HERE = True"
    (repo / "pkg" / "big.py").write_text("\n".join(lines) + "\n", encoding="utf-8")

    trace, run_id, _ = drive(
        tmp_path / "store",
        repo,
        [
            block('{"action": "read_file", "path": "pkg/big.py", "around": 600}'),
            block('{"action": "submit"}'),
        ],
    )

    store = Store(tmp_path / "store")
    read = next(
        e.event for e in store.events(run_id)
        if e.event.type == "tool_call" and e.event.name == "read_file"
    )
    body = store.blobs.get(read.result.digest).decode()
    store.close()

    assert "BUG_IS_HERE" in body, "the agent could not see the line it was pointed at"
    assert "1200 lines" in body
    assert "line_1 = 1\n" not in body, "a window should not also carry the whole file"


def test_a_whole_file_read_of_a_big_file_says_how_to_move_the_window(
    tmp_path: Path, repo: Path
) -> None:
    (repo / "pkg" / "big.py").write_text(
        "\n".join(f"x{n} = {n}" for n in range(4000)), encoding="utf-8"
    )
    _, run_id, _ = drive(
        tmp_path / "store",
        repo,
        [block('{"action": "read_file", "path": "pkg/big.py"}'), block('{"action": "submit"}')],
    )
    store = Store(tmp_path / "store")
    read = next(
        e.event for e in store.events(run_id)
        if e.event.type == "tool_call" and e.event.name == "read_file"
    )
    body = store.blobs.get(read.result.digest).decode()
    store.close()
    assert '"around"' in body, "a truncated read must tell the agent how to see the rest"


def test_search_shows_the_code_around_a_hit(tmp_path: Path, repo: Path) -> None:
    _, run_id, _ = drive(
        tmp_path / "store",
        repo,
        [block('{"action": "search", "query": "slice_window"}'), block('{"action": "submit"}')],
    )
    store = Store(tmp_path / "store")
    hit = next(
        e.event for e in store.events(run_id)
        if e.event.type == "tool_call" and e.event.name == "search"
    )
    body = store.blobs.get(hit.result.digest).decode()
    store.close()
    # The line that follows the definition is the one carrying the bug.
    assert "return xs[i : i + n + 1]" in body
    assert "pkg/window.py:1" in body


def test_an_ambiguous_edit_names_the_lines_it_could_mean(tmp_path: Path, repo: Path) -> None:
    """"Appears twice" is a dead end; the lines are what make it actionable."""
    (repo / "pkg" / "dup.py").write_text("a = 1\nb = 2\na = 1\n", encoding="utf-8")
    _, run_id, _ = drive(
        tmp_path / "store",
        repo,
        [
            block('{"action": "edit_file", "path": "pkg/dup.py", "old": "a = 1", "new": "a = 9"}'),
            block('{"action": "submit"}'),
        ],
    )
    store = Store(tmp_path / "store")
    body = store.blobs.get(
        next(
            e.event.result.digest for e in store.events(run_id)
            if e.event.type == "tool_call" and e.event.name == "edit_file"
        )
    ).decode()
    store.close()
    assert "lines 1, 3" in body
    assert '"at"' in body


def test_a_line_anchor_picks_which_occurrence_to_edit(tmp_path: Path, repo: Path) -> None:
    (repo / "pkg" / "dup.py").write_text("a = 1\nb = 2\na = 1\n", encoding="utf-8")
    trace, _, _ = drive(
        tmp_path / "store",
        repo,
        [
            block(
                '{"action": "edit_file", "path": "pkg/dup.py", "old": "a = 1", '
                '"new": "a = 9", "at": 3}'
            ),
            block('{"action": "submit"}'),
        ],
    )
    assert trace.edits == 1
    assert (repo / "pkg" / "dup.py").read_text() == "a = 1\nb = 2\na = 9\n"


def test_a_line_anchor_sent_as_a_string_still_works(tmp_path: Path, repo: Path) -> None:
    """Small models quote their numbers about half the time."""
    (repo / "pkg" / "dup.py").write_text("a = 1\nb = 2\na = 1\n", encoding="utf-8")
    trace, _, _ = drive(
        tmp_path / "store",
        repo,
        [
            block(
                '{"action": "edit_file", "path": "pkg/dup.py", "old": "a = 1", '
                '"new": "a = 9", "at": "3"}'
            ),
            block('{"action": "submit"}'),
        ],
    )
    assert trace.edits == 1
    assert (repo / "pkg" / "dup.py").read_text() == "a = 1\nb = 2\na = 9\n"


def test_an_unambiguous_edit_still_needs_no_anchor(tmp_path: Path, repo: Path) -> None:
    trace, _, _ = drive(
        tmp_path / "store",
        repo,
        [
            block(
                '{"action": "edit_file", "path": "pkg/window.py", '
                '"old": "i + n + 1", "new": "i + n"}'
            ),
            block('{"action": "submit"}'),
        ],
    )
    assert trace.edits == 1


def test_an_edit_makes_earlier_reads_and_tests_repeatable_again(
    tmp_path: Path, repo: Path
) -> None:
    """An edit changes what a read returns, so checking your work is not a repeat."""
    read = block('{"action": "read_file", "path": "pkg/window.py"}')
    test = block('{"action": "run_tests"}')
    edit = block(
        '{"action": "edit_file", "path": "pkg/window.py", "old": "i + n + 1", "new": "i + n"}'
    )
    trace, run_id, test_calls = drive(
        tmp_path / "store",
        repo,
        [read, test, edit, read, test, block('{"action": "submit"}')],
    )

    assert trace.repeats == 0, "the agent was refused permission to check its own edit"
    # Five tool actions; submit ends the run rather than spending budget.
    assert trace.actions_taken == 5
    assert trace.actions[-1] == "submit"
    assert test_calls == 2, "the second test run never reached the runner"


def test_an_edit_echoes_what_it_actually_wrote(tmp_path: Path, repo: Path) -> None:
    """Otherwise a repair has to guess at the file's current contents."""
    _, run_id, _ = drive(
        tmp_path / "store",
        repo,
        [
            block(
                '{"action": "edit_file", "path": "pkg/window.py", '
                '"old": "i + n + 1", "new": "i + n"}'
            ),
            block('{"action": "submit"}'),
        ],
    )
    store = Store(tmp_path / "store")
    body = store.blobs.get(
        next(
            e.event.result.digest for e in store.events(run_id)
            if e.event.type == "tool_call" and e.event.name == "edit_file"
        )
    ).decode()
    store.close()
    assert "now reads" in body
    assert "return xs[i : i + n]" in body


def test_an_edit_that_breaks_the_file_says_so_at_once(tmp_path: Path, repo: Path) -> None:
    """Real case: the model wrote two statements on one line separated by spaces,
    which no amount of re-indentation can rescue."""
    (repo / "pkg" / "cache.py").write_text(
        "def get(store, key):\n"
        "    if key in store:\n"
        "        return store[key]\n"
        "    return None\n",
        encoding="utf-8",
    )
    trace, run_id, _ = drive(
        tmp_path / "store",
        repo,
        [
            block(
                '{"action": "edit_file", "path": "pkg/cache.py", '
                '"old": "return None", '
                '"new": "if store is None: return None  return store"}'
            ),
            block('{"action": "submit"}'),
        ],
    )
    store = Store(tmp_path / "store")
    edit = next(
        e.event for e in store.events(run_id)
        if e.event.type == "tool_call" and e.event.name == "edit_file"
    )
    body = store.blobs.get(edit.result.digest).decode()
    store.close()
    assert edit.status == "error"
    assert "syntax error" in body
    assert "now reads" in body, "the agent needs to see the broken state to repair it"


def test_old_observations_are_elided_to_bound_the_context(tmp_path: Path, repo: Path) -> None:
    """Inference cost tracks context, and an unbounded history makes late turns dear."""
    for n in range(7):
        (repo / "pkg" / f"f{n}.py").write_text(f"UNIQUE_MARKER_{n} = {n}\n", encoding="utf-8")
    replies = [block('{"action": "read_file", "path": "pkg/f%d.py"}' % n) for n in range(7)]
    _, run_id, _ = drive(tmp_path / "store", repo, replies + [block('{"action": "submit"}')])

    store = Store(tmp_path / "store")
    calls = [e.event for e in store.events(run_id) if isinstance(e.event, ModelCallEvent)]
    store.close()

    last = "".join(m.content for m in calls[-1].messages)
    assert "UNIQUE_MARKER_6" in last, "the newest observation must survive"
    assert "UNIQUE_MARKER_0" not in last, "the oldest observation should be elided"
    assert agent.ELIDED in last

    # Setup context is never dropped: it is what the run is about.
    assert any(m.provenance == agent.TASK_ISSUE and "broken" in m.content
               for m in calls[-1].messages)
    assert any(m.provenance == agent.REPO_MAP and "pkg/" in m.content
               for m in calls[-1].messages)


def test_context_stops_growing_once_the_window_is_full(tmp_path: Path, repo: Path) -> None:
    for n in range(10):
        (repo / "pkg" / f"g{n}.py").write_text(("x = 1\n" * 200), encoding="utf-8")
    replies = [block('{"action": "read_file", "path": "pkg/g%d.py"}' % n) for n in range(10)]
    _, run_id, _ = drive(tmp_path / "store", repo, replies + [block('{"action": "submit"}')])

    store = Store(tmp_path / "store")
    sizes = [
        sum(len(m.content) for m in e.event.messages)
        for e in store.events(run_id)
        if isinstance(e.event, ModelCallEvent)
    ]
    store.close()
    assert max(sizes[6:]) < sizes[5] * 1.5, f"context still growing: {sizes}"


def test_an_elided_observation_can_be_asked_for_again(tmp_path: Path, repo: Path) -> None:
    """Telling the agent to repeat an action and then refusing it would be a trap."""
    for n in range(6):
        (repo / "pkg" / f"h{n}.py").write_text(f"y = {n}\n", encoding="utf-8")
    first = block('{"action": "read_file", "path": "pkg/h0.py"}')
    others = [block('{"action": "read_file", "path": "pkg/h%d.py"}' % n) for n in range(1, 6)]
    trace, _, _ = drive(tmp_path / "store", repo, [first, *others, first, block('{"action": "submit"}')])

    assert trace.repeats == 0, "a re-read of dropped content was refused as a repeat"


def test_an_edit_matches_even_when_the_indentation_was_stripped(
    tmp_path: Path, repo: Path
) -> None:
    """The model copies code out of a line-numbered view and loses the leading indent.

    Nineteen percent of edits in a real batch failed for exactly this and nothing
    else, which has nothing to do with whether the agent found the bug.
    """
    (repo / "pkg" / "cmp.py").write_text(
        "def compare(rc1, rc2):\n"
        "    if not rc1:\n"
        "        return 1\n"
        "    return 0\n",
        encoding="utf-8",
    )
    trace, _, _ = drive(
        tmp_path / "store",
        repo,
        [
            block(
                '{"action": "edit_file", "path": "pkg/cmp.py", '
                '"old": "if not rc1:\\nreturn 1", '
                '"new": "if not rc1:\\n    return -1"}'
            ),
            block('{"action": "submit"}'),
        ],
    )
    assert trace.edits == 1, "a correct snippet was rejected over whitespace"
    # And the replacement is re-indented into the block it landed in.
    assert (repo / "pkg" / "cmp.py").read_text() == (
        "def compare(rc1, rc2):\n"
        "    if not rc1:\n"
        "        return -1\n"
        "    return 0\n"
    )


def test_a_stripped_single_line_snippet_also_matches(tmp_path: Path, repo: Path) -> None:
    trace, _, _ = drive(
        tmp_path / "store",
        repo,
        [
            block(
                '{"action": "edit_file", "path": "pkg/window.py", '
                '"old": "return xs[i : i + n + 1]", "new": "return xs[i : i + n]"}'
            ),
            block('{"action": "submit"}'),
        ],
    )
    assert trace.edits == 1
    assert (repo / "pkg" / "window.py").read_text() == (
        "def slice_window(xs, i, n):\n    return xs[i : i + n]\n"
    )


def test_a_genuinely_absent_snippet_is_still_refused(tmp_path: Path, repo: Path) -> None:
    trace, run_id, _ = drive(
        tmp_path / "store",
        repo,
        [
            block(
                '{"action": "edit_file", "path": "pkg/window.py", '
                '"old": "this text is nowhere in the file", "new": "x"}'
            ),
            block('{"action": "submit"}'),
        ],
    )
    assert trace.edits == 0, "tolerant matching must not invent a match"


def test_search_finds_matches_in_test_files_too(tmp_path: Path, repo: Path) -> None:
    """The issue text names failing tests, and search must be able to find them."""
    trace, run_id, _ = drive(
        tmp_path / "store",
        repo,
        [
            block('{"action": "search", "query": "test_x"}'),
            block('{"action": "submit"}'),
        ],
    )
    store = Store(tmp_path / "store")
    hit = next(
        e.event for e in store.events(run_id)
        if e.event.type == "tool_call" and e.event.name == "search"
    )
    body = store.blobs.get(hit.result.digest).decode()
    store.close()
    assert "pkg/test_window.py" in body
    assert "no matches" not in body


def test_a_multi_term_search_gets_a_specific_hint(tmp_path: Path, repo: Path) -> None:
    _, run_id, _ = drive(
        tmp_path / "store",
        repo,
        [
            block('{"action": "search", "query": "foo\\nbar"}'),
            block('{"action": "submit"}'),
        ],
    )
    store = Store(tmp_path / "store")
    body = store.blobs.get(
        next(
            e.event.result.digest for e in store.events(run_id)
            if e.event.type == "tool_call" and e.event.name == "search"
        )
    ).decode()
    store.close()
    assert "one identifier at a time" in body


def test_retrying_a_stale_snippet_shows_the_current_file_not_just_an_error(
    tmp_path: Path, repo: Path
) -> None:
    """Taken from a real run: a stale 'old' after a broken edit repeated forever
    because the error told it to re-read rather than showing the current state."""
    trace, run_id, _ = drive(
        tmp_path / "store",
        repo,
        [
            block(
                '{"action": "edit_file", "path": "pkg/window.py", '
                '"old": "does not exist anywhere", "new": "x"}'
            ),
            block('{"action": "submit"}'),
        ],
    )
    store = Store(tmp_path / "store")
    body = store.blobs.get(
        next(
            e.event.result.digest for e in store.events(run_id)
            if e.event.type == "tool_call" and e.event.name == "edit_file"
        )
    ).decode()
    store.close()
    assert "currently reads" in body
    assert "def slice_window" in body


def test_a_multiline_repair_matches_even_when_offset_by_whitespace(
    tmp_path: Path, repo: Path
) -> None:
    """Reproduces a real 14B trap: the first edit breaks the file, and the
    repair's snippet — correct but re-typed without the file's original
    indentation — was rejected as 'no match' because the fallback search window
    was sized for one line while the snippet spanned two.
    """
    (repo / "pkg" / "cond.py").write_text(
        "def f(x):\n    if x:\n        return 1\n    return 0\n", encoding="utf-8"
    )
    trace, run_id, _ = drive(
        tmp_path / "store",
        repo,
        [
            block(
                '{"action": "edit_file", "path": "pkg/cond.py", '
                '"old": "if x:\\n        return 1", "new": "if x:\\nreturn 1"}'
            ),
            block(
                '{"action": "edit_file", "path": "pkg/cond.py", '
                '"old": "if x:\\nreturn 1", "new": "if x:\\n        return -1"}'
            ),
            block('{"action": "submit"}'),
        ],
    )
    # The first edit legitimately breaks the file and is not counted; the second
    # is the repair, and it must be *found* rather than rejected as "no match" by
    # too narrow a fallback search window. Exact indentation of the repair is not
    # what this test pins: a model that sends its replacement body at the file's
    # absolute indent rather than relative to its own first line lands deeper than
    # it meant to, which parses and means the same thing.
    import ast

    assert trace.edits == 1, "the repair's snippet was rejected as a false no-match"
    result = (repo / "pkg" / "cond.py").read_text()
    ast.parse(result)
    assert "return -1" in result, "the repair did not land"
    assert "return 1" not in result, "the original body survived the repair"


def test_both_edit_paths_reindent_a_multiline_replacement(tmp_path: Path, repo: Path) -> None:
    """The exact-match path spliced verbatim while the tolerant one re-indented,
    so identical inputs produced a valid file down one branch and a broken one
    down the other. The exact path is the common one: a single-line `old` almost
    always matches as a substring.
    """
    import ast

    from bench.agent import _fuzzy_replace, _replace_at

    text = (
        "class C:\n"
        "    def get(self, k):\n"
        "        try:\n"
        "            return self.d[k]\n"
        "        except KeyError:\n"
        "            ret = self.miss(k)\n"
        "            return ret\n"
    )
    old = "ret = self.miss(k)"
    new = "if self.miss is not None:\n    ret = self.miss(k)"

    exact = _replace_at(text, old, new, 6)
    fuzzy, _ = _fuzzy_replace(text, old, new, 6)
    ast.parse(exact)
    ast.parse(fuzzy)
    assert exact == fuzzy, "the two paths disagree on the same inputs"


def test_a_single_line_replacement_is_left_alone(tmp_path: Path, repo: Path) -> None:
    """Re-indentation must not touch a replacement that spans one line."""
    from bench.agent import _replace_at

    text = "def f(x):\n    return x + 1\n"
    assert _replace_at(text, "x + 1", "x - 1", 2) == "def f(x):\n    return x - 1\n"


def test_the_file_listing_goes_through_the_recorder(tmp_path: Path, repo: Path) -> None:
    """Which files exist is an input the agent acts on, so it belongs in the log:
    a replay against a differently-populated tree must raise, not quietly return
    a different answer."""
    (repo / "pkg" / "sub").mkdir()
    (repo / "pkg" / "sub" / "deep.py").write_text("z = 1\n", encoding="utf-8")
    (repo / "pkg" / "notes.txt").write_text("not python\n", encoding="utf-8")

    _, run_id, _ = drive(
        tmp_path / "store",
        repo,
        [block('{"action": "search", "query": "z = 1"}'), block('{"action": "submit"}')],
    )

    store = Store(tmp_path / "store")
    events = store.events(run_id)
    listings = [e.event for e in events if e.event.type == "fs_read" and e.event.kind == "listing"]
    store.close()

    assert listings, "the directory walk never reached the log"
    assert any("pkg" in e.path for e in listings)


def test_the_listing_is_walked_once_not_per_search(tmp_path: Path, repo: Path) -> None:
    """Re-walking on every search would put a listing event in the log for every
    directory, several times a run."""
    _, run_id, _ = drive(
        tmp_path / "store",
        repo,
        [
            block('{"action": "search", "query": "def"}'),
            block('{"action": "search", "query": "return"}'),
            block('{"action": "search", "query": "xs"}'),
            block('{"action": "submit"}'),
        ],
    )
    store = Store(tmp_path / "store")
    listings = [
        e.event for e in store.events(run_id)
        if e.event.type == "fs_read" and e.event.kind == "listing"
    ]
    store.close()
    roots = [e for e in listings if e.path in {".", ""}]
    assert len(roots) == 1, f"the tree was walked {len(roots)} times for three searches"

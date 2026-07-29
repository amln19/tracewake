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
        text = replies[len(sent)]
        sent.append(1)
        for index, piece in enumerate([text[i : i + 9] for i in range(0, len(text), 9)]):
            yield StreamChunk(index=index, text_delta=piece)
        return ModelResponse(
            text=text, finish_reason="end_turn", usage=Usage(input_tokens=10, output_tokens=5)
        )

    return stream


def block(payload: str) -> str:
    return f"Looking at it now.\n\n```json\n{payload}\n```"


def drive(store: Path, repo: Path, replies: list[str], green: bool = True) -> tuple:
    calls: list[int] = []

    def run_tests() -> tuple[str, bool]:
        calls.append(1)
        return ("1 failed, 3 passed", green)

    with locus.record("agent", store=store) as session:
        tools = agent.Tools(session, repo, ("pkg",), run_tests)
        model = session.model(provider="test", model_id="test-1", stream_fn=scripted(replies))
        trace = agent.run(session, model, "it is broken", tools, max_steps=len(replies))
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


def test_running_out_of_steps_is_recorded_not_raised(tmp_path: Path, repo: Path) -> None:
    trace, _, _ = drive(
        tmp_path / "store", repo, [block('{"action": "list_files"}')] * 3
    )
    assert trace.stop_reason == "step_budget"
    assert trace.steps == 3
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

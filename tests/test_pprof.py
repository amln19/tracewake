"""Token pprof: attributed totals equal measured usage, and the profile decodes."""

from __future__ import annotations

import gzip
import subprocess
import sys
from pathlib import Path

import pytest

import tracewake
from tracewake import (
    DecodeParams,
    Message,
    ModelResponse,
    Store,
    ToolCallRequest,
    ToolOutcome,
    Usage,
)
from tracewake.pprof import (
    RESPONSE_LEAF,
    attribute_tokens,
    build_token_profile,
    decode_profile,
    format_top,
    gzip_profile,
    proportional,
    read_gzipped_profile,
    sample_totals,
    usage_totals,
    write_token_profile,
)


def test_proportional_parts_sum_to_the_total():
    # Deterministic: earliest index wins ties on remainder.
    assert proportional(10, [1, 1, 1]) == [4, 3, 3]
    assert sum(proportional(10, [1, 1, 1])) == 10
    assert proportional(100, [2, 3, 5]) == [20, 30, 50]
    assert proportional(7, [0, 0, 0]) == [7, 0, 0]
    assert proportional(0, [4, 1]) == [0, 0]
    assert proportional(5, []) == []


def test_proportional_rejects_negatives():
    with pytest.raises(ValueError, match="negative"):
        proportional(-1, [1])
    with pytest.raises(ValueError, match="non-negative"):
        proportional(3, [1, -1])


def _record(store: Path, *, big: str = "file contents " * 50) -> str:
    turn = 0
    script = [
        (
            "Reading the helper.",
            "read_file",
            {"path": "a.py"},
            big,
            Usage(input_tokens=100, output_tokens=10),
        ),
        (
            "Editing it.",
            "edit_file",
            {"path": "a.py", "old": "x", "new": "y"},
            "patched",
            Usage(input_tokens=250, output_tokens=20),
        ),
    ]

    def create(model_id: str, messages: list[Message], params: DecodeParams) -> ModelResponse:
        nonlocal turn
        reasoning, tool, args, _, usage = script[turn]
        response = ModelResponse(
            text=reasoning,
            tool_calls=[ToolCallRequest(id=f"t{turn}", name=tool, args=args, batch_index=0)],
            finish_reason="tool_use",
            usage=usage,
        )
        turn += 1
        return response

    def dispatch(name: str, args: dict) -> ToolOutcome:
        for _, tool, tool_args, content, _ in script:
            if tool == name and tool_args == args:
                return ToolOutcome(content=content)
        raise AssertionError(f"unexpected tool {name} {args}")

    with tracewake.record("spend", store=store, task_id="win-off_by_one-1") as rec:
        model = rec.model(provider="acme", model_id="acme-1", create_fn=create)
        tools = rec.tools(dispatch_fn=dispatch)
        messages = [
            Message(role="system", content="sys " * 20, provenance="system_prompt"),
            Message(role="user", content="fix the bug", provenance="task_issue"),
        ]
        for _ in script:
            call = model.create(messages=messages)
            messages.append(
                Message(
                    role="assistant",
                    content=call.response.text,
                    provenance="assistant_reasoning",
                )
            )
            for request in call.response.tool_calls:
                outcome = tools.call(call.call_id, request)
                messages.append(
                    Message(
                        role="tool",
                        content=outcome.content,
                        tool_call_id=request.id,
                        provenance="file_read" if request.name == "read_file" else "tool_output",
                    )
                )
        rec.outcome(status="ok")
        return rec.run_id


def test_attributed_totals_equal_recorded_usage(tmp_path: Path):
    run_id = _record(tmp_path)
    db = Store(tmp_path)
    header = db.run(run_id)
    events = db.events(run_id)
    db.close()

    shares = attribute_tokens(header, events)
    inp = sum(s.input_tokens for s in shares)
    out = sum(s.output_tokens for s in shares)
    assert (inp, out) == usage_totals(events)
    assert (inp, out) == (350, 30)
    assert any(s.leaf == RESPONSE_LEAF and s.output_tokens for s in shares)
    assert any(s.leaf == "system_prompt" and s.input_tokens for s in shares)
    # Second turn resends the first tool result; both file_read shares merge by stack? 
    # No — different turns, different stacks. Same leaf name can appear on multiple turns.
    assert {s.turn for s in shares} == {0, 1}


def test_profile_sample_totals_match_usage(tmp_path: Path):
    run_id = _record(tmp_path)
    db = Store(tmp_path)
    header = db.run(run_id)
    events = db.events(run_id)
    path = tmp_path / "run.pb.gz"
    write_token_profile(path, header, events)
    db.close()

    decoded = read_gzipped_profile(path)
    assert decoded["sample_types"] == [
        ("input_tokens", "count"),
        ("output_tokens", "count"),
    ]
    assert decoded["strings"][0] == ""
    assert sample_totals(decoded) == (350, 30)

    # Every sample location resolves; leaf is first in the stack.
    for sample in decoded["samples"]:
        assert len(sample["values"]) == 2
        assert sample["location_ids"]
        for lid in sample["location_ids"]:
            fid = decoded["locations"][lid]
            assert fid in decoded["functions"]
        leaf_fid = decoded["locations"][sample["location_ids"][0]]
        leaf = decoded["functions"][leaf_fid]
        assert leaf  # provenance tag or "response"


def test_raw_profile_round_trips_through_gzip(tmp_path: Path):
    run_id = _record(tmp_path)
    db = Store(tmp_path)
    raw = build_token_profile(db.run(run_id), db.events(run_id))
    db.close()
    assert decode_profile(raw)["strings"][0] == ""
    path = tmp_path / "x.pb.gz"
    with gzip.open(path, "wb") as fh:
        fh.write(raw)
    assert read_gzipped_profile(path)["sample_types"][0][0] == "input_tokens"


def test_stack_is_run_model_turn_leaf(tmp_path: Path):
    run_id = _record(tmp_path)
    db = Store(tmp_path)
    decoded = decode_profile(build_token_profile(db.run(run_id), db.events(run_id)))
    db.close()
    names = set(decoded["functions"].values())
    assert "run:win-off_by_one-1" in names
    assert "model:acme-1" in names
    assert "turn:1" in names
    assert "turn:2" in names
    assert "system_prompt" in names
    assert RESPONSE_LEAF in names


def test_profile_bytes_do_not_depend_on_when_or_where_they_were_written(tmp_path: Path):
    run_id = _record(tmp_path)
    db = Store(tmp_path)
    header, events = db.run(run_id), db.events(run_id)
    db.close()
    first, second = tmp_path / "first.pb.gz", tmp_path / "second-name.pb.gz"
    write_token_profile(first, header, events)
    write_token_profile(second, header, events)

    assert first.read_bytes() == second.read_bytes()
    flags, mtime = first.read_bytes()[3], first.read_bytes()[4:8]
    assert flags == 0  # no original file name, no comment
    assert mtime == b"\x00\x00\x00\x00"
    assert gzip_profile(build_token_profile(header, events)) == first.read_bytes()


def test_format_top_mentions_proportional_split(tmp_path: Path):
    run_id = _record(tmp_path)
    db = Store(tmp_path)
    text = format_top(db.run(run_id), db.events(run_id), n=5)
    db.close()
    assert "total  input=350  output=30" in text
    assert "proportional" in text


def test_pprof_cli_writes_gzipped_profile(tmp_path: Path):
    run_id = _record(tmp_path)
    out = tmp_path / "tokens.pb.gz"
    done = subprocess.run(
        [
            sys.executable, "-m", "tracewake", "pprof", run_id,
            "--view", "tokens", "-o", str(out), "--store", str(tmp_path),
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    assert done.returncode == 0, done.stderr
    assert "350 input + 30 output" in done.stdout
    assert out.exists()
    assert sample_totals(read_gzipped_profile(out)) == (350, 30)


def test_pprof_cli_rejects_unknown_view(tmp_path: Path):
    run_id = _record(tmp_path)
    done = subprocess.run(
        [
            sys.executable, "-m", "tracewake", "pprof", run_id,
            "--view", "cpu", "-o", str(tmp_path / "x.pb.gz"), "--store", str(tmp_path),
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    assert done.returncode != 0
    assert "tokens" in (done.stderr + done.stdout)


def test_empty_messages_still_account_for_usage(tmp_path: Path):
    def create(model_id, messages, params):
        return ModelResponse(
            text="", finish_reason="end_turn", usage=Usage(input_tokens=9, output_tokens=1)
        )

    with tracewake.record("empty", store=tmp_path) as rec:
        model = rec.model(provider="p", model_id="m", create_fn=create)
        model.create(messages=[])
        rec.outcome(status="ok")
        run_id = rec.run_id

    db = Store(tmp_path)
    shares = attribute_tokens(db.run(run_id), db.events(run_id))
    db.close()
    assert sum(s.input_tokens for s in shares) == 9
    assert sum(s.output_tokens for s in shares) == 1

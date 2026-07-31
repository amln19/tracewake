"""The HTML report opens with no server, matches the diff, and stays in budget."""

from __future__ import annotations

import json
import re
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

import locus
from locus import (
    DecodeParams,
    Message,
    ModelResponse,
    Store,
    ToolCallRequest,
    ToolOutcome,
    Usage,
)
from locus.align import LexicalEmbedder, Step, diff_runs
from locus.report import build_payload, render, write_report

SYSTEM = "You are a coding agent. Read before you edit." * 12

# (reasoning, tool name, args, tool output)
Script = list[tuple[str, str, dict[str, Any], str]]

SOURCE = "def slice_window(xs, i, n):\n    return xs[i : i + n]\n" * 120

GOOD: Script = [
    ("The failing assert is in the window helper.", "read_file", {"path": "src/win.py"}, SOURCE),
    ("Off by one on the upper bound.", "edit_file", {"path": "src/win.py", "old": "n", "new": "n-1"}, "patched"),
    ("Running the suite.", "run_tests", {}, "12 passed"),
]

BAD: Script = [
    ("The failing assert is in the window helper.", "read_file", {"path": "src/win.py"}, SOURCE),
    ("Maybe the caller is wrong.", "search", {"query": "slice_window"}, "3 hits"),
    ("Editing the test instead.", "edit_file", {"path": "tests/test_win.py", "old": "a", "new": "b"}, "patched"),
    ("Running the suite.", "run_tests", {}, "1 failed, 11 passed"),
    ("Still red. Looking elsewhere.", "search", {"query": "bounds"}, "1 hit"),
]


def _record(store: Path, name: str, script: Script, *, resolve: bool) -> str:
    turn = 0

    def create(model_id: str, messages: list[Message], params: DecodeParams) -> ModelResponse:
        reasoning, tool, args, _ = script[turn]
        return ModelResponse(
            text=reasoning,
            tool_calls=[ToolCallRequest(id=f"t{turn}", name=tool, args=args, batch_index=0)],
            finish_reason="tool_use",
            usage=Usage(input_tokens=100 * (turn + 1), output_tokens=len(reasoning)),
        )

    def dispatch(tool: str, args: dict[str, Any]) -> ToolOutcome:
        return ToolOutcome(content=next(out for _, n, a, out in script if n == tool and a == args))

    with locus.record(name, store=store, task_id="win-off_by_one-1") as rec:
        model = rec.model(provider="acme", model_id="acme-1", create_fn=create)
        tools = rec.tools(dispatch_fn=dispatch)
        messages = [
            Message(role="system", content=SYSTEM, provenance="system_prompt"),
            Message(role="user", content="the window slice is wrong", provenance="task_issue"),
        ]
        for turn in range(len(script)):
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
                        provenance="tool_output",
                    )
                )
        rec.outcome(status="ok", coverage=True, resolve=resolve, test_summary="from the suite")
        return rec.run_id


@pytest.fixture
def pair(tmp_path: Path) -> tuple[Store, str, str]:
    store = Store(tmp_path / "store")
    store.close()
    good = _record(tmp_path / "store", "good", GOOD, resolve=True)
    bad = _record(tmp_path / "store", "bad", BAD, resolve=False)
    db = Store(tmp_path / "store")
    return db, good, bad


def _built(db: Store, good: str, bad: str, budget: int = 5_000_000) -> dict[str, Any]:
    good_events = db.events(good)
    bad_events = db.events(bad)
    result = diff_runs(good_events, bad_events, embed=LexicalEmbedder())
    return build_payload(
        db.run(good),
        good_events,
        db.run(bad),
        bad_events,
        result,
        blobs=db.blobs,
        store_path=str(db.root),
        budget=budget,
    )


def _island(html: str) -> dict[str, Any]:
    match = re.search(r'id="locus-data">(.*?)</script>', html, re.S)
    assert match is not None, "the report has no embedded data island"
    return json.loads(match.group(1))


def test_the_page_carries_its_data_and_references_nothing_external(
    pair: tuple[Store, str, str],
) -> None:
    db, good, bad = pair
    html = render(_built(db, good, bad), title="win-off_by_one-1")

    # A report that fetches anything is not self-contained: it would break from a
    # file:// page, offline, or behind a README link. Recorded content may name a
    # URL of its own, so only the page around the data island is checked.
    page = re.sub(r'id="locus-data">.*?</script>', "", html, flags=re.S)
    assert not re.search(r'(?:src|href)\s*=\s*"(?!#)', page)
    assert "http://" not in page and "https://" not in page
    assert "@import" not in page and "url(" not in page
    assert _island(html)["good"]["run_id"] == good


def test_recorded_content_cannot_close_the_data_island(pair: tuple[Store, str, str]) -> None:
    db, good, bad = pair
    payload = _built(db, good, bad)
    payload["blocks"][0]["text"] = "</script><script>alert(1)</script>"
    html = render(payload, title="t")
    assert "<script>alert(1)" not in html
    assert _island(html)["blocks"][0]["text"] == "</script><script>alert(1)</script>"


def test_the_report_and_the_diff_name_the_same_divergence(
    pair: tuple[Store, str, str],
) -> None:
    db, good, bad = pair
    result = diff_runs(db.events(good), db.events(bad), embed=LexicalEmbedder())
    payload = _built(db, good, bad)
    assert result.divergence is not None
    assert payload["divergence"] == result.divergence
    assert len(payload["columns"]) == len(result.alignment)
    assert [c["g"] for c in payload["columns"]] == [i for i, _ in result.alignment]
    assert [c["b"] for c in payload["columns"]] == [j for _, j in result.alignment]


def test_the_spend_panel_adds_up_to_the_recorded_usage(pair: tuple[Store, str, str]) -> None:
    db, good, bad = pair
    payload = _built(db, good, bad)

    for side in ("good", "bad"):
        run = payload[side]
        assert run["spend"], "no spend breakdown"
        assert sum(r["input_tokens"] for r in run["spend"]) == run["usage"]["input_tokens"]
        assert sum(r["output_tokens"] for r in run["spend"]) == run["usage"]["output_tokens"]
        # Heaviest first, so the panel reads as a ranking without the page sorting.
        weights = [r["input_tokens"] + r["output_tokens"] for r in run["spend"]]
        assert weights == sorted(weights, reverse=True)


def test_each_context_block_can_name_the_call_that_would_neutralize_it(
    pair: tuple[Store, str, str],
) -> None:
    db, good, bad = pair
    payload = _built(db, good, bad)

    # A step's turn is its model call, which is what an intervention addresses.
    # Without it the page would offer a command aimed at the wrong turn.
    for side in ("good", "bad"):
        turns = [s["turn"] for s in payload["steps"][side]]
        assert turns == sorted(turns)
        assert all(t is not None for t in turns)


def test_repeated_context_is_stored_once(pair: tuple[Store, str, str]) -> None:
    """Why the cap is reachable at all: a prompt resent every turn is one block."""
    db, good, bad = pair
    payload = _built(db, good, bad)
    system = [b for b in payload["blocks"] if b["provenance"] == "system_prompt"]
    assert len(system) == 1
    # Both runs send it on every turn, and both index the same block.
    for side in ("good", "bad"):
        for step in payload["steps"][side]:
            assert payload["blocks"].index(system[0]) in step["context"]


def test_a_tight_budget_clips_context_and_says_what_it_dropped(
    pair: tuple[Store, str, str],
) -> None:
    db, good, bad = pair
    full = _built(db, good, bad)
    assert full["truncation"] == {"blocks": 0, "chars": 0, "limit": None}

    budget = 12_000
    clipped = _built(db, good, bad, budget=budget)
    assert len(json.dumps(clipped, ensure_ascii=False, separators=(",", ":"))) <= budget
    assert clipped["truncation"]["blocks"] > 0
    assert clipped["truncation"]["chars"] > 0
    limit = clipped["truncation"]["limit"]
    assert all(len(b["text"]) <= limit for b in clipped["blocks"])
    assert any(b["clipped"] for b in clipped["blocks"])
    # The step structure survives clipping; only text is cut.
    assert clipped["divergence"] == full["divergence"]
    assert clipped["steps"] == full["steps"]
    # And the pointer back to the full text is on the page.
    assert clipped["store"] == str(db.root)


def test_an_impossible_budget_fails_instead_of_writing_a_broken_page(
    pair: tuple[Store, str, str],
) -> None:
    db, good, bad = pair
    with pytest.raises(ValueError, match="cannot fit the comparison"):
        _built(db, good, bad, budget=200)


def test_the_report_refuses_steps_the_diff_did_not_align(
    pair: tuple[Store, str, str],
) -> None:
    """The report and the aligner must read one trajectory, not two."""
    db, good, bad = pair
    good_events, bad_events = db.events(good), db.events(bad)
    result = diff_runs(good_events, bad_events, embed=LexicalEmbedder())
    wrong = type(result)(
        good_steps=[Step("read_file", {"path": "elsewhere.py"}, target="elsewhere.py")],
        bad_steps=result.bad_steps,
        alignment=result.alignment,
        score=result.score,
        divergence=result.divergence,
        length_ratio=result.length_ratio,
    )
    with pytest.raises(ValueError, match="do not match the ones the diff aligned"):
        build_payload(db.run(good), good_events, db.run(bad), bad_events, wrong)


def test_view_writes_one_file_and_names_the_divergence(tmp_path: Path) -> None:
    store = tmp_path / "store"
    good = _record(store, "good", GOOD, resolve=True)
    bad = _record(store, "bad", BAD, resolve=False)
    out = tmp_path / "report.html"
    done = subprocess.run(
        [
            sys.executable, "-m", "locus", "view", good, bad,
            "--store", str(store), "--lexical", "-o", str(out),
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    assert done.returncode == 0, done.stderr
    assert "divergence at failing step" in done.stdout
    assert out.exists()
    assert list(tmp_path.glob("*.html")) == [out]
    payload = _island(out.read_text(encoding="utf-8"))
    assert payload["good"]["outcome"]["resolve"] is True
    assert payload["bad"]["outcome"]["resolve"] is False


def test_a_run_against_itself_has_no_standing_divergence(
    pair: tuple[Store, str, str],
) -> None:
    db, good, _ = pair
    payload = _built(db, good, good)
    assert payload["divergence"] is None
    assert all(c["agree"] for c in payload["columns"])


def test_write_report_returns_the_payload_it_embedded(tmp_path: Path) -> None:
    store = tmp_path / "store"
    good = _record(store, "good", GOOD, resolve=True)
    bad = _record(store, "bad", BAD, resolve=False)
    db = Store(store)
    good_events, bad_events = db.events(good), db.events(bad)
    result = diff_runs(good_events, bad_events, embed=LexicalEmbedder())
    out = tmp_path / "r.html"
    payload = write_report(
        out, db.run(good), good_events, db.run(bad), bad_events, result, blobs=db.blobs
    )
    db.close()
    assert _island(out.read_text(encoding="utf-8")) == payload

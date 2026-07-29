from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

import locus
from locus import Config, DecodeParams, Message, ModelResponse, Usage

SYSTEM = Message(role="system", content="You are a coding agent.")


def _backend(texts: list[str]) -> Any:
    calls = iter(texts)

    def create(model_id: str, messages: list[Message], params: DecodeParams) -> ModelResponse:
        return ModelResponse(text=next(calls), finish_reason="end_turn", usage=Usage())

    return create


def _record(store: Path, tools: list[str] | None = None) -> str:
    with locus.record("match", store=store) as rec:
        model = rec.model(
            provider="p", model_id="m", create_fn=_backend(["first", "second"])
        )
        model.create(messages=[SYSTEM, Message(role="user", content="one")], tools=tools)
        model.create(messages=[SYSTEM, Message(role="user", content="two")], tools=tools)
        rec.outcome(status="ok")
        return rec.run_id


def _replay(store: Path, run_id: str, contents: list[str], **overrides: Any) -> list[str]:
    out: list[str] = []
    with locus.replay(run_id, store=store, **overrides) as rep:
        model = rep.model(provider="p", model_id="m")
        for content in contents:
            out.append(
                model.create(
                    messages=[SYSTEM, Message(role="user", content=content)],
                    tools=overrides.pop("tools", None),
                ).response.text
            )
        out.append(rep.report.summary())
    return out


def test_the_default_matches_strictly_on_model_and_messages(tmp_path: Path) -> None:
    run_id = _record(tmp_path)
    assert _replay(tmp_path, run_id, ["one", "two"])[:2] == ["first", "second"]


def test_a_divergent_request_misses_under_the_default(tmp_path: Path) -> None:
    run_id = _record(tmp_path)
    with pytest.raises(locus.ReplayMiss, match="never made"):
        _replay(tmp_path, run_id, ["something else"])


def test_ordinal_replays_by_position_and_reports_the_match_as_degraded(
    tmp_path: Path,
) -> None:
    run_id = _record(tmp_path)
    result = _replay(
        tmp_path, run_id, ["nothing", "like it"], match_on=("model", "ordinal")
    )
    assert result[:2] == ["first", "second"]
    assert "2 degraded" in result[2] and "0 matched" in result[2]


def test_ordinal_is_never_reached_without_asking_for_it(tmp_path: Path) -> None:
    """The miss must not fall back to position; a fallback would hide divergence."""
    run_id = _record(tmp_path)
    with pytest.raises(locus.ReplayMiss):
        _replay(tmp_path, run_id, ["nothing like it"])


def test_system_prompt_matching_ignores_the_rest_of_the_conversation(
    tmp_path: Path,
) -> None:
    run_id = _record(tmp_path)
    result = _replay(
        tmp_path, run_id, ["different", "also different"], match_on=("system_prompt", "ordinal")
    )
    assert result[:2] == ["first", "second"]


def test_a_different_system_prompt_still_misses(tmp_path: Path) -> None:
    run_id = _record(tmp_path)
    with pytest.raises(locus.ReplayMiss):
        with locus.replay(run_id, store=tmp_path, match_on=("system_prompt",)) as rep:
            rep.model(provider="p", model_id="m").create(
                messages=[Message(role="system", content="a different system prompt")]
            )


def test_tool_names_take_part_in_the_match(tmp_path: Path) -> None:
    run_id = _record(tmp_path, tools=["read_file", "grep"])
    with locus.replay(run_id, store=tmp_path, match_on=("model", "tool_names", "ordinal")) as rep:
        model = rep.model(provider="p", model_id="m")
        assert model.create(messages=[SYSTEM], tools=["read_file", "grep"]).response.text == "first"
    with pytest.raises(locus.ReplayMiss):
        with locus.replay(
            run_id, store=tmp_path, match_on=("model", "tool_names", "ordinal")
        ) as rep:
            rep.model(provider="p", model_id="m").create(messages=[SYSTEM], tools=["grep"])


def test_a_different_model_id_misses(tmp_path: Path) -> None:
    run_id = _record(tmp_path)
    with pytest.raises(locus.ReplayMiss, match="model="):
        with locus.replay(run_id, store=tmp_path, match_on=("model", "ordinal")) as rep:
            rep.model(provider="p", model_id="other").create(messages=[SYSTEM])


def test_the_report_counts_what_the_replay_left_unused(tmp_path: Path) -> None:
    run_id = _record(tmp_path)
    with locus.replay(run_id, store=tmp_path) as rep:
        rep.model(provider="p", model_id="m").create(
            messages=[SYSTEM, Message(role="user", content="one")]
        )
        assert rep.report.matched == 1
        assert rep.report.unconsumed == 1
        assert "1 recorded calls unused" in rep.report.summary()


def test_an_unknown_matcher_names_the_ones_that_exist() -> None:
    with pytest.raises(ValueError, match="messages_hash"):
        Config(match_on=("model", "vibes"))


def test_an_empty_matcher_list_is_rejected() -> None:
    with pytest.raises(ValueError, match="every recorded call"):
        Config(match_on=())

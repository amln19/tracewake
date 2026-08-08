from __future__ import annotations

from pathlib import Path

import pytest

from tracewake import DecodeParams, Message, ModelResponse, Usage

pytest_plugins = ["pytester"]


class Backend:
    def __init__(self) -> None:
        self.calls = 0

    def create(
        self, model_id: str, messages: list[Message], params: DecodeParams
    ) -> ModelResponse:
        self.calls += 1
        return ModelResponse(text="recorded answer", finish_reason="end_turn", usage=Usage())


def test_the_fixture_replays_a_cassette_at_no_cost(tracewake_cassette, tmp_path: Path) -> None:
    backend = Backend()
    with tracewake_cassette("regression", store=str(tmp_path), mode="all") as rec:
        model = rec.model(provider="p", model_id="m", create_fn=backend.create)
        model.create(messages=[Message(role="user", content="hi")])
        rec.outcome(status="ok")

    with tracewake_cassette("regression", store=str(tmp_path), mode="none") as rep:
        model = rep.model(provider="p", model_id="m")
        assert model.create(messages=[Message(role="user", content="hi")]).response.text == (
            "recorded answer"
        )
        rep.outcome(status="ok")

    assert backend.calls == 1, "the replay went back to the model"


def test_the_cassette_name_defaults_to_the_test(tracewake_cassette, tmp_path: Path) -> None:
    with tracewake_cassette(store=str(tmp_path), mode="all") as rec:
        rec.outcome(status="ok")
        assert rec.name == "test_the_cassette_name_defaults_to_the_test"


@pytest.mark.tracewake("named-by-marker")
def test_a_marker_names_the_cassette(tracewake_cassette, tmp_path: Path) -> None:
    with tracewake_cassette(store=str(tmp_path), mode="all") as rec:
        rec.outcome(status="ok")
        assert rec.name == "named-by-marker"


def test_the_plugin_defaults_to_replay_only(pytester: pytest.Pytester) -> None:
    """A recorded run becomes a regression test that costs nothing to re-run."""
    pytester.makepyfile(
        test_replayed="""
        import tracewake
        from tracewake import Message, ModelResponse, Usage

        CALLS = []

        def create(model_id, messages, params):
            CALLS.append(1)
            return ModelResponse(text="answer", finish_reason="end_turn", usage=Usage())

        def test_record_then_replay(tracewake_cassette):
            with tracewake_cassette("cassette", mode="all") as rec:
                rec.model(provider="p", model_id="m", create_fn=create).create(
                    messages=[Message(role="user", content="hi")]
                )
                rec.outcome(status="ok")

            with tracewake_cassette("cassette") as rep:
                text = rep.model(provider="p", model_id="m").create(
                    messages=[Message(role="user", content="hi")]
                ).response.text
                rep.outcome(status="ok")

            assert text == "answer"
            assert len(CALLS) == 1
        """
    )
    result = pytester.runpytest_subprocess("-p", "no:cacheprovider")
    result.assert_outcomes(passed=1)


def test_a_test_that_diverges_from_its_cassette_fails(pytester: pytest.Pytester) -> None:
    pytester.makepyfile(
        test_diverged="""
        import pytest
        import tracewake
        from tracewake import Message, ModelResponse, Usage

        def create(model_id, messages, params):
            return ModelResponse(text="answer", finish_reason="end_turn", usage=Usage())

        def test_diverges(tracewake_cassette):
            with tracewake_cassette("cassette", mode="all") as rec:
                rec.model(provider="p", model_id="m", create_fn=create).create(
                    messages=[Message(role="user", content="hi")]
                )
                rec.outcome(status="ok")

            with pytest.raises(tracewake.ReplayMiss):
                with tracewake_cassette("cassette") as rep:
                    rep.model(provider="p", model_id="m").create(
                        messages=[Message(role="user", content="something else")]
                    )
        """
    )
    result = pytester.runpytest_subprocess("-p", "no:cacheprovider")
    result.assert_outcomes(passed=1)


def test_unused_recorded_calls_fail_the_test(pytester: pytest.Pytester) -> None:
    pytester.makepyfile(
        test_unused="""
        from tracewake import Message, ModelResponse, Usage

        def create(model_id, messages, params):
            return ModelResponse(text="answer", finish_reason="end_turn", usage=Usage())

        def test_stops_early(tracewake_cassette):
            with tracewake_cassette("cassette", mode="all") as rec:
                rec.model(provider="p", model_id="m", create_fn=create).create(
                    messages=[Message(role="user", content="hi")]
                )
                rec.model(provider="p", model_id="m", create_fn=create).create(
                    messages=[Message(role="user", content="again")]
                )
                rec.outcome(status="ok")

            with tracewake_cassette("cassette") as rep:
                rep.model(provider="p", model_id="m").create(
                    messages=[Message(role="user", content="hi")]
                )
                # Leaves the second recorded call unconsumed.
                rep.outcome(status="ok")
        """
    )
    result = pytester.runpytest_subprocess("-p", "no:cacheprovider")
    result.assert_outcomes(failed=1)
    assert "unused" in result.stdout.str() or "unused" in result.stderr.str()


def test_the_plugin_registers_its_options(pytester: pytest.Pytester) -> None:
    result = pytester.runpytest_subprocess("--help")
    result.stdout.fnmatch_lines(["*--tracewake-record*"])

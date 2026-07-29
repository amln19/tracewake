from __future__ import annotations

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

SECRET = "sk-test-0123456789-super-secret"


def _create(model_id: str, messages: list[Message], params: DecodeParams) -> ModelResponse:
    return ModelResponse(text="done", finish_reason="end_turn", usage=Usage())


def _dispatch(name: str, args: dict[str, Any]) -> ToolOutcome:
    return ToolOutcome(content=f"the key is {SECRET} and home is {Path.home()}")


def _record(store: Path, output: Path | None = None, **overrides: Any) -> str:
    output = output or store.parent / "out.txt"
    with locus.record("redact", store=store, **overrides) as rec:
        model = rec.model(provider="p", model_id="m", create_fn=_create)
        completion = model.create(
            messages=[
                Message(role="system", content=f"Authorization: Bearer {SECRET}"),
                Message(role="user", content=f"read {Path.home()}/notes.txt"),
            ]
        )
        rec.tools(_dispatch).call(
            completion.call_id,
            ToolCallRequest(
                id="t0",
                name="http",
                args={"url": "https://x", "headers": {"authorization": f"Bearer {SECRET}"}},
                batch_index=0,
            ),
        )
        rec.fs.write_text(output, f"wrote {SECRET}")
        rec.outcome(status="ok")
        return rec.run_id


def _store_bytes(store: Path) -> bytes:
    return b"".join(p.read_bytes() for p in sorted(store.rglob("*")) if p.is_file())


def test_no_secret_reaches_disk(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DEMO_API_KEY", SECRET)
    store = tmp_path / "store"
    _record(store)

    written = _store_bytes(store)
    assert SECRET.encode() not in written
    assert str(Path.home()).encode() not in written
    assert b"[REDACTED]" in written


def test_the_file_the_agent_wrote_still_has_the_real_content(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Redaction scrubs the log, not the work. The agent's own output is untouched."""
    monkeypatch.setenv("DEMO_API_KEY", SECRET)
    store = tmp_path / "store"
    _record(store, output=tmp_path / "out.txt")
    assert SECRET in (tmp_path / "out.txt").read_text()


def test_a_header_key_is_scrubbed_even_when_the_value_is_unknown(tmp_path: Path) -> None:
    store = tmp_path / "store"
    run_id = _record(store)
    db = Store(store)
    (tool,) = [e.event for e in db.events(run_id) if e.event.type == "tool_call"]
    db.close()
    assert tool.args["headers"]["authorization"] == locus.REDACTED
    assert tool.args["url"] == "https://x"


def test_disabling_redaction_leaves_the_secret_in_the_log(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("DEMO_API_KEY", SECRET)
    store = tmp_path / "store"
    _record(store, redact=False)
    assert SECRET.encode() in _store_bytes(store)


def test_a_cassette_replays_where_the_same_variable_holds_a_different_secret(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Both machines scrub their own credential to the same placeholder, so the
    request hashes agree and the cassette is portable."""
    monkeypatch.setenv("DEMO_API_KEY", SECRET)
    store = tmp_path / "store"
    run_id = _record(store)

    other = "sk-live-9876543210-different-secret"
    monkeypatch.setenv("DEMO_API_KEY", other)
    with locus.replay(run_id, store=store) as rep:
        model = rep.model(provider="p", model_id="m")
        completion = model.create(
            messages=[
                Message(role="system", content=f"Authorization: Bearer {other}"),
                Message(role="user", content=f"read {Path.home()}/notes.txt"),
            ]
        )
        assert completion.response.text == "done"
        assert rep.report.matched == 1


def test_a_secret_too_short_to_be_one_is_left_alone(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("SHORT_TOKEN", "abc")
    store = tmp_path / "store"
    with locus.record("short", store=store) as rec:
        model = rec.model(provider="p", model_id="m", create_fn=_create)
        model.create(messages=[Message(role="user", content="abc is a normal word")])
        rec.outcome(status="ok")
    assert b"abc is a normal word" in _store_bytes(store)


def test_an_unredacted_cassette_still_replays(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The cassette records how it was written, so the match path scrubs the same
    way it did. Otherwise every call would miss."""
    monkeypatch.setenv("DEMO_API_KEY", SECRET)
    store = tmp_path / "store"
    run_id = _record(store, redact=False)

    with locus.replay(run_id, store=store) as rep:
        model = rep.model(provider="p", model_id="m")
        completion = model.create(
            messages=[
                Message(role="system", content=f"Authorization: Bearer {SECRET}"),
                Message(role="user", content=f"read {Path.home()}/notes.txt"),
            ]
        )
        assert completion.response.text == "done"
        assert rep.report.matched == 1


def test_a_home_path_is_scrubbed_in_the_log_but_usable_on_replay(tmp_path: Path) -> None:
    """An environment variable is an input the agent acts on, so replaying HOME
    as the placeholder would hand it a path that cannot be opened."""
    import os

    store = tmp_path / "store"
    with locus.record("home", store=store) as rec:
        assert os.environ["HOME"] == str(Path.home())
        rec.outcome(status="ok")
        run_id = rec.run_id

    db = Store(store)
    (event,) = [e.event for e in db.events(run_id) if e.event.type == "environment"]
    db.close()
    assert event.value == "<HOME>"
    assert str(Path.home()) not in _store_bytes(store).decode("utf-8", "replace")

    with locus.replay(run_id, store=store) as rep:
        assert os.environ["HOME"] == str(Path.home())
        rep.outcome(status="ok")


def test_configure_sets_the_process_default(tmp_path: Path) -> None:
    original = locus.current_config()
    try:
        locus.configure(filter_values=("a-hardcoded-secret-value",))
        store = tmp_path / "store"
        with locus.record("configured", store=store) as rec:
            model = rec.model(provider="p", model_id="m", create_fn=_create)
            model.create(
                messages=[Message(role="user", content="key is a-hardcoded-secret-value")]
            )
            rec.outcome(status="ok")
        assert b"a-hardcoded-secret-value" not in _store_bytes(store)
    finally:
        locus.configure(**{f: getattr(original, f) for f in type(original).__dataclass_fields__})


def test_before_record_can_drop_an_event(tmp_path: Path) -> None:
    store = tmp_path / "store"
    with locus.record(
        "hook",
        store=store,
        before_record=lambda ev: None if ev.type == "environment" else ev,
    ) as rec:
        rec.clock.time()
        rec.outcome(status="ok")
        run_id = rec.run_id

    db = Store(store)
    kinds = [e.event.type for e in db.events(run_id)]
    db.close()
    assert "environment" not in kinds and "outcome" in kinds


def test_an_env_var_named_like_a_secret_is_redacted_by_name(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("SOMETHING_TOKEN", "a-value-nobody-would-guess")
    store = tmp_path / "store"
    with locus.record("env", store=store) as rec:
        import os

        assert os.environ["SOMETHING_TOKEN"] == "a-value-nobody-would-guess"
        rec.outcome(status="ok")
        run_id = rec.run_id

    db = Store(store)
    (event,) = [e.event for e in db.events(run_id) if e.event.type == "environment"]
    db.close()
    assert event.key == "SOMETHING_TOKEN"
    assert event.value == locus.REDACTED

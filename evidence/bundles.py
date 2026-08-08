"""Deterministic bundles for the measured scenarios.

Every bundle comes from a real recorded session with a scripted model and
scripted tools, so what the hosted path analyses is what local Tracewake records.
"""

from __future__ import annotations

import shutil
from pathlib import Path
from typing import Any

import tracewake
from tracewake import DecodeParams, Message, ModelResponse, ToolCallRequest, ToolOutcome, Usage
from tracewake.bundle import build_bundle
from tracewake.cassette import export_cassette
from tracewake.store import Store


def _script(flavour: str, steps: int) -> list[dict[str, Any]]:
    turns: list[dict[str, Any]] = []
    for step in range(steps):
        # The first turns agree across flavours so a diff has a real point of
        # divergence rather than disagreeing from the start.
        target = "src/window.py" if step < steps // 2 else f"src/{flavour}_{step}.py"
        turns.append(
            {
                "text": f"step {step}: editing {target}",
                "tool_calls": [{"name": "edit", "args": {"path": target, "text": f"{flavour} {step}"}}],
            }
        )
    turns.append({"text": "done", "tool_calls": []})
    return turns


def _create(flavour: str, steps: int) -> Any:
    script = _script(flavour, steps)
    state = {"index": 0}

    def create(model_id: str, messages: list[Message], params: DecodeParams) -> ModelResponse:
        index = min(state["index"], len(script) - 1)
        state["index"] += 1
        turn = script[index]
        calls = [
            ToolCallRequest(id=f"call_{index}_{position}", name=call["name"], args=call["args"], batch_index=position)
            for position, call in enumerate(turn["tool_calls"])
        ]
        return ModelResponse(
            text=turn["text"],
            tool_calls=calls,
            finish_reason="tool_use" if calls else "end_turn",
            usage=Usage(input_tokens=32 * (index + 1), output_tokens=16),
        )

    return create


def _dispatch(name: str, args: dict[str, Any]) -> ToolOutcome:
    if name != "edit":
        return ToolOutcome(content=f"no such tool {name!r}", status="error", error="unknown tool")
    return ToolOutcome(content=f"wrote {len(str(args.get('text', '')))} bytes to {args.get('path', '?')}")


def _record(root: Path, name: str, flavour: str, steps: int) -> Path:
    store_path = root / f"{name}-store"
    with tracewake.record(name, store=store_path) as session:
        model = session.model(provider="scripted", model_id="scripted-1", create_fn=_create(flavour, steps))
        tools = session.tools(_dispatch)
        messages = [
            Message(role="system", content="You are a coding agent.", provenance="system_prompt"),
            Message(role="user", content="Fix the off-by-one in the window slice.", provenance="user_task"),
        ]
        for _ in range(steps + 2):
            completion = model.create(messages=messages, temperature=0.0)
            response = completion.response
            messages.append(Message(role="assistant", content=response.text))
            for request in response.tool_calls:
                outcome = tools.call(completion.call_id, request)
                messages.append(Message(role="tool", content=outcome.content, tool_call_id=request.id))
            if not response.tool_calls:
                break
        session.outcome(status="ok", patch=f"{flavour} patch\n" * 16)
        run_id = session.run_id
    store = Store(store_path)
    try:
        cassette = export_cassette(store, run_id, root / f"{name}-cassette")
    finally:
        store.close()
    bundle = build_bundle(cassette, root / f"{name}.tracewake")
    shutil.rmtree(store_path, ignore_errors=True)
    shutil.rmtree(cassette, ignore_errors=True)
    return bundle


def pair(root: Path, steps: int = 8, prefix: str = "") -> tuple[bytes, bytes]:
    """Two runs of one task that agree and then diverge."""
    root.mkdir(parents=True, exist_ok=True)
    return (
        _record(root, f"{prefix}good", "good", steps).read_bytes(),
        _record(root, f"{prefix}bad", "bad", steps).read_bytes(),
    )


def series(root: Path, count: int, steps: int = 4) -> list[bytes]:
    root.mkdir(parents=True, exist_ok=True)
    return [_record(root, f"load-{index:03d}", f"load{index}", steps).read_bytes() for index in range(count)]

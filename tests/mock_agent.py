"""A fully mocked agent run: scripted model, scripted tools, parallel batches.

The agent records everything it observes into a `Transcript`. Byte-equality of
two transcripts is the gate: it compares what the agent actually saw, not what
Tracewake stored.
"""

from __future__ import annotations

import random
import time
from collections.abc import Generator
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from typing import Any

from tracewake import (
    DecodeParams,
    Message,
    ModelResponse,
    StreamChunk,
    ToolCallRequest,
    ToolOutcome,
    Usage,
)

SYSTEM = "You are a coding agent. Use tools before editing."
TASK = "fix the off-by-one in slice_window"


@dataclass
class Transcript:
    lines: list[str] = field(default_factory=list)

    def observe(self, label: str, value: str) -> None:
        self.lines.append(f"{label}\t{value}")

    def to_bytes(self) -> bytes:
        return "\n".join(self.lines).encode("utf-8")


def _chunks(text: str, size: int = 7) -> list[StreamChunk]:
    parts = [text[i : i + size] for i in range(0, len(text), size)] or [""]
    return [StreamChunk(index=i, text_delta=p) for i, p in enumerate(parts)]


def _turn(text: str, tools: list[tuple[str, dict[str, Any]]], finish: str) -> ModelResponse:
    return ModelResponse(
        text=text,
        tool_calls=[
            ToolCallRequest(id=f"call_{i}_{name}", name=name, args=args, batch_index=i)
            for i, (name, args) in enumerate(tools)
        ],
        finish_reason=finish,
        usage=Usage(input_tokens=len(text) * 2, output_tokens=len(text)),
    )


SCRIPT: list[ModelResponse] = [
    _turn(
        "I'll read the file and look for the window slice before changing anything.",
        [
            ("read_file", {"path": "src/window.py"}),
            ("grep", {"pattern": "slice_window", "path": "src"}),
            ("list_dir", {"path": "tests"}),
        ],
        "tool_use",
    ),
    _turn(
        "The range bound is wrong. Applying the patch and running the suite.",
        [
            ("apply_patch", {"path": "src/window.py", "old": "i + n", "new": "i + n - 1"}),
            ("run_tests", {"target": "tests/test_window.py"}),
        ],
        "tool_use",
    ),
    _turn("Patched the off-by-one; the window test passes now.", [], "end_turn"),
]


TOOL_OUTPUTS: dict[str, ToolOutcome] = {
    "read_file": ToolOutcome(content="def slice_window(xs, i, n):\n    return xs[i : i + n]\n"),
    "grep": ToolOutcome(content="src/window.py:1:def slice_window(xs, i, n):"),
    "list_dir": ToolOutcome(content="test_window.py\ntest_shapes.py"),
    "apply_patch": ToolOutcome(content="patched src/window.py (1 hunk)"),
    # A failed tool is a normal agent input, and the gate has to cover it:
    # error text and status must replay exactly like a success.
    "run_tests": ToolOutcome(
        content="1 failed, 12 passed\nFAILED tests/test_window.py::test_edge",
        status="error",
        error="pytest exited 1",
    ),
}

# Reversed relative to batch order, so a batch always completes in an order that
# differs from its batch_index. Without the intra-batch index the recorded
# sequence numbers would come out in this order instead.
_TOOL_DELAY_MS = {"read_file": 30, "grep": 20, "list_dir": 5, "apply_patch": 20, "run_tests": 5}


class MockBackend:
    def __init__(self) -> None:
        self.creates = 0
        self.streams = 0
        self.dispatches = 0

    def create(
        self, model_id: str, messages: list[Message], params: DecodeParams
    ) -> ModelResponse:
        response = SCRIPT[self.creates]
        self.creates += 1
        return response

    def stream(
        self, model_id: str, messages: list[Message], params: DecodeParams
    ) -> Generator[StreamChunk, None, ModelResponse]:
        response = SCRIPT[self.streams]
        self.streams += 1
        yield from _chunks(response.text)
        for call in response.tool_calls:
            yield StreamChunk(
                index=-1, tool_call_delta={"id": call.id, "name": call.name}
            )
        return response

    def dispatch(self, name: str, args: dict[str, Any]) -> ToolOutcome:
        self.dispatches += 1
        time.sleep(_TOOL_DELAY_MS[name] / 1000.0)
        return TOOL_OUTPUTS[name]


def forbidden_create(*args: Any, **kwargs: Any) -> ModelResponse:
    raise AssertionError("replay called the model backend; replay must never leave the log")


def forbidden_stream(*args: Any, **kwargs: Any) -> Any:
    raise AssertionError("replay called the model backend; replay must never leave the log")


def forbidden_dispatch(*args: Any, **kwargs: Any) -> ToolOutcome:
    raise AssertionError("replay called the tool backend; replay must never leave the log")


def run_agent(model: Any, tools: Any, clock: Any, transcript: Transcript, task: str = TASK) -> Usage:
    """The agent under test. Identical code drives record and replay."""
    messages = [
        Message(role="system", content=SYSTEM, provenance="system_prompt"),
        Message(role="user", content=task, provenance="user_task"),
    ]
    total = Usage()
    pool = ThreadPoolExecutor(max_workers=4)
    shuffler = random.Random(1234)

    for _ in range(len(SCRIPT)):
        started = clock.monotonic()
        with model.stream(messages=messages, temperature=0.0, max_tokens=512) as stream:
            for chunk in stream:
                transcript.observe("chunk", chunk.model_dump_json())
        response, call_id = stream.response, stream.call_id
        transcript.observe("response", response.model_dump_json())

        total = Usage(
            input_tokens=total.input_tokens + response.usage.input_tokens,
            output_tokens=total.output_tokens + response.usage.output_tokens,
        )
        messages.append(
            Message(role="assistant", content=response.text, provenance="assistant")
        )

        if not response.tool_calls:
            break

        order = list(response.tool_calls)
        shuffler.shuffle(order)
        futures = {req.id: pool.submit(tools.call, call_id, req) for req in order}
        for req in response.tool_calls:
            outcome = futures[req.id].result()
            transcript.observe(
                "tool", f"{req.batch_index}\t{req.name}\t{outcome.model_dump_json()}"
            )
            messages.append(
                Message(
                    role="tool",
                    content=outcome.content,
                    tool_call_id=req.id,
                    provenance="tool_output",
                )
            )

        # Both reads are recorded, so the difference replays exactly. A clock
        # that was not intercepted would produce a different elapsed here.
        transcript.observe("elapsed", repr(clock.monotonic() - started))
        transcript.observe("wall", repr(clock.time()))

    pool.shutdown()
    transcript.observe("usage", total.model_dump_json())
    return total

"""A small no-network tool-calling agent using an OpenAI-compatible message shape.

It uses Tracewake's `ToolCallRequest` model directly. Replace `fake_create` with
an adapter around a real client that returns `tracewake.ModelResponse`.

Run standalone:

    python examples/openai_agent.py --scenario good

or wrapped, so `tracewake` owns the recording:

    tracewake record -- python examples/openai_agent.py --scenario good

See `examples/demo.py` for the end-to-end path.
"""

from __future__ import annotations

import argparse
from typing import Any

import tracewake
from tracewake import (
    DecodeParams,
    Message,
    ModelResponse,
    ToolCallRequest,
    ToolOutcome,
    Usage,
)

CITY_WEATHER = {"lisbon": "68F and sunny"}

# Keep the first step identical so the changed query and note path exercise the
# aligner's tool-target matching.
_SCRIPTS: dict[str, list[dict[str, Any]]] = {
    "good": [
        {
            "text": "Let me check the weather in Lisbon first.",
            "tool_calls": [{"name": "get_weather", "args": {"query": "Lisbon"}}],
        },
        {
            "text": "Sunny and mild — I'll note that down.",
            "tool_calls": [
                {
                    "name": "write_note",
                    "args": {
                        "path": "trip-notes.txt",
                        "text": "Lisbon: 68F and sunny. Pack light clothes.",
                    },
                }
            ],
        },
        {"text": "Done — the note is saved.", "tool_calls": []},
    ],
    "bad": [
        {
            "text": "Let me check the weather in Lisbon first.",
            "tool_calls": [{"name": "get_weather", "args": {"query": "Lisbon"}}],
        },
        {
            "text": "I should double check that city name.",
            "tool_calls": [{"name": "get_weather", "args": {"query": "Lisbon, Portugal"}}],
        },
        {
            "text": "That did not resolve — I'll log the problem instead of guessing.",
            "tool_calls": [
                {
                    "name": "write_note",
                    "args": {
                        "path": "error-log.txt",
                        "text": "Could not confirm the weather; skipping the packing note.",
                    },
                }
            ],
        },
        {"text": "Done — the note is saved.", "tool_calls": []},
    ],
}


def fake_create(scenario: str) -> Any:
    """Return a deterministic `create_fn` for the selected scenario."""
    call_index = 0
    script = _SCRIPTS[scenario]

    def create(model_id: str, messages: list[Message], params: DecodeParams) -> ModelResponse:
        nonlocal call_index
        index = min(call_index, len(script) - 1)
        call_index += 1
        turn = script[index]
        tool_calls = [
            ToolCallRequest(id=f"call_{index}_{i}", name=c["name"], args=c["args"], batch_index=i)
            for i, c in enumerate(turn["tool_calls"])
        ]
        return ModelResponse(
            text=turn["text"],
            tool_calls=tool_calls,
            finish_reason="tool_use" if tool_calls else "end_turn",
            usage=Usage(input_tokens=20 * (index + 1), output_tokens=12),
        )

    return create


def dispatch(name: str, args: dict[str, Any]) -> ToolOutcome:
    match name:
        case "get_weather":
            query = str(args.get("query", ""))
            report = CITY_WEATHER.get(query.strip().lower())
            if report is None:
                return ToolOutcome(
                    content=f"unknown city {query!r} — try the city name alone",
                    status="error",
                    error="unknown city",
                )
            return ToolOutcome(content=report)
        case "write_note":
            return ToolOutcome(content=f"note saved to {args.get('path', '?')}")
        case _:
            return ToolOutcome(
                content=f"no such tool {name!r}", status="error", error="unknown tool"
            )


def run(session: tracewake.Session, scenario: str) -> None:
    model = session.model(provider="fake", model_id="fake-1", create_fn=fake_create(scenario))
    tools = session.tools(dispatch)
    messages = [
        Message(
            role="system", content="You are a travel assistant.", provenance="system_prompt"
        ),
        Message(
            role="user",
            content="Check the weather for a Lisbon trip and save a packing note.",
            provenance="user_task",
        ),
    ]

    for _ in range(len(_SCRIPTS[scenario])):
        completion = model.create(messages=messages, temperature=0.0)
        response = completion.response
        messages.append(Message(role="assistant", content=response.text))
        for request in response.tool_calls:
            outcome = tools.call(completion.call_id, request)
            messages.append(
                Message(role="tool", content=outcome.content, tool_call_id=request.id)
            )
        if not response.tool_calls:
            break


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--scenario", choices=["good", "bad"], default="good")
    parser.add_argument("--name", default="")
    args = parser.parse_args()

    with tracewake.record(args.name or f"openai-agent-{args.scenario}") as rec:
        run(rec, args.scenario)
        rec.outcome(status="ok")


if __name__ == "__main__":
    main()

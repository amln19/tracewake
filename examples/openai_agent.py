"""A tool-calling agent wired through locus, using the shape most
OpenAI-compatible clients speak: chat messages, an assistant turn carrying
`tool_calls`, and tool results threaded back by `tool_call_id`.

`bench/agent.py` is not a portability proof — it parses actions out of a
fenced JSON block tuned for small local models. This example uses the
structured shape `locus.ToolCallRequest` already models directly, so wiring
in a real client is a one-line change: replace `fake_create` below with
`openai.OpenAI().chat.completions.create` (adapted to return a
`locus.ModelResponse`) and pass it to `session.model(create_fn=...)`.

Run standalone:

    python examples/openai_agent.py --scenario good

or wrapped, so `locus` owns the recording:

    locus record -- python examples/openai_agent.py --scenario good

See `examples/demo.py` for the end-to-end path: record both scenarios, replay
one, and diff them.
"""

from __future__ import annotations

import argparse
from typing import Any

import locus
from locus import DecodeParams, Message, ModelResponse, ToolCallRequest, ToolOutcome, Usage

CITY_WEATHER = {"lisbon": "68F and sunny"}

# `locus diff` groups a step by tool name plus a "target" pulled from a
# `query` or `path` argument (see `target_of` in `locus/align.py`) — the same
# convention a file-editing tool would use for a path. `get_weather` and
# `write_note` reuse it so a query that resolves to nothing, or a note filed
# under a different path, actually registers as a divergence rather than
# aligning on name alone.
#
# One script per scenario, so the two runs share an identical first step —
# same tool call, same args, same result — and only part ways afterward.
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
    """A deterministic stand-in for `create_fn`, matching its real shape:
    `(model_id, messages, params) -> ModelResponse`. Scripted so this example
    needs no API key and makes no network call.
    """
    calls = {"n": 0}
    script = _SCRIPTS[scenario]

    def create(model_id: str, messages: list[Message], params: DecodeParams) -> ModelResponse:
        index = min(calls["n"], len(script) - 1)
        calls["n"] += 1
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


def run(session: locus.Session, scenario: str) -> None:
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

    for _ in range(6):
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

    with locus.record(args.name or f"openai-agent-{args.scenario}") as rec:
        run(rec, args.scenario)
        rec.outcome(status="ok")


if __name__ == "__main__":
    main()

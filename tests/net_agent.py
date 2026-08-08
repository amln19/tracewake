"""An agent whose model backend genuinely goes over the network.

Run under `tracewake record -- ...` it connects to the test server for every model
call. Run under `tracewake replay <id>` it must answer from the log instead, and any
attempt to reach the server is both blocked and counted.

The transcript is written with a plain file write rather than through the
recorded filesystem, so it is instrumentation the run does not observe.
"""

from __future__ import annotations

import json
import os
import random
import socket
import uuid
from pathlib import Path
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

SYSTEM = "You are a coding agent."
TASK = "summarize the module"


def fetch(prompt_size: int) -> dict[str, Any]:
    port = int(os.environ["TRACEWAKE_TEST_PORT"])
    with socket.create_connection(("127.0.0.1", port), timeout=5) as sock:
        sock.sendall(f"{prompt_size}\n".encode())
        with sock.makefile("r") as stream:
            return json.loads(stream.readline())


def create(model_id: str, messages: list[Message], params: DecodeParams) -> ModelResponse:
    payload = fetch(len(messages))
    return ModelResponse(
        text=payload["text"],
        tool_calls=[
            ToolCallRequest(id=c["id"], name=c["name"], args=c["args"], batch_index=i)
            for i, c in enumerate(payload["tool_calls"])
        ],
        finish_reason=payload["finish_reason"],
        usage=Usage(input_tokens=payload["input_tokens"], output_tokens=1),
    )


def dispatch(name: str, args: dict[str, Any]) -> ToolOutcome:
    return ToolOutcome(content=f"{name} ran on {args.get('path', '?')}")


def main() -> None:
    lines: list[str] = []
    with tracewake.record("net-agent") as rec:
        model = rec.model(provider="testnet", model_id="testnet-1", create_fn=create)
        tools = rec.tools(dispatch)
        messages = [
            Message(role="system", content=SYSTEM, provenance="system_prompt"),
            Message(role="user", content=TASK, provenance="user_task"),
        ]

        for _ in range(2):
            completion = model.create(messages=messages, temperature=0.0)
            response = completion.response
            lines.append(f"response\t{response.model_dump_json()}")
            messages.append(Message(role="assistant", content=response.text))

            for request in response.tool_calls:
                outcome = tools.call(completion.call_id, request)
                lines.append(f"tool\t{request.name}\t{outcome.content}")
                messages.append(
                    Message(role="tool", content=outcome.content, tool_call_id=request.id)
                )
            if not response.tool_calls:
                break

        # Every nondeterministic input the agent consumes, through the ordinary
        # module-level calls an agent would actually make.
        lines.append(f"source-bytes\t{len(rec.fs.read_text(__file__))}")
        lines.append(f"env\t{os.environ['TRACEWAKE_TEST_TAG']}")
        lines.append(f"random\t{random.random()!r}")
        lines.append(f"randint\t{random.randint(0, 10**9)}")
        lines.append(f"uuid\t{uuid.uuid4()}")
        lines.append(f"wall\t{rec.clock.time()!r}")
        rec.outcome(status="ok")

    Path(os.environ["TRACEWAKE_TEST_TRANSCRIPT"]).write_text("\n".join(lines), encoding="utf-8")


if __name__ == "__main__":
    main()

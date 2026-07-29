"""A minimal ReAct coding agent, instrumented end to end.

Every nondeterministic input it consumes goes through a locus session: the model
through `session.model`, its tools through `session.tools`, file access through
`session.fs`, and elapsed time through `session.clock`. Nothing reaches the disk
or the clock around the side, because a corpus recorded through a partial surface
would leave every later analysis with the same hole in it.

Actions are a fenced JSON block the agent parses itself rather than a provider's
tool-calling API. Small models are unreliable at the structured form, and a
malformed block is more useful as a recorded observation the model has to recover
from than as an exception.
"""

from __future__ import annotations

import json
import re
import subprocess
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from locus import Message, Session, ToolCallRequest, ToolOutcome, Usage

from .tasks import relative_source_files

# Every context block a model call carries is labelled with where it came from.
# Free to capture here and impossible to reconstruct from a finished transcript,
# because by then a file's contents and a tool's output are both just text.
SYSTEM_PROMPT = "system_prompt"
TOOL_SCHEMA = "tool_schema"
TASK_ISSUE = "task_issue"
ASSISTANT_REASONING = "assistant_reasoning"
TOOL_OUTPUT = "tool_output"
FILE_READ = "file_read"
TEST_OUTPUT = "test_output"
ERROR_FEEDBACK = "error_feedback"
REPO_MAP = "repo_map"

PROVENANCE = (
    SYSTEM_PROMPT,
    TOOL_SCHEMA,
    TASK_ISSUE,
    ASSISTANT_REASONING,
    TOOL_OUTPUT,
    FILE_READ,
    TEST_OUTPUT,
    ERROR_FEEDBACK,
    REPO_MAP,
)

ROLE = """\
You are a software engineer fixing a bug in a Python repository. You work by \
taking one action at a time: look at the code, form a hypothesis, change one \
thing, and run the tests to check.

The bug is a single small mistake in the library source — a wrong comparison, an \
off-by-one bound, two arguments the wrong way round, or a missing early return. \
It is not in the tests. Do not edit any test file.

Because the bug is one small mistake, the fix is one small edit. Read the file \
first, find the single line that is wrong, and change just that line. Do not \
rewrite a function, do not reformat anything, and do not add print statements or \
logging — you cannot see their output, and an edit that changes more than the one \
wrong line usually breaks the file."""

TOOL_HELP = """\
Reply with a short line of reasoning, then exactly one action in a fenced json \
block. Nothing after the block.

Every "path" must be copied exactly from the file list you were given. Paths \
that appear in this help are shapes, not real files.

Available actions:

  {"action": "read_file", "path": "PATH"}
      Read a file. Output is line-numbered; the numbers are not part of the file.

  {"action": "search", "query": "TEXT"}
      Show every source line containing TEXT. Search for the name of the \
function or class the failing tests exercise, not for the name of the test.

  {"action": "edit_file", "path": "PATH", "old": "OLD", "new": "NEW"}
      Replace an exact snippet. OLD must appear exactly once in the file and be \
copied character for character from what you read, without the line numbers.

  {"action": "run_tests"}
      Run the suite and see what passes.

  {"action": "list_files"}
      Show the source file list again.

  {"action": "submit"}
      Stop, once the tests pass.

A good run looks like: search for the function the failing test exercises, read \
the file it is in, change the one wrong line, run the tests, submit."""

ACTION_BLOCK = re.compile(r"```(?:json)?\s*(\{.*?\})\s*```", re.DOTALL)
BARE_OBJECT = re.compile(r"(\{[^{}]*\"action\"[^{}]*\})", re.DOTALL)

MAX_FILE_CHARS = 12_000
MAX_TOOL_CHARS = 6_000


@dataclass
class Trace:
    steps: int = 0
    edits: int = 0
    test_runs: int = 0
    parse_failures: int = 0
    submitted: bool = False
    stop_reason: str = "step_budget"
    elapsed: float = 0.0
    usage: Usage = field(default_factory=Usage)
    actions: list[str] = field(default_factory=list)


def _clip(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    half = limit // 2
    dropped = len(text) - limit
    return f"{text[:half]}\n\n... [{dropped} characters omitted] ...\n\n{text[-half:]}"


class Tools:
    """The agent's hands. Every filesystem touch goes through the session."""

    def __init__(self, session: Session, root: Path, source_dirs: tuple[str, ...],
                 run_tests: Callable[[], tuple[str, bool]]) -> None:
        self._fs = session.fs.rooted(root)
        self._root = root
        self._source_dirs = source_dirs
        self._run_tests = run_tests
        self.last_tests_green = False

    def source_files(self) -> list[str]:
        return relative_source_files(self._root, self._source_dirs)

    def dispatch(self, name: str, args: dict[str, Any]) -> ToolOutcome:
        match name:
            case "list_files":
                return ToolOutcome(content="\n".join(self.source_files()))
            case "read_file":
                return self._read(str(args.get("path", "")))
            case "search":
                return self._search(str(args.get("query", "")))
            case "edit_file":
                return self._edit(
                    str(args.get("path", "")), str(args.get("old", "")), str(args.get("new", ""))
                )
            case "run_tests":
                output, green = self._run_tests()
                self.last_tests_green = green
                return ToolOutcome(
                    content=_clip(output, MAX_TOOL_CHARS), status="ok" if green else "error",
                    error=None if green else "the suite is still failing",
                )
            case _:
                return ToolOutcome(
                    content=f"there is no action called {name!r}",
                    status="error",
                    error="unknown action",
                )

    def _read(self, path: str) -> ToolOutcome:
        if not path:
            return ToolOutcome(content="read_file needs a path", status="error", error="no path")
        try:
            text = self._fs.read_text(path)
        except (FileNotFoundError, ValueError) as exc:
            return ToolOutcome(
                content=f"cannot read {path}: {exc}", status="error", error="unreadable"
            )
        numbered = "\n".join(
            f"{n:>5}  {line}" for n, line in enumerate(text.splitlines(), start=1)
        )
        return ToolOutcome(content=_clip(numbered, MAX_FILE_CHARS))

    def _search(self, query: str) -> ToolOutcome:
        if not query:
            return ToolOutcome(content="search needs a query", status="error", error="no query")
        hits: list[str] = []
        for relative in self.source_files():
            try:
                text = self._fs.read_text(relative)
            except (FileNotFoundError, ValueError):
                continue
            for number, line in enumerate(text.splitlines(), start=1):
                if query in line:
                    hits.append(f"{relative}:{number}: {line.strip()}")
                    if len(hits) >= 40:
                        return ToolOutcome(content="\n".join(hits) + "\n... more matches")
        return ToolOutcome(content="\n".join(hits) if hits else f"no matches for {query!r}")

    def _edit(self, path: str, old: str, new: str) -> ToolOutcome:
        if not path or not old:
            return ToolOutcome(
                content="edit_file needs a path and an exact 'old' snippet",
                status="error",
                error="incomplete edit",
            )
        if "test" in Path(path).name:
            return ToolOutcome(
                content=f"{path} is a test file and the bug is not in the tests",
                status="error",
                error="refused",
            )
        try:
            text = self._fs.read_text(path)
        except (FileNotFoundError, ValueError) as exc:
            return ToolOutcome(
                content=f"cannot read {path}: {exc}", status="error", error="unreadable"
            )
        occurrences = text.count(old)
        if occurrences == 0:
            return ToolOutcome(
                content=(
                    f"that snippet does not appear in {path}. Copy it exactly as the file "
                    f"shows it, without the line numbers."
                ),
                status="error",
                error="no match",
            )
        if occurrences > 1:
            return ToolOutcome(
                content=(
                    f"that snippet appears {occurrences} times in {path}. Include enough "
                    f"surrounding lines to make it unique."
                ),
                status="error",
                error="ambiguous match",
            )
        self._fs.write_text(path, text.replace(old, new, 1))
        return ToolOutcome(content=f"replaced 1 occurrence in {path}")


def parse_action(text: str) -> dict[str, Any] | None:
    blocks = ACTION_BLOCK.findall(text) or BARE_OBJECT.findall(text)
    for block in reversed(blocks):
        try:
            parsed = json.loads(block)
        except json.JSONDecodeError:
            continue
        if isinstance(parsed, dict) and isinstance(parsed.get("action"), str):
            return parsed
    return None


PROVENANCE_FOR = {
    "read_file": FILE_READ,
    "search": FILE_READ,
    "run_tests": TEST_OUTPUT,
}


TOOL_NAMES = ["list_files", "read_file", "search", "edit_file", "run_tests", "submit"]


def run(
    session: Session,
    model: Any,
    issue: str,
    tools: Tools,
    max_steps: int = 18,
    temperature: float = 0.7,
) -> Trace:
    # The file list is given up front rather than left to a first `list_files`
    # step: without it a small model invents paths out of the examples in its own
    # instructions and spends most of its budget guessing.
    listing = "\n".join(tools.source_files())
    messages = [
        Message(role="system", content=ROLE, provenance=SYSTEM_PROMPT),
        Message(role="system", content=TOOL_HELP, provenance=TOOL_SCHEMA),
        Message(
            role="user",
            content=f"The library source files are:\n\n{listing}",
            provenance=REPO_MAP,
        ),
        Message(role="user", content=issue, provenance=TASK_ISSUE),
    ]
    trace = Trace()
    dispatcher = session.tools(tools.dispatch)
    started = session.clock.monotonic()

    for step in range(max_steps):
        trace.steps = step + 1
        with model.stream(
            messages=messages, tools=TOOL_NAMES, temperature=temperature
        ) as stream:
            for _ in stream:
                pass
        response, call_id = stream.response, stream.call_id
        trace.usage = Usage(
            input_tokens=trace.usage.input_tokens + response.usage.input_tokens,
            output_tokens=trace.usage.output_tokens + response.usage.output_tokens,
        )
        messages.append(
            Message(role="assistant", content=response.text, provenance=ASSISTANT_REASONING)
        )

        action = parse_action(response.text)
        if action is None:
            trace.parse_failures += 1
            messages.append(
                Message(
                    role="user",
                    content=(
                        "That reply had no action in it. End your next reply with exactly "
                        'one fenced json block, for example:\n\n```json\n{"action": '
                        '"list_files"}\n```'
                    ),
                    provenance=ERROR_FEEDBACK,
                )
            )
            continue

        name = action.pop("action")
        trace.actions.append(name)
        if name == "submit":
            trace.submitted = True
            trace.stop_reason = "submitted"
            break

        request = ToolCallRequest(
            id=f"step{step}-{name}", name=name, args=action, batch_index=0
        )
        outcome = dispatcher.call(call_id, request)
        if name == "edit_file" and outcome.status == "ok":
            trace.edits += 1
        if name == "run_tests":
            trace.test_runs += 1

        messages.append(
            Message(
                role="user",
                content=outcome.content or "(no output)",
                tool_call_id=request.id,
                provenance=PROVENANCE_FOR.get(name, TOOL_OUTPUT),
            )
        )

    trace.elapsed = session.clock.monotonic() - started
    return trace

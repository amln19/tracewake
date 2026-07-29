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

  {"action": "read_file", "path": "PATH", "around": N}
      Read a file, line-numbered; the numbers are not part of the file. "around"
      is optional and takes a line number: a large file is shown as a window
      centred on that line, so use it to jump to the line a test failure named.

  {"action": "search", "query": "TEXT"}
      Show every source line containing TEXT. Search for the name of the \
function or class the failing tests exercise, not for the name of the test.

  {"action": "edit_file", "path": "PATH", "old": "OLD", "new": "NEW", "at": N}
      Replace a snippet. OLD must be copied character for character from what you
      read, without the line numbers. "at" is optional and takes a line number;
      use it when the same snippet appears more than once.

  {"action": "run_tests"}
      Run the suite and see what passes.

  {"action": "list_files"}
      Show the source file list again.

  {"action": "submit"}
      Stop, once the tests pass.

Start by running the tests. The failure output names the file and line that blew \
up and shows the values involved, which is far more use than guessing from the \
report. Then read that file around that line, change the one wrong line, run the \
tests again, and submit.

Never repeat an action you have already taken — if a search or a read did not \
tell you what you needed, the answer is a different action, not the same one \
again."""

ACTION_BLOCK = re.compile(r"```(?:json)?\s*(\{.*?\})\s*```", re.DOTALL)
BARE_OBJECT = re.compile(r"(\{[^{}]*\"action\"[^{}]*\})", re.DOTALL)

MAX_FILE_CHARS = 12_000
MAX_TOOL_CHARS = 6_000
# Lines shown per windowed read, and lines of context around a search hit. Both
# exist so the agent can locate code without pulling a whole module into context.
WINDOW_LINES = 120
SEARCH_CONTEXT = 2


# Raised from three once file reads were windowed: with the bug actually
# visible, a model that fumbles a turn often recovers on the next one, and a
# stalled turn is cheap now that it carries no file contents.
MAX_CONSECUTIVE_STALLS = 5


@dataclass
class Trace:
    # Turns the model was called; actions it actually got to take. They come
    # apart when a reply carries no usable action, and the gap is the signal.
    turns: int = 0
    actions_taken: int = 0
    stalled: int = 0
    edits: int = 0
    test_runs: int = 0
    parse_failures: int = 0
    repeats: int = 0
    submitted: bool = False
    stop_reason: str = "step_budget"
    elapsed: float = 0.0
    usage: Usage = field(default_factory=Usage)
    actions: list[str] = field(default_factory=list)


def _line_number(value: Any) -> int | None:
    """A line number the model may have sent as an int or as a string."""
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, int | float):
        return int(value)
    if isinstance(value, str) and value.strip().isdigit():
        return int(value)
    return None


def _replace_at(text: str, old: str, new: str, line: int) -> str | None:
    """Replace the occurrence of `old` that starts on `line`, leaving others alone."""
    lines = text.splitlines(keepends=True)
    offset = sum(len(x) for x in lines[: line - 1])
    if text[offset : offset + len(old)] == old:
        return text[:offset] + new + text[offset + len(old) :]
    # The model copied from a line-numbered view, so leading whitespace is the
    # usual mismatch; try the snippet anchored anywhere on the named line.
    end = offset + len(lines[line - 1]) if line <= len(lines) else len(text)
    found = text.find(old, offset, max(end, offset + len(old)))
    if found == -1:
        return None
    return text[:found] + new + text[found + len(old) :]


def _number(lines: list[str], first: int) -> str:
    return "\n".join(f"{n:>5}  {line}" for n, line in enumerate(lines, start=first))


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
                return self._read(str(args.get("path", "")), _line_number(args.get("around")))
            case "search":
                return self._search(str(args.get("query", "")))
            case "edit_file":
                return self._edit(
                    str(args.get("path", "")),
                    str(args.get("old", "")),
                    str(args.get("new", "")),
                    _line_number(args.get("at")),
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

    def _read(self, path: str, around: int | None = None) -> ToolOutcome:
        """Read a file, or a window of it.

        A whole-file read of a large module cannot be shown in full, and clipping
        the middle out of it is worse than useless: the line that needs fixing is
        usually in the middle, so the agent is handed a file that provably does
        not contain its bug and re-reads it looking for what was never there.
        Large files are therefore windowed, and the agent is told how to move the
        window rather than left to guess.
        """
        if not path:
            return ToolOutcome(content="read_file needs a path", status="error", error="no path")
        try:
            text = self._fs.read_text(path)
        except (FileNotFoundError, ValueError) as exc:
            return ToolOutcome(
                content=f"cannot read {path}: {exc}", status="error", error="unreadable"
            )

        lines = text.splitlines()
        if around is None and len(text) <= MAX_FILE_CHARS:
            return ToolOutcome(content=_number(lines, 1))

        centre = around if around is not None else WINDOW_LINES // 2
        first = max(1, centre - WINDOW_LINES // 2)
        last = min(len(lines), first + WINDOW_LINES - 1)
        first = max(1, last - WINDOW_LINES + 1)
        header = (
            f"{path} has {len(lines)} lines; showing {first}-{last}. "
            f'Use {{"action": "read_file", "path": "{path}", "around": N}} to see '
            f"another part, where N is a line number.\n\n"
        )
        return ToolOutcome(content=header + _number(lines[first - 1 : last], first))

    def _search(self, query: str) -> ToolOutcome:
        """Every source line containing `query`, with the lines around it.

        Context is included because a bare file:line list sends the agent off to
        read the whole module, which is the expensive move this tool exists to
        avoid.
        """
        if not query:
            return ToolOutcome(content="search needs a query", status="error", error="no query")
        blocks: list[str] = []
        found = 0
        for relative in self.source_files():
            try:
                lines = self._fs.read_text(relative).splitlines()
            except (FileNotFoundError, ValueError):
                continue
            for number, line in enumerate(lines, start=1):
                if query not in line:
                    continue
                found += 1
                if found > 20:
                    blocks.append("... more matches; narrow the query")
                    return ToolOutcome(content="\n\n".join(blocks))
                first = max(1, number - SEARCH_CONTEXT)
                last = min(len(lines), number + SEARCH_CONTEXT)
                blocks.append(
                    f"{relative}:{number}\n" + _number(lines[first - 1 : last], first)
                )
        return ToolOutcome(
            content="\n\n".join(blocks) if blocks else f"no matches for {query!r}"
        )

    def _edit(self, path: str, old: str, new: str, at: int | None = None) -> ToolOutcome:
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
        if text.count(old) == 0:
            return ToolOutcome(
                content=(
                    f"that snippet does not appear in {path}. Copy it exactly as the file "
                    f"shows it, without the line numbers."
                ),
                status="error",
                error="no match",
            )

        # Which occurrence, by the line each starts on. A snippet that appears
        # twice is not a dead end: the agent already knows the line it means,
        # because the test failure and the file window both gave it one.
        hits = [n for n, line in enumerate(text.splitlines(), start=1) if old.split("\n")[0] in line]
        if at is not None and len(hits) > 1:
            nearest = min(hits, key=lambda n: abs(n - at))
            hits = [nearest]
        if len(hits) > 1:
            return ToolOutcome(
                content=(
                    f"that snippet appears {len(hits)} times in {path}, starting at lines "
                    f"{', '.join(str(n) for n in hits[:8])}. Add \"at\": <line number> to say "
                    f"which one you mean, or include more surrounding lines to make it unique."
                ),
                status="error",
                error="ambiguous match",
            )

        updated = _replace_at(text, old, new, hits[0])
        if updated is None:
            return ToolOutcome(
                content=(
                    f"the snippet does not start at line {hits[0]} of {path} as written. Read "
                    f"the file around that line and copy it exactly."
                ),
                status="error",
                error="no match",
            )
        self._fs.write_text(path, updated)
        return ToolOutcome(content=f"replaced 1 occurrence in {path} at line {hits[0]}")


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
    taken: dict[str, int] = {}
    dispatcher = session.tools(tools.dispatch)
    started = session.clock.monotonic()

    # Two separate budgets. `max_steps` counts actions the agent actually took,
    # because a turn that produced no action gave it nothing to reason from and
    # charging it would let a stuck model spend the whole run saying nothing.
    # `turns` bounds total cost regardless, and a run that stalls repeatedly ends
    # early rather than grinding out a full budget of refusals.
    while trace.actions_taken < max_steps and trace.turns < max_steps * 2:
        if trace.stalled >= MAX_CONSECUTIVE_STALLS:
            trace.stop_reason = "stuck"
            break
        trace.turns += 1
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
            trace.stalled += 1
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

        # A small model that gets nothing from an action will take the identical
        # action again. Executing it a second time wastes the budget; refusing it
        # and charging a step wastes the budget faster, because the model tends to
        # repeat the refused action rather than pick a new one. So the repeat is
        # refused, charged nothing, and counted towards ending a run that is stuck.
        signature = f"{name}:{json.dumps(action, sort_keys=True)}"
        if signature in taken:
            trace.repeats += 1
            trace.stalled += 1
            messages.append(
                Message(
                    role="user",
                    content=(
                        f"You already ran exactly that at step {taken[signature]}, and its "
                        f"output is above. Repeating it will return the same thing. Choose a "
                        f"different action: read a file you have not read, or edit the line "
                        f"you think is wrong."
                    ),
                    provenance=ERROR_FEEDBACK,
                )
            )
            continue

        trace.stalled = 0
        step = trace.actions_taken
        trace.actions_taken += 1
        taken[signature] = step + 1

        request = ToolCallRequest(
            id=f"step{step}-{name}", name=name, args=action, batch_index=0
        )
        outcome = dispatcher.call(call_id, request)
        if name == "edit_file" and outcome.status == "ok":
            trace.edits += 1
        if name == "run_tests":
            trace.test_runs += 1

        left = max_steps - step - 1
        budget = f"\n\n[{left} step{'' if left == 1 else 's'} left]" if left <= 4 else ""
        messages.append(
            Message(
                role="user",
                content=(outcome.content or "(no output)") + budget,
                tool_call_id=request.id,
                provenance=PROVENANCE_FOR.get(name, TOOL_OUTPUT),
            )
        )

    trace.elapsed = session.clock.monotonic() - started
    return trace

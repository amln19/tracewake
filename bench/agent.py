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
      use it when the same snippet appears more than once. The result is echoed
      back to you. If NEW spans several lines, every line needs the indentation
      of the block it sits in.

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
# Lines of the result echoed back after an edit, so the agent can see what it
# actually wrote rather than guess when it needs to repair it.
EDIT_ECHO = 6
# Tool observations kept in full. Older ones are elided: a run that reads eight
# file windows carries fifty thousand characters by its last turn, and inference
# cost tracks context, so an unbounded history makes late turns cost several
# times what early ones did. The content is re-readable on demand, so dropping it
# costs the agent nothing it cannot recover.
KEEP_OBSERVATIONS = 4
ELIDED = "[earlier output dropped to keep the context small; repeat the action to see it again]"


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


def _reindent(new: str, indent: str, lead: bool = True) -> str:
    """Put `new` at `indent`, keeping the relative shape of its own lines.

    A model writes a replacement block the way it reads — the first line flush
    left and the body relative to it — because it copied the original out of a
    line-numbered view that stripped the file's own indentation. Splicing that in
    verbatim drops a dedented line into an indented block and breaks the file.

    `lead` says whether the first line needs the prefix. Replacing whole lines it
    does; splicing into the middle of a line it does not, because the file's own
    indentation is already to the left of the splice point.
    """
    if "\n" not in new:
        return new
    lines = new.splitlines()
    body = [x for x in lines if x.strip()]
    common = min((len(x) - len(x.lstrip()) for x in body), default=0)
    out = [indent + x[common:] if x.strip() else "" for x in lines]
    if not lead:
        out[0] = lines[0][common:]
    return "\n".join(out)


def _replace_at(text: str, old: str, new: str, line: int) -> str | None:
    """Replace the occurrence of `old` that starts on `line`, leaving others alone."""
    lines = text.splitlines(keepends=True)
    offset = sum(len(x) for x in lines[: line - 1])
    anchor = lines[line - 1] if line <= len(lines) else ""
    indent = anchor[: len(anchor) - len(anchor.lstrip())]
    # Re-indented on both paths. `_fuzzy_replace` already did this; doing it here
    # too is what keeps an exact match and a whitespace-tolerant one from
    # producing different files from the same inputs — and the exact path is the
    # common one, since a single-line `old` almost always matches as a substring.
    shaped = _reindent(new, indent, lead=False)
    if text[offset : offset + len(old)] == old:
        return text[:offset] + shaped + text[offset + len(old) :]
    # The model copied from a line-numbered view, so leading whitespace is the
    # usual mismatch; try the snippet anchored near the named line. The window
    # has to cover every line `old` spans, not just the first one — sizing it to
    # one line left the tail of a multi-line match outside the search window
    # whenever the match itself started a few characters short of `offset`.
    span = old.count("\n") + 2
    end = sum(len(x) for x in lines[: min(len(lines), line - 1 + span)])
    found = text.find(old, offset, max(end, offset + len(old)))
    if found == -1:
        return None
    return text[:found] + shaped + text[found + len(old) :]


def _context(history: list[Message], observations: list[tuple[str, int]]) -> list[Message]:
    """The history as the model should see it, with stale observations elided."""
    stale = {index for _, index in observations[: max(0, len(observations) - KEEP_OBSERVATIONS)]}
    return [
        Message(
            role=m.role, content=ELIDED, tool_call_id=m.tool_call_id, provenance=m.provenance
        )
        if position in stale
        else m
        for position, m in enumerate(history)
    ]


def _fuzzy_replace(
    text: str, old: str, new: str, at: int | None
) -> tuple[str | None, list[int]]:
    """Replace `old` matching on content, ignoring the indentation it was sent with.

    A model reading a line-numbered view copies the code and drops the file's
    leading whitespace along with the number prefix, so an otherwise correct
    snippet fails an exact match. Matching on stripped lines and re-indenting the
    replacement to the block it lands in removes an obstacle that has nothing to
    do with finding the bug. Returns the new text and the lines that matched.
    """
    lines = text.splitlines()
    wanted = [line.strip() for line in old.splitlines()]
    if not wanted:
        return (None, [])
    found = [
        index + 1
        for index in range(len(lines) - len(wanted) + 1)
        if all(lines[index + offset].strip() == wanted[offset] for offset in range(len(wanted)))
    ]
    if not found:
        return (None, [])
    if at is not None and len(found) > 1:
        found = [min(found, key=lambda n: abs(n - at))]
    if len(found) > 1:
        return (None, found)

    start = found[0]
    anchor = lines[start - 1]
    indent = anchor[: len(anchor) - len(anchor.lstrip())]
    rebuilt = _reindent(new, indent).splitlines() or [""]
    out = lines[: start - 1] + rebuilt + lines[start - 1 + len(wanted) :]
    return ("\n".join(out) + ("\n" if text.endswith("\n") else ""), found)


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
        self._files: list[str] | None = None
        self.last_tests_green = False

    def source_files(self) -> list[str]:
        return relative_source_files(self._root, self._source_dirs)

    def _all_python_files(self) -> list[str]:
        """Every Python file under the root, tests included.

        `search` uses this rather than `source_files()`: the issue text names
        failing tests prominently, so the agent naturally searches for them, and
        a query that structurally cannot match sends it looping on a dead end.
        `read_file` can already open a test file to see why it fails, so `search`
        seeing the same files removes an inconsistency rather than widening what
        the agent may act on — editing a test file is still refused separately.

        Walked through `session.fs` rather than `rglob` so the listing reaches the
        log: which files exist is an input the agent acts on, and a replay against
        a differently-populated tree would otherwise return a different answer
        silently instead of raising. Walked once and cached, because no tool
        creates or removes a file and re-walking on every search would put a
        listing event in the log for every directory, several times a run.
        """
        if self._files is None:
            self._files = self._walk()
        return self._files

    def _walk(self) -> list[str]:
        found: list[str] = []
        pending = [""]
        while pending:
            current = pending.pop()
            try:
                names = self._fs.listdir(current or ".")
            except (FileNotFoundError, ValueError):
                continue
            for name in names:
                if name in {"__pycache__", ".git"}:
                    continue
                relative = f"{current}/{name}" if current else name
                if name.endswith(".py"):
                    found.append(relative)
                elif "." not in name:
                    # No suffix, so a directory is worth trying. Anything else is
                    # a file, and probing it would put a failed listing in the log
                    # for every README and LICENSE in the tree.
                    pending.append(relative)
        return sorted(found)

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
        for relative in self._all_python_files():
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
        if blocks:
            return ToolOutcome(content="\n\n".join(blocks))
        # A bare "no matches" gives the model nothing to correct, and a query
        # combining several identifiers is the usual cause: substring search
        # cannot match "a\nb" or "a and b" against any single line.
        hint = (
            " That query has more than one line or word in it — search for one "
            "identifier at a time."
            if len(query.split()) > 1 or "\n" in query
            else ""
        )
        return ToolOutcome(content=f"no matches for {query!r}.{hint}")

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
            loose, where = _fuzzy_replace(text, old, new, at)
            if loose is None:
                if len(where) > 1:
                    return ToolOutcome(
                        content=(
                            f"that snippet appears at lines {', '.join(str(n) for n in where[:8])} "
                            f"in {path}. Add \"at\": <line number> to say which one you mean."
                        ),
                        status="error",
                        error="ambiguous match",
                    )
                # Shown immediately rather than telling the agent to go read the
                # file: after a previous edit broke this file, the agent tends to
                # retry the same "old" text rather than spend a turn re-reading,
                # and repeats the identical failing edit until it stalls out.
                return ToolOutcome(
                    content=(
                        f"that snippet does not appear in {path} — the file may have changed "
                        f"since you last read it.\n\n{self._nearby(path, text, at)}"
                    ),
                    status="error",
                    error="no match",
                )
            return self._apply(path, loose, where[0], new)

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
                    f"the snippet does not start at line {hits[0]} of {path} as written — the "
                    f"file may have changed since you last read it.\n\n"
                    f"{self._nearby(path, text, hits[0])}"
                ),
                status="error",
                error="no match",
            )
        return self._apply(path, updated, hits[0], new)

    def _nearby(self, path: str, text: str, around: int | None) -> str:
        lines = text.splitlines()
        centre = around if around is not None else 1
        first = max(1, centre - EDIT_ECHO)
        last = min(len(lines), centre + EDIT_ECHO)
        return f"{path} currently reads:\n" + _number(lines[first - 1 : last], first)

    def _apply(self, path: str, updated: str, line: int, new: str) -> ToolOutcome:
        """Write the edit and show the agent what it actually produced.

        Reporting a syntax error here rather than leaving it for the next test run
        matters because the agent otherwise has to guess at the file's current
        state to write a matching snippet for the repair.
        """
        self._fs.write_text(path, updated)
        lines = updated.splitlines()
        first = max(1, line - EDIT_ECHO)
        last = min(len(lines), line + new.count("\n") + EDIT_ECHO)
        shown = f"{path} now reads:\n" + _number(lines[first - 1 : last], first)
        try:
            compile(updated, path, "exec")
        except SyntaxError as exc:
            return ToolOutcome(
                content=(
                    f"that edit left {path} with a syntax error on line {exc.lineno}: "
                    f"{exc.msg}. Python is whitespace-sensitive, so a replacement spanning "
                    f"several lines has to carry the indentation of the block it sits in.\n\n"
                    f"{shown}"
                ),
                status="error",
                error="syntax error",
            )
        return ToolOutcome(content=f"replaced 1 occurrence in {path} at line {line}.\n\n{shown}")


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
    observations: list[tuple[str, int]] = []
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
        # Dropping an observation makes its action worth taking again, so the
        # signature goes with it; otherwise the agent is told to repeat an action
        # and then refused permission to.
        stale = observations[: max(0, len(observations) - KEEP_OBSERVATIONS)]
        for signature, _ in stale:
            taken.pop(signature, None)
        with model.stream(
            messages=_context(messages, observations), tools=TOOL_NAMES, temperature=temperature
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
            # An edit changes what every earlier read and test run would return,
            # so none of them are repeats any more. Without this the agent is
            # refused permission to check its own work and spirals.
            taken = {signature: step + 1}
        if name == "run_tests":
            trace.test_runs += 1

        left = max_steps - step - 1
        budget = f"\n\n[{left} step{'' if left == 1 else 's'} left]" if left <= 4 else ""
        observations.append((signature, len(messages)))
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

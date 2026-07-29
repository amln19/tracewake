"""Mechanically injected bugs, and the tasks they become.

A task is a bug that was proven to matter: the repository's suite is green
before the mutation and red after it. That check is what makes the task's
ground truth real rather than asserted — the file named in `ground_truth_file`
is the file that has to change, because it is the file that changed.

Mutations are located with `ast` and applied by splicing the source text at the
node's own offsets. Unparsing the tree instead would reformat the whole file,
and a diff that reformats everything tells the agent exactly nothing about
where to look.
"""

from __future__ import annotations

import ast
import json
import random
import shutil
import tempfile
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Literal

from .repos import CORPUS_ROOT, REPOS, Repo, SuiteReport, run_tests, working_copy

MANIFEST = CORPUS_ROOT / "tasks.json"

Operator = Literal["operator_swap", "off_by_one", "argument_swap", "deleted_guard"]

COMPARISONS: dict[type[ast.cmpop], str] = {
    ast.Lt: "<",
    ast.LtE: "<=",
    ast.Gt: ">",
    ast.GtE: ">=",
    ast.Eq: "==",
    ast.NotEq: "!=",
}

# Each swap changes behaviour at a boundary rather than everywhere, which is what
# makes the resulting bug worth finding: it survives the obvious cases.
COMPARISON_SWAPS = {"<": "<=", "<=": "<", ">": ">=", ">=": ">", "==": "!=", "!=": "=="}
ARITHMETIC_SWAPS = {"+": "-", "-": "+", "*": "//", "//": "*"}
BOOLEAN_SWAPS = {"and": "or", "or": "and"}


@dataclass(frozen=True)
class Mutation:
    operator: Operator
    file: str
    line: int
    column: int
    end_line: int
    end_column: int
    before: str
    after: str
    description: str


@dataclass
class Task:
    task_id: str
    repo: str
    operator: Operator
    # The file the fix belongs in. Known because it is the file that was broken,
    # which is the whole reason to inject rather than to collect.
    ground_truth_file: str
    ground_truth_line: int
    mutation: Mutation
    issue: str
    broken_tests: tuple[str, ...]
    baseline_summary: str
    broken_summary: str

    def to_json(self) -> dict[str, object]:
        data = asdict(self)
        data["broken_tests"] = list(self.broken_tests)
        return data

    @staticmethod
    def from_json(data: dict) -> Task:
        mutation = Mutation(**data.pop("mutation"))
        data["broken_tests"] = tuple(data["broken_tests"])
        return Task(mutation=mutation, **data)


def apply_mutation(source: str, mutation: Mutation) -> str:
    """Put the bug back into a clean file, at the offsets it was found at."""
    lines = source.splitlines(keepends=True)
    starts = [0]
    for line in lines:
        starts.append(starts[-1] + len(line))
    begin = starts[mutation.line - 1] + mutation.column
    end = starts[mutation.end_line - 1] + mutation.end_column
    if source[begin:end] != mutation.before:
        raise RuntimeError(
            f"the mutation for {mutation.file}:{mutation.line} no longer applies: expected "
            f"{mutation.before!r} at that position but found {source[begin:end]!r}. The "
            f"manifest and the pinned checkout disagree; rebuild the manifest."
        )
    return source[:begin] + mutation.after + source[end:]


def relative_source_files(root: Path, source_dirs: tuple[str, ...]) -> list[str]:
    found: list[str] = []
    for entry in source_dirs:
        target = root / entry
        if target.is_file():
            found.append(entry)
        elif target.is_dir():
            found.extend(
                str(p.relative_to(root))
                for p in sorted(target.rglob("*.py"))
                if "__pycache__" not in p.parts
            )
    return found


def source_files(repo: Repo) -> list[Path]:
    found: list[Path] = []
    for entry in repo.source_dirs:
        target = repo.path / entry
        if target.is_file():
            found.append(target)
        elif target.is_dir():
            found.extend(
                p
                for p in sorted(target.rglob("*.py"))
                if "test" not in p.name and "__pycache__" not in p.parts
            )
    return found


def _splice(source: str, node: ast.AST, replacement: str) -> str:
    lines = source.splitlines(keepends=True)
    starts = [0]
    for line in lines:
        starts.append(starts[-1] + len(line))
    begin = starts[node.lineno - 1] + node.col_offset
    end = starts[node.end_lineno - 1] + node.end_col_offset
    return source[:begin] + replacement + source[end:]


def _at(node: ast.AST, source: str) -> str:
    lines = source.splitlines(keepends=True)
    starts = [0]
    for line in lines:
        starts.append(starts[-1] + len(line))
    begin = starts[node.lineno - 1] + node.col_offset
    end = starts[node.end_lineno - 1] + node.end_col_offset
    return source[begin:end]


def _mutation(
    operator: Operator, path: Path, node: ast.AST, before: str, after: str, description: str
) -> Mutation:
    return Mutation(
        operator=operator,
        file=path.name,
        line=node.lineno,
        column=node.col_offset,
        end_line=node.end_lineno,
        end_column=node.end_col_offset,
        before=before,
        after=after,
        description=description,
    )


def candidates(path: Path, source: str) -> list[tuple[Mutation, str]]:
    """Every mutation this file admits, each with the mutated source it produces."""
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return []

    found: list[tuple[Mutation, str]] = []
    for node in ast.walk(tree):
        found.extend(_comparison(path, source, node))
        found.extend(_arithmetic(path, source, node))
        found.extend(_boolean(path, source, node))
        found.extend(_off_by_one(path, source, node))
        found.extend(_argument_swap(path, source, node))
        found.extend(_deleted_guard(path, source, node))
    return found


def _comparison(path: Path, source: str, node: ast.AST) -> list[tuple[Mutation, str]]:
    if not isinstance(node, ast.Compare) or len(node.ops) != 1:
        return []
    symbol = COMPARISONS.get(type(node.ops[0]))
    if symbol is None or symbol not in COMPARISON_SWAPS:
        return []
    original = _at(node, source)
    # Only the operator between the two operands is rewritten, so a `<` inside a
    # string or a nested comparison is left alone.
    left, right = _at(node.left, source), _at(node.comparators[0], source)
    middle = original[len(left) : len(original) - len(right)]
    if middle.strip() != symbol:
        return []
    swapped = COMPARISON_SWAPS[symbol]
    replacement = left + middle.replace(symbol, swapped, 1) + right
    return [
        (
            _mutation(
                "operator_swap",
                path,
                node,
                original,
                replacement,
                f"comparison {symbol} became {swapped}",
            ),
            _splice(source, node, replacement),
        )
    ]


def _arithmetic(path: Path, source: str, node: ast.AST) -> list[tuple[Mutation, str]]:
    symbols = {ast.Add: "+", ast.Sub: "-", ast.Mult: "*", ast.FloorDiv: "//"}
    if not isinstance(node, ast.BinOp):
        return []
    symbol = symbols.get(type(node.op))
    if symbol is None:
        return []
    original = _at(node, source)
    left, right = _at(node.left, source), _at(node.right, source)
    middle = original[len(left) : len(original) - len(right)]
    if middle.strip() != symbol:
        return []
    swapped = ARITHMETIC_SWAPS[symbol]
    replacement = left + middle.replace(symbol, swapped, 1) + right
    return [
        (
            _mutation(
                "operator_swap",
                path,
                node,
                original,
                replacement,
                f"arithmetic {symbol} became {swapped}",
            ),
            _splice(source, node, replacement),
        )
    ]


def _boolean(path: Path, source: str, node: ast.AST) -> list[tuple[Mutation, str]]:
    if not isinstance(node, ast.BoolOp) or len(node.values) != 2:
        return []
    symbol = "and" if isinstance(node.op, ast.And) else "or"
    original = _at(node, source)
    left, right = _at(node.values[0], source), _at(node.values[1], source)
    middle = original[len(left) : len(original) - len(right)]
    if middle.strip() != symbol:
        return []
    swapped = BOOLEAN_SWAPS[symbol]
    replacement = left + middle.replace(symbol, swapped, 1) + right
    return [
        (
            _mutation(
                "operator_swap", path, node, original, replacement, f"{symbol} became {swapped}"
            ),
            _splice(source, node, replacement),
        )
    ]


def _off_by_one(path: Path, source: str, node: ast.AST) -> list[tuple[Mutation, str]]:
    """Shift an index, slice bound or range argument by one."""
    targets: list[ast.expr] = []
    if isinstance(node, ast.Subscript):
        if isinstance(node.slice, ast.Slice):
            targets = [b for b in (node.slice.lower, node.slice.upper) if b is not None]
        elif not isinstance(node.slice, ast.Tuple):
            targets = [node.slice]
    elif (
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id in {"range", "len"}
        and node.args
    ):
        targets = list(node.args)

    found: list[tuple[Mutation, str]] = []
    for target in targets:
        original = _at(target, source)
        if "\n" in original or len(original) > 40:
            continue
        for delta, sign in ((1, "+"), (-1, "-")):
            replacement = f"{original} {sign} 1"
            # A literal shift is folded rather than appended, so the mutation
            # reads like something a person would have typed.
            if isinstance(target, ast.Constant) and isinstance(target.value, int):
                replacement = str(target.value + delta)
            found.append(
                (
                    _mutation(
                        "off_by_one",
                        path,
                        target,
                        original,
                        replacement,
                        f"bound {original} became {replacement}",
                    ),
                    _splice(source, target, replacement),
                )
            )
    return found


def _argument_swap(path: Path, source: str, node: ast.AST) -> list[tuple[Mutation, str]]:
    if not isinstance(node, ast.Call) or len(node.args) < 2:
        return []
    if any(isinstance(a, ast.Starred) for a in node.args):
        return []
    first, second = node.args[0], node.args[1]
    a, b = _at(first, source), _at(second, source)
    if a == b or "\n" in a or "\n" in b or len(a) > 40 or len(b) > 40:
        return []
    # Splice the later argument first so the earlier one's offsets still hold.
    mutated = _splice(source, second, a)
    mutated = _splice(mutated, first, b)
    return [
        (
            _mutation(
                "argument_swap",
                path,
                node,
                f"{a}, {b}",
                f"{b}, {a}",
                "the first two arguments to this call were swapped",
            ),
            mutated,
        )
    ]


def _deleted_guard(path: Path, source: str, node: ast.AST) -> list[tuple[Mutation, str]]:
    """Remove an early-exit guard: `if cond: return/raise/continue/break`."""
    if not isinstance(node, ast.If) or node.orelse or len(node.body) != 1:
        return []
    if not isinstance(node.body[0], ast.Return | ast.Raise | ast.Continue | ast.Break):
        return []
    original = _at(node, source)
    if "\n" in original.strip() and len(original.splitlines()) > 3:
        return []
    # `pass` keeps the block syntactically valid without keeping its effect.
    replacement = "pass"
    return [
        (
            _mutation(
                "deleted_guard",
                path,
                node,
                original,
                replacement,
                "a guard clause was removed",
            ),
            _splice(source, node, replacement),
        )
    ]


SYMPTOM = {
    "operator_swap": (
        "The results come back wrong at the boundary. Values that sit exactly on the "
        "edge of the condition are handled as though they were on the other side of it."
    ),
    "off_by_one": (
        "The output is off by one element. Sequences come back one item short or one "
        "item long, and the edges are what go wrong first."
    ),
    "argument_swap": (
        "The behaviour looks inverted: what should be applied to one value is being "
        "applied to the other, so results come back transposed."
    ),
    "deleted_guard": (
        "An input that used to be rejected early now falls through into the main path, "
        "so instead of the expected early return there is either a crash or a nonsense "
        "result."
    ),
}


def write_issue(repo: Repo, mutation: Mutation, failing: tuple[str, ...]) -> str:
    """A bug report a user could have filed.

    Deliberately says nothing about which file or line is at fault. About a third
    of the issues in the public SWE-bench set contain their own solution, which
    makes any later claim about which context mattered unfalsifiable; naming the
    location here would build that same flaw in on purpose.
    """
    # Keep the class, drop the file: two suites routinely share a method name,
    # and a list that says the same thing twice reads like a broken generator.
    names = list(dict.fromkeys(node.split("::", 1)[-1] for node in failing))
    listed = "\n".join(f"- {name}" for name in names[:4])
    more = f"\n\n...and {len(names) - 4} further failures." if len(names) > 4 else ""
    return (
        f"Something in {repo.name} is broken on the current checkout.\n\n"
        f"{SYMPTOM[mutation.operator]}\n\n"
        f"Running the test suite shows these failing:\n\n{listed}{more}\n\n"
        f"The suite was green before this regression, so the change is somewhere in "
        f"the library itself rather than in the tests. Please track it down and fix it."
    )


@dataclass
class Attempt:
    accepted: bool
    reason: str = ""
    task: Task | None = None
    report: SuiteReport | None = None


# A mutation that breaks the world is not a bug report, it is a broken checkout,
# and the agent learns nothing from it that generalizes.
MAX_BROKEN_TESTS = 12


def evaluate(
    repo: Repo,
    mutation: Mutation,
    mutated_source: str,
    relative_path: str,
    index: int,
    baseline: SuiteReport,
    scratch: Path,
) -> Attempt:
    try:
        ast.parse(mutated_source)
    except SyntaxError:
        return Attempt(False, "the mutation does not parse")

    copy = scratch / f"{repo.name}-{index}"
    if copy.exists():
        shutil.rmtree(copy)
    working_copy(repo, copy)
    (copy / relative_path).write_text(mutated_source, encoding="utf-8")

    report = run_tests(repo, copy, timeout=max(60.0, repo.baseline_seconds * 20))
    shutil.rmtree(copy, ignore_errors=True)

    if report.timed_out:
        return Attempt(False, "the mutated suite hangs")
    if report.green:
        return Attempt(False, "no test noticed the mutation")
    if report.errors and not report.failed:
        return Attempt(False, "the mutation breaks collection rather than behaviour")
    if len(report.failing_tests) > MAX_BROKEN_TESTS:
        return Attempt(False, f"{len(report.failing_tests)} tests broke, which is too many")
    if not report.failing_tests:
        return Attempt(False, "the suite is red but names no failing test")

    task_id = f"{repo.name}-{mutation.operator}-{index}"
    task = Task(
        task_id=task_id,
        repo=repo.name,
        operator=mutation.operator,
        ground_truth_file=relative_path,
        ground_truth_line=mutation.line,
        mutation=mutation,
        issue=write_issue(repo, mutation, report.failing_tests),
        broken_tests=report.failing_tests,
        baseline_summary=baseline.summary,
        broken_summary=report.summary,
    )
    return Attempt(True, "", task, report)


def build(
    per_repo: int = 4, seed: int = 20260729, repos: tuple[Repo, ...] = REPOS
) -> list[Task]:
    rng = random.Random(seed)
    tasks: list[Task] = []
    scratch = Path(tempfile.mkdtemp(prefix="bench-inject-"))
    try:
        for repo in repos:
            baseline = run_tests(repo, repo.path)
            if not baseline.green:
                raise RuntimeError(
                    f"{repo.name} is not green on an untouched checkout ({baseline.summary}), "
                    f"so a mutation in it has no oracle. Fix the environment or drop the "
                    f"repository from the manifest."
                )
            tasks.extend(_build_repo(repo, baseline, per_repo, rng, scratch))
    finally:
        shutil.rmtree(scratch, ignore_errors=True)
    return tasks


def _build_repo(
    repo: Repo, baseline: SuiteReport, per_repo: int, rng: random.Random, scratch: Path
) -> list[Task]:
    pool: list[tuple[Path, Mutation, str]] = []
    for path in source_files(repo):
        source = path.read_text(encoding="utf-8")
        for mutation, mutated in candidates(path, source):
            pool.append((path, mutation, mutated))
    rng.shuffle(pool)

    # Spread the four operators rather than taking whatever the shuffle put
    # first: one repo's source can offer thousands of comparisons and no guards.
    by_operator: dict[str, list[tuple[Path, Mutation, str]]] = {}
    for entry in pool:
        by_operator.setdefault(entry[1].operator, []).append(entry)

    accepted: list[Task] = []
    index = 0
    rounds = 0
    while len(accepted) < per_repo and rounds < 200:
        rounds += 1
        available = [k for k, v in by_operator.items() if v]
        if not available:
            break
        operator = available[rounds % len(available)]
        path, mutation, mutated = by_operator[operator].pop()
        relative = str(path.relative_to(repo.path))
        index += 1
        attempt = evaluate(
            repo, mutation, mutated, relative, index, baseline, scratch
        )
        if attempt.accepted and attempt.task is not None:
            accepted.append(attempt.task)
    return accepted


def save(tasks: list[Task], path: Path = MANIFEST) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps([t.to_json() for t in tasks], indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    return path


def load(path: Path = MANIFEST) -> list[Task]:
    if not path.exists():
        raise FileNotFoundError(
            f"no task manifest at {path}. Run `python -m bench build-tasks` to inject bugs "
            f"into the pinned repositories first."
        )
    return [Task.from_json(entry) for entry in json.loads(path.read_text(encoding="utf-8"))]

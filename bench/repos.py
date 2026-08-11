"""The repositories bugs are injected into, and the working copies runs get."""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path

# Wall-clock fragments in pytest's summary. Re-running the suite on a fork
# changes them even when the pass/fail set is identical, which breaks the
# free intervene prefix at the first `run_tests`. Strip at the tool boundary
# so recordings and live re-executions agree on the observation text.
_PYTEST_DURATION = re.compile(
    r"\s+in\s+(?:\d+:)?\d+(?:\.\d+)?s(?:\s*\([^)]*\))?"
)

# Absolute, because tests run with the working copy as the cwd and a relative
# path would resolve against that instead.
CORPUS_ROOT = Path(os.environ.get("BENCH_CORPUS", "corpus")).resolve()
CLONE_ROOT = CORPUS_ROOT / "repos"
VENV_ROOT = CORPUS_ROOT / "venv"


def corpus_metadata_path(path: Path) -> str:
    resolved = path.resolve()
    try:
        relative = resolved.relative_to(CORPUS_ROOT)
    except ValueError:
        return path.name
    return (Path("corpus") / relative).as_posix()

# Injected into the shared environment rather than per repo. These are test-only
# imports a few suites reach for; the libraries under test are pure stdlib.
TEST_REQUIREMENTS = (
    "pytest>=8.4",
    "hypothesis",
    "pytest-mock",
    "pytest-benchmark",
    "typing_extensions",
    "text-unidecode",
    "Unidecode",
    "whatever",
    "numpy",
)


@dataclass(frozen=True)
class Repo:
    """One pinned upstream repository.

    `source_dirs` are the directories bugs may be injected into — the library
    itself, never its tests. Mutating a test would produce a task whose oracle is
    the thing that was broken.

    `excluded_tests` are tests that fail on an untouched checkout in this
    environment, so they say nothing about an injected bug. Recording them here
    rather than tolerating them keeps "the clean suite is green" a real check.
    """

    name: str
    url: str
    commit: str
    source_dirs: tuple[str, ...]
    import_paths: tuple[str, ...] = (".",)
    excluded_tests: tuple[str, ...] = ()
    # Suites vary from 0.1s to 4s and the agent runs them repeatedly, so a repo
    # whose suite is slow costs the whole batch.
    baseline_seconds: float = 1.0

    @property
    def path(self) -> Path:
        return CLONE_ROOT / self.name


REPOS: tuple[Repo, ...] = (
    Repo(
        name="bidict",
        url="https://github.com/jab/bidict.git",
        commit="0ce44cc8570dfc48f5827738470f1adf25ced562",
        source_dirs=("bidict",),
        baseline_seconds=1.7,
    ),
    Repo(
        name="boltons",
        url="https://github.com/mahmoud/boltons.git",
        commit="e66cade323f5c11cebb4bd0f099e634b245adccd",
        source_dirs=("boltons",),
        baseline_seconds=2.4,
    ),
    Repo(
        name="cachetools",
        url="https://github.com/tkem/cachetools.git",
        commit="13bb86a55e36e501cf0b3e4c35db516ed9409fd7",
        source_dirs=("src/cachetools",),
        import_paths=("src", "."),
        baseline_seconds=4.2,
    ),
    Repo(
        name="funcy",
        url="https://github.com/Suor/funcy.git",
        commit="9eb04473e31b6b60bd459e4dda24f6b1db5a3773",
        source_dirs=("funcy",),
        baseline_seconds=0.2,
    ),
    Repo(
        name="inflection",
        url="https://github.com/jpvanhal/inflection.git",
        commit="88eefaacf7d0caaa701af7c8ab2d0ab3f17086f1",
        source_dirs=("inflection",),
        baseline_seconds=0.2,
    ),
    Repo(
        name="iniconfig",
        url="https://github.com/pytest-dev/iniconfig.git",
        commit="00e7d87c7353b1ffecc4cd55f19acfffedd5233e",
        source_dirs=("src/iniconfig",),
        import_paths=("src", "."),
        baseline_seconds=0.1,
    ),
    Repo(
        name="natsort",
        url="https://github.com/SethMMorton/natsort.git",
        commit="b543bdce8771b6e7a7dae0c6745ddf7e80299797",
        source_dirs=("natsort",),
        baseline_seconds=1.7,
    ),
    Repo(
        name="parse",
        url="https://github.com/r1chardj0n3s/parse.git",
        commit="fc3875c33f4ae5f5703c3f17e8226cca0dea6eeb",
        source_dirs=("parse",),
        baseline_seconds=0.1,
    ),
    Repo(
        name="pathspec",
        url="https://github.com/cpburnz/python-pathspec.git",
        commit="6568072c2703c72796cd02467feb924540157c92",
        source_dirs=("pathspec",),
        baseline_seconds=0.2,
    ),
    Repo(
        name="schema",
        url="https://github.com/keleshev/schema.git",
        commit="310a1239b62f500284ce3bd91b7dabf70467f23e",
        source_dirs=("schema",),
        baseline_seconds=0.1,
    ),
    Repo(
        name="semver",
        url="https://github.com/python-semver/python-semver.git",
        commit="4e09ef0e4c94314731f960d4ce763a2da2e096f1",
        source_dirs=("src/semver",),
        import_paths=("src", "."),
        baseline_seconds=0.2,
    ),
    Repo(
        name="slugify",
        url="https://github.com/un33k/python-slugify.git",
        commit="7b6d5d96c1995e6dccb39a19a13ba78d7d0a3ee4",
        source_dirs=("slugify",),
        baseline_seconds=0.1,
    ),
    Repo(
        name="sortedcontainers",
        url="https://github.com/grantjenks/python-sortedcontainers.git",
        commit="3ac358631f58c1347f1d6d2d92784117db0f38ed",
        source_dirs=("src/sortedcontainers",),
        import_paths=("src", "."),
        baseline_seconds=2.1,
    ),
    Repo(
        name="tabulate",
        url="https://github.com/astanin/python-tabulate.git",
        commit="268615a5c27dc40e5c22454c07b44d5c50410da0",
        source_dirs=("tabulate",),
        baseline_seconds=0.7,
    ),
    Repo(
        name="toolz",
        url="https://github.com/pytoolz/toolz.git",
        commit="568c2b8393973cd172a466546c9d95779c452438",
        source_dirs=("toolz",),
        # Reads the installed distribution's metadata, which a source checkout on
        # the path does not have. It tests packaging, not behaviour.
        excluded_tests=("toolz/tests/test_package.py::test_has_version",),
        baseline_seconds=0.2,
    ),
    Repo(
        name="voluptuous",
        url="https://github.com/alecthomas/voluptuous.git",
        commit="44593ce7c330faf9418252c4b5448f4736144a7f",
        source_dirs=("voluptuous",),
        baseline_seconds=0.2,
    ),
)

BY_NAME = {r.name: r for r in REPOS}


def python() -> Path:
    exe = VENV_ROOT / "bin" / "python"
    if not exe.exists():
        raise FileNotFoundError(
            f"no corpus environment at {VENV_ROOT}. Run `python -m bench setup` to clone "
            f"the pinned repositories and build the environment their suites run in."
        )
    return exe


def setup(force: bool = False) -> None:
    CLONE_ROOT.mkdir(parents=True, exist_ok=True)
    if force and VENV_ROOT.exists():
        shutil.rmtree(VENV_ROOT)
    if not VENV_ROOT.exists():
        subprocess.run(
            ["uv", "venv", "--python", sys.executable, str(VENV_ROOT)],
            check=True,
            capture_output=True,
        )
        subprocess.run(
            ["uv", "pip", "install", "--python", str(python()), *TEST_REQUIREMENTS],
            check=True,
            capture_output=True,
        )
    for repo in REPOS:
        clone(repo, force=force)


def clone(repo: Repo, force: bool = False) -> Path:
    if force and repo.path.exists():
        shutil.rmtree(repo.path)
    if repo.path.exists():
        return repo.path
    repo.path.parent.mkdir(parents=True, exist_ok=True)
    # A pinned commit is the whole point: an upstream that moves under the corpus
    # would silently change what every recorded run was working on.
    subprocess.run(
        ["git", "clone", "--quiet", "--filter=blob:none", repo.url, str(repo.path)],
        check=True,
        capture_output=True,
    )
    subprocess.run(
        ["git", "-C", str(repo.path), "checkout", "--quiet", repo.commit],
        check=True,
        capture_output=True,
    )
    return repo.path


def working_copy(repo: Repo, destination: Path) -> Path:
    """A private copy of the repo for one run to edit and break freely."""
    shutil.copytree(
        repo.path,
        destination,
        ignore=shutil.ignore_patterns(".git", "__pycache__", "*.pyc", ".pytest_cache"),
    )
    return destination


@dataclass
class SuiteReport:
    passed: int = 0
    failed: int = 0
    errors: int = 0
    returncode: int = 0
    timed_out: bool = False
    summary: str = ""
    failing_tests: tuple[str, ...] = field(default_factory=tuple)
    output: str = ""

    @property
    def green(self) -> bool:
        return not self.timed_out and self.returncode == 0 and self.failed == self.errors == 0


def run_tests(repo: Repo, root: Path, timeout: float = 180.0) -> SuiteReport:
    """Run a working copy's suite.

    `addopts` is cleared because several of these projects inject coverage or
    xdist flags that are not installed here and are noise for this purpose.
    """
    env = dict(os.environ)
    env["PYTHONPATH"] = os.pathsep.join(str(root / p) for p in repo.import_paths)
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    command = [
        str(python()),
        "-m",
        "pytest",
        "-q",
        "--no-header",
        "--tb=line",
        # Force the short summary: which tests failed is what both the injector
        # and the agent need, and it is not printed under every tb setting.
        "-rf",
        "-o",
        "addopts=",
        "-p",
        "no:cacheprovider",
        "-p",
        "no:randomly",
    ]
    for node in repo.excluded_tests:
        command += ["--deselect", node]

    try:
        completed = subprocess.run(
            command,
            cwd=root,
            env=env,
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
    except subprocess.TimeoutExpired:
        return SuiteReport(
            timed_out=True,
            returncode=-1,
            summary=f"the test suite did not finish within {timeout:.0f}s",
        )

    output = completed.stdout + completed.stderr
    return _parse(output, completed.returncode)


def stabilize_pytest_output(text: str) -> str:
    """Drop pytest wall-clock from an observation string."""
    return _PYTEST_DURATION.sub("", text)


def _parse(output: str, returncode: int) -> SuiteReport:
    stable = stabilize_pytest_output(output)
    report = SuiteReport(returncode=returncode, output=stable)
    failing = [
        line.split(" ")[1]
        for line in stable.splitlines()
        if line.startswith(("FAILED ", "ERROR "))
    ]
    report.failing_tests = tuple(dict.fromkeys(failing))
    for line in reversed(stable.splitlines()):
        if " passed" in line or " failed" in line or " error" in line:
            report.summary = line.strip().strip("=").strip()
            break
    report.passed = _count(report.summary, "passed")
    report.failed = _count(report.summary, "failed")
    report.errors = _count(report.summary, "error") + _count(report.summary, "errors")
    if not report.summary:
        report.summary = f"pytest exited {returncode} without a summary line"
    return report


def _count(summary: str, word: str) -> int:
    parts = summary.replace(",", " ").split()
    for index, part in enumerate(parts):
        if part == word and index > 0 and parts[index - 1].isdigit():
            return int(parts[index - 1])
    return 0


def sources(repo: Repo) -> list[Path]:
    """The files a bug may be injected into.

    Checked as part of verification rather than left to the injector: a
    `source_dirs` entry that names a path the project no longer has yields no
    mutations at all, and a repository that silently contributes nothing looks
    exactly like one whose mutations were all rejected on merit.
    """
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
        else:
            raise FileNotFoundError(
                f"{repo.name} lists {entry!r} as source, but {target} does not exist. The "
                f"project's layout has changed since the manifest was written; correct "
                f"source_dirs for {repo.name}."
            )
    return found


def main(argv: list[str]) -> int:
    setup(force="--force" in argv)
    failures = 0
    for repo in REPOS:
        try:
            files = sources(repo)
        except FileNotFoundError as exc:
            failures += 1
            print(f"{repo.name:<18} {'NO SOURCE':<9} {exc}")
            continue
        report = run_tests(repo, repo.path)
        state = "ok" if report.green else "BROKEN"
        if not report.green:
            failures += 1
        print(f"{repo.name:<18} {state:<9} {len(files):>3} files  {report.summary}")
    if failures:
        print(
            f"\n{failures} repositories do not pass on an untouched checkout. A task built "
            f"on one of those has no oracle; fix the environment or drop the repository.",
            file=sys.stderr,
        )
    return 1 if failures else 0

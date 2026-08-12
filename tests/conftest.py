from __future__ import annotations

import ast
import sys
from pathlib import Path

import pytest

# The suite exercises the real enforcement rather than waiving it, and the flag
# is fixed at interpreter startup, so it cannot be set from here.
if sys.flags.hash_randomization:
    raise pytest.UsageError(
        "run the suite as `PYTHONHASHSEED=0 uv run pytest`. Replay refuses to run under "
        "hash randomization, and these tests replay."
    )

# The distribution ships the library, its contracts, and this suite; `bench/`
# and `evidence/` are checkout-only harnesses. The tests that drive them import
# them at module scope, which runs before a skip mark could take effect, so an
# unpacked sdist has to drop those modules during collection instead. Only
# absence is waived: in a checkout both directories exist, nothing here runs,
# and a harness that is present but broken still fails the suite.
_ROOT = Path(__file__).parents[1]
_absent = {name for name in ("bench", "evidence") if not (_ROOT / name).is_dir()}


def _imports_absent_harness(path: Path) -> bool:
    # Parsed rather than pattern-matched: a harness named in a docstring or a
    # comment must not silently drop a module the distribution can still run.
    try:
        parsed = ast.parse(path.read_text(encoding="utf-8"))
    except SyntaxError:
        return False  # leave it collected so pytest reports it against the module
    for node in ast.walk(parsed):
        if isinstance(node, ast.Import):
            roots = {alias.name.split(".", 1)[0] for alias in node.names}
        elif isinstance(node, ast.ImportFrom) and not node.level and node.module:
            roots = {node.module.split(".", 1)[0]}
        else:
            continue
        if roots & _absent:
            return True
    return False


if _absent:
    collect_ignore = [
        path.name
        for path in sorted(Path(__file__).parent.glob("test_*.py"))
        if _imports_absent_harness(path)
    ]

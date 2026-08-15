"""Write-once development / held-out splits for every labelled set.

`partition.py` did this for the OpenHands sheet alone. RootSE and nebius were
scored whole, which is why the improvement sweep of 2026-08-15 ended up
selecting on the same pairs it reported: with no reserved half there was nothing
to check a choice against, and "already spent once" was allowed to justify
spending them thirty more times.

Splitting them now does not undo that. A half that has already been read is not
held out, and no seed makes it so. What this buys is future work: from here a
candidate can be developed on `dev` and checked on `test`, and the check means
something. The genuinely untouched measurement is the fresh nebius slice, which
`bench.nebius` exports from pairs nothing has rendered or scored.

Splits are stratified by failing-trace length so neither half collects the long
trajectories, and refuse to be rewritten. Re-splitting after seeing a result
turns a prediction into a fit.
"""

from __future__ import annotations

import json
import random
from dataclasses import dataclass
from pathlib import Path

from .repos import CORPUS_ROOT

SPLIT_ROOT = CORPUS_ROOT / "alignment" / "splits"
SEED = 20260815
STRATA = 4


@dataclass(frozen=True)
class Split:
    dev: tuple[str, ...]
    test: tuple[str, ...]

    def side(self, *, final: bool) -> tuple[str, ...]:
        return self.test if final else self.dev


def stratified(items: dict[str, int], seed: int = SEED) -> Split:
    """Halve `id -> length` into two length-balanced sides, deterministically."""
    ordered = sorted(items, key=lambda k: (items[k], k))
    rng = random.Random(seed)
    dev: list[str] = []
    test: list[str] = []
    size = max(1, len(ordered) // STRATA)
    for start in range(0, len(ordered), size):
        block = ordered[start : start + size]
        rng.shuffle(block)
        for i, name in enumerate(block):
            (dev if i % 2 == 0 else test).append(name)
    return Split(tuple(sorted(dev)), tuple(sorted(test)))


def write(name: str, items: dict[str, int], *, seed: int = SEED) -> Path:
    """Persist a split once. A second call with different content is refused."""
    SPLIT_ROOT.mkdir(parents=True, exist_ok=True)
    path = SPLIT_ROOT / f"{name}.json"
    split = stratified(items, seed)
    payload = {"seed": seed, "strata": STRATA, "dev": list(split.dev), "test": list(split.test)}
    if path.exists():
        existing = json.loads(path.read_text(encoding="utf-8"))
        if existing != payload:
            raise RuntimeError(
                f"{path} already exists and differs. A split is written once; "
                f"re-splitting after seeing results turns a prediction into a fit."
            )
        return path
    path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return path


def read(name: str) -> Split:
    path = SPLIT_ROOT / f"{name}.json"
    if not path.exists():
        raise FileNotFoundError(f"no split at {path}; write it before scoring a half")
    payload = json.loads(path.read_text(encoding="utf-8"))
    return Split(tuple(payload["dev"]), tuple(payload["test"]))

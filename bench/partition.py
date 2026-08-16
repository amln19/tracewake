"""Development / held-out partition for the labeled external transfer set.

The 80 OpenHands packets have already shaped published work: their aggregate
score is in the README and their by-length breakdown is in the scout file. That
makes the whole sheet unusable as an untouched final test for a *new* algorithm.
Splitting it now, before any per-pair label is read for design, restores one
half as a final test — the aggregates already published tell you almost nothing
about which individual packets land where.

`load_split` refuses to hand back the held-out labels unless the caller says
`final=True`, which is what keeps an ordinary development loop from reading
them by accident.
"""

from __future__ import annotations

import json
import random
from dataclasses import dataclass
from pathlib import Path

from .external import OPENHANDS_LABEL_ROOT
from .repos import CORPUS_ROOT

PARTITION_PATH = CORPUS_ROOT / "alignment" / "partition.json"
PARTITION_SEED = 20260812
# Four length strata, so neither half ends up carrying the long trajectories.
STRATA = 4


@dataclass(frozen=True)
class Split:
    dev: tuple[str, ...]
    final: tuple[str, ...]


def build_partition(
    key_path: Path = OPENHANDS_LABEL_ROOT / "key.jsonl",
    seed: int = PARTITION_SEED,
) -> Split:
    """Half dev, half final, stratified by failure-side length.

    Assignment depends only on packet id and failure length — never on a label
    or on any method's prediction.
    """
    rows = [
        json.loads(line)
        for line in key_path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    rows.sort(key=lambda r: (r["bad_steps"], r["packet_id"]))
    size = max(1, len(rows) // STRATA)
    dev: list[str] = []
    final: list[str] = []
    rng = random.Random(seed)
    for start in range(0, len(rows), size):
        stratum = [r["packet_id"] for r in rows[start : start + size]]
        rng.shuffle(stratum)
        for i, packet_id in enumerate(stratum):
            (dev if i % 2 == 0 else final).append(packet_id)
    return Split(dev=tuple(sorted(dev)), final=tuple(sorted(final)))


def write_partition(path: Path = PARTITION_PATH, seed: int = PARTITION_SEED) -> Path:
    if path.exists():
        raise FileExistsError(
            f"{path} already exists. The partition is frozen once written; "
            f"rewriting it would let a method be re-split until it wins."
        )
    split = build_partition(seed=seed)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {
                "seed": seed,
                "strata": STRATA,
                "source": "corpus/labels/openhands/key.jsonl",
                "dev": list(split.dev),
                "final": list(split.final),
            },
            indent=2,
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )
    return path


def read_partition(path: Path = PARTITION_PATH) -> Split:
    data = json.loads(path.read_text(encoding="utf-8"))
    return Split(dev=tuple(data["dev"]), final=tuple(data["final"]))


def load_split(
    *,
    final: bool = False,
    labels_path: Path = OPENHANDS_LABEL_ROOT / "labels.jsonl",
    partition_path: Path = PARTITION_PATH,
) -> dict[str, int]:
    """packet_id → label for one side of the partition.

    Development work asks for the default. Passing `final=True` spends the
    held-out set; do it once, for the method development already selected.
    """
    split = read_partition(partition_path)
    wanted = set(split.final if final else split.dev)
    out: dict[str, int] = {}
    for line in labels_path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        row = json.loads(line)
        if row["packet_id"] in wanted and row.get("label") is not None:
            out[row["packet_id"]] = int(row["label"])
    return out

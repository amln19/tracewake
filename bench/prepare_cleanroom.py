"""Create the inputs for an isolated, independent rebuild.

This deliberately writes only the task, scoring harness, submission template,
and anonymised development data. Generating a predictor from Tracewake's own
implementation would make the clean-room result circular.
"""

from __future__ import annotations

import argparse
import pathlib
import shutil

from .cleanroom import export, write_id_map

PROJECT_ROOT = pathlib.Path(__file__).resolve().parents[1]
DEFAULT_DESTINATION = PROJECT_ROOT.parent / "tracewake-cleanroom"
TEMPLATE_ROOT = pathlib.Path(__file__).with_name("cleanroom_template")
TEMPLATES = ("TASK.md", "score.py", "SUBMISSION.md")


def scaffold(destination: pathlib.Path) -> pathlib.Path:
    """Create a fresh clean-room directory without exposing implementation details."""
    destination = destination.resolve()
    if destination.exists() and not destination.is_dir():
        raise FileExistsError(f"clean-room destination is not a directory: {destination}")
    if destination.exists() and any(destination.iterdir()):
        raise FileExistsError(
            f"clean-room destination is not empty: {destination}\n"
            "Choose a fresh directory so existing work is never overwritten."
        )

    destination.mkdir(parents=True, exist_ok=True)
    for name in TEMPLATES:
        shutil.copyfile(TEMPLATE_ROOT / name, destination / name)

    mapping = export(destination / "data" / "train.jsonl")
    write_id_map(mapping)
    return destination


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "destination",
        nargs="?",
        type=pathlib.Path,
        default=DEFAULT_DESTINATION,
        help=f"fresh directory to create (default: {DEFAULT_DESTINATION})",
    )
    args = parser.parse_args(argv)
    destination = scaffold(args.destination)
    print(f"clean-room inputs written to {destination}")
    print("Give that directory to an independent author. Do not share this repository.")
    print("After predictor.py is submitted, score it from Tracewake with:")
    print(f"  uv run --group bench python -m bench.score_cleanroom --cleanroom {destination}")


if __name__ == "__main__":
    main()

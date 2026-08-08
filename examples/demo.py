"""Record two runs of `openai_agent.py` and show where they diverge.

Entirely offline: the agent's fake model backend makes no network call, so
this needs no API key. Run it with:

    python examples/demo.py
"""

from __future__ import annotations

import subprocess
import sys
import tempfile
from pathlib import Path

AGENT = Path(__file__).parent / "openai_agent.py"


def _tracewake(*args: str) -> subprocess.CompletedProcess[str]:
    completed = subprocess.run(
        [sys.executable, "-m", "tracewake", *args],
        capture_output=True,
        text=True,
        check=False,
    )
    print(completed.stdout, end="")
    if completed.returncode != 0:
        print(completed.stderr, end="", file=sys.stderr)
        raise SystemExit(completed.returncode)
    return completed


def main() -> None:
    with tempfile.TemporaryDirectory(prefix="tracewake-demo-") as store:
        print(f"recording into {store}\n")
        for scenario in ("good", "bad"):
            _tracewake(
                "record",
                "--store",
                store,
                "--name",
                scenario,
                "--",
                sys.executable,
                str(AGENT),
                "--scenario",
                scenario,
            )

        print("\nreplaying the good run (answers from the log, no new model calls):\n")
        _tracewake("replay", "good", "--store", store)

        print("\naligning the two runs:\n")
        _tracewake("diff", "good", "bad", "--store", store, "--lexical")


if __name__ == "__main__":
    main()

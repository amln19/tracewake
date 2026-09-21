"""Score Tracewake's shipped localizer on the held-out evaluation set.

Unlike ``score_cleanroom``, this imports the implementation users actually
install. It is the ordinary reproducibility path; the clean-room score answers
the separate question of whether an isolated rebuild found the same rule.
"""

from __future__ import annotations

from tracewake.diverge import first_nonscratch_write

from .score_cleanroom import _load_test, _row


def main() -> None:
    pools = _load_test()

    print("SHIPPED TRACEWAKE LOCALIZER, on its held-out evaluation set\n")
    print(f"  {'pool':<14}{'exact':<24}{'+/-2':<24}{'+/-5'}")
    for name in ("nebius", "openhands", "rootse"):
        print(_row(name, pools[name], first_nonscratch_write))
    every = pools["nebius"] + pools["openhands"] + pools["rootse"]
    print(_row("ALL", every, first_nonscratch_write))


if __name__ == "__main__":
    main()

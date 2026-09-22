from __future__ import annotations

import pytest

from bench import score_shipped
from tracewake.align import Step


def test_chance_rate_bounds() -> None:
    # trace length 5, label 3, +/- 2 covers entire trace [1, 5] -> 1.0
    assert score_shipped.chance(3, 5, 2) == pytest.approx(1.0)
    # trace length 10, label 5, +/- 2 covers [3, 7] -> 5/10 = 0.5
    assert score_shipped.chance(5, 10, 2) == pytest.approx(0.5)
    # exact match on length 10 -> 1/10 = 0.1
    assert score_shipped.chance(5, 10, 0) == pytest.approx(0.1)


def test_shipped_score_imports_tracewakes_localizer(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    steps = [Step(name="read", args={}, target="src/a.py")]
    pools = {name: [("case", 1, steps)] for name in ("nebius", "openhands", "rootse")}
    monkeypatch.setattr(score_shipped, "_load_test", lambda: pools)

    score_shipped.main()

    assert "SHIPPED TRACEWAKE LOCALIZER" in capsys.readouterr().out

"""The `examples/` demo is the portability story: an agent using the generic
tool-calling shape, not this repo's own, still gets a real divergence report.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

DEMO = Path(__file__).parent.parent / "examples" / "demo.py"

# The examples are not packaged, so this check only applies to a checkout.
pytestmark = pytest.mark.skipif(
    not DEMO.is_file(), reason="the examples are not part of the distribution"
)


def test_the_demo_records_replays_and_reports_a_real_divergence() -> None:
    result = subprocess.run(
        [sys.executable, str(DEMO)], capture_output=True, text=True, check=False
    )
    assert result.returncode == 0, result.stderr
    assert "3 matched, 0 degraded, 0 missed" in result.stdout
    assert "divergence at BAD" in result.stdout

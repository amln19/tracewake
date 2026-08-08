from __future__ import annotations

import sys
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from typing import Any

import pytest

from .config import RECORD_MODES
from .session import Session

CassetteFactory = Callable[..., Any]


def pytest_addoption(parser: pytest.Parser) -> None:
    group = parser.getgroup("tracewake", "recorded agent runs as regression tests")
    group.addoption(
        "--tracewake-store",
        default=".tracewake",
        help="Store directory holding the cassettes (default: .tracewake).",
    )
    group.addoption(
        "--tracewake-record",
        default="none",
        choices=RECORD_MODES,
        help=(
            "Record mode for tracewake fixtures. The default 'none' replays only, so a test "
            "run costs nothing and touches no network; use 'once' or 'all' to record."
        ),
    )


def pytest_configure(config: pytest.Config) -> None:
    config.addinivalue_line(
        "markers", "tracewake(name): cassette name for the tracewake_cassette fixture"
    )
    if sys.flags.hash_randomization and config.getoption("--tracewake-record") == "none":
        config.issue_config_time_warning(
            pytest.PytestConfigWarning(
                "tracewake replay needs PYTHONHASHSEED=0 and this interpreter has hash "
                "randomization on, so any test that replays a cassette will fail. Re-run "
                "as `PYTHONHASHSEED=0 pytest`."
            ),
            stacklevel=2,
        )


@pytest.fixture
def tracewake_cassette(request: pytest.FixtureRequest) -> CassetteFactory:
    """Open a cassette for this test, replay-only by default.

    The cassette name defaults to the test's node id, so a recorded run becomes a
    regression test for the code that produced it without naming anything twice.
    """
    import tracewake

    marker = request.node.get_closest_marker("tracewake")
    default_name = marker.args[0] if marker and marker.args else request.node.name
    default_store = request.config.getoption("--tracewake-store")
    default_mode = request.config.getoption("--tracewake-record")

    @contextmanager
    def open_cassette(
        name: str | None = None,
        *,
        store: str | None = None,
        mode: str | None = None,
        **overrides: Any,
    ) -> Iterator[Session]:
        with tracewake.session(
            name or default_name,
            store=store or default_store,
            mode=mode or default_mode,  # type: ignore[arg-type]
            **overrides,
        ) as active:
            yield active
            report = active.report
            if report.missed or report.degraded or report.unconsumed:
                pytest.fail(
                    f"cassette {active.name!r}: {report.summary()}. Replay must consume "
                    f"every recorded call by messages_hash; degraded or unused calls mean "
                    f"the agent under test diverged from the recording."
                )

    return open_cassette

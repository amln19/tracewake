from __future__ import annotations

import logging
import os
import random
import socket
import time
import uuid
from pathlib import Path

import pytest

import locus
from locus import Store


def _sources(store: Path, run_id: str) -> list[tuple[str, str | None]]:
    db = Store(store)
    out = [
        (e.event.source, e.event.key)
        for e in db.events(run_id)
        if e.event.type == "environment"
    ]
    db.close()
    return out


def _observe() -> list[str]:
    return [
        f"time={time.time()!r}",
        f"monotonic={time.monotonic()!r}",
        f"perf={time.perf_counter()!r}",
        f"ns={time.time_ns()}",
        f"random={random.random()!r}",
        f"randint={random.randint(0, 10**9)}",
        f"choice={random.choice('abcdefghij')}",
        f"uuid4={uuid.uuid4()}",
        f"env={os.environ['LOCUS_PATCH_TEST']}",
        f"getenv={os.getenv('LOCUS_PATCH_TEST')}",
    ]


@pytest.fixture(autouse=True)
def tagged(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("LOCUS_PATCH_TEST", "recorded-value")


def test_every_patched_input_replays_exactly(tmp_path: Path) -> None:
    with locus.record("patched", store=tmp_path) as rec:
        recorded = _observe()
        rec.outcome(status="ok")
        run_id = rec.run_id

    with locus.replay(run_id, store=tmp_path) as rep:
        replayed = _observe()
        rep.outcome(status="ok")

    assert replayed == recorded
    assert "recorded-value" in recorded[-1]


def test_each_kind_of_input_is_recorded_under_its_own_source(tmp_path: Path) -> None:
    with locus.record("patched", store=tmp_path) as rec:
        _observe()
        rec.outcome(status="ok")
        run_id = rec.run_id

    sources = _sources(tmp_path, run_id)
    assert ("clock", None) in sources
    assert ("clock", "ns") in sources
    assert ("monotonic", None) in sources
    assert ("perf_counter", None) in sources
    assert ("random", None) in sources
    assert ("uuid", "uuid4") in sources
    assert ("env", "LOCUS_PATCH_TEST") in sources
    assert any(
        key and key.startswith("getrandbits:") for source, key in sources if source == "random"
    )


def test_the_environment_the_agent_sees_is_the_recorded_one(tmp_path: Path) -> None:
    with locus.record("patched", store=tmp_path) as rec:
        assert os.environ["LOCUS_PATCH_TEST"] == "recorded-value"
        rec.outcome(status="ok")
        run_id = rec.run_id

    os.environ["LOCUS_PATCH_TEST"] = "changed-since"
    with locus.replay(run_id, store=tmp_path) as rep:
        assert os.environ["LOCUS_PATCH_TEST"] == "recorded-value"
        rep.outcome(status="ok")


@pytest.mark.parametrize(
    "read",
    [
        pytest.param(lambda: os.environ["LOCUS_PATCH_TEST"], id="subscript"),
        pytest.param(lambda: os.getenv("LOCUS_PATCH_TEST"), id="os.getenv"),
        pytest.param(lambda: os.environ.get("LOCUS_PATCH_TEST"), id="mapping.get"),
    ],
)
def test_every_way_of_reading_a_variable_replays_the_recorded_value(
    tmp_path: Path, read
) -> None:
    """Reading env through a forwarder must come from the log, not the machine.

    `os.getenv` and `Mapping.get` reach the patched `__getitem__` through a
    stdlib frame, and the walk that skips such a frame matches it by filename.
    Several stdlib modules are frozen into the interpreter, and a frozen
    module's frames report `<frozen os>` while its `__file__` still points at
    the source — so a forwarder set built from `__file__` matches nothing and
    those reads silently bypass the recorder.

    The value has to change between record and replay to catch it: if the
    machine still holds what was recorded, a read that never consulted the log
    returns the right answer anyway and the bug hides.
    """
    with locus.record("patched", store=tmp_path) as rec:
        recorded = read()
        rec.outcome(status="ok")
        run_id = rec.run_id
    assert recorded == "recorded-value"

    os.environ["LOCUS_PATCH_TEST"] = "changed-since"
    with locus.replay(run_id, store=tmp_path) as rep:
        assert read() == "recorded-value"
        rep.outcome(status="ok")


def test_a_variable_the_run_never_read_is_divergence(tmp_path: Path) -> None:
    with locus.record("patched", store=tmp_path) as rec:
        os.environ["LOCUS_PATCH_TEST"]
        rec.outcome(status="ok")
        run_id = rec.run_id

    with pytest.raises(locus.ReplayMiss, match="no unconsumed 'env' value"):
        with locus.replay(run_id, store=tmp_path):
            os.environ["PATH"]


def test_re_reading_a_variable_is_not_divergence(tmp_path: Path) -> None:
    """A variable is looked up by name, not consumed from a sequence, and how
    many times library code reads one varies between runs."""
    with locus.record("patched", store=tmp_path) as rec:
        os.environ["LOCUS_PATCH_TEST"]
        rec.outcome(status="ok")
        run_id = rec.run_id

    with locus.replay(run_id, store=tmp_path) as rep:
        for _ in range(5):
            assert os.environ["LOCUS_PATCH_TEST"] == "recorded-value"
        rep.outcome(status="ok")


def test_a_missing_variable_is_missing_on_replay_too(tmp_path: Path) -> None:
    with locus.record("patched", store=tmp_path) as rec:
        with pytest.raises(KeyError):
            os.environ["LOCUS_DEFINITELY_UNSET"]
        rec.outcome(status="ok")
        run_id = rec.run_id

    os.environ["LOCUS_DEFINITELY_UNSET"] = "appeared later"
    try:
        with locus.replay(run_id, store=tmp_path) as rep:
            with pytest.raises(KeyError):
                os.environ["LOCUS_DEFINITELY_UNSET"]
            rep.outcome(status="ok")
    finally:
        del os.environ["LOCUS_DEFINITELY_UNSET"]


def test_the_runtimes_own_clock_reads_are_not_recorded(tmp_path: Path) -> None:
    """How often the standard library reads the clock varies run to run, so
    recording those would make replay fail on a count nothing observed."""
    with locus.record("quiet", store=tmp_path) as rec:
        logger = logging.getLogger("locus.test")
        for _ in range(20):
            logger.handle(logger.makeRecord("locus.test", logging.INFO, "f", 1, "m", (), None))
        rec.outcome(status="ok")
        run_id = rec.run_id

    assert _sources(tmp_path, run_id) == []


def test_a_seeded_generator_of_your_own_is_left_alone(tmp_path: Path) -> None:
    """Only the module-level generator is nondeterministic; an explicitly seeded
    Random is already reproducible and recording it would be noise."""
    with locus.record("seeded", store=tmp_path) as rec:
        own = random.Random(1234)
        expected = [own.random() for _ in range(5)]
        rec.outcome(status="ok")
        run_id = rec.run_id

    assert _sources(tmp_path, run_id) == []
    assert random.Random(1234).random() == expected[0]


def test_patches_are_removed_when_the_session_ends(tmp_path: Path) -> None:
    before = (time.time, random.random, uuid.uuid4, type(os.environ).__getitem__)
    with locus.record("patched", store=tmp_path) as rec:
        assert time.time is not before[0]
        rec.outcome(status="ok")
    assert (time.time, random.random, uuid.uuid4, type(os.environ).__getitem__) == before
    assert socket.socket.connect is not None


def test_patching_can_be_turned_off(tmp_path: Path) -> None:
    with locus.record("plain", store=tmp_path, patch_environment=False) as rec:
        assert time.time is not None
        time.time()
        rec.outcome(status="ok")
        run_id = rec.run_id
    assert _sources(tmp_path, run_id) == []


def test_the_hash_seed_check_reports_the_command_to_run(tmp_path: Path) -> None:
    with locus.record("seed", store=tmp_path) as rec:
        rec.outcome(status="ok")
        run_id = rec.run_id

    from locus import patches

    original = patches.sys.flags
    try:
        patches.sys.flags = type("Flags", (), {"hash_randomization": 1})()
        with pytest.raises(locus.HashSeedError, match="PYTHONHASHSEED=0"):
            with locus.replay(run_id, store=tmp_path):
                pass
    finally:
        patches.sys.flags = original


def test_the_hash_seed_check_can_be_waived(tmp_path: Path) -> None:
    with locus.record("seed", store=tmp_path) as rec:
        rec.outcome(status="ok")
        run_id = rec.run_id

    from locus import patches

    original = patches.sys.flags
    try:
        patches.sys.flags = type("Flags", (), {"hash_randomization": 1})()
        with locus.replay(run_id, store=tmp_path, require_hash_seed=False) as rep:
            rep.outcome(status="ok")
    finally:
        patches.sys.flags = original

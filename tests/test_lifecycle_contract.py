from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path

import pytest


CONTRACT = json.loads(
    (Path(__file__).parents[1] / "contracts" / "lifecycle-v1.json").read_text(
        encoding="utf-8"
    )
)
TERMINAL = frozenset(CONTRACT["terminal_states"])


@dataclass
class Model:
    state: str = "queued"
    attempt: int = 0
    current: int | None = None
    cancel_requested: bool = False
    result: str | None = None
    idempotency: dict[str, tuple[str, str]] = field(default_factory=dict)

    def claim(self) -> int | None:
        if self.state != "queued" or self.cancel_requested:
            return None
        self.attempt += 1
        self.current = self.attempt
        self.state = "running"
        return self.current

    def request_cancel(self) -> bool:
        if self.state in TERMINAL:
            return False
        self.cancel_requested = True
        return True

    def resolve_cancel(self) -> bool:
        if self.state in TERMINAL or not self.cancel_requested:
            return False
        self.state = "cancelled"
        self.current = None
        return True

    def complete(self, attempt: int, artifact: str) -> bool:
        if self.state != "running" or attempt != self.current:
            return False
        self.state = "succeeded"
        self.result = artifact
        self.current = None
        return True

    def expire(self, attempt: int) -> bool:
        if self.state != "running" or attempt != self.current:
            return False
        self.current = None
        self.state = "retry_wait" if self.attempt < 3 else "failed"
        return True

    def release_retry(self) -> bool:
        if self.state != "retry_wait" or self.cancel_requested:
            return False
        self.state = "queued"
        return True

    def idempotent_create(self, key: str, digest: str) -> str:
        if key in self.idempotency:
            stored_digest, job_id = self.idempotency[key]
            if stored_digest != digest:
                raise ValueError("idempotency conflict")
            return job_id
        job_id = f"job-{len(self.idempotency) + 1}"
        self.idempotency[key] = (digest, job_id)
        return job_id


def test_every_transition_has_explicit_preconditions_and_postconditions() -> None:
    names = set()
    states = set(CONTRACT["states"])
    for transition in CONTRACT["transitions"]:
        assert transition["name"] not in names
        names.add(transition["name"])
        assert transition["preconditions"]
        assert transition["postconditions"]
        assert all(state is None or state in states for state in transition["from"])
        assert transition["to"] in states | {"unchanged"}


def test_duplicate_delivery_preserves_one_current_attempt() -> None:
    model = Model()
    assert model.claim() == 1
    assert model.claim() is None
    assert (model.state, model.current, model.attempt) == ("running", 1, 1)


def test_late_attempt_cannot_commit_after_a_retry() -> None:
    model = Model()
    first = model.claim()
    assert first == 1 and model.expire(first)
    assert model.release_retry()
    second = model.claim()
    assert second == 2
    assert not model.complete(first, "stale-object")
    assert model.complete(second, "current-object")
    assert model.result == "current-object"


def test_cancellation_and_completion_have_one_terminal_winner() -> None:
    cancelled = Model()
    attempt = cancelled.claim()
    assert attempt == 1 and cancelled.request_cancel() and cancelled.resolve_cancel()
    assert not cancelled.complete(attempt, "too-late")
    assert cancelled.state == "cancelled"

    completed = Model()
    attempt = completed.claim()
    assert attempt == 1 and completed.request_cancel()
    assert completed.complete(attempt, "winner")
    assert not completed.resolve_cancel()
    assert completed.state == "succeeded"


def test_retry_exhaustion_is_terminal() -> None:
    model = Model()
    for expected in (1, 2, 3):
        attempt = model.claim()
        assert attempt == expected
        assert model.expire(attempt)
        if expected < 3:
            assert model.release_retry()
    assert model.state == "failed"
    assert model.claim() is None


def test_idempotency_replays_same_request_and_rejects_conflict() -> None:
    model = Model()
    first = model.idempotent_create("key", "a" * 64)
    assert model.idempotent_create("key", "a" * 64) == first
    with pytest.raises(ValueError, match="idempotency conflict"):
        model.idempotent_create("key", "b" * 64)

"""Self-contained HTML comparison report for two runs.

The page carries its own data. A `file://` page cannot read the SQLite store —
that needs either a server or a WASM SQLite build — so the comparison is
embedded as JSON and the file opens anywhere, including from a README link.

Everything long lives in one deduplicated block table. A growing prompt repeats
its whole history on every turn, so storing each turn's context inline would
make the payload quadratic in trajectory length; keyed by content it is stored
once. What is left over the budget is clipped, and the page says so and names
the store to read for the full text.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from .align import DiffResult, Step, StepTrace, extract_traces, target_agree, target_of
from .events import ModelCallEvent, OutcomeEvent, RunHeader, StoredEvent
from .store import BlobStore

PAYLOAD_BUDGET = 5_000_000

_TEMPLATE = Path(__file__).with_name("report.html")
_PAYLOAD_MARK = "__LOCUS_PAYLOAD__"
_TITLE_MARK = "__LOCUS_TITLE__"

# A preview, not the artifact. Tool results are already in the blob store and
# the page points at it.
_RESULT_PREVIEW = 20_000


class _Blocks:
    """Content-keyed text table shared by both runs."""

    def __init__(self) -> None:
        self.texts: list[str] = []
        self.meta: list[dict[str, Any]] = []
        self._index: dict[tuple[str, str, str], int] = {}

    def add(self, text: str, *, kind: str, provenance: str = "") -> int:
        key = (kind, provenance, text)
        found = self._index.get(key)
        if found is not None:
            return found
        index = len(self.texts)
        self._index[key] = index
        self.texts.append(text)
        self.meta.append({"kind": kind, "provenance": provenance, "chars": len(text)})
        return index


def _serialize(payload: dict[str, Any]) -> str:
    return json.dumps(payload, ensure_ascii=False, separators=(",", ":"))


def _fit_budget(payload: dict[str, Any], texts: list[str], budget: int) -> None:
    """Clip block texts to the longest common limit that fits inside `budget`.

    A per-block limit rather than a global one, so a handful of huge file reads
    cannot crowd out every short block. Small blocks survive whole.
    """

    def apply(limit: int | None) -> int:
        """Clip to `limit` and return the size the report would then have."""
        clipped = dropped = 0
        for block, text in zip(payload["blocks"], texts):
            if limit is None or len(text) <= limit:
                block["text"] = text
                block["clipped"] = False
            else:
                block["text"] = text[:limit]
                block["clipped"] = True
                clipped += 1
                dropped += len(text) - limit
        # Written before measuring, so what is measured is what gets embedded.
        payload["truncation"] = {"blocks": clipped, "chars": dropped, "limit": limit}
        return len(_serialize(payload).encode("utf-8"))

    if apply(None) <= budget:
        return

    low, high, best = 0, max((len(t) for t in texts), default=0), 0
    while low <= high:
        mid = (low + high) // 2
        if apply(mid) <= budget:
            best = mid
            low = mid + 1
        else:
            high = mid - 1
    if apply(best) > budget:
        raise ValueError(
            f"cannot fit the comparison into {budget} bytes even with every context "
            f"block dropped. Raise --max-bytes."
        )


def _run_summary(header: RunHeader, events: list[StoredEvent], steps: int) -> dict[str, Any]:
    calls = [e.event for e in events if isinstance(e.event, ModelCallEvent)]
    outcome = next((e.event for e in events if isinstance(e.event, OutcomeEvent)), None)
    return {
        "run_id": header.run_id,
        "name": header.name,
        "task_id": header.task_id,
        "status": header.status,
        "models": [m.model_id for m in header.models],
        "started_at": header.started_at,
        "finished_at": header.finished_at,
        "events": len(events),
        "model_calls": len(calls),
        "steps": steps,
        "usage": {
            "input_tokens": sum(c.response.usage.input_tokens for c in calls),
            "output_tokens": sum(c.response.usage.output_tokens for c in calls),
        },
        "outcome": None
        if outcome is None
        else {
            "status": outcome.status,
            "coverage": outcome.coverage,
            "resolve": outcome.resolve,
            "test_summary": outcome.test_summary,
            "error": outcome.error,
        },
    }


def _step_details(
    traces: list[StepTrace],
    events: list[StoredEvent],
    blocks: _Blocks,
    blobs: BlobStore | None,
) -> list[dict[str, Any]]:
    calls = {e.event.call_id: e.event for e in events if isinstance(e.event, ModelCallEvent)}
    # An intervention is addressed by model call, and a turn that produced no
    # action is not a step, so the two indices come apart on real runs.
    turns = {call_id: turn for turn, call_id in enumerate(calls)}
    seen_files: set[str] = set()
    out: list[dict[str, Any]] = []

    for index, trace in enumerate(traces):
        step = trace.step
        call = calls.get(trace.parent_call_id)
        context: list[int] = []
        usage = {"input_tokens": 0, "output_tokens": 0}
        if call is not None:
            for message in call.messages:
                context.append(
                    blocks.add(
                        message.content,
                        kind=message.role,
                        provenance=message.provenance or "untagged",
                    )
                )
            usage = {
                "input_tokens": call.response.usage.input_tokens,
                "output_tokens": call.response.usage.output_tokens,
            }

        tools: list[dict[str, Any]] = []
        for tool in trace.tools:
            preview: int | None = None
            if blobs is not None and blobs.has(tool.result.digest):
                raw = blobs.get(tool.result.digest)[:_RESULT_PREVIEW]
                preview = blocks.add(
                    raw.decode("utf-8", errors="replace"), kind="tool_result"
                )
            tools.append(
                {
                    "name": tool.name,
                    "target": target_of(dict(tool.args)),
                    "args": json.dumps(dict(tool.args), indent=2, sort_keys=True),
                    "status": tool.status,
                    "error": tool.error,
                    "result": preview,
                    "result_size": tool.result.size,
                    "duration_ms": tool.meta.duration_ms,
                }
            )

        paths = {path for path, _ in step.changed_files}
        out.append(
            {
                "i": index,
                "turn": turns.get(trace.parent_call_id),
                "name": step.name,
                "target": step.target,
                "targets": sorted(step.targets),
                "batch": bool(step.batch_names),
                "reasoning": blocks.add(step.reasoning, kind="reasoning")
                if step.reasoning
                else None,
                "context": context,
                "usage": usage,
                "tools": tools,
                "wrote": sorted(paths - seen_files),
                "changed": len(paths),
            }
        )
        seen_files = paths
    return out


def build_payload(
    good_header: RunHeader,
    good_events: list[StoredEvent],
    bad_header: RunHeader,
    bad_events: list[StoredEvent],
    result: DiffResult,
    *,
    blobs: BlobStore | None = None,
    store_path: str = "",
    budget: int = PAYLOAD_BUDGET,
) -> dict[str, Any]:
    good_traces = extract_traces(good_events)
    bad_traces = extract_traces(bad_events)
    _check_same_steps(good_traces, result.good_steps, "passing")
    _check_same_steps(bad_traces, result.bad_steps, "failing")

    blocks = _Blocks()
    steps = {
        "good": _step_details(good_traces, good_events, blocks, blobs),
        "bad": _step_details(bad_traces, bad_events, blocks, blobs),
    }

    columns = []
    for i, j in result.alignment:
        agree = (
            i is not None
            and j is not None
            and target_agree(result.good_steps[i], result.bad_steps[j])
        )
        similarity = result.column_similarity(i, j)
        columns.append(
            {
                "g": i,
                "b": j,
                "agree": agree,
                "similarity": None if similarity is None else round(similarity, 4),
            }
        )

    payload: dict[str, Any] = {
        "good": _run_summary(good_header, good_events, len(result.good_steps)),
        "bad": _run_summary(bad_header, bad_events, len(result.bad_steps)),
        "divergence": result.divergence,
        "score": round(result.score, 4),
        "length_ratio": round(result.length_ratio, 4),
        "excluded_by_length": result.excluded_by_length,
        "embedding": {
            "model": result.embedding_model,
            "revision": result.embedding_revision,
        },
        "store": store_path,
        "columns": columns,
        "steps": steps,
        "blocks": [dict(m) for m in blocks.meta],
    }
    _fit_budget(payload, blocks.texts, budget)
    return payload


def _check_same_steps(traces: list[StepTrace], steps: list[Step], side: str) -> None:
    if [t.step for t in traces] != list(steps):
        raise ValueError(
            f"the {side} run's steps do not match the ones the diff aligned "
            f"({len(traces)} vs {len(steps)}). The report and the aligner must read "
            f"the same trajectory; re-run the diff against this store."
        )


def render(payload: dict[str, Any], *, title: str) -> str:
    template = _TEMPLATE.read_text(encoding="utf-8")
    # `<` cannot appear raw inside the data island or a `</script` in recorded
    # content would end it early.
    data = _serialize(payload).replace("<", "\\u003c")
    return template.replace(_TITLE_MARK, _escape(title)).replace(_PAYLOAD_MARK, data)


def _escape(text: str) -> str:
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def write_report(
    path: Path,
    good_header: RunHeader,
    good_events: list[StoredEvent],
    bad_header: RunHeader,
    bad_events: list[StoredEvent],
    result: DiffResult,
    *,
    blobs: BlobStore | None = None,
    store_path: str = "",
    budget: int = PAYLOAD_BUDGET,
) -> dict[str, Any]:
    payload = build_payload(
        good_header,
        good_events,
        bad_header,
        bad_events,
        result,
        blobs=blobs,
        store_path=store_path,
        budget=budget,
    )
    title = good_header.task_id or f"{good_header.name} vs {bad_header.name}"
    path.write_text(render(payload, title=title), encoding="utf-8")
    return payload

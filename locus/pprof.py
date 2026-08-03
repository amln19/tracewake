"""Token spend as a standard pprof profile.

Usage is measured per model call, not per context block. Input tokens are
split across a call's messages in proportion to character length (largest-
remainder, so the attributed sum equals the measured total exactly). That
split is disclosed as proportional, not measured. Output tokens go to a
single `response` leaf.

The stack is run → model → turn → provenance. Emitting gzipped protobuf
means Speedscope, `go tool pprof`, and Pyroscope all work without a custom
renderer.
"""

from __future__ import annotations

import gzip
from collections import defaultdict
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import BinaryIO

from .events import ModelCallEvent, RunHeader, StoredEvent

# Leaf for output tokens. Not a provenance tag on a recorded message — the
# response is what the model produced, not what it consumed.
RESPONSE_LEAF = "response"
UNTAGGED = "untagged"


# ---------------------------------------------------------------------------
# Minimal protobuf writer for the subset of profile.proto we emit.
# ---------------------------------------------------------------------------


def _varint(n: int) -> bytes:
    if n < 0:
        raise ValueError(f"varint cannot encode a negative integer ({n})")
    out = bytearray()
    while n > 0x7F:
        out.append((n & 0x7F) | 0x80)
        n >>= 7
    out.append(n & 0x7F)
    return bytes(out)


def _tag(field: int, wire: int) -> bytes:
    return _varint((field << 3) | wire)


def _encode_varint_field(field: int, value: int) -> bytes:
    if value == 0:
        return b""  # proto3 default
    return _tag(field, 0) + _varint(value)


def _encode_bytes_field(field: int, raw: bytes) -> bytes:
    return _tag(field, 2) + _varint(len(raw)) + raw


def _encode_string_field(field: int, text: str) -> bytes:
    return _encode_bytes_field(field, text.encode("utf-8"))


def _encode_message_field(field: int, body: bytes) -> bytes:
    return _encode_bytes_field(field, body)


# ---------------------------------------------------------------------------
# Attribution
# ---------------------------------------------------------------------------


def proportional(total: int, weights: Sequence[int]) -> list[int]:
    """Split `total` across `weights` so the parts sum to `total` exactly.

    Largest remainder: floor each share, then give the leftover units to the
    shares with the largest fractional parts. Ties break on earlier index so
    the result is deterministic.
    """
    if total < 0:
        raise ValueError(f"token total cannot be negative ({total})")
    if not weights:
        return []
    if any(w < 0 for w in weights):
        raise ValueError(f"weights must be non-negative, got {list(weights)}")
    mass = sum(weights)
    if mass == 0:
        # Nothing to weigh by. Park everything on the first slot so the call's
        # measured total is not silently dropped.
        out = [0] * len(weights)
        out[0] = total
        return out
    # Exact share in fixed-point to avoid float drift on large totals.
    floors = [(total * w) // mass for w in weights]
    remainders = [(total * w) % mass for w in weights]
    leftover = total - sum(floors)
    order = sorted(range(len(weights)), key=lambda i: (-remainders[i], i))
    for i in order[:leftover]:
        floors[i] += 1
    return floors


@dataclass(frozen=True)
class TokenShare:
    run_id: str
    run_name: str
    task_id: str | None
    model_id: str
    turn: int
    leaf: str
    input_tokens: int
    output_tokens: int

    @property
    def tokens(self) -> int:
        return self.input_tokens + self.output_tokens


def attribute_tokens(
    header: RunHeader, events: Sequence[StoredEvent]
) -> list[TokenShare]:
    """Per-block token shares for one run. Sums equal the recorded usage."""
    shares: list[TokenShare] = []
    turn = 0
    for stored in events:
        event = stored.event
        if not isinstance(event, ModelCallEvent):
            continue
        usage = event.response.usage
        weights = [len(m.content) for m in event.messages]
        leaves = [m.provenance or UNTAGGED for m in event.messages]
        if not leaves:
            # A call with no messages still has a measured total.
            leaves = [UNTAGGED]
            weights = [0]
        parts = proportional(usage.input_tokens, weights)
        for leaf, inp in zip(leaves, parts, strict=True):
            if inp == 0:
                continue
            shares.append(
                TokenShare(
                    run_id=header.run_id,
                    run_name=header.name,
                    task_id=header.task_id,
                    model_id=event.model_id,
                    turn=turn,
                    leaf=leaf,
                    input_tokens=inp,
                    output_tokens=0,
                )
            )
        if usage.output_tokens:
            shares.append(
                TokenShare(
                    run_id=header.run_id,
                    run_name=header.name,
                    task_id=header.task_id,
                    model_id=event.model_id,
                    turn=turn,
                    leaf=RESPONSE_LEAF,
                    input_tokens=0,
                    output_tokens=usage.output_tokens,
                )
            )
        turn += 1
    return _merge_shares(shares)


def _merge_shares(shares: Sequence[TokenShare]) -> list[TokenShare]:
    """Collapse identical stacks. pprof samples with the same stack add."""
    bucket: dict[tuple[str, str, str | None, str, int, str], list[int]] = {}
    order: list[tuple[str, str, str | None, str, int, str]] = []
    for s in shares:
        key = (s.run_id, s.run_name, s.task_id, s.model_id, s.turn, s.leaf)
        if key not in bucket:
            bucket[key] = [0, 0]
            order.append(key)
        bucket[key][0] += s.input_tokens
        bucket[key][1] += s.output_tokens
    return [
        TokenShare(
            run_id=k[0],
            run_name=k[1],
            task_id=k[2],
            model_id=k[3],
            turn=k[4],
            leaf=k[5],
            input_tokens=bucket[k][0],
            output_tokens=bucket[k][1],
        )
        for k in order
        if bucket[k][0] or bucket[k][1]
    ]


def usage_totals(events: Sequence[StoredEvent]) -> tuple[int, int]:
    inp = out = 0
    for stored in events:
        if isinstance(stored.event, ModelCallEvent):
            inp += stored.event.response.usage.input_tokens
            out += stored.event.response.usage.output_tokens
    return inp, out


# ---------------------------------------------------------------------------
# Profile builder
# ---------------------------------------------------------------------------


class _Profile:
    def __init__(self) -> None:
        self.strings: list[str] = [""]
        self._string_index: dict[str, int] = {"": 0}
        self.functions: list[bytes] = []  # already-encoded Function messages
        self.locations: list[bytes] = []
        self.samples: list[bytes] = []
        self._function_ids: dict[str, int] = {}
        self._location_ids: dict[int, int] = {}  # function_id → location_id

    def string(self, text: str) -> int:
        found = self._string_index.get(text)
        if found is not None:
            return found
        index = len(self.strings)
        self.strings.append(text)
        self._string_index[text] = index
        return index

    def function(self, name: str) -> int:
        found = self._function_ids.get(name)
        if found is not None:
            return found
        fid = len(self.functions) + 1
        name_i = self.string(name)
        body = (
            _encode_varint_field(1, fid)
            + _encode_varint_field(2, name_i)
            + _encode_varint_field(3, name_i)
        )
        self.functions.append(body)
        self._function_ids[name] = fid
        return fid

    def location_for(self, function_id: int) -> int:
        found = self._location_ids.get(function_id)
        if found is not None:
            return found
        lid = len(self.locations) + 1
        # Line: function_id only (field 1). Location: id, line message.
        line = _encode_varint_field(1, function_id)
        body = _encode_varint_field(1, lid) + _encode_message_field(4, line)
        self.locations.append(body)
        self._location_ids[function_id] = lid
        return lid

    def add_sample(self, stack_names: Sequence[str], values: Sequence[int]) -> None:
        # stack_names are root → leaf; pprof wants leaf at location_id[0].
        ids = [
            self.location_for(self.function(name))
            for name in reversed(stack_names)
        ]
        body = b"".join(_encode_varint_field(1, i) for i in ids)
        # Zeros must be written: every sample needs len(value) == len(sample_type).
        for v in values:
            if v < 0:
                raise ValueError(f"sample value cannot be negative ({v})")
            body += _tag(2, 0) + _varint(v)
        self.samples.append(body)

    def serialize(
        self,
        *,
        sample_types: Sequence[tuple[str, str]],
        time_nanos: int = 0,
        duration_nanos: int = 0,
        comments: Sequence[str] = (),
    ) -> bytes:
        # sample_type strings must be interned before the table is written.
        type_msgs: list[bytes] = []
        for type_name, unit in sample_types:
            type_msgs.append(
                _encode_varint_field(1, self.string(type_name))
                + _encode_varint_field(2, self.string(unit))
            )
        comment_ids = [self.string(c) for c in comments]

        out = bytearray()
        for msg in type_msgs:
            out += _encode_message_field(1, msg)
        for msg in self.samples:
            out += _encode_message_field(2, msg)
        for msg in self.locations:
            out += _encode_message_field(4, msg)
        for msg in self.functions:
            out += _encode_message_field(5, msg)
        for text in self.strings:
            out += _encode_string_field(6, text)
        if time_nanos:
            out += _encode_varint_field(9, time_nanos)
        if duration_nanos:
            out += _encode_varint_field(10, duration_nanos)
        for cid in comment_ids:
            out += _encode_varint_field(13, cid)
        return bytes(out)


def _stack(share: TokenShare) -> list[str]:
    run_label = share.task_id or share.run_name or share.run_id[:12]
    return [
        f"run:{run_label}",
        f"model:{share.model_id}",
        f"turn:{share.turn + 1}",
        share.leaf,
    ]


def build_token_profile(
    header: RunHeader, events: Sequence[StoredEvent]
) -> bytes:
    shares = attribute_tokens(header, events)
    profile = _Profile()
    for share in shares:
        profile.add_sample(
            _stack(share), [share.input_tokens, share.output_tokens]
        )
    started = int(header.started_at * 1_000_000_000)
    duration = 0
    if header.finished_at is not None:
        duration = max(0, int((header.finished_at - header.started_at) * 1_000_000_000))
    comments = [
        "locus token profile",
        "input tokens split across context blocks by character share "
        "(proportional, not measured per block)",
        f"run {header.run_id}",
    ]
    return profile.serialize(
        sample_types=(("input_tokens", "count"), ("output_tokens", "count")),
        time_nanos=started,
        duration_nanos=duration,
        comments=comments,
    )


def write_token_profile(
    path: Path, header: RunHeader, events: Sequence[StoredEvent]
) -> tuple[int, int]:
    raw = build_token_profile(header, events)
    with gzip.open(path, "wb") as fh:
        fh.write(raw)
    return usage_totals(events)


def format_top(
    header: RunHeader,
    events: Sequence[StoredEvent],
    *,
    n: int = 20,
) -> str:
    shares = attribute_tokens(header, events)
    by_leaf: dict[str, list[int]] = defaultdict(lambda: [0, 0])
    for s in shares:
        by_leaf[s.leaf][0] += s.input_tokens
        by_leaf[s.leaf][1] += s.output_tokens
    ranked = sorted(
        by_leaf.items(), key=lambda kv: -(kv[1][0] + kv[1][1])
    )
    inp, out = usage_totals(events)
    lines = [
        f"run {header.run_id[:12]}  {header.name}",
        f"total  input={inp}  output={out}",
        f"{'leaf':<28} {'input':>10} {'output':>10} {'share':>8}",
    ]
    total = inp + out
    for leaf, (i, o) in ranked[:n]:
        share = (i + o) / total if total else 0.0
        lines.append(f"{leaf:<28} {i:>10} {o:>10} {share:>7.1%}")
    if len(ranked) > n:
        lines.append(f"… {len(ranked) - n} more leaves")
    lines.append(
        "input split across leaves is proportional by character length"
    )
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Decoder — enough to prove what we emit, used by tests and Speedscope prep.
# ---------------------------------------------------------------------------


def _read_varint(buf: memoryview, i: int) -> tuple[int, int]:
    shift = 0
    value = 0
    while True:
        if i >= len(buf):
            raise ValueError("truncated varint")
        byte = buf[i]
        i += 1
        value |= (byte & 0x7F) << shift
        if not (byte & 0x80):
            return value, i
        shift += 7
        if shift > 70:
            raise ValueError("varint too long")


def decode_profile(raw: bytes) -> dict:
    """Decode the subset of profile.proto that `build_token_profile` writes."""
    buf = memoryview(raw)
    i = 0
    sample_types: list[tuple[int, int]] = []
    samples: list[dict] = []
    locations: dict[int, int] = {}  # location_id → function_id
    functions: dict[int, int] = {}  # function_id → name string index
    strings: list[str] = []
    comments: list[int] = []
    time_nanos = duration_nanos = 0

    while i < len(buf):
        key, i = _read_varint(buf, i)
        field, wire = key >> 3, key & 7
        if wire == 0:
            value, i = _read_varint(buf, i)
            if field == 9:
                time_nanos = value
            elif field == 10:
                duration_nanos = value
            elif field == 13:
                comments.append(value)
            continue
        if wire != 2:
            raise ValueError(f"unsupported wire type {wire} for field {field}")
        length, i = _read_varint(buf, i)
        chunk = buf[i : i + length]
        i += length
        if field == 1:
            sample_types.append(_decode_value_type(chunk))
        elif field == 2:
            samples.append(_decode_sample(chunk))
        elif field == 4:
            lid, fid = _decode_location(chunk)
            locations[lid] = fid
        elif field == 5:
            fid, name_i = _decode_function(chunk)
            functions[fid] = name_i
        elif field == 6:
            strings.append(bytes(chunk).decode("utf-8"))
        # ignore unknown length-delimited fields

    if not strings or strings[0] != "":
        raise ValueError("string_table[0] must be the empty string")

    return {
        "sample_types": [
            (strings[t], strings[u]) for t, u in sample_types
        ],
        "samples": samples,
        "locations": locations,
        "functions": {fid: strings[name_i] for fid, name_i in functions.items()},
        "strings": strings,
        "comments": [strings[c] for c in comments],
        "time_nanos": time_nanos,
        "duration_nanos": duration_nanos,
    }


def _decode_value_type(chunk: memoryview) -> tuple[int, int]:
    i = 0
    type_i = unit_i = 0
    while i < len(chunk):
        key, i = _read_varint(chunk, i)
        field, wire = key >> 3, key & 7
        if wire != 0:
            raise ValueError("ValueType fields are varints")
        value, i = _read_varint(chunk, i)
        if field == 1:
            type_i = value
        elif field == 2:
            unit_i = value
    return type_i, unit_i


def _decode_sample(chunk: memoryview) -> dict:
    i = 0
    location_ids: list[int] = []
    values: list[int] = []
    while i < len(chunk):
        key, i = _read_varint(chunk, i)
        field, wire = key >> 3, key & 7
        if wire != 0:
            # labels would be length-delimited; we don't emit them
            if wire == 2:
                length, i = _read_varint(chunk, i)
                i += length
                continue
            raise ValueError(f"unexpected wire {wire} in Sample")
        value, i = _read_varint(chunk, i)
        if field == 1:
            location_ids.append(value)
        elif field == 2:
            values.append(value)
    return {"location_ids": location_ids, "values": values}


def _decode_location(chunk: memoryview) -> tuple[int, int]:
    i = 0
    lid = 0
    function_id = 0
    while i < len(chunk):
        key, i = _read_varint(chunk, i)
        field, wire = key >> 3, key & 7
        if wire == 0:
            value, i = _read_varint(chunk, i)
            if field == 1:
                lid = value
            continue
        if wire != 2:
            raise ValueError(f"unexpected wire {wire} in Location")
        length, i = _read_varint(chunk, i)
        sub = chunk[i : i + length]
        i += length
        if field == 4:
            function_id = _decode_line_function(sub)
    return lid, function_id


def _decode_line_function(chunk: memoryview) -> int:
    i = 0
    function_id = 0
    while i < len(chunk):
        key, i = _read_varint(chunk, i)
        field, wire = key >> 3, key & 7
        if wire != 0:
            raise ValueError("Line fields are varints")
        value, i = _read_varint(chunk, i)
        if field == 1:
            function_id = value
    return function_id


def _decode_function(chunk: memoryview) -> tuple[int, int]:
    i = 0
    fid = name_i = 0
    while i < len(chunk):
        key, i = _read_varint(chunk, i)
        field, wire = key >> 3, key & 7
        if wire != 0:
            raise ValueError("Function fields are varints")
        value, i = _read_varint(chunk, i)
        if field == 1:
            fid = value
        elif field == 2:
            name_i = value
    return fid, name_i


def read_gzipped_profile(path: Path | BinaryIO) -> dict:
    if isinstance(path, Path):
        with gzip.open(path, "rb") as fh:
            return decode_profile(fh.read())
    with gzip.GzipFile(fileobj=path) as fh:
        return decode_profile(fh.read())


def sample_totals(decoded: dict) -> tuple[int, int]:
    inp = out = 0
    for sample in decoded["samples"]:
        values = sample["values"]
        if len(values) != 2:
            raise ValueError(
                f"expected 2 sample values (input, output), got {len(values)}"
            )
        inp += values[0]
        out += values[1]
    return inp, out

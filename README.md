# locus

Find where two coding agent runs diverged. Record an agent run once, replay it
offline for free, then diff a good run against a bad one to get the exact step
where they stopped agreeing.

Right now locus does the first two. Record and replay work; trajectory alignment —
the part that answers "where did these two runs stop agreeing" — is next.

```python
import locus

with locus.record("fix-off-by-one") as rec:
    model = rec.model(
        provider="acme", model_id="acme-1",
        create_fn=client.create, stream_fn=client.stream,
    )
    agent.run(task, model, rec.tools(client.dispatch), rec.clock)
    rec.outcome(status="ok")
    run_id = rec.run_id

with locus.replay(run_id) as rep:
    model = rep.model(provider="acme", model_id="acme-1")
    agent.run(task, model, rep.tools(), rep.clock)
```

The agent code is the same in both blocks. On replay it observes the same model
chunks, the same tool results, and the same clock values, byte for byte, without
touching the network.

Runs are stored as an append-only event log in SQLite, with file contents and
large tool results kept in a content-addressed blob store so a hundred runs over
the same repo store those files once. Every event names the model call that
caused it, so a run reconstructs as a tree rather than a flat list.

Streaming is recorded as the assembled response plus its chunk boundaries, and
re-emitted through the same iterator interface, so agent code that consumes a
stream still works on replay. Inter-chunk timing is recorded but deliberately
not reproduced — it isn't semantically meaningful and it would make replay slow.

Parallel tool batches are recorded under their parent model call with an
intra-batch index. A batch is a partial order, not a total one, so it replays
correctly no matter what order its calls happen to complete in.

Replay matches a request strictly on model and message hash, and raises on a
miss. A replayed agent that builds a request the recorded run never made fails
loudly rather than being handed something close enough — surfacing that
divergence is the whole point.

The record/replay design — cassettes, request matchers, record modes — is
borrowed from VCR.py and Ruby's VCR, which solved this for HTTP fifteen years
ago. What's new here is applying it to model calls, tool calls, and the clock,
and using it as the substrate for trajectory alignment.

## Development

```
uv sync
uv run pytest
```

`tests/test_gate.py` is the load-bearing test: a fully mocked agent run —
streaming, a parallel tool batch that completes out of order, a failing tool,
and clock reads — replays byte-identically.

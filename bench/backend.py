"""A local model, in process.

In process rather than behind a server because a corpus run must not depend on
anything the recorder cannot see: a socket to a local daemon is a nondeterministic
input that would sit outside the interception surfaces and be invisible on replay.
"""

from __future__ import annotations

from collections.abc import Generator
from dataclasses import dataclass
from functools import lru_cache
from typing import Any

from locus import DecodeParams, Message, ModelResponse, StreamChunk, Usage

PROVIDER = "mlx"
DEFAULT_MODEL = "mlx-community/Qwen2.5-Coder-7B-Instruct-4bit"
# A larger option for the same task. Coverage saturates on the 7B while resolve
# stays near zero — the agent reliably produces a patch and rarely a correct one,
# which is the gap capability closes rather than harness work.
LARGER_MODEL = "mlx-community/Qwen2.5-Coder-14B-Instruct-4bit"

# Long enough for a reasoning paragraph plus one action block. A small model that
# is allowed to ramble spends its budget narrating instead of acting.
DEFAULT_MAX_TOKENS = 640


END_MARKERS = ("<|im_end|>", "<|endoftext|>", "<|eot_id|>")


def _visible(text: str) -> tuple[str, bool]:
    for marker in END_MARKERS:
        if marker in text:
            return (text[: text.index(marker)], True)
    return (text, False)


@lru_cache(maxsize=2)
def _load(model_id: str) -> tuple[Any, Any]:
    from mlx_lm import load

    return load(model_id)


@dataclass
class LocalModel:
    """Wraps mlx-lm as the streaming backend a locus session records.

    Sampling is seeded per call so a batch is reproducible as a batch, while
    still varying across the repeats of one task — which is the point of running
    at temperature 0.7. The seed is derived from the caller, not from the clock,
    so re-running the batch produces the same corpus.
    """

    model_id: str = DEFAULT_MODEL
    temperature: float = 0.7
    max_tokens: int = DEFAULT_MAX_TOKENS
    seed: int = 0
    calls: int = 0

    def warm(self) -> None:
        _load(self.model_id)

    def _prompt(self, messages: list[Message]) -> str:
        model, tokenizer = _load(self.model_id)
        chat = [{"role": m.role, "content": m.content} for m in messages]
        return tokenizer.apply_chat_template(chat, tokenize=False, add_generation_prompt=True)

    def stream(
        self, model_id: str, messages: list[Message], params: DecodeParams
    ) -> Generator[StreamChunk, None, ModelResponse]:
        import mlx.core as mx
        from mlx_lm import stream_generate
        from mlx_lm.sample_utils import make_sampler

        model, tokenizer = _load(self.model_id)
        prompt = self._prompt(messages)
        mx.random.seed(self.seed + self.calls)
        self.calls += 1

        temperature = params.temperature if params.temperature is not None else self.temperature
        sampler = make_sampler(temp=temperature, top_p=params.top_p or 1.0)
        limit = params.max_tokens or self.max_tokens

        text: list[str] = []
        index = 0
        prompt_tokens = 0
        finish = "length"
        for response in stream_generate(
            model, tokenizer, prompt, max_tokens=limit, sampler=sampler
        ):
            prompt_tokens = response.prompt_tokens
            if response.finish_reason:
                finish = "end_turn" if response.finish_reason == "stop" else "length"
            # The detokenizer hands back the turn-end marker as ordinary text.
            # Recorded chunks have to reassemble to the recorded response exactly,
            # so it is dropped here rather than stripped from the assembled text.
            delta, stop = _visible(response.text)
            if delta:
                yield StreamChunk(index=index, text_delta=delta)
                text.append(delta)
                index += 1
            if stop:
                finish = "end_turn"
                break

        assembled = "".join(text)
        return ModelResponse(
            text=assembled,
            finish_reason=finish,
            usage=Usage(input_tokens=prompt_tokens, output_tokens=index),
        )

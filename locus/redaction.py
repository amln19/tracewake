from __future__ import annotations

import fnmatch
import os
from pathlib import Path
from typing import Any

from .config import Config
from .events import EVENT_ADAPTER, AnyEvent, Message

REDACTED = "[REDACTED]"
HOME_PLACEHOLDER = "<HOME>"

# A short value is not a credential and is very likely to occur inside unrelated
# text, where replacing it would corrupt the log rather than protect anything.
MIN_SECRET_LENGTH = 8


class Redactor:
    """Scrubs secrets out of everything on its way to disk.

    Also runs on the replay match path. Redaction rewrites message content, so a
    hash taken before it and a hash taken after it disagree; running it on both
    sides is what keeps a redacted cassette matchable. It is idempotent, so
    applying it twice to the same value is safe.

    Values are collected from the local environment rather than guessed from
    shape. Two machines with different credentials in the same variable both
    scrub to the same placeholder, so a cassette recorded on one replays on the
    other; pattern-matching key formats would neither generalize nor be safe
    against false positives.
    """

    def __init__(self, config: Config) -> None:
        self.enabled = config.redact
        self._before_record = config.before_record
        self._header_globs = tuple(g.lower() for g in config.filter_headers)
        self._env_globs = tuple(g.upper() for g in config.filter_env)
        self._values = self._collect_values(config)
        self._paths = self._collect_paths() if config.filter_paths else ()

    def _collect_values(self, config: Config) -> tuple[str, ...]:
        found = {v for v in config.filter_values if len(v) >= MIN_SECRET_LENGTH}
        for name, value in os.environ.items():
            if self.is_secret_env(name) and len(value) >= MIN_SECRET_LENGTH:
                found.add(value)
        # Longest first, so a secret that contains another secret as a prefix is
        # replaced whole rather than leaving a fragment behind.
        return tuple(sorted(found, key=len, reverse=True))

    def _collect_paths(self) -> tuple[tuple[str, str], ...]:
        home = str(Path.home())
        if not home or home == "/":
            return ()
        return ((home, HOME_PLACEHOLDER),)

    def is_secret_env(self, name: str | None) -> bool:
        if name is None:
            return False
        upper = name.upper()
        return any(fnmatch.fnmatchcase(upper, g) for g in self._env_globs)

    def _is_filtered_key(self, key: str) -> bool:
        lower = key.lower()
        return any(fnmatch.fnmatchcase(lower, g) for g in self._header_globs)

    def text(self, value: str) -> str:
        if not self.enabled:
            return value
        for secret in self._values:
            if secret in value:
                value = value.replace(secret, REDACTED)
        for raw, placeholder in self._paths:
            if raw in value:
                value = value.replace(raw, placeholder)
        return value

    def blob(self, data: bytes) -> bytes:
        if not self.enabled:
            return data
        try:
            text = data.decode("utf-8")
        except UnicodeDecodeError:
            # Binary content has no text to scrub and re-encoding would corrupt it.
            return data
        return self.text(text).encode("utf-8")

    def _walk(self, value: Any, key: str | None = None) -> Any:
        if key is not None and self._is_filtered_key(key):
            return REDACTED
        match value:
            case str():
                return self.text(value)
            case dict():
                return {k: self._walk(v, k) for k, v in value.items()}
            case list():
                return [self._walk(v) for v in value]
            case _:
                return value

    def messages(self, messages: list[Message]) -> list[Message]:
        if not self.enabled:
            return list(messages)
        return [Message.model_validate(self._walk(m.model_dump(mode="json"))) for m in messages]

    def event(self, event: AnyEvent) -> AnyEvent | None:
        if self.enabled:
            data = event.model_dump(mode="json")
            if (
                data.get("type") == "environment"
                and data.get("source") == "env"
                and self.is_secret_env(data.get("key"))
            ):
                data["value"] = REDACTED
            meta = data.pop("meta")
            data = self._walk(data)
            data["meta"] = meta
            event = EVENT_ADAPTER.validate_python(data)
        if self._before_record is not None:
            return self._before_record(event)
        return event

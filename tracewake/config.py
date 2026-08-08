from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, replace
from typing import TYPE_CHECKING, Any, Literal

if TYPE_CHECKING:
    from .events import AnyEvent

RecordMode = Literal["once", "none", "new_episodes", "all"]
RECORD_MODES: tuple[RecordMode, ...] = ("once", "none", "new_episodes", "all")

MATCHERS = ("model", "messages_hash", "system_prompt", "tool_names", "ordinal")
DEFAULT_MATCH_ON = ("model", "messages_hash")

DEFAULT_FILTER_HEADERS = (
    "authorization",
    "proxy-authorization",
    "x-api-key",
    "api-key",
    "cookie",
    "set-cookie",
)

# Deliberately narrow. Every value read from a variable matching one of these is
# replaced wherever it appears in the log, so a pattern broad enough to catch a
# non-secret (a region, a hostname) would rewrite unrelated text that happens to
# contain it.
DEFAULT_FILTER_ENV = (
    "*_API_KEY",
    "*_SECRET",
    "*_SECRET_KEY",
    "*_TOKEN",
    "*_PASSWORD",
    "*_PASSWD",
    "*_CREDENTIALS",
)


@dataclass(frozen=True)
class Config:
    match_on: tuple[str, ...] = DEFAULT_MATCH_ON
    redact: bool = True
    filter_headers: tuple[str, ...] = DEFAULT_FILTER_HEADERS
    filter_env: tuple[str, ...] = DEFAULT_FILTER_ENV
    filter_values: tuple[str, ...] = ()
    filter_paths: bool = True
    before_record: Callable[[AnyEvent], AnyEvent | None] | None = None
    stale_after_days: float | None = 30.0
    require_hash_seed: bool = True
    block_network: bool = True
    patch_environment: bool = True

    def __post_init__(self) -> None:
        if not self.match_on:
            raise ValueError(
                f"match_on is empty, so every recorded call would match every request. "
                f"Choose from {', '.join(MATCHERS)}."
            )
        unknown = [m for m in self.match_on if m not in MATCHERS]
        if unknown:
            raise ValueError(
                f"unknown matcher(s) {', '.join(repr(m) for m in unknown)}. "
                f"Available matchers are {', '.join(MATCHERS)}."
            )
        object.__setattr__(self, "match_on", tuple(self.match_on))
        for field in ("filter_headers", "filter_env", "filter_values"):
            object.__setattr__(self, field, tuple(getattr(self, field)))

    @property
    def proves_request_identity(self) -> bool:
        return "messages_hash" in self.match_on


_default = Config()


def configure(**overrides: Any) -> Config:
    """Set the process-wide default configuration and return it."""
    global _default
    known = {f for f in Config.__dataclass_fields__}
    unknown = set(overrides) - known
    if unknown:
        raise TypeError(
            f"unknown configuration option(s) {', '.join(sorted(unknown))}. "
            f"Valid options are {', '.join(sorted(known))}."
        )
    _default = replace(_default, **overrides)
    return _default


def current_config() -> Config:
    return _default


def resolve(config: Config | None, overrides: dict[str, Any]) -> Config:
    base = config if config is not None else _default
    supplied = {k: v for k, v in overrides.items() if v is not None}
    return replace(base, **supplied) if supplied else base

from __future__ import annotations

import json
import os
from contextvars import ContextVar
from pathlib import Path
from typing import TYPE_CHECKING

from .events import FsReadEvent, FsWriteEvent, canonical_json, sha256_hex

if TYPE_CHECKING:
    from .session import Session

# Set while a tool call is on the stack. `Tools.call` runs in the worker thread
# that dispatches the tool, so a parallel batch gives each of its calls its own
# context and the reads a tool performs are attributed to that tool rather than
# to whichever call happened to finish first.
tool_scope: ContextVar[tuple[str | None, str | None]] = ContextVar(
    "tracewake_tool_scope", default=(None, None)
)


class Fs:
    """The filesystem as the agent's tools see it.

    Interception is at the tool boundary rather than at the syscall: a coding
    agent touches the world through its tools, so wrapping them captures what the
    agent consumed without a VM or a ptrace layer. Paths are stored scrubbed,
    which is also what lets a cassette leave the machine that recorded it.

    Replay serves reads from the log and never touches the disk, including for
    writes: a write is checked against what was recorded and raises when it
    differs, rather than being applied a second time.

    Given a `root`, paths are resolved against it and recorded relative to it.
    Repeated runs over the same repo each get their own working copy, so without
    a root the same file would be logged under a different absolute path in every
    run — which leaves nothing to compare across runs and makes the cassette
    describe a directory that no longer exists.
    """

    def __init__(self, session: Session, root: Path | None = None) -> None:
        self._session = session
        self._root = root.resolve() if root is not None else None

    def rooted(self, root: str | Path) -> Fs:
        return Fs(self._session, Path(root))

    def resolve(self, path: str | Path) -> Path:
        if self._root is None:
            return Path(path)
        candidate = Path(path)
        target = (self._root / candidate).resolve() if not candidate.is_absolute() else (
            candidate.resolve()
        )
        if not target.is_relative_to(self._root):
            raise ValueError(
                f"{path} resolves outside the root {self._root}. A task's agent may only "
                f"read and write inside the working copy it was given."
            )
        return target

    def _locate(self, path: str | Path) -> tuple[str, Path]:
        target = self.resolve(path)
        if self._root is None:
            return (self._session._fs_key(path), target)
        return (self._session._fs_key(target.relative_to(self._root)), target)

    def read_text(self, path: str | Path) -> str:
        return self.read_bytes(path).decode("utf-8")

    def read_bytes(self, path: str | Path) -> bytes:
        key, target = self._locate(path)
        recorded = self._session._replay_fs("content", key)
        if recorded is not None:
            if not recorded.exists or recorded.content is None:
                raise FileNotFoundError(key)
            return self._session._store.blobs.get(recorded.content.digest)

        exists = target.is_file()
        data = target.read_bytes() if exists else b""
        self._session._append(
            FsReadEvent(
                path=key,
                kind="content",
                exists=exists,
                content=self._session._put_blob(data) if exists else None,
                **self._session._scope_fields(),
            )
        )
        if not exists:
            raise FileNotFoundError(key)
        return data

    def exists(self, path: str | Path) -> bool:
        key, target = self._locate(path)
        recorded = self._session._replay_fs("exists", key)
        if recorded is not None:
            return recorded.exists

        exists = target.exists()
        self._session._append(
            FsReadEvent(
                path=key, kind="exists", exists=exists, **self._session._scope_fields()
            )
        )
        return exists

    def listdir(self, path: str | Path) -> list[str]:
        key, target = self._locate(path)
        recorded = self._session._replay_fs("listing", key)
        if recorded is not None:
            if not recorded.exists or recorded.content is None:
                raise FileNotFoundError(key)
            blob = self._session._store.blobs.get(recorded.content.digest)
            return list(json.loads(blob.decode("utf-8")))

        exists = target.is_dir()
        # Directory order is filesystem-dependent and differs between machines,
        # so the listing is sorted before it is recorded and callers get a stable
        # order on both paths.
        names = sorted(os.listdir(target)) if exists else []
        self._session._append(
            FsReadEvent(
                path=key,
                kind="listing",
                exists=exists,
                content=(
                    self._session._put_blob(canonical_json(names).encode("utf-8"))
                    if exists
                    else None
                ),
                **self._session._scope_fields(),
            )
        )
        if not exists:
            raise FileNotFoundError(key)
        return names

    def write_text(self, path: str | Path, data: str) -> None:
        self.write_bytes(path, data.encode("utf-8"))

    def write_bytes(self, path: str | Path, data: bytes) -> None:
        key, target = self._locate(path)
        recorded = self._session._replay_fs("write", key)
        if recorded is not None:
            digest = sha256_hex(self._session._redactor.blob(data))
            if digest != recorded.content.digest:
                self._session._miss(
                    f"the replayed agent wrote different content to {key} than the "
                    f"recorded run did ({digest[:12]} now vs "
                    f"{recorded.content.digest[:12]} recorded). The replayed agent diverged."
                )
            return

        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)
        self._session._append(
            FsWriteEvent(
                path=key,
                content=self._session._put_blob(data),
                **self._session._scope_fields(),
            )
        )

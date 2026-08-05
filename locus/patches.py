from __future__ import annotations

import os
import random
import secrets
import socket
import sys
import sysconfig
import threading
import time
import uuid
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from pathlib import Path
from types import CodeType
from typing import TYPE_CHECKING, Any

from .config import Config

if TYPE_CHECKING:
    from .session import Session

# Captured before anything is patched. Locus records wall-clock metadata and
# mints its own ids while the patches are live; going through the patched
# functions would record locus's own bookkeeping as agent input, and recording
# an event would recurse into the recorder that is writing it.
real_time = time.time
real_monotonic = time.monotonic
real_perf_counter = time.perf_counter
real_uuid4 = uuid.uuid4


class LocusError(Exception):
    pass


class NetworkBlocked(LocusError):
    pass


class HashSeedError(LocusError):
    pass


_STDLIB = Path(sysconfig.get_paths()["stdlib"]).resolve()
_LOCUS = Path(__file__).parent.resolve()
def _frame_files(*functions: Any) -> frozenset[str]:
    """The filenames these functions' frames will actually carry.

    Asked of the code objects rather than assumed from `__file__`. Several
    stdlib modules are frozen into the interpreter, and a frozen module's
    frames report `<frozen os>` while its `__file__` still points at the source
    — so a forwarder set built from `__file__` silently matches nothing.
    """
    files: set[str] = set()
    for function in functions:
        code = getattr(getattr(function, "__func__", function), "__code__", None)
        if code is not None:
            files.add(code.co_filename)
    return frozenset(files)


# Modules that only forward a call to the function actually being intercepted:
# `os.getenv` and `Mapping.get` reach `__getitem__`, and `randint`/`choice`/
# `shuffle` reach `getrandbits`. The frame that decides whether a call is worth
# recording is the one above the forwarding layer, not the layer itself.
_Mapping = sys.modules["_collections_abc"].Mapping
_ENV_FORWARDERS = _frame_files(
    os.getenv, _Mapping.get, _Mapping.__contains__
) | frozenset({os.__file__, sys.modules["_collections_abc"].__file__})
_RANDOM_FORWARDERS = _frame_files(
    random.randint, random.choice, random.shuffle, random.sample
) | frozenset({random.__file__})
_SECRETS_FORWARDERS = _frame_files(
    secrets.token_bytes, secrets.token_hex, secrets.token_urlsafe
) | frozenset({secrets.__file__})

_instrumented: dict[CodeType, bool] = {}
_reentrant = threading.local()


def _is_instrumented_code(code: CodeType) -> bool:
    cached = _instrumented.get(code)
    if cached is not None:
        return cached
    name = code.co_filename
    if name.startswith("<"):
        verdict = False
    else:
        path = Path(name)
        resolved = path.resolve() if path.is_absolute() else path
        verdict = not (
            resolved.is_relative_to(_STDLIB) or resolved.is_relative_to(_LOCUS)
        )
    _instrumented[code] = verdict
    return verdict


def _caller_is_instrumented(
    depth: int = 2, forwarders: frozenset[str] = frozenset()
) -> bool:
    """Whether the code that made this call is code worth recording.

    The interpreter reads the clock and the environment constantly on its own —
    inside `threading`, `tempfile`, `logging` — and how many times it does so
    varies between two runs of the same program. Recording those would fill the
    log with values the agent never saw and make replay fail when the counts
    differ, so calls originating inside the standard library are passed straight
    through.
    """
    frame: Any = sys._getframe(depth)
    while frame is not None and frame.f_code.co_filename in forwarders:
        frame = frame.f_back
    if frame is None:
        return False
    return _is_instrumented_code(frame.f_code)


@contextmanager
def _no_reentry() -> Iterator[bool]:
    if getattr(_reentrant, "active", False):
        yield False
        return
    _reentrant.active = True
    try:
        yield True
    finally:
        _reentrant.active = False


def require_hash_seed(config: Config) -> None:
    if not config.require_hash_seed or sys.flags.hash_randomization == 0:
        return
    raise HashSeedError(
        "replay needs PYTHONHASHSEED=0: set iteration order otherwise varies between "
        "runs and silently breaks determinism in agent code you do not control. Re-run "
        f"as `PYTHONHASHSEED=0 {Path(sys.argv[0]).name or 'python'} ...`, use the locus "
        "CLI, which sets it for you, or pass require_hash_seed=False to accept the risk."
    )


@contextmanager
def block_network() -> Iterator[None]:
    """Make an attempted network call fail loudly instead of succeeding quietly."""
    originals = {
        (socket.socket, "connect"): socket.socket.connect,
        (socket.socket, "connect_ex"): socket.socket.connect_ex,
        (socket.socket, "sendto"): socket.socket.sendto,
        (socket, "create_connection"): socket.create_connection,
    }

    def blocked(*args: Any, **kwargs: Any) -> Any:
        raise NetworkBlocked(
            "a network call was attempted during replay. Replay answers every request "
            "from the recorded log, so reaching the network means the agent asked for "
            "something the log does not contain. Record a new run instead, or pass "
            "block_network=False if you meant to let it through."
        )

    for target, name in originals:
        setattr(target, name, blocked)
    try:
        yield
    finally:
        for (target, name), original in originals.items():
            setattr(target, name, original)


class _Patcher:
    def __init__(self, session: Session) -> None:
        self._session = session
        self._undo: list[Callable[[], None]] = []

    def _set(self, target: Any, name: str, value: Any) -> None:
        original = getattr(target, name)
        self._undo.append(lambda: setattr(target, name, original))
        setattr(target, name, value)

    def _shadow(self, instance: Any, name: str, value: Any) -> None:
        """Override a method on one instance, leaving the class untouched."""
        self._undo.append(lambda: instance.__dict__.pop(name, None))
        setattr(instance, name, value)

    def _value(self, source: str, key: str | None, produce: Callable[[], Any]) -> Any:
        return self._session.env_value(source, key, produce)

    def _clock(self, source: str, key: str | None, real: Callable[[], Any]) -> Callable[[], Any]:
        def patched() -> Any:
            if not _caller_is_instrumented():
                return real()
            with _no_reentry() as first:
                if not first:
                    return real()
                return self._value(source, key, real)

        return patched

    def install(self) -> None:
        self._install_time()
        self._install_random()
        self._install_secrets()
        self._install_uuid()
        self._install_environ()

    def _install_time(self) -> None:
        for name, source, key in (
            ("time", "clock", None),
            ("time_ns", "clock", "ns"),
            ("monotonic", "monotonic", None),
            ("monotonic_ns", "monotonic", "ns"),
            ("perf_counter", "perf_counter", None),
            ("perf_counter_ns", "perf_counter", "ns"),
        ):
            self._set(time, name, self._clock(source, key, getattr(time, name)))

    # datetime.now / date.today are not patched: on current CPython the type is
    # immutable (`cannot set 'now' attribute`), and even a module-level class
    # swap would miss `from datetime import datetime` holders. Prefer
    # time.time() / session.clock for recordable wall time.

    def _shadow_random_instance(self, inst: random.Random) -> None:
        real_random, real_getrandbits = inst.random, inst.getrandbits

        def patched_random() -> float:
            if not _caller_is_instrumented(forwarders=_RANDOM_FORWARDERS):
                return real_random()
            with _no_reentry() as first:
                if not first:
                    return real_random()
                return float(self._value("random", None, real_random))

        def patched_getrandbits(k: int) -> int:
            if not _caller_is_instrumented(forwarders=_RANDOM_FORWARDERS):
                return real_getrandbits(k)
            with _no_reentry() as first:
                if not first:
                    return real_getrandbits(k)
                return int(
                    self._value("random", f"getrandbits:{k}", lambda: real_getrandbits(k))
                )

        self._shadow(inst, "random", patched_random)
        self._shadow(inst, "getrandbits", patched_getrandbits)

    def _install_random(self) -> None:
        # Every other function on the module-level generator — randint, choice,
        # shuffle, sample — is built from these two on the same instance, so
        # overriding them on the instance covers the whole module surface.
        inst = random._inst
        self._shadow_random_instance(inst)
        self._set(random, "random", inst.random)
        self._set(random, "getrandbits", inst.getrandbits)

        # Unseeded Random() and SystemRandom draw OS entropy. An explicitly
        # seeded Random is already reproducible — leave it alone (noise).
        real_init = random.Random.__init__

        def patched_init(this: random.Random, x: Any = None) -> None:
            real_init(this, x)
            if isinstance(this, random.SystemRandom) or x is None:
                self._shadow_random_instance(this)

        self._set(random.Random, "__init__", patched_init)

    def _install_secrets(self) -> None:
        # secrets is stdlib, so patching SystemRandom alone would see a stdlib
        # caller and skip recording. Intercept at the secrets surface instead.
        real_bytes = secrets.token_bytes
        real_hex = secrets.token_hex
        real_urlsafe = secrets.token_urlsafe

        def patched_token_bytes(nbytes: int | None = None) -> bytes:
            n = 32 if nbytes is None else nbytes
            if not _caller_is_instrumented(forwarders=_SECRETS_FORWARDERS):
                return real_bytes(n)
            with _no_reentry() as first:
                if not first:
                    return real_bytes(n)
                hexed = self._value(
                    "random", f"token_bytes:{n}", lambda: real_bytes(n).hex()
                )
                return bytes.fromhex(str(hexed))

        def patched_token_hex(nbytes: int | None = None) -> str:
            n = 32 if nbytes is None else nbytes
            if not _caller_is_instrumented(forwarders=_SECRETS_FORWARDERS):
                return real_hex(n)
            with _no_reentry() as first:
                if not first:
                    return real_hex(n)
                return str(self._value("random", f"token_hex:{n}", lambda: real_hex(n)))

        def patched_token_urlsafe(nbytes: int | None = None) -> str:
            n = 32 if nbytes is None else nbytes
            if not _caller_is_instrumented(forwarders=_SECRETS_FORWARDERS):
                return real_urlsafe(n)
            with _no_reentry() as first:
                if not first:
                    return real_urlsafe(n)
                return str(
                    self._value("random", f"token_urlsafe:{n}", lambda: real_urlsafe(n))
                )

        self._set(secrets, "token_bytes", patched_token_bytes)
        self._set(secrets, "token_hex", patched_token_hex)
        self._set(secrets, "token_urlsafe", patched_token_urlsafe)

    def _install_uuid(self) -> None:
        for name in ("uuid1", "uuid4"):
            real = getattr(uuid, name)

            def patched(_real: Any = real, _key: str = name) -> uuid.UUID:
                if not _caller_is_instrumented():
                    return _real()
                with _no_reentry() as first:
                    if not first:
                        return _real()
                    return uuid.UUID(str(self._value("uuid", _key, lambda: str(_real()))))

            self._set(uuid, name, patched)

    def _install_environ(self) -> None:
        environ_type = type(os.environ)
        real_getitem = environ_type.__getitem__

        def patched(this: Any, key: Any) -> Any:
            if not isinstance(key, str) or not _caller_is_instrumented(
                forwarders=_ENV_FORWARDERS
            ):
                return real_getitem(this, key)
            with _no_reentry() as first:
                if not first:
                    return real_getitem(this, key)

                def produce() -> str | None:
                    try:
                        return real_getitem(this, key)
                    except KeyError:
                        # Absence is an input too: an agent that branches on a
                        # missing variable must take the same branch on replay.
                        return None

                value = self._value("env", key, produce)
            if value is None:
                raise KeyError(key)
            return str(value)

        self._set(environ_type, "__getitem__", patched)

    def uninstall(self) -> None:
        for undo in reversed(self._undo):
            undo()
        self._undo.clear()


@contextmanager
def patch_environment(session: Session) -> Iterator[None]:
    patcher = _Patcher(session)
    try:
        patcher.install()
        yield
    finally:
        # Always undo: a mid-install failure must not leave live patches pointing
        # at a session that never opened (or that is about to close).
        patcher.uninstall()

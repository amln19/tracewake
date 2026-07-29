from __future__ import annotations

import sys

import pytest

# The suite exercises the real enforcement rather than waiving it, and the flag
# is fixed at interpreter startup, so it cannot be set from here.
if sys.flags.hash_randomization:
    raise pytest.UsageError(
        "run the suite as `PYTHONHASHSEED=0 uv run pytest`. Replay refuses to run under "
        "hash randomization, and these tests replay."
    )

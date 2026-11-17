"""One asyncio loop on a daemon thread; sync code uses `run` or `submit`."""

from __future__ import annotations

import sys

import flncore.loop as _core

sys.modules[__name__] = _core

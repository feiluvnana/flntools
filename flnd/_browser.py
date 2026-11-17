"""A browser library: async Playwright on an asyncio loop, shared tabs, scrolling."""

from __future__ import annotations

import sys

import flncore.browser as _core

sys.modules[__name__] = _core

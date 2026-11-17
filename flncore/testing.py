"""Fakes for testing: HTTP, clock, terminal, outside tools."""

from __future__ import annotations

import io

from flncore.clock import Fake as FakeClock
from flncore.files import Result as ToolResult
from flncore.files import Tool
from flncore.files import fake as fake_tools
from flncore.net import Reply, Response
from flncore.net import fake as fake_net

__all__ = ["FakeClock", "Reply", "Response", "TTY", "Tool", "ToolResult", "fake_net", "fake_tools"]


class TTY(io.StringIO):
    """A text buffer treated as a terminal (`isatty` is True)."""

    def isatty(self) -> bool:
        return True

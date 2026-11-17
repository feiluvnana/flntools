"""Run outside tools from PATH with argument lists, never a shell."""

from __future__ import annotations

import sys

import flncore.files as _core

sys.modules[__name__] = _core

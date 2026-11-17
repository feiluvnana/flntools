"""Safe file writes: whole files through locked parts, plus walking, linking and file kinds."""

from __future__ import annotations

import sys

import flncore.files as _core

sys.modules[__name__] = _core

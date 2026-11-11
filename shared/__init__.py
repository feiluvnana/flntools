"""Small helpers for scripts. Phase 1: the terminal, stdlib only."""

from shared import _cli as cli
from shared import _fmt as fmt
from shared import _prompt as prompt
from shared._log import log
from shared._style import style
from shared._theme import theme

__all__ = ["cli", "fmt", "log", "prompt", "style", "theme"]

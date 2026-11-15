"""flnd: a dead simple media downloader, and its engine API."""

__version__ = "0.1.0"

from flnd.engine import AuthInfo, Engine, Error, Report, TargetInfo, TargetReport  # noqa: E402
from flnd.source import LoginError, Range, Target, TargetError  # noqa: E402

__all__ = [
    "AuthInfo",
    "Engine",
    "Error",
    "LoginError",
    "Range",
    "Report",
    "Target",
    "TargetError",
    "TargetInfo",
    "TargetReport",
]

"""The HTTP client: curl_cffi on the run's asyncio loop, with retries and concurrency limits."""

from __future__ import annotations

import sys

import flncore.net as _core
from flnd import _html

_core.Response._html_parser = _html.parse
sys.modules[__name__] = _core

"""
Source registry for flnd.

`SOURCES` is the one list of sources. Target parsing, help, prompt mode, and cookie
detection all read it, so adding a source means writing its class and adding it here.
"""

from typing import Dict, Optional, Type

from flnd.sources.base import BaseSource
from flnd.sources.fbk import FbkSource
from flnd.sources.ins import InsSource
from flnd.sources.tik import TikSource
from flnd.sources.twt import TwtSource

# The one list of sources. Everything that names sources reads it.
SOURCES: Dict[str, Type[BaseSource]] = {
    source.code: source for source in (TikSource, FbkSource, InsSource, TwtSource)
}


def get_source(code: str) -> BaseSource:
    """An instance of the source with this code. An unknown code raises ValueError."""
    source_class = SOURCES.get(code.strip().lower())
    if source_class is None:
        raise unknown_source(code)
    return source_class()


def source_for_domain(domain: str) -> Optional[str]:
    """The code of the source a cookie domain belongs to, or None.

    `www.instagram.com` and `.instagram.com` both belong to `instagram.com`;
    `notinstagram.com` does not.
    """
    domain = domain.strip().lower().lstrip(".")
    for code, source in SOURCES.items():
        if any(domain == owned or domain.endswith("." + owned) for owned in source.domains):
            return code
    return None


def unknown_source(code: str) -> ValueError:
    return ValueError(f"unknown source '{code}'. Sources: {', '.join(SOURCES)}")

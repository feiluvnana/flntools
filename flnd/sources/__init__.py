"""
Source registry for flnd.

Maps the 4 supported 3-letter codes to their respective autonomous source classes:
  - tik: TikSource
  - fbk: FbkSource
  - ins: InsSource
  - twt: TwtSource
"""

from typing import Dict, Type
from flnd.sources.base import BaseSource
from flnd.sources.fbk import FbkSource
from flnd.sources.ins import InsSource
from flnd.sources.tik import TikSource
from flnd.sources.twt import TwtSource

# Registry of source handlers
SOURCES: Dict[str, Type[BaseSource]] = {
    "tik": TikSource,
    "fbk": FbkSource,
    "ins": InsSource,
    "twt": TwtSource,
}


def get_source(code: str) -> BaseSource:
    """
    Get an instantiated source handler by its 3-letter code.

    Raises:
        KeyError: if the code is not one of 'tik', 'fbk', 'ins', 'twt'.
    """
    source_class = SOURCES.get(code.lower())
    if not source_class:
        raise KeyError(f"Unsupported source code '{code}'. Supported: {', '.join(sorted(SOURCES.keys()))}")
    return source_class()

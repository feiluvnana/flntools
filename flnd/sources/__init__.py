"""The source registry: `SOURCES` maps each built-in source's code to its class, loaded lazily.

Who reads it: the engine and the library (to resolve and run targets), login and logout. The
CLI's help and `flnd sources` read `flnd.sources._manifest` instead, which holds the same facts
as plain data, so they import no source at all.

How it loads:
1. The codes, their order and where each class lives come from `_manifest.SOURCES`, so listing
   the codes (`list(SOURCES)`, `code in SOURCES`) imports nothing.
2. `SOURCES["ins"]` imports `flnd.sources.ins` alone, on its first use, and keeps the class.
3. Anything that needs every class (`SOURCES.values()`, `.items()`) imports them all.

Adding a built-in source means writing its module, adding its class to `BUILT_IN` below, and
running `make manifest`; `tests/test_manifest.py` fails until the manifest matches.
"""

from __future__ import annotations

import importlib
from collections.abc import Iterator, MutableMapping
from typing import TYPE_CHECKING

from flnd.sources import _manifest

if TYPE_CHECKING:
    from flnd.source import Source

__all__ = ["BUILT_IN", "SOURCES", "Registry"]

# The built-in sources, by module and class name, in the order help lists them. The manifest
# generator imports these; at run time the manifest's own copy of each is used.
BUILT_IN = (
    ("flnd.sources.tik", "TikTok"),
    ("flnd.sources.fbk", "Facebook"),
    ("flnd.sources.ins", "Instagram"),
    ("flnd.sources.twt", "Twitter"),
    ("flnd.sources.loi", "Loibus"),
    ("flnd.sources.tlg", "Telegram"),
    ("flnd.sources.mht", "Mimi"),
)


class Registry(MutableMapping[str, "type[Source]"]):
    """Source classes by code, each imported the first time it is asked for.

    It is a mutable mapping, so a test may add a source with `mock.patch.dict`. A source set
    here is kept as it is; one never set is imported from the module the manifest names.

    Args:
        places: each lazily known code's module and class name, in order.

    Example:
        >>> registry = Registry({"ins": ("flnd.sources.ins", "Instagram")})
        >>> list(registry), registry["ins"].__name__
        (['ins'], 'Instagram')
    """

    def __init__(self, places: dict[str, tuple[str, str]]) -> None:
        self._places = dict(places)
        self._loaded: dict[str, type[Source]] = {}
        # Every code in order: the lazy ones first, then any set later.
        self._codes: list[str] = list(places)

    def __getitem__(self, code: str) -> type[Source]:
        if code in self._loaded:
            return self._loaded[code]
        if code not in self._places:
            raise KeyError(code)
        module, name = self._places[code]
        cls = getattr(importlib.import_module(module), name)
        self._loaded[code] = cls
        return cls

    def __setitem__(self, code: str, source: type[Source]) -> None:
        if code not in self._codes:
            self._codes.append(code)
        self._loaded[code] = source

    def __delitem__(self, code: str) -> None:
        if code not in self._codes:
            raise KeyError(code)
        self._codes.remove(code)
        self._loaded.pop(code, None)
        self._places.pop(code, None)

    def __iter__(self) -> Iterator[str]:
        return iter(list(self._codes))

    def __len__(self) -> int:
        return len(self._codes)

    def __contains__(self, code: object) -> bool:
        return code in self._codes

    def copy(self) -> dict[str, type[Source]]:
        """Every source as a plain dictionary, importing those not loaded yet."""
        return dict(self.items())

    def __repr__(self) -> str:
        return f"<flnd.sources.Registry {', '.join(self._codes)}>"


SOURCES: Registry = Registry({
    code: (str(entry["module"]), str(entry["cls"])) for code, entry in _manifest.SOURCES.items()
})

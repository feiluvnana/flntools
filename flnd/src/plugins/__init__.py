"""The built-in plugins on plugin API 2, one module per site.

SPEC.md#writing-a-plugin explains it."""

from __future__ import annotations

import importlib
from collections.abc import Iterator, MutableMapping
from typing import TYPE_CHECKING

from flnd.plugins import _manifest

if TYPE_CHECKING:
    from flnd.plugin import Plugin

__all__ = ["BUILT_IN", "PLUGINS", "Registry"]

# The built-in plugins, by module and class name, in the order help lists them and links are
# matched: `web` reads any link, so it comes last. The manifest generator imports these; at run
# time the manifest's own copy of each is used.
BUILT_IN = (
    ("flnd.plugins.tik", "TikTok"),
    ("flnd.plugins.fbk", "Facebook"),
    ("flnd.plugins.ins", "Instagram"),
    ("flnd.plugins.twt", "Twitter"),
    ("flnd.plugins.loi", "Loibus"),
    ("flnd.plugins.tlg", "Telegram"),
    ("flnd.plugins.mht", "Mimi"),
    ("flnd.plugins.web", "Web"),
)


class Registry(MutableMapping[str, "type[Plugin]"]):
    """Built-in plugin classes by code, each imported the first time it is asked for.

    It is a mutable mapping, so a test may add a plugin with `mock.patch.dict`. A plugin set
    here is kept as it is; a built-in never set is imported from the module the manifest
    names.

    Args:
        places: each built-in code's module and class name, in order.

    Example:
        >>> registry = Registry({"ins": ("flnd.plugins.ins", "Instagram")})
        >>> list(registry), registry["ins"].__name__
        (['ins'], 'Instagram')
    """

    def __init__(self, places: dict[str, tuple[str, str]]) -> None:
        self._places = dict(places)
        self._loaded: dict[str, type[Plugin]] = {}
        self._codes: list[str] = list(places)

    def __getitem__(self, code: str) -> type[Plugin]:
        if code in self._loaded:
            return self._loaded[code]
        if code not in self._places:
            raise KeyError(code)
        module, name = self._places[code]
        cls = getattr(importlib.import_module(module), name)
        self._loaded[code] = cls
        return cls

    def __setitem__(self, code: str, source: type[Plugin]) -> None:
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

    def copy(self) -> dict[str, type[Plugin]]:
        """Every built-in plugin as a plain dictionary, importing those not loaded yet."""
        return dict(self.items())

    def __repr__(self) -> str:
        return f"<flnd.plugins.Registry {', '.join(self._codes)}>"


PLUGINS: Registry = Registry({
    code: (str(entry["module"]), str(entry["cls"])) for code, entry in _manifest.PLUGINS.items()
})

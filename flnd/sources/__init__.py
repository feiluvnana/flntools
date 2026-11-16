"""The source registry: `SOURCES` maps each source's code to its class, loaded lazily.

It holds the built-in sources and the plugins (`flnd._plugins`): installed packages' and the
user's plugin folder's. Who reads it: the engine and the library (to resolve and run targets),
login and logout. The CLI's help reads `flnd.sources._manifest` instead, which holds the
built-ins' facts as plain data, so `--help` imports no source and looks for no plugin.

How it loads:
1. The built-ins' codes, their order and where each class lives come from `_manifest.SOURCES`,
   so a built-in code (`"ins" in SOURCES`, `SOURCES["ins"]`) imports that source alone and
   never looks for plugins.
2. A code that isn't built in makes the registry look for plugins once (`_plugins.discover`,
   which reads names only), then import that one plugin (`_plugins.load`).
3. Iterating (`list(SOURCES)`, `.items()`) gives the built-ins first, without importing them,
   then imports every plugin and gives the ones that work. A broken plugin is left out, with a
   warning; `SOURCES.plugins` lists it with its reason.

Adding a built-in source means writing its module, adding its class to `BUILT_IN` below, and
running `make manifest`; `tests/test_manifest.py` fails until the manifest matches.
"""

from __future__ import annotations

import importlib
from collections.abc import Callable, Iterator, MutableMapping
from typing import TYPE_CHECKING

from flnd.sources import _manifest

if TYPE_CHECKING:
    from flnd._plugins import Plugin
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
    here is kept as it is; a built-in never set is imported from the module the manifest
    names, and a plugin from where `find` found it.

    Args:
        places: each built-in code's module and class name, in order.
        find: what finds the plugins, given the codes taken (`flnd._plugins.discover`); called
            once, the first time a code that isn't built in is needed. None finds none.

    Example:
        >>> registry = Registry({"ins": ("flnd.sources.ins", "Instagram")})
        >>> list(registry), registry["ins"].__name__
        (['ins'], 'Instagram')
    """

    def __init__(self, places: dict[str, tuple[str, str]], *,
                 find: Callable[[dict[str, str]], list[Plugin]] | None = None) -> None:
        self._places = dict(places)
        self._loaded: dict[str, type[Source]] = {}
        # Every code that is surely there, in order: the built-ins, then any set later. A
        # plugin's code joins once it has loaded.
        self._codes: list[str] = list(places)
        self._find = find
        # Every plugin found, in order, and those not refused as they were found, by code;
        # empty until `_discover` runs.
        self._found: list[Plugin] = []
        self._plugins: dict[str, Plugin] = {}

    def __getitem__(self, code: str) -> type[Source]:
        if code in self._loaded:
            return self._loaded[code]
        if code in self._places:
            module, name = self._places[code]
            cls = getattr(importlib.import_module(module), name)
            self._loaded[code] = cls
            return cls
        found = self._plugin(code)
        if found is None:
            raise KeyError(code)
        return found

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
        self._plugins.pop(code, None)

    def __iter__(self) -> Iterator[str]:
        # A generator, so a caller that stops at a built-in never looks for plugins.
        known = list(self._codes)
        yield from known
        self._discover()
        for code in list(self._plugins):
            if code not in known and self._plugin(code) is not None:
                yield code

    def __len__(self) -> int:
        return sum(1 for _ in self)

    def __contains__(self, code: object) -> bool:
        if code in self._codes:
            return True
        return isinstance(code, str) and self._plugin(code) is not None

    def copy(self) -> dict[str, type[Source]]:
        """Every working source as a plain dictionary, importing those not loaded yet."""
        return dict(self.items())

    @property
    def plugins(self) -> list[Plugin]:
        """Every plugin found, each imported, in order; a disabled one's `error` says why."""
        self._discover()
        for code in list(self._plugins):
            self._plugin(code)
        return list(self._found)

    def plugin(self, cls: type[Source]) -> Plugin | None:
        """The plugin that gave `cls`, if a plugin did; it never looks for plugins itself.

        Args:
            cls: a source class.

        Returns:
            The plugin, loaded; None for a built-in, a class set by hand, or before any plugin
            was looked for.
        """
        return next((plugin for plugin in self._found if plugin.cls is cls), None)

    def _discover(self) -> None:
        """Find the plugins, once: their codes and where they are, importing none."""
        if self._find is None:
            return
        find, self._find = self._find, None
        taken = {code: f"the built-in {_manifest.SOURCES.get(code, {}).get('name', code)}"
                 for code in self._codes}
        self._found = find(taken)
        # A plugin refused as it was found (a code taken, a bad file name) is only listed.
        self._plugins = {plugin.code: plugin for plugin in self._found if not plugin.error}

    def _plugin(self, code: str) -> type[Source] | None:
        """A plugin's class, imported on first use.

        Args:
            code: the plugin's code.

        Returns:
            The class; None when no plugin has the code, or it is disabled.
        """
        from flnd import _plugins

        self._discover()
        plugin = self._plugins.get(code)
        if plugin is None or plugin.error:
            return None
        cls = _plugins.load(plugin)
        if cls is not None and code not in self._loaded:
            self._loaded[code] = cls
            self._codes.append(code)
        return cls

    def __repr__(self) -> str:
        return f"<flnd.sources.Registry {', '.join(self._codes)}>"


def _find(taken: dict[str, str]) -> list[Plugin]:
    """The plugins, found by `flnd._plugins` only once a code that isn't built in is needed."""
    from flnd import _plugins

    return _plugins.discover(taken)


SOURCES: Registry = Registry({
    code: (str(entry["module"]), str(entry["cls"])) for code, entry in _manifest.SOURCES.items()
}, find=_find)

"""The built-in plugins on plugin API 2, one module per site, and the registry: `PLUGINS` maps
each plugin's code to its class, loaded lazily.

src/docs/writing-a-plugin explains it.

It holds the built-in plugins and the installed ones (`flnd.engine.plugins`): installed packages'
and the user's plugin folder's. Who reads it: the engine and the library (to resolve and run
targets), login and logout. The CLI's help reads `flnd.plugins._manifest` instead, which holds the
built-ins' facts as plain data, so `--help` imports no plugin and looks for no installed one.

How it loads:
1. The built-ins' codes, their order and where each class lives come from `_manifest.PLUGINS`,
   so a built-in code (`"ins" in PLUGINS`, `PLUGINS["ins"]`) imports that plugin alone and
   never looks for installed ones.
2. A code that isn't built in makes the registry look for plugins once (`plugins.discover`,
   which reads names only), then import that one plugin (`plugins.load`).
3. Iterating (`list(SOURCES)`, `.items()`) gives the built-ins first, without importing them,
   then imports every plugin and gives the ones that work. A broken plugin is left out, with a
   warning; `PLUGINS.plugins` lists it with its reason.

Adding a built-in plugin means writing its module, adding its class to `BUILT_IN` below, and
running `make manifest`; `tests/test_manifest.py` fails until the manifest matches.
"""

from __future__ import annotations

import importlib
from collections.abc import Callable, Iterator, MutableMapping
from typing import TYPE_CHECKING, Any

from flnd.plugins import _manifest

if TYPE_CHECKING:
    import flnd.engine.plugins
    from flnd.plugin import Plugin

__all__ = ["BUILT_IN", "PLUGINS", "Registry", "facts"]

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


def facts(cls: type[Plugin]) -> dict[str, Any]:
    """What resolving and help read of a plugin without importing it, as plain data: its name,
    domains, routes (patterns, `single`, `discovers`, `shown`, categories with `own`) and
    `unsupported`. The manifest and `plugins.json` hold it."""
    return {
        "name": cls.name, "domains": list(cls.domains),
        "routes": [{"name": known.kind, "single": known.single, "discovers": known.discovers,
                    "shown": known.shown,
                    "patterns": [list(item) if isinstance(item, tuple) else item
                                 for item in known.patterns],
                    "categories": [{"name": category.name, "own": category.own}
                                   for category in known.categories]}
                   for known in cls.routes],
        "unsupported": dict(cls.unsupported),
    }


class Registry(MutableMapping[str, "type[Plugin]"]):
    """Plugin classes by code, each imported the first time it is asked for.

    It is a mutable mapping, so a test may add a plugin with `mock.patch.dict`. A plugin set
    here is kept as it is; a built-in never set is imported from the module the manifest
    names, and an installed one from where `find` found it.

    Args:
        places: each built-in code's module and class name, in order.
        find: what finds the plugins, given the codes taken (`flnd.engine.plugins.discover`); called
            once, the first time a code that isn't built in is needed. None finds none.

    Example:
        >>> registry = Registry({"ins": ("flnd.plugins.ins", "Instagram")})
        >>> list(registry), registry["ins"].__name__
        (['ins'], 'Instagram')
    """

    def __init__(self, places: dict[str, tuple[str, str]], *,
                 find: Callable[[dict[str, str]], list[flnd.engine.plugins.Plugin]] | None = None
                 ) -> None:
        self._places = dict(places)
        self._loaded: dict[str, type[Plugin]] = {}
        # Every code that is surely there, in order: the built-ins, then any set later. A
        # plugin's code joins once it has loaded.
        self._codes: list[str] = list(places)
        self._find = find
        # Every plugin found, in order, and those not refused as they were found, by code;
        # empty until `_discover` runs.
        self._found: list[flnd.engine.plugins.Plugin] = []
        self._plugins: dict[str, flnd.engine.plugins.Plugin] = {}

    def __getitem__(self, code: str) -> type[Plugin]:
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

    def copy(self) -> dict[str, type[Plugin]]:
        """Every working source as a plain dictionary, importing those not loaded yet."""
        return dict(self.items())

    @property
    def plugins(self) -> list[flnd.engine.plugins.Plugin]:
        """Every plugin found, each imported, in order; a disabled one's `error` says why."""
        self._discover()
        for code in list(self._plugins):
            self._plugin(code)
        return list(self._found)

    def plugin(self, cls: type[Plugin]) -> flnd.engine.plugins.Plugin | None:
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
        taken = {code: f"the built-in {_manifest.PLUGINS.get(code, {}).get('name', code)}"
                 for code in self._codes}
        self._found = find(taken)
        # A plugin refused as it was found (a code taken, a bad file name) is only listed.
        self._plugins = {plugin.code: plugin for plugin in self._found if not plugin.error}

    def _plugin(self, code: str) -> type[Plugin] | None:
        """A plugin's class, imported on first use.

        Args:
            code: the plugin's code.

        Returns:
            The class; None when no plugin has the code, or it is disabled.
        """
        from flnd.engine import plugins

        self._discover()
        plugin = self._plugins.get(code)
        if plugin is None or plugin.error:
            return None
        cls = plugins.load(plugin)
        if cls is not None and code not in self._loaded:
            self._loaded[code] = cls
            self._codes.append(code)
        return cls

    def __repr__(self) -> str:
        return f"<flnd.plugins.Registry {', '.join(self._codes)}>"


def _find(taken: dict[str, str]) -> list[flnd.engine.plugins.Plugin]:
    """The plugins, found by `flnd.engine.plugins` only once a code that isn't built in is
    needed."""
    from flnd.engine import plugins

    return plugins.discover(taken)


PLUGINS: Registry = Registry({
    code: (str(entry["module"]), str(entry["cls"])) for code, entry in _manifest.PLUGINS.items()
}, find=_find)

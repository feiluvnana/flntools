"""The plugin loader: a code to its class, imported the first time it is asked for.

SPEC.md#for-developers explains it."""

from __future__ import annotations

import importlib
from collections.abc import Iterator, Mapping, MutableMapping

from flnd.plugin import Plugin
from flnd.plugins import TABLE

__all__ = ["PLUGINS", "Registry", "domains"]


class Registry(MutableMapping[str, "type[Plugin]"]):
    """The built-in plugin classes by code, in `flnd.plugins.TABLE`'s order.

    A class is imported by its first `[code]`; knowing the codes or their domains imports none.
    It is a mutable mapping, so a test may add a plugin with `mock.patch.dict`.
    """

    def __init__(self) -> None:
        self._codes: list[str] = [entry.code for entry in TABLE]
        self._loaded: dict[str, type[Plugin]] = {}

    def __getitem__(self, code: str) -> type[Plugin]:
        if code not in self._loaded:
            entry = next((entry for entry in TABLE if entry.code == code), None)
            if entry is None or code not in self._codes:
                raise KeyError(code)
            self._loaded[code] = getattr(importlib.import_module(entry.module), entry.cls)
        return self._loaded[code]

    def __setitem__(self, code: str, cls: type[Plugin]) -> None:
        if code not in self._codes:
            self._codes.append(code)
        self._loaded[code] = cls

    def __delitem__(self, code: str) -> None:
        if code not in self._codes:
            raise KeyError(code)
        self._codes.remove(code)
        self._loaded.pop(code, None)

    def __iter__(self) -> Iterator[str]:
        return iter(list(self._codes))

    def __len__(self) -> int:
        return len(self._codes)

    def __contains__(self, code: object) -> bool:
        return code in self._codes

    def domains(self, code: str) -> tuple[str, ...]:
        """The domains of a plugin, from its class when it is loaded, else from the table."""
        if code in self._loaded:
            return tuple(self._loaded[code].domains)
        return next((entry.domains for entry in TABLE if entry.code == code), ())


PLUGINS = Registry()


def domains(sources: Mapping[str, type[Plugin]], code: str) -> tuple[str, ...]:
    """The domains of `code` in `sources`, importing no plugin a `Registry` has not loaded."""
    if isinstance(sources, Registry):
        return sources.domains(code)
    return tuple(sources[code].domains) if code in sources else ()

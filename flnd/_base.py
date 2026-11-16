"""The five error bases that every private module of flnd subclasses.

Who calls it: each private module (`_unpack`, `_browser`, `_http`, ...) defines its own `Error`
under one of these, so a caller can catch the whole family at once.
What it calls: nothing. It imports only the stdlib, so importing it costs nothing.

The bases are: `Error` (the root), `UnsafeError` and `BrokenError` (both also `ValueError`, so
code that expects a ValueError still catches them), `RefusedError` (a remote end or a person said
no) and `MissingError` (a program, extra or browser this machine lacks).

A later step of the rebuild replaces these five with the one hierarchy of `flnd.errors`. Until
then, a caller picks how wide to catch:

    try:
        archive.unpack("photos.zip", "photos/")
    except UnsafeError:
        ...

Rule: a new private error subclasses one of these, never a bare `Exception`.
"""


class Error(Exception):
    """The base of every error the private modules raise; each module's `Error` subclasses it."""


class UnsafeError(Error, ValueError):
    """Input refused because trusting it could do harm: traversal, bomb, or exploit.

    A ValueError, so callers expecting ValueError still catch it.
    """


class BrokenError(Error, ValueError):
    """Input that is damaged or isn't what it claims: a cut-off video, a bad archive.

    A ValueError, so callers expecting ValueError still catch it.
    """


class RefusedError(Error):
    """A remote end or a person said no: an HTTP 4xx, a wrong password."""


class MissingError(Error, RuntimeError):
    """A program, an extra or a browser this machine lacks: ffmpeg, unrar, Chrome."""

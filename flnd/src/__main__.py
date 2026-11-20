"""`python -m flnd`: the command line, as the `flnd` script runs it.

SPEC.md#commands-and-flags explains it.
"""

from flnd.cli import main

if __name__ == "__main__":
    raise SystemExit(main())

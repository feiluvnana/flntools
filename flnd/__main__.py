"""
Entrypoint for python -m flnd
"""

import sys
from flnd.cli import main

if __name__ == "__main__":
    sys.exit(main())

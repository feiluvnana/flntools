"""Download targets with the flnd library, printing one line per file saved or failed.

    python examples/download.py ins:example_user https://www.tiktok.com/@example_user
    python examples/download.py exm:example_user --home ~/Media/flnd --only posts

It does what `flnd download` does, in a few lines of the library: open a home, start a run,
read its events, and exit 1 when something failed. Plugins load as they do for the CLI, from
`$FLND_SOURCES` (else `~/.config/flnd/sources/`) and installed packages.
"""

from __future__ import annotations

import argparse
import sys

import flnd


def main(argv: list[str] | None = None) -> int:
    """Download each target given on the command line; 0 when nothing failed, else 1."""
    parser = argparse.ArgumentParser(description="Download targets with the flnd library.")
    parser.add_argument("targets", nargs="+", help="ins:example_user, a link, or exm:example_user")
    parser.add_argument("--home", help="the home folder (default: $FLND_HOME, else ./.flnd)")
    parser.add_argument("--only", help="only these categories, such as posts,reels")
    args = parser.parse_args(argv)
    with flnd.open(args.home) as fl:
        run = fl.download(args.targets, only=args.only)
        for event in run:
            if isinstance(event, flnd.Saved):
                print(f"saved   {event.target}  {event.path.name}")
            elif isinstance(event, flnd.Failed):
                print(f"failed  {event.target}  {event.error}", file=sys.stderr)
        report = run.report
    print(f"{report.saved} saved, {report.skipped} skipped, {len(report.failed)} failed")
    return 1 if report.failed else 0


if __name__ == "__main__":
    sys.exit(main())

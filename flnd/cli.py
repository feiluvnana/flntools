"""
CLI entry point for flnd.

Usage:
  flnd [targets...]
  python -m flnd [targets...]

Examples:
  flnd ins:example_user
  flnd tik:example_creator
  flnd twt:example_handle
  flnd fbk:example.page
  flnd ins:example_user twt:example_handle --limit 5
"""

import argparse
import sys
from pathlib import Path
from typing import List, Optional

from flnd.sources import get_source
from flnd.target import parse_target


def create_parser() -> argparse.ArgumentParser:
    """Create command-line argument parser."""
    parser = argparse.ArgumentParser(
        prog="flnd",
        description="Dead simple media downloader for TikTok, Facebook, Instagram, and Twitter/X.",
        epilog=(
            "Target format:\n"
            "  <source>:<want to download>\n"
            "  Supported sources: tik, fbk, ins, twt\n\n"
            "Examples:\n"
            "  flnd ins:example_user\n"
            "  flnd tik:example_creator\n"
            "  flnd twt:example_handle\n"
            "  flnd fbk:example.page\n"
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )

    parser.add_argument(
        "targets",
        nargs="*",
        help="One or more targets in format <source>:<value> (e.g. ins:example_user, twt:example_handle)",
    )

    parser.add_argument(
        "-o",
        "--output",
        type=Path,
        default=Path(".flnd/downloads"),
        help="Root download directory (default: .flnd/downloads)",
    )

    parser.add_argument(
        "-l",
        "--limit",
        type=int,
        default=None,
        help="Limit number of items to download per target (default: unlimited)",
    )

    parser.add_argument(
        "--headless",
        action=argparse.BooleanOptionalAction,
        default=False,
        help="Run browser automation in headless mode (default: False, use --headless to hide)",
    )

    return parser


def main(args: Optional[List[str]] = None) -> int:
    """Main CLI execution flow."""
    parser = create_parser()
    parsed_args = parser.parse_args(args)

    if not parsed_args.targets:
        parser.print_help()
        return 0

    download_dir: Path = parsed_args.output.resolve()
    limit: Optional[int] = parsed_args.limit
    headless: bool = parsed_args.headless

    print(f"==================================================")
    print(f" flnd - Media Downloader (tik, fbk, ins, twt)")
    print(f" Storage: {download_dir}")
    print(f"==================================================")

    # 1. Parse all targets
    valid_targets = []
    for raw in parsed_args.targets:
        try:
            target = parse_target(raw)
            valid_targets.append(target)
        except ValueError as err:
            print(f"[!] Error: {err}", file=sys.stderr)
            return 1

    # 2. Dispatch each target to its autonomous source handler
    total_downloaded = 0
    for target in valid_targets:
        try:
            source_handler = get_source(target.source)
            count = source_handler.download(
                target=target,
                download_dir=download_dir,
                limit=limit,
                headless=headless,
            )
            total_downloaded += count
        except Exception as e:
            print(f"[!] Error processing {target}: {e}", file=sys.stderr)

    print(f"\n==================================================")
    print(f" Complete! Total files downloaded across all targets: {total_downloaded}")
    print(f"==================================================")
    return 0


if __name__ == "__main__":
    sys.exit(main())

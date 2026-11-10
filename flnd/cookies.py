"""
Cookie handling and Netscape cookies.txt parser for flnd.

Supports reading Netscape format (standard cookies.txt) and JSON format.
Cookies are located under `.flnd/cookies/<source>.txt` (or .json).
"""

import json
from pathlib import Path
from typing import Any, Dict, List, Optional


def find_cookie_file(source: str, custom_dir: Optional[Path] = None) -> Optional[Path]:
    """
    Search for a cookie file for the specified source in standard directories:
      1. custom_dir (if provided)
      2. .flnd/cookies/<source>.txt or .json (relative to current directory)
      3. E:/flntools/.flnd/cookies/<source>.txt or .json
      4. E:/.flnd/cookies/<source>.txt or .json
    """
    candidates = []
    if custom_dir:
        candidates.extend([custom_dir / f"{source}.txt", custom_dir / f"{source}.json"])

    # Standard locations
    candidates.extend([
        Path(".flnd/cookies") / f"{source}.txt",
        Path(".flnd/cookies") / f"{source}.json",
        Path("E:/flntools/.flnd/cookies") / f"{source}.txt",
        Path("E:/flntools/.flnd/cookies") / f"{source}.json",
        Path("E:/.flnd/cookies") / f"{source}.txt",
        Path("E:/.flnd/cookies") / f"{source}.json",
    ])

    for path in candidates:
        if path.is_file():
            return path.resolve()

    return None


def parse_netscape_cookies(filepath: Path) -> List[Dict[str, Any]]:
    """
    Parse a Netscape format cookies.txt file into a list of cookie dictionaries
    suitable for Playwright and HTTPX.

    Netscape format line format:
      domain  include_subdomains  path  is_secure  expires  name  value
    """
    cookies: List[Dict[str, Any]] = []

    with open(filepath, "r", encoding="utf-8", errors="replace") as f:
        for line in f:
            line = line.strip()
            # Skip blank lines and comments (except special headers)
            if not line or line.startswith("#"):
                continue

            parts = line.split("\t")
            if len(parts) >= 7:
                domain, subdomains, path, secure, expires, name, value = parts[:7]

                try:
                    expires_int = int(expires)
                except ValueError:
                    expires_int = 0

                cookie_dict = {
                    "name": name,
                    "value": value,
                    "domain": domain,
                    "path": path,
                    "expires": expires_int,
                    "httpOnly": False,
                    "secure": secure.upper() == "TRUE",
                }
                cookies.append(cookie_dict)

    return cookies


def parse_json_cookies(filepath: Path) -> List[Dict[str, Any]]:
    """Parse JSON cookie format exported by browser extensions."""
    with open(filepath, "r", encoding="utf-8") as f:
        data = json.load(f)
        if isinstance(data, list):
            return data
        return []


def load_cookies(source: str, custom_dir: Optional[Path] = None) -> List[Dict[str, Any]]:
    """
    Load cookies for a given source ('tik', 'fbk', 'ins', 'twt').
    Returns an empty list if no cookie file is found.
    """
    cookie_path = find_cookie_file(source, custom_dir)
    if not cookie_path:
        return []

    if cookie_path.suffix.lower() == ".json":
        return parse_json_cookies(cookie_path)
    return parse_netscape_cookies(cookie_path)


def cookies_to_dict(cookies: List[Dict[str, Any]]) -> Dict[str, str]:
    """Convert cookie list to a simple {name: value} dictionary for HTTP headers."""
    return {c["name"]: c["value"] for c in cookies if "name" in c and "value" in c}


def prompt_missing_cookies(source: str) -> None:
    """Print an actionable message explaining where the user should put their cookie file."""
    print(f"\n[!] Missing cookies for source '{source}'.")
    print(f"    Please place your Netscape cookies.txt file at:")
    print(f"    -> .flnd/cookies/{source}.txt")
    print(f"    -> or E:/flntools/.flnd/cookies/{source}.txt\n")

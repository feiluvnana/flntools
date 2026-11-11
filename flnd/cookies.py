"""
Cookie handling for flnd: Netscape cookies.txt and JSON, reading, storing, and asking.

Cookies are located under `.flnd/cookies/<source>.txt` (or .json).
"""

import json
import os
import tempfile
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from shared import log, prompt, theme

COOKIE_DIR = Path(".flnd/cookies")


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
        COOKIE_DIR / f"{source}.txt",
        COOKIE_DIR / f"{source}.json",
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
    """Parse a Netscape format cookies.txt file. See `parse_netscape_text`."""
    with open(filepath, "r", encoding="utf-8", errors="replace") as f:
        return parse_netscape_text(f.read())


def parse_netscape_text(text: str) -> List[Dict[str, Any]]:
    """
    Parse Netscape cookies.txt text into a list of cookie dictionaries
    suitable for Playwright and HTTPX.

    Netscape format line format:
      domain  include_subdomains  path  is_secure  expires  name  value
    """
    cookies: List[Dict[str, Any]] = []
    for line in text.splitlines():
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

            cookies.append({
                "name": name,
                "value": value,
                "domain": domain,
                "path": path,
                "expires": expires_int,
                "httpOnly": False,
                "secure": secure.upper() == "TRUE",
            })
    return cookies


def parse_json_cookies(filepath: Path) -> List[Dict[str, Any]]:
    """Parse JSON cookie format exported by browser extensions."""
    with open(filepath, "r", encoding="utf-8") as f:
        return parse_json_text(f.read())


def parse_json_text(text: str) -> List[Dict[str, Any]]:
    """Parse the JSON cookie list browser extensions export. Anything but a list gives no cookies."""
    try:
        data = json.loads(text)
    except json.JSONDecodeError as err:
        raise ValueError(f"cookie text is not valid JSON: {err}") from None
    if isinstance(data, list):
        return [cookie for cookie in data if isinstance(cookie, dict)]
    return []


def load_cookies(source: str, custom_dir: Optional[Path] = None) -> List[Dict[str, Any]]:
    """
    Load cookies for a given source code.
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


def store_cookies(text: str) -> Dict[str, Tuple[int, Path]]:
    """Write each cookie in text to `.flnd/cookies/<source>.txt` for the source its domain belongs to.

    Returns {source code: (cookie count, file written)}. Raises ValueError when the text
    holds no cookies, or no cookie for any source; nothing is written then.
    """
    by_source, skipped = sort_cookies(text)
    if skipped:
        log.info("skipped {n} cookies for other sites", n=skipped)
    COOKIE_DIR.mkdir(parents=True, exist_ok=True)
    saved = {}
    for code, cookies in by_source.items():
        path = COOKIE_DIR / f"{code}.txt"
        write_private(path, write_netscape(cookies))
        # Only one cookie file per source, so an old JSON file cannot shadow the new one.
        (COOKIE_DIR / f"{code}.json").unlink(missing_ok=True)
        saved[code] = (len(cookies), path)
    return saved


def write_private(path: Path, text: str) -> None:
    """Write text to path readable only by its owner, replacing any old file in one step."""
    # mkstemp creates the file with mode 0600, and the rename means a reader never sees half a file.
    handle, temp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.")
    try:
        with os.fdopen(handle, "w", encoding="utf-8") as file:
            file.write(text)
        os.replace(temp, path)
    except BaseException:
        Path(temp).unlink(missing_ok=True)
        raise


def sort_cookies(text: str) -> Tuple[Dict[str, List[Dict[str, Any]]], int]:
    """The cookies in text grouped by source code, and how many belong to no source."""
    # flnd.sources imports this module through each source, so the registry is imported here.
    from flnd.sources import SOURCES, source_for_domain

    stripped = text.lstrip()
    cookies = parse_json_text(text) if stripped[:1] in ("[", "{") else parse_netscape_text(text)
    if not cookies:
        raise ValueError("no cookies found")
    by_source: Dict[str, List[Dict[str, Any]]] = {}
    skipped = 0
    for cookie in cookies:
        code = source_for_domain(str(cookie.get("domain", "")))
        if code is None:
            skipped += 1
        else:
            by_source.setdefault(code, []).append(cookie)
    if not by_source:
        raise ValueError(f"no cookies for any source ({', '.join(SOURCES)})")
    return by_source, skipped


def write_netscape(cookies: List[Dict[str, Any]]) -> str:
    """Cookies as Netscape cookies.txt text, seven tab-separated fields per line."""
    lines = ["# Netscape HTTP Cookie File"]
    for cookie in cookies:
        domain = str(cookie.get("domain", ""))
        # Netscape files name it "expires"; JSON exports name it "expirationDate".
        try:
            expiry = int(float(cookie.get("expires", cookie.get("expirationDate")) or 0))
        except (TypeError, ValueError):
            expiry = 0
        lines.append("\t".join([
            domain,
            "TRUE" if domain.startswith(".") else "FALSE",
            str(cookie.get("path") or "/"),
            "TRUE" if cookie.get("secure") else "FALSE",
            str(expiry),
            str(cookie.get("name", "")),
            str(cookie.get("value", "")),
        ]))
    return "\n".join(lines) + "\n"


def ask_cookie_path() -> Path:
    """Ask for a cookie file that has cookies for at least one source."""
    return Path(prompt.input("Path to cookies.txt or .json", validate=readable_cookie_file)).expanduser()


def readable_cookie_file(raw: str) -> Optional[str]:
    """Why the file at raw cannot be stored as cookies, or None when it can."""
    path = Path(raw.strip()).expanduser()
    if not path.is_file():
        return f"no file at {raw.strip() or '(empty)'}"
    try:
        sort_cookies(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, ValueError) as err:
        return str(err)
    return None


def ask_for_cookies(source: str) -> Optional[Path]:
    """Offer to set a cookie file for a source that has none. Returns the stored file, or None."""
    from flnd.sources import SOURCES

    name = SOURCES[source].name
    expected = COOKIE_DIR / f"{source}.txt"
    if not theme.screen.interactive:
        log.error("No cookies for {name}", name=name)
        log.info("Put a Netscape cookies.txt at {path}, or run: flnd auth set-cookie --path FILE", path=expected)
        return None
    if not prompt.confirm(f"No cookies for {name}. Set a cookie file now?", default=True):
        return None
    saved = store_cookies(ask_cookie_path().read_text(encoding="utf-8"))
    if source not in saved:
        log.warn("That file has no cookies for {name}", name=name)
        return None
    count, path = saved[source]
    log.ok("{source}: {n} cookies saved to {path}", source=source, n=count, path=path)
    return path

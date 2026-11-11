"""LOIBUS (loix.cc): one post's info, preview images and, for VIP accounts, its archives.

  - Every API request is signed with a fresh timestamp, and carries the `access-token` cookie as
    a Bearer token. Files on other hosts get neither.
  - The download links come AES-encrypted, with the site's own key; the unzip password doesn't.
  - Archives are `--from archive`, and count against the account's daily VIP quota.

Folder layout, under `.flnd/downloads/loi/<id>/<author>/<id> <title>/`:
  info.md         the post's fields, its download links and the unzip password
  images/         the preview images, 01.jpg, 02.jpg, …
  loi-<hash>.7z   the archives (--from archive), named as the server names them; --unpack
                  unpacks each into a folder of its name
"""

from __future__ import annotations

import base64
import binascii
import datetime
import secrets
from pathlib import PurePosixPath
from typing import TYPE_CHECKING, Callable, Mapping, Sequence
from urllib.parse import unquote, urlsplit

from flnd.session import Session
from flnd.sources.base import FetchError, Source
from flnd.target import Target, link, unreadable
from shared import crypto

if TYPE_CHECKING:
    import httpx

API = "https://api.moegoat.com/api/"
API_HOST = "api.moegoat.com"
SITE = "https://loix.cc/"
# The web client's signing constants, from its JavaScript.
SALT = "loibus-sec-decoy-salt-v1"
REQUEST_ID_PREFIX = "loibus-web-request-id-v1"
# Where the request id hides the digest's five characters.
DIGEST_POSITIONS = frozenset({1, 9, 19, 24, 30})
SITE_KEY = b"7811595744111520"
SITE_IV = b"RgaXNyE5aWckBwI7"
# Archives a day per VIP level; an SVIP account gets twice as many.
VIP_DAILY_LIMITS = {1: 15, 2: 25, 3: 40, 4: 60}
MARKDOWN_SPECIAL = set("\\`*_[]<>|#")


class Loibus(Source):
    """LOIBUS posts, from its signed API; it never opens a browser, which the site blocks."""

    code = "loi"
    name = "LOIBUS"
    domains = ("loix.cc", "moegoat.com")
    kinds_of_target = ("post",)
    examples = ("loi:5930", "https://loix.cc/lois/5930")
    # Without it: the info and images only, no archives.
    needs = ("access-token",)
    # Its content is naturally typed, so the types are the categories.
    categories = ("photo", "archive")
    default_categories = ("photo",)

    def headers(self, cookies: Mapping[str, str]) -> Mapping[str, str]:
        """The site's referer and origin, and the login's Bearer token, which `sign` keeps for the API only."""
        token = decode_token(cookies.get("access-token", ""))
        return {"referer": SITE, "origin": SITE.rstrip("/"), **({"authorization": f"Bearer {token}"} if token else {})}

    def sign(self, request: httpx.Request) -> httpx.Request:
        """Sign an API request afresh on every attempt; a file on another host loses the token instead."""
        if request.url.host != API_HOST:
            request.headers.pop("authorization", None)
            return request
        return sign_request(request, now_ms=_clock_ms())

    def resolve(self, text: str) -> Target:
        """A post id, or a post URL: `5930`, `https://loix.cc/lois/5930`."""
        found = link(text, self.domains)
        if found is None:
            post_id = text.strip() if text.strip().isdigit() else None
        else:
            post_id = next((post for part, post in zip(found.parts, found.parts[1:])
                            if part == "lois" and post.isdigit()), None)
        if post_id is None:
            raise unreadable(self, text)
        return Target(self.code, "post", post_id)

    def run(self, session: Session) -> None:
        post = session.fetch_json(f"{API}lois/{session.target.value}").get("data")
        if not isinstance(post, dict) or not post.get("loi_title"):
            raise FetchError(f"post {session.target.value} isn't there")
        day = posted_on(post)
        if not session.wants(date=day):
            session.info("posted {day}, outside --since and --until", day=day)
            return
        session.folder = folder_of(post)
        links = self._links(session, post) if session.wants(category="archive") else None
        session.write("info.md", info_md(post, links))
        for number, url in enumerate(post.get("images") or [], start=1):
            session.save(url, image_name(number, url), category="photo", date=day, folder=f"{session.folder}/images")
        if links is None:
            return
        queued = [session.save(mirrors, name, category="archive", date=day, password=post.get("open_key") or None)
                  for name, mirrors in group_mirrors(links)]
        if any(queued):
            count_download(session, post["id"])

    def _links(self, session: Session, post: dict) -> dict | None:
        """The post's decrypted download links, or None (with a warning) when there are none for this login."""
        if not decode_token(session.cookies.get("access-token", "")):
            session.warn("archives need a login; run flnd login loi")
            return None
        used, limit = quota(session, post["id"])
        if limit and used >= limit:
            session.warn("the daily VIP quota is used up ({used} of {limit}); archives skipped", used=used, limit=limit)
            return None
        body = session.fetch_json(f"{API}user/loi/download_v6/{post['id']}", soft=True)
        data = body.get("data") if isinstance(body, dict) else None
        if not isinstance(data, dict) or not data.get("sv_list"):
            needed = f"VIP{post['vip_requirement']}" if post.get("vip_requirement") else "a login"
            session.warn("the site gives no download links for this post ({needed} needed)", needed=needed)
            return None
        return {"servers": [(str(server.get("sv_name") or "Server"), decrypt_field(server.get("sv_url")))
                            for server in data["sv_list"] if isinstance(server, dict) and server.get("sv_url")],
                "pan": decrypt_field(data.get("bdp")), "pan_key": decrypt_field(data.get("bdp_key"))}


def posted_on(post: dict) -> datetime.date | None:
    """The day a post went up, from its `2026-11-10 01:58:16`."""
    try:
        return datetime.date.fromisoformat(str(post.get("date") or "")[:10])
    except ValueError:
        return None


def folder_of(post: dict) -> str:
    """Where a post's files go, under the target's folder: `<author>/<id> <title>`."""
    author = (post.get("category") or {}).get("name") or "misc"
    # A slash in a title is part of its name, not a folder.
    return f"{author.replace('/', '_')}/{post['id']} {post['loi_title'].replace('/', '_')}"


def image_name(number: int, url: str) -> str:
    """A preview image's name, `01.jpg`; the site's image URLs have no extension, and serve JPEG."""
    suffix = PurePosixPath(urlsplit(url).path).suffix
    return f"{number:02d}{suffix or '.jpg'}"


def group_mirrors(links: dict) -> list[tuple[str, list[str]]]:
    """Each archive's name, from its URL, and the mirror URLs that serve it, in the site's order."""
    groups: dict[str, list[str]] = {}
    for _, url in links["servers"]:
        name = unquote(PurePosixPath(urlsplit(url).path).name) or "archive.zip"
        groups.setdefault(name, []).append(url)
    return list(groups.items())


def quota(session: Session, post_id: int) -> tuple[int, int]:
    """Archives taken today and the account's daily VIP limit, 0 when unknown; a post taken today is free again."""
    taken = today_posts(session)
    user = session.fetch_json(f"{API}user/info", soft=True)
    data = user.get("data") if isinstance(user, dict) else None
    if post_id in taken or not isinstance(data, dict):
        return len(taken), 0
    limit = VIP_DAILY_LIMITS.get(int(data.get("vip_id") or 0), 0)
    return len(taken), limit * (2 if data.get("svip") == 1 else 1)


def count_download(session: Session, post_id: int) -> None:
    """Count a post against today's quota, once."""
    taken = today_posts(session)
    if post_id not in taken:
        session.state["quota"] = {"day": datetime.date.today().isoformat(), "posts": [*taken, post_id]}


def today_posts(session: Session) -> list[int]:
    """The posts whose archives were taken today; yesterday's list no longer counts."""
    record = session.state.get("quota") or {}
    return list(record.get("posts", [])) if record.get("day") == datetime.date.today().isoformat() else []


def info_md(post: dict, links: dict | None) -> str:
    """The post as Markdown: its fields, unzip password, description, download links and preview images."""
    author = post.get("category") or {}
    tags = " ".join(f"`{markdown(tag.get('name', ''))}`" for tag in post.get("tags") or [])
    rows = [
        ("ID", f"`{post['id']}`"),
        ("Author", f"[{markdown(author.get('name', ''))} (#{author.get('id', '')})]({SITE}library/loier/{author.get('id', '')})"),
        ("Date", str(post.get("date") or "")),
        ("URL", f"[{post['id']}]({SITE}lois/{post['id']})"),
        ("File Size", str(post.get("file_size") or "N/A")),
        ("VIP Requirement", f"VIP{post['vip_requirement']}" if post.get("vip_requirement") else "Free"),
        ("Media Count", f"pictures {post.get('pic_count', 0)} · videos {post.get('video_count', 0)}"),
        ("Stats", f"likes {post.get('like_count', 0)} · views {post.get('viewer_count', 0)} · "
                  f"downloads {post.get('download_count', 0)}"),
        *([("Unzip Password", f"`{markdown(post['open_key'])}`")] if post.get("open_key") else []),
        *([("Tags", tags)] if tags else []),
    ]
    lines = [f"# {markdown(post['loi_title'])}", "", "| Property | Value |", "| --- | --- |",
             *(f"| {name} | {value} |" for name, value in rows)]
    if post.get("loi_description"):
        lines += ["", "## Description", "",
                  *(f"> {line}" if line else ">" for line in post["loi_description"].splitlines())]
    if links:
        lines += ["", "## Download Sources", "",
                  *(f"- **Server [{markdown(name)}]**: [Download]({url})" for name, url in links["servers"])]
        if links["pan"]:
            key = f" (Password: `{markdown(links['pan_key'])}`)" if links["pan_key"] else ""
            lines.append(f"- **Baidu**: [Link]({links['pan']}){key}")
    if post.get("images"):
        lines += ["", "## Preview Images", "",
                  *(f"{number}. ![Preview {number}]({url})" for number, url in enumerate(post["images"], start=1))]
    return "\n".join(lines) + "\n"


def markdown(text: str) -> str:
    """Text with the characters Markdown would read escaped."""
    return "".join(f"\\{character}" if character in MARKDOWN_SPECIAL else character for character in str(text))


def sign_request(request: httpx.Request, *, now_ms: int, pick: Callable[[Sequence[str]], str] = secrets.choice,
                 noise: Callable[[int], str] = secrets.token_hex) -> httpx.Request:
    """Add LOIBUS's signature headers to an API request, for the time `now_ms`.

        sign_request(httpx.Request("GET", API + "lois/5930"), now_ms=1_700_000_000_000)

    `pick` and `noise` are the random parts, replaced in tests.
    """
    where = urlsplit(str(request.url)).path.removeprefix("/").removeprefix("api/")
    method, at = request.method.lower(), str(now_ms)
    request.headers.update({
        "x-security-challenge": "turnstile-v1",
        "x-request-id": request_id(method, where, now_ms=now_ms, pick=pick, noise=noise),
        "x-signature-timestamp": at,
        "x-request-signature": crypto.hash_sha256(f"{method}|{where}||{at}|{SALT}"),
    })
    return request


def request_id(method: str, where: str, *, now_ms: int, pick: Callable[[Sequence[str]], str],
               noise: Callable[[int], str]) -> str:
    """A UUID-shaped id: the five-minute slot and random hex, with a digest of the request hidden inside."""
    seed = format(now_ms // 300_000, "x").zfill(8)[-8:] + noise(9)[:17]
    digest = crypto.hash_sha256(f"{REQUEST_ID_PREFIX}|{seed}|{method}|{where}|{crypto.hash_sha256('{}')}")[:5]
    seeds, digests, characters = iter(seed), iter(digest), []
    for position in range(32):
        if position == 12:
            characters.append("4")
        elif position == 16:
            characters.append(pick(["8", "9", "a", "b"]))
        else:
            characters.append(next(digests if position in DIGEST_POSITIONS else seeds, "0"))
    text = "".join(characters)
    return f"{text[:8]}-{text[8:12]}-{text[12:16]}-{text[16:20]}-{text[20:]}"


def decrypt_field(text: str | None) -> str:
    """Decrypt one of the site's AES fields (a link, a password); an empty one stays empty.

    Raises `ValueError` when it doesn't decrypt: the site has changed its key.
    """
    text = (text or "").strip()
    if not text:
        return ""
    try:
        return crypto.decrypt_aes_cbc(base64.b64decode(text), SITE_KEY, iv=SITE_IV).decode("utf-8", "replace")
    except (binascii.Error, ValueError):
        raise ValueError(f"loi: can't decrypt {text[:16]!r}…; the site changed its key") from None


def decode_token(raw: str) -> str:
    """The Bearer token in an `access-token` cookie, which the site stores %-encoded with its `Bearer ` prefix."""
    return unquote(raw).removeprefix("Bearer ").removeprefix("bearer ").strip()


def _clock_ms() -> int:
    return int(datetime.datetime.now(datetime.timezone.utc).timestamp() * 1000)

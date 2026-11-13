"""LOIBUS (loix.cc): one post's info, preview images and, for VIP accounts, its archives.

The `post` route reads the post's JSON once. `photo` yields its info.md and preview images.
`archive` decrypts the download links, counts the post against the day's VIP quota, and yields
info.md again, now with the links, and the archives:

    flnd download loi:5930 --from archive

The site's quirks:
  - Every API request is signed with a fresh timestamp, and carries the `access-token` cookie as
    a Bearer token. `prepare` does both for api.moegoat.com on every attempt. A file on another
    host gets neither, and gets the site's referer and origin from its post instead.
  - The download links come AES-128-CBC encrypted, with the site's own key and IV, which its
    JavaScript holds. The unzip password comes in the clear.
  - Archives count against the account's daily VIP quota. The site says how many an account has
    taken today, and counts a post taken twice in a day once, so `state` remembers which posts
    this tool took.
  - Archives are saved as the server sends them, under the name in their URL, whatever their
    extension says; info.md's unzip password opens them with any archive tool.

Folder layout, under `.flnd/media/loi/<id>/<author>/<id> <title>/`:
  info.md         the post's fields, its download links and the unzip password
  images/         the preview images, 01.jpg, 02.jpg, …
  loi-<hash>.7z   the archives (--from archive), saved as the server sends them and named as it
                  names them

Every file's history key is its path, as it was before the categories became methods, so a file
saved then is not downloaded again.
"""

from __future__ import annotations

import base64
import binascii
import datetime
import secrets
from pathlib import PurePosixPath
from typing import TYPE_CHECKING, Any, Callable, Iterator, Mapping, Sequence
from urllib.parse import unquote, urlsplit

from flnd.source import FetchError, File, Post, Site, Source, day, target
from shared import crypto

if TYPE_CHECKING:
    import httpx

API = "https://api.moegoat.com/api/"
API_HOST = "api.moegoat.com"
SITE = "https://loix.cc/"
# What the site's own pages send with every request. Its image servers and archive mirrors give
# 403 without them, and a post gives them to its files because those hosts aren't the source's.
SITE_HEADERS = {"referer": SITE, "origin": SITE.rstrip("/")}
# The web client's signing constants, from its JavaScript.
SALT = "loibus-sec-decoy-salt-v1"
REQUEST_ID_PREFIX = "loibus-web-request-id-v1"
# Where the request id hides the digest's five characters.
DIGEST_POSITIONS = frozenset({1, 9, 19, 24, 30})
# The site's own AES-128 key and IV, from its JavaScript; `_decrypt_aes_cbc` decrypts its links with them.
SITE_KEY = b"7811595744111520"
SITE_IV = b"RgaXNyE5aWckBwI7"
# Archives a day per VIP level; an SVIP account gets twice as many.
VIP_DAILY_LIMITS = {1: 15, 2: 25, 3: 40, 4: 60}
# The characters Markdown reads as syntax in a heading, a table cell or a link; `markdown` escapes them.
MARKDOWN_SPECIAL = set("\\`*_[]<>|#")


class Loibus(Source):
    """LOIBUS posts, from its signed API; it never opens a browser, which the site blocks."""

    code = "loi"
    name = "LOIBUS"
    domains = ("loix.cc", "moegoat.com")
    # Without it: the info and images only, no archives.
    needs = ("access-token",)
    # `<author>/<id> <title>/<part>`: `part` is "images" for the previews and empty for the rest.
    folder = "{author}/{id} {title}/{part}"
    # Every file is named by its post (`File.name`), so this format is never used.
    filename = "{id}"
    # Every key is the file's path, as the history has always had it, so this never changes.
    key = ""

    @target(r"{id:\d+}", r"loix.cc/lois/{id:\d+}", r"moegoat.com/lois/{id:\d+}")
    def post(self, site: Site, id: str) -> dict[str, Any]:
        """One post, by its id: `Target("loi", "post", "5930")`, or a post's URL on either site.

        Args:
            site: the target's site.
            id: the post's id.

        Returns:
            The post's JSON, which both categories receive.

        Raises:
            FetchError: the site has no post with this id.
        """
        body = site.fetch(f"{API}lois/{id}").json
        post = body.get("data") if isinstance(body, dict) else None
        if not isinstance(post, dict) or not post.get("loi_title"):
            raise FetchError(f"post {id} isn't there")
        return post

    @post.category
    def photo(self, site: Site, post: dict[str, Any]) -> Iterator[Post]:
        """The post's info.md, without links, and its preview images.

        Args:
            site: the target's site.
            post: the post's JSON, as `post` returned it.
        """
        yield Post(post["id"], [File.text("info.md", info_md(post, None))], date=post.get("date"), **fields_of(post))
        if post.get("images"):
            files = [File(url, name=image_name(number, url)) for number, url in enumerate(post["images"], start=1)]
            yield Post(post["id"], files, date=post.get("date"), headers=SITE_HEADERS, part="images",
                       **fields_of(post))

    @post.category
    def archive(self, site: Site, post: dict[str, Any]) -> Iterator[Post]:
        """The post's info.md, now with its download links, and its archives; each post counts once against the VIP quota.

        Without a login, with the quota used up, or when the site gives this account no links, it
        yields info.md without links and warns why.

        Args:
            site: the target's site, whose login and saved state decide the quota.
            post: the post's JSON, as `post` returned it.
        """
        # The job skips a post outside `--time` too, but only after its links were fetched and counted.
        if day(post.get("date")) not in site.time:
            return
        links = self._links(site, post)
        archives = group_mirrors(links) if links else []
        # Counted as soon as the links are in hand: the job's own checks (a file already saved)
        # come after the yield, and can't be told from here.
        if archives:
            count_download(site, post["id"])
        yield Post(post["id"], [File.text("info.md", info_md(post, links)),
                                *(File(mirrors, name=name) for name, mirrors in archives)],
                   date=post.get("date"), headers=SITE_HEADERS, **fields_of(post))

    def prepare(self, request: httpx.Request, cookies: Mapping[str, str], /) -> httpx.Request:
        """Give a request to the source's domains the site's referer and origin, and sign an API request with the token.

        Args:
            request: the request about to be sent.
            cookies: the login's cookies, whose `access-token` becomes the Bearer token.

        Returns:
            The same request. It runs again for each retry, so every attempt has a fresh signature.
        """
        request.headers.setdefault("referer", SITE_HEADERS["referer"])
        request.headers.setdefault("origin", SITE_HEADERS["origin"])
        # Only the API is signed and given the token; the image and archive hosts take the referer and origin alone.
        if request.url.host != API_HOST:
            return request
        token = decode_token(cookies.get("access-token", ""))
        if token:
            request.headers["authorization"] = f"Bearer {token}"
        return sign_request(request, now_ms=_clock_ms())

    def _links(self, site: Site, post: dict[str, Any]) -> dict[str, Any] | None:
        """The post's decrypted download links, or None (with a warning) when there are none for this login."""
        if not decode_token(site.cookies.get("access-token", "")):
            site.warn("archives need a login; run flnd login")
            return None
        used, limit = quota(site, post["id"])
        # A limit of 0 means unknown (see `quota`), so only a known, used-up quota stops the links here.
        if limit and used >= limit:
            site.warn("the daily VIP quota is used up ({used} of {limit}); archives skipped", used=used, limit=limit)
            return None
        body = _json(site, f"{API}user/loi/download_v6/{post['id']}")
        data = body.get("data") if isinstance(body, dict) else None
        # The site sends no links to an account that can't take the post; the warning names the level it needs.
        if not isinstance(data, dict) or not data.get("sv_list"):
            needed = f"VIP{post['vip_requirement']}" if post.get("vip_requirement") else "a login"
            site.warn("the site gives no download links for this post ({needed} needed)", needed=needed)
            return None
        return {"servers": [(str(server.get("sv_name") or "Server"), decrypt_field(server.get("sv_url")))
                            for server in data["sv_list"] if isinstance(server, dict) and server.get("sv_url")],
                "pan": decrypt_field(data.get("bdp")), "pan_key": decrypt_field(data.get("bdp_key"))}


def fields_of(post: dict[str, Any]) -> dict[str, Any]:
    """The post's fields for the folder format: its `author`, `misc` when it has none, and its `title`."""
    return {"author": (post.get("category") or {}).get("name") or "misc", "title": post["loi_title"]}


def image_name(number: int, url: str) -> str:
    """A preview image's name, `01.jpg`; the site's image URLs have no extension, and serve JPEG.

    Args:
        number: the image's place in the post, counted from 1.
        url: the image's URL, whose extension is kept when it has one.
    """
    suffix = PurePosixPath(urlsplit(url).path).suffix
    return f"{number:02d}{suffix or '.jpg'}"


def group_mirrors(links: dict) -> list[tuple[str, list[str]]]:
    """Each archive's name, from its URL, and the mirror URLs that serve it, in the site's order."""
    groups: dict[str, list[str]] = {}
    for _, url in links["servers"]:
        name = unquote(PurePosixPath(urlsplit(url).path).name) or "archive.zip"
        groups.setdefault(name, []).append(url)
    return list(groups.items())


def _json(site: Site, url: str) -> Any:
    """A URL's JSON, or None when the site refuses this login (401, 403 or 404)."""
    # `soft` returns None for a refusal instead of raising, so the caller can warn about the login rather than fail.
    response = site.fetch(url, soft=True)
    return None if response is None else response.json


def quota(site: Site, post_id: int) -> tuple[int, int]:
    """Archives taken today and the account's daily VIP limit, 0 when unknown; a post taken today is free again.

    Args:
        site: the target's site, whose login and saved state are read.
        post_id: the post's id; a post already counted today is free to take again.

    Returns:
        `(taken today, the daily limit)`; the limit is 0 when the account or its level is unknown.
    """
    taken = today_posts(site)
    user = _json(site, f"{API}user/info")
    data = user.get("data") if isinstance(user, dict) else None
    # A post already counted today costs nothing more, so its limit is not checked again.
    if post_id in taken or not isinstance(data, dict):
        return len(taken), 0
    # VIP level 0 (no level) is not in the table, so it gets no daily archives.
    limit = VIP_DAILY_LIMITS.get(int(data.get("vip_id") or 0), 0)
    # An SVIP account (`svip` 1) gets twice its VIP level's archives.
    return len(taken), limit * (2 if data.get("svip") == 1 else 1)


def count_download(site: Site, post_id: int) -> None:
    """Count a post against today's quota, once.

    Args:
        site: the target's site, whose saved state holds today's count.
        post_id: the post whose archives were just taken.
    """
    taken = today_posts(site)
    if post_id not in taken:
        site.state["quota"] = {"day": datetime.date.today().isoformat(), "posts": [*taken, post_id]}


def today_posts(site: Site) -> list[int]:
    """The posts whose archives were taken today; yesterday's list no longer counts."""
    record = site.state.get("quota") or {}
    return list(record.get("posts", [])) if record.get("day") == datetime.date.today().isoformat() else []


def info_md(post: dict, links: dict | None) -> str:
    """The post as Markdown: its fields, unzip password, description, download links and preview images.

    Args:
        post: the post's JSON.
        links: the decrypted links from `_links`, or None for an info.md without them.
    """
    author = post.get("category") or {}
    tags = " ".join(f"`{markdown(tag.get('name', ''))}`" for tag in post.get("tags") or [])
    # The fields as a two-column table: each row is (name, value), already escaped for Markdown.
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
    # The title, then the table, then each optional section in turn; a section a post lacks is left out.
    lines = [f"# {markdown(post['loi_title'])}", "", "| Property | Value |", "| --- | --- |",
             *(f"| {name} | {value} |" for name, value in rows)]
    # The description is quoted line by line, so a blank line stays inside the quote.
    if post.get("loi_description"):
        lines += ["", "## Description", "",
                  *(f"> {line}" if line else ">" for line in post["loi_description"].splitlines())]
    if links:
        lines += ["", "## Download Sources", "",
                  *(f"- **Server [{markdown(name)}]**: [Download]({url})" for name, url in links["servers"])]
        # The Baidu link is the one with its own password, which is shown beside it when there is one.
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

    Args:
        request: the API request, whose headers are set in place.
        now_ms: the time in milliseconds, which the signature and the request id both use.
        pick: chooses the variant character of the request id; replaced in tests.
        noise: returns that many random hex characters; replaced in tests.

    Returns:
        The same request, with the five signature headers set.
    """
    where = urlsplit(str(request.url)).path.removeprefix("/").removeprefix("api/")
    method, at = request.method.lower(), str(now_ms)
    request.headers.update({
        "x-security-challenge": "turnstile-v1",
        "x-request-id": request_id(method, where, now_ms=now_ms, pick=pick, noise=noise),
        "x-signature-timestamp": at,
        "x-request-signature": crypto.sha256(f"{method}|{where}||{at}|{SALT}"),
    })
    return request


def request_id(method: str, where: str, *, now_ms: int, pick: Callable[[Sequence[str]], str],
               noise: Callable[[int], str]) -> str:
    """A UUID-shaped id: the five-minute slot and random hex, with a digest of the request hidden inside.

    Args:
        method: the request's lower-case HTTP method.
        where: the request's path, without its `/api/` prefix.
        now_ms: the time in milliseconds; the id changes every five minutes.
        pick: chooses the variant character; replaced in tests.
        noise: returns that many random hex characters; replaced in tests.

    Returns:
        The id as `8-4-4-4-12` hex, with a version nibble of 4 and a variant of 8, 9, a or b.
    """
    # The slot is the time in five-minute units, so one slot's ids share a prefix; random hex fills the rest.
    seed = format(now_ms // 300_000, "x").zfill(8)[-8:] + noise(9)[:17]
    # The digest covers the prefix, the request, and the digest of an empty `{}` body.
    digest = crypto.sha256(f"{REQUEST_ID_PREFIX}|{seed}|{method}|{where}|{crypto.sha256('{}')}")[:5]
    seeds, digests, characters = iter(seed), iter(digest), []
    for position in range(32):
        # Position 12 is the UUID's version nibble, which is always 4.
        if position == 12:
            characters.append("4")
        # Position 16 is the UUID's variant nibble, which is one of 8, 9, a and b.
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
        return _decrypt_aes_cbc(base64.b64decode(text)).decode("utf-8", "replace")
    # Bad base64, or bytes the key doesn't decrypt to valid padding; the site's key change is the likely cause.
    except (binascii.Error, ValueError):
        raise ValueError(f"loi: can't decrypt {text[:16]!r}…; the site changed its key") from None


def _decrypt_aes_cbc(blob: bytes) -> bytes:
    """`blob` decrypted with the site's AES-128-CBC key and IV, its PKCS7 padding removed.

    CBC is the site's choice, not ours: `shared.crypto` offers only authenticated AES-GCM, so the
    site's format is read here with `cryptography` directly. A blob that isn't whole blocks, or
    whose padding is wrong, raises ValueError, as `cryptography` does.
    """
    from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
    from cryptography.hazmat.primitives.padding import PKCS7

    decryptor = Cipher(algorithms.AES(SITE_KEY), modes.CBC(SITE_IV)).decryptor()
    unpadder = PKCS7(128).unpadder()
    padded = decryptor.update(blob) + decryptor.finalize()
    return unpadder.update(padded) + unpadder.finalize()


def decode_token(raw: str) -> str:
    """The Bearer token in an `access-token` cookie, which the site stores %-encoded with its `Bearer ` prefix."""
    return unquote(raw).removeprefix("Bearer ").removeprefix("bearer ").strip()


def _clock_ms() -> int:
    return int(datetime.datetime.now(datetime.timezone.utc).timestamp() * 1000)

"""LOIBUS (loix.cc): posts and authors: info, preview images and, for VIP, archives.

SPEC.md#loi-loibus explains it: the signed API, the encrypted links and the daily VIP quota.
"""

from __future__ import annotations

import base64
import binascii
import contextlib
import functools
import hashlib
import random
import secrets
import time
from collections.abc import AsyncIterator, Callable, Mapping, Sequence
from enum import Enum
from pathlib import PurePosixPath
from typing import Any
from urllib.parse import unquote, urlsplit

from flnd.plugin import (
    Changed,
    Ctx,
    Error,
    File,
    LoginError,
    NotFound,
    Plugin,
    Post,
    Request,
    Response,
    Target,
    cookies,
    numbered,
    route,
)

API = "https://api.moegoat.com/api/"
API_HOST = "api.moegoat.com"
# The links request, under `API`: the one answer whose JSON may say "slow down".
LINKS = "user/loi/download_v6/"
# An author's posts, under `API` (seen 2026-11-23): `category_id`, `sort=latest` and `page`.
AUTHOR = "user/library/loier/id"
PAGE_SIZE = 20
SITE = "https://loix.cc/"
# The site's image servers and archive mirrors answer 403 without these (seen 2026-11-11); they
# aren't the plugin's domains, so each post passes them to its files.
SITE_HEADERS = {"referer": SITE, "origin": SITE.rstrip("/")}
# The web client's signing constants, from its JavaScript (seen 2026-11-11).
SALT = "loibus-sec-decoy-salt-v1"
REQUEST_ID_PREFIX = "loibus-web-request-id-v1"
# Where the request id hides the digest's five characters.
DIGEST_POSITIONS = frozenset({1, 9, 19, 24, 30})
# The site's own AES-128 key and IV, from its JavaScript (seen 2026-11-11).
SITE_KEY = b"7811595744111520"
SITE_IV = b"RgaXNyE5aWckBwI7"
# Archives a day per VIP level; SVIP doubles it, and a level not listed (0) gets none.
VIP_DAILY_LIMITS = {1: 15, 2: 25, 3: 40, 4: 60}
# The characters Markdown reads as syntax in a heading, table cell, or link.
MARKDOWN_SPECIAL = set("\\`*_[]<>|#")
# Words of the API's JSON answers (English or Chinese, seen 2026-11-15) that say "slow down", and
# those that say "today's quota is used up".
RATE_WORDS = ("rate", "too many", "频繁", "稍后")
QUOTA_WORDS = ("quota", "limit", "exceeded", "次数", "上限")


class Refusal(Enum):
    """Why an API answer gave no links, as its JSON says."""

    # Too many requests: hold, and ask again.
    RATE = "rate"
    # Today's archives are used up: ask again tomorrow.
    QUOTA = "quota"


class Loibus(Plugin):
    """LOIBUS posts, from its signed API; it never opens a browser, which the site blocks."""

    code = "loi"
    name = "LOIBUS"
    domains = ("loix.cc", "moegoat.com")
    # Images, media and fonts load until a live check shows its pages work without them.
    assets = True
    # A more literal route (a post, an author) wins over these catch-alls.
    unsupported = {
        **{f"{host}/library": "library pages aren't supported; give a post or an author"
           for host in ("loix.cc", "moegoat.com")},
        **{host: "that page isn't supported; give a post or an author"
           for host in ("loix.cc", "moegoat.com")},
    }
    # Without it: the info and images only, no archives.
    login = cookies("access-token")
    pace = 1.5
    limits = {"api.moegoat.com": 2}
    # Everything sits in the post's folder: previews, info.md, the archives' files.
    folder = "{author}/{post} {title}"
    fields = ("author", "title", "part")
    # Every file is named by its post (`File.name`), so this template is never used.
    filename = "{post}"
    # The history key is the file's old path, from when previews sat in an `images` folder
    # (`part` is "images" for them, empty otherwise): a saved post never downloads again.
    key = "{dir}/{part}/{file}"

    def __init__(self) -> None:
        # One instance per run (the crawl's rule), so these last the run.
        self._limit: int | None = None
        self._limit_read = False
        # Posts whose `archive` was listed: that category writes their info.md, with links.
        self._archived: set[str] = set()
        # Archives skipped for the day's quota, said once at the run's end.
        self._left = 0

    @contextlib.asynccontextmanager
    async def connect(self, ctx: Ctx) -> AsyncIterator[None]:
        """Around the run's targets; at its end, say how many archives are left for tomorrow."""
        try:
            yield
        finally:
            if self._left:
                ctx.log("info", "archives left for tomorrow by the VIP quota: {n}", n=self._left)

    @route(*((pattern, "author/{id}") for pattern in (
        r"loi:author/{id:\d+}", r"loix.cc/library/loier/{id:\d+}",
        r"moegoat.com/library/loier/{id:\d+}")), discovers=True)
    async def author(self, ctx: Ctx, id: str) -> AsyncIterator[Target]:
        """An author's posts, such as `loi:author/183`, each as `loi:<id>`: the API's
        list, paged by `page` until the last page or a short or empty one.

        Raises:
            Blocked: the API kept saying "slow down".
        """
        id = id.removeprefix("author/")
        first = Request(f"{API}{AUTHOR}", params={"category_id": id, "sort": "latest",
                                                  "page": "1"})
        async for target in _list_posts(ctx, first):
            yield target

    @route(*((pattern, "{id}") for pattern in (
        r"loi:{id:\d+}", r"loix.cc/lois/{id:\d+}",
        r"moegoat.com/lois/{id:\d+}")), single=True)
    async def post(self, ctx: Ctx, id: str) -> dict[str, Any]:
        """One post by id (`loi:5930`), or its link on either site: its JSON, for the
        categories.

        Raises:
            NotFound: the API has no such post, or one without a title.
        """
        body = (await ctx.fetch(f"{API}lois/{id}")).json
        post = body.get("data") if isinstance(body, dict) else None
        if not isinstance(post, dict) or not post.get("loi_title"):
            raise NotFound(f"post {id} isn't there")
        return post

    # `archive` comes first, so the crawl (which lists in declaration order) has listed it when
    # `photo` asks whether to write info.md.
    @post.category
    async def archive(self, ctx: Ctx, post: dict[str, Any]) -> AsyncIterator[Post]:
        """info.md with the download links, and the archives (the VIP quota applies).

        The post is lazy: its links are fetched, and the post counted against the quota, only
        for a post the engine will download.
        """
        self._archived.add(str(post["id"]))
        yield Post(post["id"], functools.partial(self._archive_files, ctx, post),
                   date=post.get("date"), headers=SITE_HEADERS, **fields_of(post))

    @post.category
    async def photo(self, ctx: Ctx, post: dict[str, Any]) -> AsyncIterator[Post]:
        """The preview images, and info.md without links when `archive` isn't wanted."""
        if str(post["id"]) not in self._archived:
            yield Post(post["id"], [File.text("info.md", info_md(post, None))],
                       date=post.get("date"), **fields_of(post))
        if post.get("images"):
            files = [File(url, name=image_name(number, url))
                     for number, url in enumerate(post["images"], start=1)]
            yield Post(post["id"], files, date=post.get("date"), headers=SITE_HEADERS,
                       **fields_of(post, part="images"))

    def sign(self, request: Request, cookies: Mapping[str, str]) -> Request:
        """Add the site's referer and origin; on the API, also the token and the signature.

        Called before each try, so a retried request is signed again with a new time and id.
        """
        headers = {**SITE_HEADERS, **(request.headers or {})}
        if urlsplit(request.url).hostname == API_HOST:
            token = decode_token(cookies.get("access-token", ""))
            if token:
                headers["authorization"] = f"Bearer {token}"
            headers.update(signature(request.method, request.url,
                                     now_ms=int(time.time() * 1000)))
        return request.replace(headers=headers)

    def blocked(self, response: Response) -> float | None:
        """Hold (the engine's block round) on a rate limit the links answer states in its JSON.

        Only the links and author answers are read: a post's own refusal ("请稍后再试")
        is `NotFound` at once. A plain HTTP 429 is left to the engine's own 429 hold. A used-up
        quota is no hold: the archive category skips the day's posts itself.
        """
        where = urlsplit(response.url)
        path = where.path.rstrip("/")
        if (response.status == 429 or where.hostname != API_HOST
                or not (where.path.startswith(f"/api/{LINKS}")
                        or path == f"/api/{AUTHOR}")):
            return None
        try:
            body = response.json
        except Error:
            return None
        if not isinstance(body, dict) or body.get("data"):
            return None
        return 0.0 if refusal(response.status, body) is Refusal.RATE else None

    # -- Archives and links ----------------------------------------------------------------

    async def _archive_files(self, ctx: Ctx, post: dict[str, Any]) -> list[File]:
        """info.md with the links, then each archive with its mirrors; info.md alone without."""
        links = await self._links(ctx, post)
        archives = group_mirrors(links) if links else []
        if archives:
            count_download(ctx, post["id"])
        password = str(post["open_key"]) if post.get("open_key") else None
        return [
            File.text("info.md", info_md(post, links)),
            *(File(mirrors=mirrors, name=name, password=password, unpack="here")
              for name, mirrors in archives),
        ]

    async def _links(self, ctx: Ctx, post: dict[str, Any]) -> dict[str, Any] | None:
        """The post's decrypted links, or None with a warning when they can't be had."""
        if not decode_token(ctx.cookies.get("access-token", "")):
            ctx.log("warn", "archives need a login; run flnd auth login loi")
            return None
        used, limit = await self._quota(ctx, post["id"])
        if limit is not None and used >= limit:
            if limit == 0:
                ctx.log("warn", "the account has no VIP level, so no archives a day; "
                        "archives skipped")
            else:
                self._left += 1
                ctx.log("warn", "the daily VIP quota is used up ({used} of {limit}); "
                        "archives skipped", used=used, limit=limit)
            return None
        status, body = await _answer(ctx, f"{API}{LINKS}{post['id']}")
        data = body.get("data") if isinstance(body, dict) else None
        if not isinstance(data, dict) or not data.get("sv_list"):
            if refusal(status, body) is Refusal.QUOTA:
                _mark_exhausted(ctx)
                self._left += 1
                ctx.log("warn", "the daily VIP quota is used up; archives skipped")
                return None
            needed = f"VIP{post['vip_requirement']}" if post.get("vip_requirement") else "a login"
            ctx.log("warn", "the site gives no download links for this post ({needed} needed)",
                    needed=needed)
            return None
        servers = [
            (str(server.get("sv_name") or "Server"), decrypted)
            for server in data["sv_list"]
            if isinstance(server, dict) and server.get("sv_url")
            and (decrypted := decrypt_field(server.get("sv_url")))
        ]
        return {"servers": servers, "pan": decrypt_field(data.get("bdp")),
                "pan_key": decrypt_field(data.get("bdp_key"))}

    async def _quota(self, ctx: Ctx, post_id: int) -> tuple[int, int | None]:
        """The archives taken today, and today's limit; None when the limit doesn't apply."""
        record = ctx.state.get("quota") or {}
        taken = today_posts(ctx)
        if record.get("day") == _today(ctx) and record.get("exhausted"):
            used = max(len(taken), 1)
            return used, used
        if post_id in taken:
            return len(taken), None
        if not self._limit_read:
            self._limit, self._limit_read = await _daily_limit(ctx), True
        return len(taken), self._limit


async def _daily_limit(ctx: Ctx) -> int | None:
    """The account's archives a day, from `user/info`; None when it can't be read."""
    status, user = await _answer(ctx, f"{API}user/info")
    data = user.get("data") if 200 <= status < 300 and isinstance(user, dict) else None
    if not isinstance(data, dict):
        return None
    limit = VIP_DAILY_LIMITS.get(int(data.get("vip_id") or 0), 0)
    return limit * (2 if data.get("svip") == 1 else 1)


async def _answer(ctx: Ctx, url: str) -> tuple[int, Any]:
    """A request's status and JSON (None when it isn't JSON); aborts on HTTP/JSON 401."""
    response = await ctx.fetch(Request(url, check=False))
    try:
        body = response.json
    except Changed:
        body = None
    code = str(body.get("code") or "") if isinstance(body, dict) else ""
    if response.status == 401 or code == "401":
        raise LoginError("loi: session expired or invalid; log in again",
                         hint="run flnd auth login loi", retryable=False)
    return response.status, body


def refusal(status: int, body: Any) -> Refusal | None:
    """Why an answer without links refused: a rate limit, today's quota, or neither (None).

    A rate limit is an HTTP 429, a JSON `code` of 429, or a message that says to slow down;
    it is tested first, since its message may name a limit too. The quota is an error (an
    HTTP error, a JSON `status` of error or fail, or a `code` of 403) naming a limit or quota.
    """
    if not isinstance(body, dict):
        return Refusal.RATE if status == 429 else None
    data = body.get("data")
    if isinstance(data, dict) and data.get("sv_list"):
        return None
    code = str(body.get("code") or "")
    state = str(body.get("status") or "").casefold()
    message = str(body.get("message") or body.get("msg") or "").casefold()
    if status == 429 or code == "429" or any(word in message for word in RATE_WORDS):
        return Refusal.RATE
    failed = status >= 400 or state in ("error", "fail") or code == "403"
    if failed and any(word in message for word in QUOTA_WORDS):
        return Refusal.QUOTA
    return None


async def _list_posts(ctx: Ctx, first: Request) -> AsyncIterator[Target]:
    """Every post id a paged API list gives, once each, as `loi:<id>`."""
    seen: set[str] = set()
    page = 1

    def next_page(response: Response) -> str | None:
        nonlocal page
        page += 1
        body = response.json
        paging = body.get("pagination") if isinstance(body, dict) else None
        if isinstance(paging, dict) and paging.get("total_pages") is not None:
            return str(page) if page <= int(paging["total_pages"]) else None
        return str(page) if len(_hits(response)) >= PAGE_SIZE else None

    pages = 0
    async for response in ctx.pages(first, cursor=next_page, send="page"):
        if pages:
            await ctx.pause(random.uniform(1.0, 2.5), "pacing author pages")
        pages += 1
        for found in _hits(response):
            post_id = str(found.get("id") or "")
            if post_id and post_id not in seen:
                seen.add(post_id)
                yield Target(f"loi:{post_id}", kind="post")


def _hits(response: Response) -> list[dict[str, Any]]:
    """The posts of one author answer: `data` as a list, or `data.list` / `data.items`."""
    body = response.json
    data = body.get("data") if isinstance(body, dict) else None
    if isinstance(data, dict):
        data = data.get("list") or data.get("items")
    return [hit for hit in data if isinstance(hit, dict)] if isinstance(data, list) else []


def fields_of(post: dict[str, Any], *, part: str = "") -> dict[str, Any]:
    """Fields: `author` (`misc` when absent), `title` and `part` (only in the key), raw."""
    category = post.get("category") or {}
    return {"author": category.get("name") or "misc", "title": post["loi_title"], "part": part}


def image_name(number: int, url: str) -> str:
    """A preview's name, `01.jpg`: the URL's extension as written (`.JPG`), else `.jpg`."""
    suffix = PurePosixPath(urlsplit(url).path).suffix
    return numbered(number, suffix=suffix or ".jpg")


def group_mirrors(links: dict[str, Any]) -> list[tuple[str, list[str]]]:
    """Each archive's name, from its URL ("archive.zip" without one), and its mirrors, in order.

    The site lists one link per server; links ending in the same file name are one archive.
    """
    groups: dict[str, list[str]] = {}
    for _, url in links["servers"]:
        name = unquote(PurePosixPath(urlsplit(url).path).name) or "archive.zip"
        groups.setdefault(name, []).append(url)
    return list(groups.items())


# -- The daily VIP quota, in the plugin's state ------------------------------------------------
# `ctx.state["quota"]` is `{"day": "YYYY-MM-DD", "posts": [ids], "exhausted": bool}`: the posts
# whose links were taken on that UTC day, and whether the site said the quota is used up.

def _today(ctx: Ctx) -> str:
    return ctx.time.day().isoformat()


def _mark_exhausted(ctx: Ctx) -> None:
    """Record today's quota as used up, so later posts skip the links request."""
    ctx.state["quota"] = {"day": _today(ctx), "posts": today_posts(ctx), "exhausted": True}


def count_download(ctx: Ctx, post_id: int) -> None:
    """Count a post against today's quota, once; a count means the quota wasn't used up yet."""
    taken = today_posts(ctx)
    if post_id not in taken:
        ctx.state["quota"] = {"day": _today(ctx), "posts": [*taken, post_id]}


def today_posts(ctx: Ctx) -> list[int]:
    """The posts whose archives were taken today; another day's list counts as empty."""
    record = ctx.state.get("quota") or {}
    return list(record.get("posts", [])) if record.get("day") == _today(ctx) else []


# -- info.md -----------------------------------------------------------------------------------

def _info_rows(post: dict[str, Any]) -> list[tuple[str, str]]:
    """The table's `(property, value)` rows; the password and tags only when the post has them."""
    author = post.get("category") or {}
    author_name = markdown(author.get("name", ""))
    author_id = author.get("id", "")
    tags = " ".join(f"`{markdown(tag.get('name', ''))}`" for tag in post.get("tags") or [])
    vip = f"VIP{post['vip_requirement']}" if post.get("vip_requirement") else "Free"
    stats = (f"likes {post.get('like_count', 0)} · views {post.get('viewer_count', 0)} · "
             f"downloads {post.get('download_count', 0)}")
    rows = [
        ("ID", f"`{post['id']}`"),
        ("Author", f"[{author_name} (#{author_id})]({SITE}library/loier/{author_id})"),
        ("Date", str(post.get("date") or "")),
        ("URL", f"[{post['id']}]({SITE}lois/{post['id']})"),
        ("File Size", str(post.get("file_size") or "N/A")),
        ("VIP Requirement", vip),
        ("Media Count",
         f"pictures {post.get('pic_count', 0)} · videos {post.get('video_count', 0)}"),
        ("Stats", stats),
    ]
    if post.get("open_key"):
        rows.append(("Unzip Password", f"`{markdown(post['open_key'])}`"))
    if tags:
        rows.append(("Tags", tags))
    return rows


def info_md(post: dict[str, Any], links: dict[str, Any] | None) -> str:
    """The post as Markdown: its fields, description, links (when given) and preview images."""
    lines = [
        f"# {markdown(post['loi_title'])}", "", "| Property | Value |", "| --- | --- |",
        *(f"| {name} | {value} |" for name, value in _info_rows(post)),
    ]
    if post.get("loi_description"):
        lines += [
            "", "## Description", "",
            *(f"> {line}" if line else ">" for line in post["loi_description"].splitlines()),
        ]
    if links:
        lines += [
            "", "## Download Sources", "",
            *(f"- **Server [{markdown(n)}]**: [Download]({u})" for n, u in links["servers"]),
        ]
        if links["pan"]:
            key = f" (Password: `{markdown(links['pan_key'])}`)" if links["pan_key"] else ""
            lines.append(f"- **Baidu**: [Link]({links['pan']}){key}")
    if post.get("images"):
        lines += [
            "", "## Preview Images", "",
            *(f"{i}. ![Preview {i}]({url})" for i, url in enumerate(post["images"], start=1)),
        ]
    return "\n".join(lines) + "\n"


def markdown(text: str) -> str:
    """Text with the characters Markdown would read escaped."""
    return "".join(f"\\{character}" if character in MARKDOWN_SPECIAL else character
                   for character in str(text))


# -- The request signature (the web client's, from its JavaScript, seen 2026-11-11) ------------

def _digest(text: str) -> str:
    """The SHA-256 hex digest of `text` as UTF-8, as the web client computes it."""
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def signature(method: str, url: str, *, now_ms: int,
              pick: Callable[[Sequence[str]], str] = secrets.choice,
              noise: Callable[[int], str] = secrets.token_hex) -> dict[str, str]:
    """LOIBUS's four signature headers for an API request, signed at `now_ms`.

    The signature covers the lowercase method, the path after `/api/` (without the query) and
    the time; the body isn't part of it (the `||` is an empty slot the client leaves).

    Args:
        now_ms: the time, in Unix milliseconds.
        pick: picks the UUID variant nibble; tests pass a fixed choice.
        noise: gives random hex of a byte count; tests pass a fixed string.
    """
    where = urlsplit(url).path.removeprefix("/").removeprefix("api/")
    method, at = method.lower(), str(now_ms)
    return {
        "x-security-challenge": "turnstile-v1",
        "x-request-id": request_id(method, where, now_ms=now_ms, pick=pick, noise=noise),
        "x-signature-timestamp": at,
        "x-request-signature": _digest(f"{method}|{where}||{at}|{SALT}"),
    }


def request_id(method: str, where: str, *, now_ms: int, pick: Callable[[Sequence[str]], str],
               noise: Callable[[int], str]) -> str:
    """A UUID-shaped id, `xxxxxxxx-xxxx-4xxx-yxxx-xxxxxxxxxxxx`, with a request digest inside."""
    # The slot is the time in five-minute units; random hex fills the rest.
    seed = format(now_ms // 300_000, "x").zfill(8)[-8:] + noise(9)[:17]
    # The digest covers the prefix, the request, and the digest of an empty `{}` body.
    digest = _digest(f"{REQUEST_ID_PREFIX}|{seed}|{method}|{where}|{_digest('{}')}")[:5]
    seeds, digests, characters = iter(seed), iter(digest), []
    for position in range(32):
        # Position 12 is the UUID's version nibble (4); 16 its variant (8, 9, a or b).
        if position == 12:
            characters.append("4")
        elif position == 16:
            characters.append(pick(["8", "9", "a", "b"]))
        else:
            characters.append(next(digests if position in DIGEST_POSITIONS else seeds, "0"))
    text = "".join(characters)
    return f"{text[:8]}-{text[8:12]}-{text[12:16]}-{text[16:20]}-{text[20:]}"


# -- Encrypted fields and the token ------------------------------------------------------------

def decrypt_field(text: str | None) -> str:
    """Decrypt one of the site's AES fields (a link, a password); an empty one stays empty.

    Raises:
        Changed: the field isn't base64, or doesn't decrypt: the site likely changed its key.
    """
    text = (text or "").strip()
    if not text:
        return ""
    try:
        return _decrypt_aes_cbc(base64.b64decode(text)).decode("utf-8", "replace")
    except (binascii.Error, ValueError):
        raise Changed(f"loi: can't decrypt {text[:16]!r}…; the site changed its key") from None


def _decrypt_aes_cbc(blob: bytes) -> bytes:
    """`blob` decrypted with the site's key and IV, unpadded; `cryptography` loads only here."""
    from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
    from cryptography.hazmat.primitives.padding import PKCS7

    decryptor = Cipher(algorithms.AES(SITE_KEY), modes.CBC(SITE_IV)).decryptor()
    unpadder = PKCS7(128).unpadder()
    padded = decryptor.update(blob) + decryptor.finalize()
    return unpadder.update(padded) + unpadder.finalize()


def decode_token(raw: str) -> str:
    """The Bearer token of the `access-token` cookie: %-decoded, without a `Bearer ` prefix."""
    return unquote(raw).removeprefix("Bearer ").removeprefix("bearer ").strip()

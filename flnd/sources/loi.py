"""LOIBUS (loix.cc): one post's info, preview images and, for VIP accounts, archives.

What it holds: the `Loibus` source, and the helpers that know the site: its request signature
(`sign_request`, `request_id`), its encrypted links (`decrypt_field`), its daily VIP quota
(`quota`, `count_download`), and the post's Markdown summary (`info_md`).

Who calls it: the engine, through the source API (`flnd.source`): it resolves `loi:<id>` and post
links through the `post` route, lists the `photo` and `archive` categories, and calls `sign`
before every request to the site's own domains. It calls nothing of flnd but `flnd.source`; the
AES decryption imports `cryptography` lazily.

How a post is read:
1. `post` fetches the post's JSON from the API (`lois/<id>`); every category receives it.
2. `photo` yields the post's `info.md` (without links) and its preview images, at once.
3. `archive` yields one lazy post: the engine calls its files function only for a post that
   passes `--when` and that it will download. That function, `_archive_files`:
   a. checks the login and today's VIP quota (`quota`), skipping with a warning when used up;
   b. fetches the download links (`download_v6`), backing off on a rate-limit answer and marking
      the quota used up on a daily-quota answer;
   c. decrypts the links, groups mirrors of one archive, counts the post against today's quota
      (`count_download`), and returns `info.md` (with links) and the archives.
4. `sign` adds the site's referer and origin to every request on its domains, and on the API host
   also the Bearer token and the request signature, freshly for each try.

There is no feed to end: a target is one post, so every category yields once and is done. The
pace is the source API's default (0.5 seconds between requests to the site's domains).

How login works: `login = cookies("access-token")`, not required. `flnd login loi` reads the
site's cookies from a browser or a cookie file; `sign` sends the cookie's value to the API as a
Bearer token (`decode_token`). Without it, `photo` works in full and `archive` warns and writes
`info.md` without links. An expired or refused token isn't told apart: the site gives no links,
and the warning names the VIP level the post needs. The account's VIP level, read from
`user/info`, sets today's limit (`VIP_DAILY_LIMITS`).

Site quirks, each with the day it was first seen (docs/sources/loi has them for people):
- 2026-11-11: every API request must carry the web client's signature headers, built from
  constants in the site's JavaScript (`sign_request`, `request_id`).
- 2026-11-11: download links, the Baidu link and its password come AES-128-CBC encrypted under
  a key and IV from the site's JavaScript (`decrypt_field`).
- 2026-11-11: the image servers and archive mirrors answer 403 without the site's `referer` and
  `origin`, so each post passes them to its files (`SITE_HEADERS`).
- 2026-11-11: the `access-token` cookie may be %-encoded and may already start with `Bearer `.
- 2026-11-15: a rate limit and a used-up quota are told in JSON, in English or Chinese, not only
  by the HTTP status (`refusal`, `RATE_WORDS`, `QUOTA_WORDS`).
- Date not recorded: the site blocks a browser, so the source never opens one.

Rules a change must keep:
- Every history key is the file's path (`key = ""`), as it always has been: changing the folder,
  the file names or the key makes every saved file download again.
- Links are fetched, and a post counted, only inside the lazy files function, never while
  listing: the site spends a VIP quota slot on each link fetch.
- The signature must stay byte-for-byte the web client's (tests/test_loi.py pins it).
"""

from __future__ import annotations

import base64
import binascii
import datetime
import hashlib
import secrets
import time
from enum import Enum
from pathlib import PurePosixPath
from typing import Any, Callable, Iterator, Sequence
from urllib.parse import unquote, urlsplit

from flnd.source import (
    Changed, File, NotFound, Post, Request, Site, Source, cookies, target,
)

API = "https://api.moegoat.com/api/"
API_HOST = "api.moegoat.com"
SITE = "https://loix.cc/"
# What the site's own pages send with every request. Its image servers and archive mirrors give
# 403 without them, and a post gives them to its files because those hosts aren't the source's.
SITE_HEADERS = {"referer": SITE, "origin": SITE.rstrip("/")}
# The web client's signing constants, from its JavaScript (seen 2026-11-11).
SALT = "loibus-sec-decoy-salt-v1"
REQUEST_ID_PREFIX = "loibus-web-request-id-v1"
# Where the request id hides the digest's five characters.
DIGEST_POSITIONS = frozenset({1, 9, 19, 24, 30})
# The site's own AES-128 key and IV, from its JavaScript; `_decrypt_aes_cbc` decrypts links.
SITE_KEY = b"7811595744111520"
SITE_IV = b"RgaXNyE5aWckBwI7"
# Archives a day per VIP level; an SVIP account gets twice as many. A level not listed (0, no
# VIP) gets none.
VIP_DAILY_LIMITS = {1: 15, 2: 25, 3: 40, 4: 60}
# The characters Markdown reads as syntax in a heading, table cell, or link.
MARKDOWN_SPECIAL = set("\\`*_[]<>|#")
# Words of the API's JSON answers (English or Chinese, seen 2026-11-15) that say "slow down", and
# those that say "today's quota is used up".
RATE_WORDS = ("rate", "too many", "频繁", "稍后")
QUOTA_WORDS = ("quota", "limit", "exceeded", "次数", "上限")
# The most waits for one link fetch while the site answers with a rate limit. The engine's
# `back_off` waits three rounds and raises `Blocked` on a fourth in a row, so the fourth here
# stops the target; the bound keeps a site whose `back_off` never raises from looping.
RATE_WAITS = 4


class Refusal(Enum):
    """Why the links request gave no links, as its JSON says."""

    # Too many requests: wait, and ask again.
    RATE = "rate"
    # Today's archives are used up: ask again tomorrow.
    QUOTA = "quota"


class Loibus(Source):
    """LOIBUS posts, from its signed API; it never opens a browser, which the site blocks."""

    code = "loi"
    name = "LOIBUS"
    domains = ("loix.cc", "moegoat.com")
    api = 1
    # Without it: the info and images only, no archives.
    login = cookies("access-token")
    # `<author>/<post> <title>/<part>`: `part` is "images" for the previews and empty otherwise.
    folder = "{author}/{post} {title}/{part}"
    fields = ("author", "title", "part")
    # Every file is named by its post (`File.name`), so this template is never used.
    filename = "{post}"
    # Every key is the file's path, as the history has always had it, so this never changes.
    key = ""

    @target(r"loi:{id:\d+}", r"loix.cc/lois/{id:\d+}", r"moegoat.com/lois/{id:\d+}")
    def post(self, site: Site, id: str) -> dict[str, Any]:
        """One post by id, or a post link on either site: its JSON, for the categories.

        Args:
            site: the target's site.
            id: the post's number.

        Returns:
            The post's `data` object.

        Raises:
            NotFound: the API has no such post, or one without a title.
        """
        body = site.fetch(f"{API}lois/{id}").json
        post = body.get("data") if isinstance(body, dict) else None
        if not isinstance(post, dict) or not post.get("loi_title"):
            raise NotFound(f"post {id} isn't there")
        return post

    @post.category
    def photo(self, site: Site, post: dict[str, Any]) -> Iterator[Post]:
        """The post's info.md, without links, and its preview images.

        Args:
            site: the target's site.
            post: what the route returned.

        Yields:
            Two posts of the same id: the info.md, then the images (when it has any).
        """
        info_file = File.text("info.md", info_md(post, None))
        yield Post(post["id"], [info_file], date=post.get("date"), **fields_of(post))
        if post.get("images"):
            files = [
                File(url, name=image_name(number, url))
                for number, url in enumerate(post["images"], start=1)
            ]
            yield Post(
                post["id"], files, date=post.get("date"), headers=SITE_HEADERS, part="images",
                **fields_of(post),
            )

    @post.category
    def archive(self, site: Site, post: dict[str, Any]) -> Iterator[Post]:
        """The post's info.md, now with download links, and its archives (VIP quota applies).

        The post is lazy: its links are fetched, and the post counted against the quota, only
        when the engine asks for its files, which it does only for a post inside `--when` that
        it will download.

        Args:
            site: the target's site.
            post: what the route returned.

        Yields:
            One lazy post, whose files `_archive_files` makes.
        """
        yield Post(
            post["id"], lambda: _archive_files(site, post), date=post.get("date"),
            headers=SITE_HEADERS, **fields_of(post),
        )

    def sign(self, request: Request) -> None:
        """Set the site's referer and origin; on the API, add the token and the signature.

        Called before each try of a request to the source's domains, so a retried request is
        signed again with a new time and id.

        Args:
            request: the request; its headers are changed in place.
        """
        request.headers.setdefault("referer", SITE_HEADERS["referer"])
        request.headers.setdefault("origin", SITE_HEADERS["origin"])
        # Only the API is signed and given the token; other hosts take referer and origin alone.
        if urlsplit(request.url).hostname != API_HOST:
            return
        token = decode_token(request.cookies.get("access-token", ""))
        if token:
            request.headers["authorization"] = f"Bearer {token}"
        sign_request(request, now_ms=int(time.time() * 1000))


# ---------------------------------------------------------------------------------------------
# Archives and links
# ---------------------------------------------------------------------------------------------

def _archive_files(site: Site, post: dict[str, Any]) -> list[File]:
    """The archive category's files: info.md with the links, then each archive with its mirrors.

    Called by the engine only for a post it will download. The post is counted against today's
    quota once its links are in hand, since the site spent a slot giving them.

    Args:
        site: the target's site.
        post: the post's JSON.

    Returns:
        info.md, and the archives; info.md alone (without links) when the links can't be had.
    """
    links = _links(site, post)
    archives = group_mirrors(links) if links else []
    if archives:
        count_download(site, post["id"])
    # The site's unzip password, kept with each archive and written in info.md.
    password = str(post["open_key"]) if post.get("open_key") else None
    return [
        File.text("info.md", info_md(post, links)),
        *(File(mirrors, name=name, password=password) for name, mirrors in archives),
    ]


def _links(site: Site, post: dict[str, Any]) -> dict[str, Any] | None:
    """The post's decrypted download links, or None with a warning when they can't be had.

    Steps:
    1. No token: warn that archives need a login.
    2. A known daily limit that is used up (a VIP-0 account's limit is 0): warn, and skip
       the links request, which would spend nothing but a request.
    3. Fetch the links; a daily-quota answer marks today's quota used up, and any other answer
       without links is a warning naming the VIP level the post needs.

    Args:
        site: the target's site.
        post: the post's JSON.

    Returns:
        `{"servers": [(name, url), …], "pan": url, "pan_key": text}`, or None.
    """
    if not decode_token(site.cookies.get("access-token", "")):
        site.warn("archives need a login; run flnd login loi")
        return None
    used, limit = quota(site, post["id"])
    if limit is not None and used >= limit:
        if limit == 0:
            site.warn("the account has no VIP level, so no archives a day; archives skipped")
        else:
            site.warn(
                "the daily VIP quota is used up ({used} of {limit}); archives skipped",
                used=used, limit=limit,
            )
        return None
    status, body = _fetch_links(site, post["id"])
    data = body.get("data") if isinstance(body, dict) else None
    if not isinstance(data, dict) or not data.get("sv_list"):
        if refusal(status, body) is Refusal.QUOTA:
            _mark_exhausted(site)
            site.warn("the daily VIP quota is used up; archives skipped")
            return None
        needed = f"VIP{post['vip_requirement']}" if post.get("vip_requirement") else "a login"
        site.warn("the site gives no download links for this post ({needed} needed)", needed=needed)
        return None
    servers = [
        (str(server.get("sv_name") or "Server"), decrypted)
        for server in data["sv_list"]
        if isinstance(server, dict) and server.get("sv_url")
        and (decrypted := decrypt_field(server.get("sv_url")))
    ]
    return {
        "servers": servers,
        "pan": decrypt_field(data.get("bdp")),
        "pan_key": decrypt_field(data.get("bdp_key")),
    }


def _fetch_links(site: Site, post_id: int) -> tuple[int, Any]:
    """The links answer's status and JSON, waiting out rate-limit answers.

    A rate limit the site states in its JSON (not only as a 429, which the client waits out
    itself) is waited out with `site.back_off`, which raises `Blocked` once the site keeps
    refusing; `RATE_WAITS` bounds the loop besides.

    Args:
        site: the target's site.
        post_id: the post's number.

    Returns:
        (HTTP status, parsed JSON or None).

    Raises:
        Blocked: the site kept answering with a rate limit through the engine's waits.
    """
    url = f"{API}user/loi/download_v6/{post_id}"
    status, body = _answer(site, url)
    for _ in range(RATE_WAITS):
        if refusal(status, body) is not Refusal.RATE:
            break
        site.back_off("LOIBUS rate limit")
        status, body = _answer(site, url)
    return status, body


def _answer(site: Site, url: str) -> tuple[int, Any]:
    """A request's status and JSON, a refusal's included: the site explains refusals in JSON.

    A 403 here is read, not raised: `check=False` keeps the engine from taking the links
    request's refusal (a VIP post, a used-up quota) for a login wall.

    Args:
        site: the target's site.
        url: the API URL.

    Returns:
        (HTTP status, parsed JSON, or None when the body isn't JSON).
    """
    response = site.fetch(url, check=False)
    try:
        return response.status, response.json
    except Changed:
        return response.status, None


def refusal(status: int, body: Any) -> Refusal | None:
    """Why an answer without links refused: a rate limit, today's quota, or neither (None).

    A rate limit is an HTTP 429, a JSON `code` of 429 (a number or text), or a message that
    says to slow down. The daily quota is an error (an HTTP error, a JSON `status` of error or
    fail, or a `code` of 403) whose message names a limit or quota. A rate limit's message may
    name a limit too ("rate limit"), so it is tested first.

    Args:
        status: the HTTP status.
        body: the parsed JSON, or None.

    Returns:
        The `Refusal`, or None for an answer that has links or says neither.

    Example:
        >>> refusal(200, {"code": "429", "message": "slow"}) is Refusal.RATE
        True
        >>> refusal(403, {"code": 403, "message": "daily limit exceeded"}) is Refusal.QUOTA
        True
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


def _json(site: Site, url: str) -> Any:
    """A URL's JSON, or None when the site refuses it, so a caller warns rather than fails."""
    status, body = _answer(site, url)
    return body if 200 <= status < 300 else None


def fields_of(post: dict[str, Any]) -> dict[str, Any]:
    """Folder fields: `author` (`misc` when absent) and `title`, raw; the engine sanitizes."""
    category = post.get("category") or {}
    return {"author": category.get("name") or "misc", "title": post["loi_title"]}


def image_name(number: int, url: str) -> str:
    """A preview image's name, `01.jpg`, keeping the URL's extension when it has one."""
    suffix = PurePosixPath(urlsplit(url).path).suffix
    return f"{number:02d}{suffix or '.jpg'}"


def group_mirrors(links: dict[str, Any]) -> list[tuple[str, list[str]]]:
    """Each archive's name, from its URL, and the mirror URLs that serve it, in link order.

    The site lists one link per server; links that end in the same file name are the same
    archive on other servers, so they become one file with mirrors, tried in order.

    Args:
        links: what `_links` gave; only its `servers` are read.

    Returns:
        `(name, [url, ...])` per archive, in the order its first link came. A URL with no file
        name gives "archive.zip".
    """
    groups: dict[str, list[str]] = {}
    for _, url in links["servers"]:
        name = unquote(PurePosixPath(urlsplit(url).path).name) or "archive.zip"
        groups.setdefault(name, []).append(url)
    return list(groups.items())


# ---------------------------------------------------------------------------------------------
# The daily VIP quota, kept in the source's state
# ---------------------------------------------------------------------------------------------
# `site.state["quota"]` is `{"day": "YYYY-MM-DD", "posts": [ids], "exhausted": bool}`: the posts
# whose links were taken on this machine's calendar day, and whether the site said the quota is
# used up. A record of another day counts as empty.

def _mark_exhausted(site: Site) -> None:
    """Record today's quota as used up, so later posts skip the links request."""
    today = datetime.date.today().isoformat()
    site.state["quota"] = {"day": today, "posts": today_posts(site), "exhausted": True}


def quota(site: Site, post_id: int) -> tuple[int, int | None]:
    """The archives taken today, and today's limit; None when the limit doesn't apply.

    The limit is None when it is unknown (the account's info can't be read) or when this post
    was counted today already, since taking it again costs nothing. A known limit may be 0: an
    account without a VIP level gets no archives.

    Args:
        site: the target's site; its `state` holds today's count.
        post_id: the post about to be fetched.

    Returns:
        (used, limit).
    """
    record = site.state.get("quota") or {}
    today = datetime.date.today().isoformat()
    taken = today_posts(site)
    if record.get("day") == today and record.get("exhausted"):
        used = max(len(taken), 1)
        return used, used
    if post_id in taken:
        return len(taken), None
    user = _json(site, f"{API}user/info")
    data = user.get("data") if isinstance(user, dict) else None
    if not isinstance(data, dict):
        return len(taken), None
    limit = VIP_DAILY_LIMITS.get(int(data.get("vip_id") or 0), 0)
    # An SVIP account (`svip` 1) gets twice its VIP level's archives.
    return len(taken), limit * (2 if data.get("svip") == 1 else 1)


def count_download(site: Site, post_id: int) -> None:
    """Count a post against today's quota, once.

    The record is rewritten without `exhausted`: a post counted means the site still gave
    links, so the quota wasn't used up yet.

    Args:
        site: the target's site; its `state` holds today's count.
        post_id: the post whose links were just taken.
    """
    taken = today_posts(site)
    if post_id not in taken:
        site.state["quota"] = {
            "day": datetime.date.today().isoformat(), "posts": [*taken, post_id],
        }


def today_posts(site: Site) -> list[int]:
    """The posts whose archives were taken today; yesterday's list no longer counts."""
    record = site.state.get("quota") or {}
    today = datetime.date.today().isoformat()
    return list(record.get("posts", [])) if record.get("day") == today else []


# ---------------------------------------------------------------------------------------------
# info.md
# ---------------------------------------------------------------------------------------------

def _info_rows(post: dict[str, Any]) -> list[tuple[str, str]]:
    """The Markdown table rows for a post's fields.

    Text from the site is escaped (`markdown`); a missing count reads 0, a missing size "N/A".

    Args:
        post: the post's JSON.

    Returns:
        `(property, value)` per row, in table order; the unzip password and the tags only
        when the post has them.
    """
    author = post.get("category") or {}
    author_name = markdown(author.get("name", ""))
    author_id = author.get("id", "")
    author_link = f"[{author_name} (#{author_id})]({SITE}library/loier/{author_id})"
    tags = " ".join(f"`{markdown(tag.get('name', ''))}`" for tag in post.get("tags") or [])
    vip_req = f"VIP{post['vip_requirement']}" if post.get("vip_requirement") else "Free"
    media_cnt = f"pictures {post.get('pic_count', 0)} · videos {post.get('video_count', 0)}"
    stats = (
        f"likes {post.get('like_count', 0)} · views {post.get('viewer_count', 0)} · "
        f"downloads {post.get('download_count', 0)}"
    )
    rows = [
        ("ID", f"`{post['id']}`"),
        ("Author", author_link),
        ("Date", str(post.get("date") or "")),
        ("URL", f"[{post['id']}]({SITE}lois/{post['id']})"),
        ("File Size", str(post.get("file_size") or "N/A")),
        ("VIP Requirement", vip_req),
        ("Media Count", media_cnt),
        ("Stats", stats),
    ]
    if post.get("open_key"):
        rows.append(("Unzip Password", f"`{markdown(post['open_key'])}`"))
    if tags:
        rows.append(("Tags", tags))
    return rows


def info_md(post: dict[str, Any], links: dict[str, Any] | None) -> str:
    """The post as Markdown: fields, unzip password, description, links, and preview images.

    Args:
        post: the post's JSON.
        links: what `_links` gave, or None to leave the download sources out.

    Returns:
        The text, ending with a newline.
    """
    rows = _info_rows(post)
    lines = [
        f"# {markdown(post['loi_title'])}", "", "| Property | Value |", "| --- | --- |",
        *(f"| {name} | {value} |" for name, value in rows),
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
    return "".join(
        f"\\{character}" if character in MARKDOWN_SPECIAL else character for character in str(text)
    )


# ---------------------------------------------------------------------------------------------
# The request signature (the web client's, from its JavaScript, seen 2026-11-11)
# ---------------------------------------------------------------------------------------------

def _digest(text: str) -> str:
    """The SHA-256 hex digest of `text` as UTF-8, as the web client computes it."""
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def sign_request(
    request: Request,
    *,
    now_ms: int,
    pick: Callable[[Sequence[str]], str] = secrets.choice,
    noise: Callable[[int], str] = secrets.token_hex,
) -> None:
    """Add LOIBUS's four signature headers to an API request, signed at `now_ms`.

    The signature covers the lowercase method, the path after `/api/` (without the query), and
    the time; the body is not part of it (the `||` is an empty slot the client leaves).

    Args:
        request: the request; its headers are changed in place.
        now_ms: the time, in Unix milliseconds.
        pick: picks the UUID variant nibble; tests pass a fixed choice.
        noise: gives random hex of a byte count; tests pass a fixed string.
    """
    where = urlsplit(request.url).path.removeprefix("/").removeprefix("api/")
    method, at = request.method.lower(), str(now_ms)
    request.headers.update({
        "x-security-challenge": "turnstile-v1",
        "x-request-id": request_id(method, where, now_ms=now_ms, pick=pick, noise=noise),
        "x-signature-timestamp": at,
        "x-request-signature": _digest(f"{method}|{where}||{at}|{SALT}"),
    })


def request_id(
    method: str,
    where: str,
    *,
    now_ms: int,
    pick: Callable[[Sequence[str]], str],
    noise: Callable[[int], str],
) -> str:
    """A UUID-shaped id: a time slot and random hex, with a digest of the request inside.

    Args:
        method: the lowercase HTTP method.
        where: the path after `/api/`.
        now_ms: the time, in Unix milliseconds.
        pick: picks the UUID variant nibble.
        noise: gives random hex of a byte count.

    Returns:
        The id, `xxxxxxxx-xxxx-4xxx-yxxx-xxxxxxxxxxxx`.
    """
    # The slot is the time in five-minute units (one prefix per slot); random hex fills the rest.
    seed = format(now_ms // 300_000, "x").zfill(8)[-8:] + noise(9)[:17]
    # The digest covers the prefix, the request, and the digest of an empty `{}` body.
    empty = _digest("{}")
    digest = _digest(f"{REQUEST_ID_PREFIX}|{seed}|{method}|{where}|{empty}")[:5]
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


# ---------------------------------------------------------------------------------------------
# Encrypted fields and the token
# ---------------------------------------------------------------------------------------------

def decrypt_field(text: str | None) -> str:
    """Decrypt one of the site's AES fields (a link, a password); an empty one stays empty.

    A field is base64 text of AES-128-CBC ciphertext; bytes that aren't UTF-8 become U+FFFD
    rather than fail.

    Args:
        text: the field as the API gives it; None or blank is an empty field.

    Returns:
        The plain text, or "" for an empty field.

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
    """`blob` decrypted with the site's AES-128-CBC key and IV, its PKCS7 padding removed.

    `cryptography` is imported here, so a run that decrypts nothing never loads it.

    Args:
        blob: the ciphertext, a whole number of 16-byte blocks.

    Returns:
        The plain bytes.

    Raises:
        ValueError: the blob isn't whole blocks, or its padding is wrong (a changed key).
    """
    from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
    from cryptography.hazmat.primitives.padding import PKCS7

    decryptor = Cipher(algorithms.AES(SITE_KEY), modes.CBC(SITE_IV)).decryptor()
    unpadder = PKCS7(128).unpadder()
    padded = decryptor.update(blob) + decryptor.finalize()
    return unpadder.update(padded) + unpadder.finalize()


def decode_token(raw: str) -> str:
    """The Bearer token of the `access-token` cookie: %-decoded, without a `Bearer ` prefix."""
    return unquote(raw).removeprefix("Bearer ").removeprefix("bearer ").strip()

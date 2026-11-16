"""LOIBUS (loix.cc): one post's info, preview images and, for VIP accounts, archives."""

from __future__ import annotations

import base64
import binascii
import datetime
import secrets
from pathlib import PurePosixPath
from typing import TYPE_CHECKING, Any, Callable, Iterator, Mapping, Sequence
from urllib.parse import unquote, urlsplit

from flnd.source import Changed, File, NotFound, Post, Site, Source, day, target
from flnd import _time as clock, _vault as crypto

if TYPE_CHECKING:
    from flnd._net import Request

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
# The site's own AES-128 key and IV, from its JavaScript; `_decrypt_aes_cbc` decrypts links.
SITE_KEY = b"7811595744111520"
SITE_IV = b"RgaXNyE5aWckBwI7"
# Archives a day per VIP level; an SVIP account gets twice as many.
VIP_DAILY_LIMITS = {1: 15, 2: 25, 3: 40, 4: 60}
# The characters Markdown reads as syntax in a heading, table cell, or link.
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
    fields = ("author", "title", "part")
    # Every file is named by its post (`File.name`), so this format is never used.
    filename = "{id}"
    # Every key is the file's path, as the history has always had it, so this never changes.
    key = ""

    @target(
        r"{id:\d+}",
        r"loix.cc/lois/{id:\d+}",
        r"moegoat.com/lois/{id:\d+}",
        from_=("photo", "archive"),
        folder="{author}/{id} {title}/{part}",
    )
    def post(self, site: Site, id: str) -> dict[str, Any]:
        """One post by id, or a post URL on either site."""
        body = site.fetch(f"{API}lois/{id}").json
        post = body.get("data") if isinstance(body, dict) else None
        if not isinstance(post, dict) or not post.get("loi_title"):
            raise NotFound(f"post {id} isn't there")
        return post

    @post.category
    def photo(self, site: Site, post: dict[str, Any]) -> Iterator[Post]:
        """The post's info.md, without links, and its preview images."""
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
        """The post's info.md, now with download links, and its archives (VIP quota applies)."""
        # The job skips a post outside `--time` too, but only after links were fetched and counted.
        if day(post.get("date")) not in site.time:
            return
        links = self._links(site, post)
        archives = group_mirrors(links) if links else []
        # Counted as soon as the links are in hand: the job's own checks (a file already saved)
        # come after the yield, and can't be told from here.
        if archives:
            count_download(site, post["id"])
        # The site's unzip password; the unpack step extracts each archive with it.
        password = str(post["open_key"]) if post.get("open_key") else None
        yield Post(
            post["id"],
            [
                File.text("info.md", info_md(post, links)),
                *(File(mirrors, name=name, password=password) for name, mirrors in archives),
            ],
            date=post.get("date"),
            headers=SITE_HEADERS,
            **fields_of(post),
        )

    def prepare(self, request: Request, cookies: Mapping[str, str], /) -> Request:
        """Set referer and origin for source domains, signing API requests with token."""
        request.headers.setdefault("referer", SITE_HEADERS["referer"])
        request.headers.setdefault("origin", SITE_HEADERS["origin"])
        # Only the API is signed and given the token; other hosts take referer and origin alone.
        if request.url.host != API_HOST:
            return request
        token = decode_token(cookies.get("access-token", ""))
        if token:
            request.headers["authorization"] = f"Bearer {token}"
        return sign_request(request, now_ms=_clock_ms())

    def _links(self, site: Site, post: dict[str, Any]) -> dict[str, Any] | None:
        """The post's decrypted download links, or None with a warning when unavailable."""
        if not decode_token(site.cookies.get("access-token", "")):
            site.warn("archives need a login; run flnd login loi")
            return None
        used, limit = quota(site, post["id"])
        # A limit of 0 means unknown (see `quota`), so only a known, used-up quota stops links.
        if limit and used >= limit:
            site.warn(
                "the daily VIP quota is used up ({used} of {limit}); archives skipped",
                used=used, limit=limit,
            )
            return None
        body = _fetch_links(site, post["id"])
        data = body.get("data") if isinstance(body, dict) else None
        if not isinstance(data, dict) or not data.get("sv_list"):
            if isinstance(body, dict) and _quota_kind(body) == "daily":
                _mark_exhausted(site)
                site.warn("the daily VIP quota is used up; archives skipped")
                return None
            needed = f"VIP{post['vip_requirement']}" if post.get("vip_requirement") else "a login"
            site.warn(
                "the site gives no download links for this post ({needed} needed)", needed=needed,
            )
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


def fields_of(post: dict[str, Any]) -> dict[str, Any]:
    """Folder fields: `author` (`misc` when absent) and `title`."""
    category = post.get("category") or {}
    return {"author": category.get("name") or "misc", "title": post["loi_title"]}


def image_name(number: int, url: str) -> str:
    """A preview image's name, `01.jpg`."""
    suffix = PurePosixPath(urlsplit(url).path).suffix
    return f"{number:02d}{suffix or '.jpg'}"


def group_mirrors(links: dict) -> list[tuple[str, list[str]]]:
    """Each archive's name, from its URL, and the mirror URLs that serve it."""
    groups: dict[str, list[str]] = {}
    for _, url in links["servers"]:
        name = unquote(PurePosixPath(urlsplit(url).path).name) or "archive.zip"
        groups.setdefault(name, []).append(url)
    return list(groups.items())


def _json(site: Site, url: str) -> Any:
    """A URL's JSON, or None when the site refuses it, so a caller warns rather than fails."""
    response = site.fetch(url, check=False)
    return response.json if response.ok else None


def _fetch_links(site: Site, post_id: int) -> Any:
    """Fetch download links once, backing off and retrying once on a rate-limit JSON response."""
    url = f"{API}user/loi/download_v6/{post_id}"
    body = _json(site, url)
    if isinstance(body, dict) and _quota_kind(body) == "rate":
        site.back_off("LOIBUS rate limit")
        body = _json(site, url)
    return body


def _quota_kind(body: Mapping[str, Any]) -> str | None:
    """Classify a failed JSON response as `'rate'`, `'daily'`, or None."""
    data = body.get("data")
    if isinstance(data, dict) and data.get("sv_list"):
        return None
    status = str(body.get("status") or "").casefold()
    code = body.get("code")
    msg = str(body.get("message") or body.get("msg") or "").casefold()
    if code == 429 or any(word in msg for word in ("rate", "too many", "频繁", "稍后")):
        return "rate"
    if status in ("error", "fail") or code in (403, 429):
        if any(word in msg for word in ("quota", "limit", "exceeded", "次数", "上限")):
            return "daily"
    return None


def _mark_exhausted(site: Site) -> None:
    """Record today's quota as exhausted in `site.state` so later posts skip link fetches."""
    today = datetime.date.today().isoformat()
    site.state["quota"] = {"day": today, "posts": today_posts(site), "exhausted": True}


def quota(site: Site, post_id: int) -> tuple[int, int]:
    """Archives taken today and daily VIP limit, 0 when unknown."""
    record = site.state.get("quota") or {}
    today = datetime.date.today().isoformat()
    taken = today_posts(site)
    if record.get("day") == today and record.get("exhausted"):
        limit = max(len(taken), 1)
        return limit, limit
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
    """Count a post against today's quota, once."""
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


def _info_rows(post: dict[str, Any]) -> list[tuple[str, str]]:
    """Build the Markdown table rows for a post's metadata."""
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
    """The post as Markdown: fields, unzip password, description, links, and preview images."""
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


def sign_request(
    request: Request,
    *,
    now_ms: int,
    pick: Callable[[Sequence[str]], str] = secrets.choice,
    noise: Callable[[int], str] = secrets.token_hex,
) -> Request:
    """Add LOIBUS's signature headers to an API request for now_ms."""
    where = urlsplit(str(request.url)).path.removeprefix("/").removeprefix("api/")
    method, at = request.method.lower(), str(now_ms)
    request.headers.update({
        "x-security-challenge": "turnstile-v1",
        "x-request-id": request_id(method, where, now_ms=now_ms, pick=pick, noise=noise),
        "x-signature-timestamp": at,
        "x-request-signature": crypto.digest(f"{method}|{where}||{at}|{SALT}"),
    })
    return request


def request_id(
    method: str,
    where: str,
    *,
    now_ms: int,
    pick: Callable[[Sequence[str]], str],
    noise: Callable[[int], str],
) -> str:
    """A UUID-shaped id: slot and random hex with a request digest inside."""
    # The slot is the time in five-minute units (one prefix per slot); random hex fills the rest.
    seed = format(now_ms // 300_000, "x").zfill(8)[-8:] + noise(9)[:17]
    # The digest covers the prefix, the request, and the digest of an empty `{}` body.
    empty = crypto.digest("{}")
    digest = crypto.digest(f"{REQUEST_ID_PREFIX}|{seed}|{method}|{where}|{empty}")[:5]
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
    """Decrypt one of the site's AES fields (a link, a password); an empty one stays empty."""
    text = (text or "").strip()
    if not text:
        return ""
    try:
        return _decrypt_aes_cbc(base64.b64decode(text)).decode("utf-8", "replace")
    # Bad base64 or invalid padding; site key change is the likely cause.
    except (binascii.Error, ValueError):
        raise Changed(
            f"loi: can't decrypt {text[:16]!r}…; the site changed its key"
        ) from None


def _decrypt_aes_cbc(blob: bytes) -> bytes:
    """`blob` decrypted with the site's AES-128-CBC key and IV, its PKCS7 padding removed."""
    from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
    from cryptography.hazmat.primitives.padding import PKCS7

    decryptor = Cipher(algorithms.AES(SITE_KEY), modes.CBC(SITE_IV)).decryptor()
    unpadder = PKCS7(128).unpadder()
    padded = decryptor.update(blob) + decryptor.finalize()
    return unpadder.update(padded) + unpadder.finalize()


def decode_token(raw: str) -> str:
    """The Bearer token from access-token cookie (%-decoded without prefix)."""
    return unquote(raw).removeprefix("Bearer ").removeprefix("bearer ").strip()


def _clock_ms() -> int:
    return int(clock.wall() * 1000)

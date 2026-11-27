"""SimpCity (smp): the images and videos of a forum thread, read over HTTP with the login's
cookies, newest page first. See SPEC.md#smp-simpcity.
"""

from __future__ import annotations

import asyncio
import base64
import binascii
import datetime
import functools
import ipaddress
import random
import re
from collections.abc import AsyncIterator, Awaitable, Callable, Iterable
from typing import Any, NamedTuple
from urllib.parse import parse_qs, urlsplit

from flnd.plugin import (
    Blocked,
    Browser,
    Changed,
    Ctx,
    Document,
    Element,
    File,
    Hidden,
    LoginError,
    NotFound,
    Plugin,
    Post,
    Request,
    Response,
    cookies,
    route,
)

FORUM = "https://simpcity.cr"
HINT = "give a thread or a post: smp:<id>, smp:post/<id>"
LOGIN_HINT = "export a fresh cookie file, then flnd auth login smp <file>"
CAPTCHA_HINT = "open simpcity.cr in your browser, solve it, then run again"
# Seconds a person gets to solve the forum's captcha in the browser window.
CAPTCHA_TIMEOUT = 600.0
# Forum pages between two long breaks, and the break's length in seconds (a person's pace).
BREAK_EVERY = 25
BREAK = (45.0, 90.0)
# The most seconds added to the plugin's pace before a forum page: the gap is `pace` to
# `pace + JITTER`, 5 to 12 s by default.
JITTER = 7.0
# What a post's content is read from, and the noise inside it that is never read: another
# post's quote, a link preview, a signature (its promotions).
BODY = ".message-body .bbWrapper"
NOISE = ("blockquote.bbCodeBlock--quote", ".bbCodeBlock--unfurl", ".message-signature")
READ = ("a[href]", "img.bbImage", "iframe")
# The query keys the forum's outbound redirect (`/<x>/?to=<base64 of the link>`) uses.
WRAPPED = ("to", "url", "u")
# The id of a host's file or album, as a path part: never `..` or a slash.
IDENT = re.compile(r"[\w-]{1,80}")
# A thumbnail's size mark (`.md` before the extension), which the full image's name lacks.
THUMB = re.compile(r"\.(?:md|th|fr)(?=\.[A-Za-z0-9]+$)")
IMAGE = re.compile(r"\.(?:jpe?g|png|gif|webp|avif)$", re.IGNORECASE)
PAGE = re.compile(r"/page-(\d+)")
SLUG = re.compile(r"/threads/([^/]+)/")
ALBUM_PAGE = 100
# What a host's resolver gives for one link: a file's URL and the host's own name for it.
Item = tuple[str, str | None]


class Link(NamedTuple):
    """One link of a post to a known host: which, of what kind, and its id."""

    host: str
    kind: str
    id: str


class Host(NamedTuple):
    """A file host: its domains, the category its files are in, how a link's id is read, and
    how its resolver turns an id into files."""

    name: str
    domains: tuple[str, ...]
    category: str
    ident: Callable[[str], tuple[str, str] | None]
    resolve: Callable[[Ctx, str, str], Awaitable[list[Item]]]


class _Post(NamedTuple):
    id: str
    stamp: int | None
    links: dict[str, list[Link]]


class _Page(NamedTuple):
    n: int
    more: bool
    posts: list[_Post]


class Simp(Plugin):
    """SimpCity threads: their images (goonbox, image hosts' thumbnails) and videos (turbo)."""

    code = "smp"
    name = "SimpCity"
    domains = ("simpcity.cr", "simpcity.su")
    # The forum is read over HTTP. A tab opens only to let a person solve its captcha, and the
    # captcha needs its images.
    browser = Browser.ANY
    assets = True
    login = cookies(match=r".+_user", required=True)
    walls = ("/login",)
    # The least gap between two forum pages; the plugin adds up to JITTER seconds to it.
    pace = 5.0
    folder = "{category}"
    filename = "{date}_{post}_{num}"
    key = "{post}_{num}"
    unsupported = {
        **{f"{host}/{path}": f"that page isn't supported; {HINT}"
           for host in domains for path in ("forums", "members", "search", "tags", "whats-new",
                                            "watched", "conversations", "account", "posts")},
        **{host: f"that page isn't supported; {HINT}" for host in domains},
    }

    class Settings(Plugin.Settings):
        """SimpCity settings under `[plugins.smp]`; `pace` is the engine's own key.

        Attributes:
            pages_per_hour: the most forum pages one run reads in an hour.
        """

        pages_per_hour: int = 300

    def __init__(self) -> None:
        # One instance serves a run: its threads' pages, its page count, and the captcha.
        self._threads: dict[str, _Thread] = {}
        self._pages = 0
        self._solves = 0
        self._failed = False
        self._solving = asyncio.Lock()

    @route(r"smp:{id:\d+}",
           *((f"{host}/threads/{{slug:[^/]*?}}.{{id:\\d+}}", "{id}") for host in domains),
           *((f"{host}/threads/{{id:\\d+}}", "{id}") for host in domains))
    async def thread(self, ctx: Ctx, id: str) -> dict[str, str]:
        """A thread, newest post first: `smp:123`, or `https://simpcity.cr/threads/example.123/`
        (anything after the id is ignored)."""
        return {"id": id}

    @thread.category
    async def images(self, ctx: Ctx, thread: dict[str, str]) -> AsyncIterator[Post]:
        """The inline images, and the images behind links to image hosts."""
        async for post in self._posts(ctx, thread["id"], "images"):
            yield post

    @thread.category
    async def videos(self, ctx: Ctx, thread: dict[str, str]) -> AsyncIterator[Post]:
        """The inline videos: turbo embeds and links."""
        async for post in self._posts(ctx, thread["id"], "videos"):
            yield post

    def blocked(self, response: Response) -> float | None:
        """Hold on DDoS-Guard's 403, which the engine's block rounds wait out."""
        if response.status == 403 and response.headers.get("server", "").lower().startswith(
                "ddos-guard"):
            return 30.0
        return None

    async def _posts(self, ctx: Ctx, id: str, category: str) -> AsyncIterator[Post]:
        """A thread's posts that have files in `category`, newest first, each lazy."""
        thread = self._threads.setdefault(id, _Thread(id))
        index = 0
        while (page := await thread.read(self, ctx, index)) is not None:
            index += 1
            for post in reversed(page.posts):
                links = post.links.get(category)
                if links:
                    files = functools.partial(_files, ctx, post.id, post.stamp, links)
                    yield Post(post.id, files, date=post.stamp)

    # -- the forum: pace, budget, captcha ---------------------------------------------------

    async def get(self, ctx: Ctx, url: str, referer: str | None) -> Response:
        """One forum page, at a person's pace; a captcha is solved by a person once.

        Raises:
            Blocked: the captcha was not solved, or DDoS-Guard kept its block.
            LoginError: the login is gone.
        """
        if self._failed:
            raise self._refusal()
        await self._ready(ctx)
        asked = Request(url, headers={"Referer": referer} if referer else None)
        seen = self._solves
        response = await self._send(ctx, asked)
        if _captcha(response):
            await self._solve(ctx, url, seen)
            self._tick(ctx)
            response = await self._send(ctx, asked)
            if _captcha(response):
                self._failed = True
                raise self._refusal()
        if response.html.attr("data-logged-in") == "false":
            raise LoginError("smp: the forum shows a logged-out page", hint=LOGIN_HINT)
        return response

    async def _send(self, ctx: Ctx, asked: Request) -> Response:
        try:
            return await ctx.fetch(asked)
        except Blocked as error:
            error.hint = error.hint or LOGIN_HINT
            raise

    def _refusal(self) -> Blocked:
        return Blocked("smp: simpcity.cr wants a captcha", hint=CAPTCHA_HINT, retryable=False)

    async def _solve(self, ctx: Ctx, url: str, seen: int) -> None:
        """Have a person solve the captcha in a tab; one at a time, the others wait for it."""
        async with self._solving:
            if self._failed:
                raise self._refusal()
            if self._solves != seen:
                return
            landed = await ctx.open(url)
            if _on_captcha(landed.url):
                async def gone() -> bool:
                    page = await ctx.document()
                    return (page.attr("data-logged-in") is not None
                            or page.first(".p-body, article.message") is not None)

                if not await ctx.handoff("Solve the SimpCity captcha in the browser window",
                                         until=gone, timeout=CAPTCHA_TIMEOUT):
                    self._failed = True
                    raise self._refusal()
            self._solves += 1
            budget = _budget(ctx)
            budget["cap"] = max(1, budget["cap"] // 2)
            ctx.state["budget"] = budget
            ctx.log("info", "smp: captcha solved; this hour's page budget is now {n}",
                    n=budget["cap"])

    async def _ready(self, ctx: Ctx) -> None:
        """Wait as a person would before a forum page: a gap, a break, the hourly budget."""
        budget = _budget(ctx)
        if budget["used"] >= budget["cap"]:
            now = ctx.time.wall()
            turn = (budget["hour"] + 1) * 3600
            ctx.log("info", "smp: pausing until {time} UTC, hourly page budget",
                    time=datetime.datetime.fromtimestamp(turn, datetime.UTC)
                    .strftime("%H:%M"))
            await ctx.pause(turn - now + 1, "hourly page budget")
        if self._pages and self._pages % BREAK_EVERY == 0:
            await ctx.pause(random.uniform(*BREAK), "a break from the forum")
        elif self._pages:
            await ctx.pause(random.uniform(0, JITTER), "reading the page")
        self._tick(ctx)

    def _tick(self, ctx: Ctx) -> None:
        """Count one forum page, against the run's breaks and the hour's budget."""
        self._pages += 1
        budget = _budget(ctx)
        budget["used"] += 1
        ctx.state["budget"] = budget


def _budget(ctx: Ctx) -> dict[str, int]:
    """This hour's budget from the plugin's state: `hour`, pages `used` and the `cap`."""
    hour = int(ctx.time.wall() // 3600)
    saved = ctx.state.get("budget")
    if isinstance(saved, dict) and saved.get("hour") == hour:
        return {"hour": hour, "used": int(saved.get("used", 0)),
                "cap": int(saved.get("cap", 1))}
    limit = getattr(ctx.settings, "pages_per_hour", 300)
    return {"hour": hour, "used": 0, "cap": max(1, limit)}


def _on_captcha(url: str) -> bool:
    return urlsplit(url).path.rstrip("/") == "/captcha"


def _captcha(response: Response) -> bool:
    """Whether the forum sent the page to its captcha, followed or not."""
    return _on_captcha(response.url) or (
        response.status in (301, 302, 303, 307, 308)
        and _on_captcha(response.headers.get("location", "")))


class _Thread:
    """A thread's pages, newest first, read once for the run and shared by its categories."""

    def __init__(self, id: str) -> None:
        self.id = id
        self.base = id
        self.pages: list[_Page] = []
        self.cache: dict[int, _Page] = {}
        self.cur = 0
        self.done = False
        self.referer: str | None = None
        self.lock = asyncio.Lock()

    async def read(self, owner: Simp, ctx: Ctx, index: int) -> _Page | None:
        """The `index`th page in reading order (0 is the newest); None past the first."""
        async with self.lock:
            while len(self.pages) <= index and not self.done:
                await self._advance(owner, ctx)
            return self.pages[index] if index < len(self.pages) else None

    async def _advance(self, owner: Simp, ctx: Ctx) -> None:
        if not self.pages:
            await self._first(owner, ctx)
        elif self.cur <= 1:
            self.done = True
        else:
            n = self.cur - 1
            page = self.cache.get(n) or await self._load(owner, ctx, f"page-{n}" if n > 1 else "")
            self._add(page)

    async def _first(self, owner: Simp, ctx: Ctx) -> None:
        """The newest page: from the last page a run saw, else through `/latest`."""
        key = f"thread:{self.id}"
        saved = ctx.state.get(key)
        page = None
        if isinstance(saved, dict) and isinstance(saved.get("page"), int) and saved["page"] > 0:
            self.base = str(saved.get("base") or self.id)
            try:
                page = await self._load(owner, ctx, f"page-{saved['page']}")
            except NotFound:
                page = None
            if page is not None and page.more:
                page = None
        if page is None:
            page = await self._load(owner, ctx, "latest")
        if not page.posts and page.n == 1:
            raise Changed(f"smp: thread {self.id} shows no posts",
                          hint="the forum's page may have changed; report it")
        ctx.state[key] = {"page": page.n, "base": self.base}
        self._add(page)

    def _add(self, page: _Page) -> None:
        self.pages.append(page)
        self.cur = page.n

    async def _load(self, owner: Simp, ctx: Ctx, tail: str) -> _Page:
        url = f"{FORUM}/threads/{self.base}/{tail}"
        response = await owner.get(ctx, url, self.referer)
        landed = response.url.split("#", 1)[0]
        found = SLUG.search(landed)
        if found and "." in found[1]:
            self.base = found[1]
        self.referer = landed
        page = _read_page(response.html, landed)
        self.cache[page.n] = page
        return page


def _read_page(document: Document, landed: str) -> _Page:
    """A thread page's number, whether newer pages follow it, and its posts in page order."""
    found = PAGE.search(landed)
    posts = []
    for article in document.css('article.message[data-content^="post-"]'):
        number = (article.attr("data-content") or "").removeprefix("post-")
        stamp = article.first("time.u-dt[data-timestamp]")
        written = stamp.attr("data-timestamp") if stamp is not None else None
        posts.append(_Post(number, int(written) if written and written.isdigit() else None,
                           _links(article)))
    return _Page(int(found[1]) if found else 1,
                 document.first("a.pageNav-jump--next") is not None, posts)


def _signature(item: Element) -> tuple[str, str]:
    return item.tag, item.attr("href") or item.attr("src") or item.attr("data-url") or ""


def _links(article: Element) -> dict[str, list[Link]]:
    """A post's links to known hosts by category, each once, in page order, noise left out."""
    selector = ", ".join(f"{BODY} {tag}" for tag in READ)
    noise = ", ".join(f"{BODY} {block} {tag}" for block in NOISE for tag in READ)
    skip: dict[tuple[str, str], int] = {}
    for item in article.css(noise):
        skip[_signature(item)] = skip.get(_signature(item), 0) + 1
    kept = []
    for item in article.css(selector):
        mark = _signature(item)
        if skip.get(mark):
            skip[mark] -= 1
        else:
            kept.append(item)
    # An image wrapped in a link to a known host is that link's, not a thumbnail of its own.
    wrapped = set()
    for item in kept:
        if item.tag == "a" and _link(_unwrap(item.attr("href") or "")) is not None:
            wrapped.update(img.attr("src") or "" for img in item.css("img.bbImage"))
    found: dict[str, list[Link]] = {}
    seen: set[Link] = set()
    for item in kept:
        if item.tag == "a":
            url = _unwrap(item.attr("href") or "")
        elif item.tag == "img":
            if (item.attr("src") or "") in wrapped:
                continue
            url = item.attr("src") or item.attr("data-url") or ""
        else:
            url = item.attr("src") or item.attr("data-src") or ""
        link = _link(url)
        if link is not None and link not in seen:
            seen.add(link)
            found.setdefault(_host(link.host).category, []).append(link)
    return found


def _unwrap(href: str) -> str:
    """The link behind the forum's outbound redirect (`?to=`, `url=`, `u=`; base64 or plain),
    decoded here and never requested; any other link as it is."""
    parts = urlsplit(href.strip())
    if parts.hostname and parts.hostname.lower() not in ("simpcity.cr", "simpcity.su"):
        return href.strip()
    query = parse_qs(parts.query)
    for key in WRAPPED:
        for value in query.get(key, ()):
            if value.lower().startswith(("http://", "https://")):
                return value
            try:
                padded = value.replace(" ", "+") + "=" * (-len(value) % 4)
                decoded = base64.b64decode(padded, altchars=b"-_").decode()
            except (binascii.Error, UnicodeDecodeError, ValueError):
                continue
            if decoded.lower().startswith(("http://", "https://")):
                return decoded
    return href.strip()


def safe(url: str) -> bool:
    """Whether `url` may be requested: http or https, a name that is not an address, not
    `localhost` and not a local name."""
    parts = urlsplit(url)
    host = (parts.hostname or "").lower()
    if parts.scheme.lower() not in ("http", "https") or not host:
        return False
    if host == "localhost" or host.endswith((".localhost", ".local", ".internal")):
        return False
    if re.fullmatch(r"[\d.]+|0x[0-9a-f.]+", host):
        return False
    try:
        ipaddress.ip_address(host)
    except ValueError:
        return True
    return False


def _link(url: str) -> Link | None:
    """`url` as a link to a known host's file; None for any other, or an unsafe one."""
    if not safe(url):
        return None
    host = (urlsplit(url).hostname or "").lower()
    for known in HOSTS:
        if any(host == domain or host.endswith("." + domain) for domain in known.domains):
            found = known.ident(url)
            return Link(known.name, *found) if found else None
    return None


def _host(name: str) -> Host:
    return next(known for known in HOSTS if known.name == name)


# ---------------------------------------------------------------------------------------------
# Hosts: a link's id, and the files behind it
# ---------------------------------------------------------------------------------------------

def _segments(url: str) -> list[str]:
    return [part for part in urlsplit(url).path.split("/") if part]


def _goonbox_ident(url: str) -> tuple[str, str] | None:
    """`/img/<slug>.<id>` or `/a/<slug>.<id>`: the kind and the id after the last dot."""
    parts = _segments(url)
    if len(parts) >= 2 and parts[0] in ("img", "a"):
        found = parts[1].rpartition(".")[2]
        if IDENT.fullmatch(found):
            return parts[0], found
    return None


def _turbo_ident(url: str) -> tuple[str, str] | None:
    """`/embed/<id>`, `/v/<id>` and `/d/<id>`: one video."""
    parts = _segments(url)
    if len(parts) >= 2 and parts[0] in ("embed", "v", "d") and IDENT.fullmatch(parts[1]):
        return "video", parts[1]
    return None


def _thumb_ident(url: str) -> tuple[str, str] | None:
    """A thumbnail on an image host's server: its full image, the size mark taken out."""
    parts = urlsplit(url)
    if not IMAGE.search(parts.path):
        return None
    head, _, name = parts.path.rpartition("/")
    return "image", parts._replace(path=f"{head}/{THUMB.sub('', name)}", fragment="").geturl()


async def _api(ctx: Ctx, host: Host, url: str) -> Any:
    """A host's JSON answer, which must come from the host's own domains."""
    response = await ctx.fetch(url, bucket=host.name)
    landed = (urlsplit(response.url).hostname or "").lower()
    if not any(landed == domain or landed.endswith("." + domain) for domain in host.domains):
        raise Changed(f"smp: {host.name} sent {url.split('?', 1)[0]} to another site")
    return response.json


def _first(body: Any, *keys: str) -> Any:
    """The first of `keys` a JSON object has, from the object, else from the object under
    `image`, `data` or `video`."""
    for level in (body, *(body.get(inner) for inner in ("image", "data", "video")
                          if isinstance(body, dict))):
        if isinstance(level, dict):
            for key in keys:
                if level.get(key):
                    return level[key]
    return None


def _item(host: str, found: Any, id: str, *names: str) -> Item:
    url = _first(found, *names)
    if not isinstance(url, str) or not safe(url):
        raise Changed(f"smp: {host} {id} has no {names[0]}",
                      hint="the host's answer may have changed; report it")
    name = _first(found, "original_filename")
    return url, name if isinstance(name, str) else None


async def _goonbox(ctx: Ctx, kind: str, id: str) -> list[Item]:
    """One image (`/api/images/<id>`), or each image of an album, `per_page=100` a page."""
    host = _host("goonbox")
    if kind == "img":
        return [_item("goonbox", await _api(ctx, host, f"https://goonbox.cr/api/images/{id}"),
                      id, "original_url")]
    items: list[Item] = []
    page, last = 1, 1
    while page <= last and page <= 1000:
        body = await _api(ctx, host, f"https://goonbox.cr/api/albums/{id}/images"
                                     f"?page={page}&per_page={ALBUM_PAGE}")
        listed = next((body[key] for key in ("images", "data", "items")
                       if isinstance(body, dict) and isinstance(body.get(key), list)), None)
        if listed is None:
            raise Changed(f"smp: goonbox album {id} lists no images",
                          hint="the host's answer may have changed; report it")
        items += [_item("goonbox", entry, id, "original_url") for entry in listed]
        info = body.get("pagination") if isinstance(body, dict) else None
        found = info.get("last_page") if isinstance(info, dict) else None
        last = found if isinstance(found, int) else page
        page += 1
    return items


async def _turbo(ctx: Ctx, kind: str, id: str) -> list[Item]:
    """One video, from a signed link that expires (`/api/sign?v=<id>`)."""
    body = await _api(ctx, _host("turbo"), f"https://turbo.cr/api/sign?v={id}")
    return [_item("turbo", body, id, "url")]


async def _thumb(ctx: Ctx, kind: str, id: str) -> list[Item]:
    """The full image of a thumbnail: its URL is the id; nothing is asked."""
    return [(id, None)]


# Each host's own request bucket is its name. Later steps add hosts here.
HOSTS = (
    Host("goonbox", ("goonbox.cr",), "images", _goonbox_ident, _goonbox),
    Host("turbo", ("turbo.cr",), "videos", _turbo_ident, _turbo),
    Host("thumb", ("cuckcapital.cr", "host.church", "jpg5.su"), "images", _thumb_ident, _thumb),
)


async def _files(ctx: Ctx, post: str, stamp: int | None, links: Iterable[Link]) -> list[File]:
    """A post's files for some links. A removed or changed file is told of and left out; the
    post fails only when it gives nothing, or on a block, so a retry finds the rest."""
    day = (datetime.datetime.fromtimestamp(stamp, datetime.UTC).date().isoformat()
           if stamp is not None else "")
    found: list[File] = []
    errors: list[Exception] = []
    for link in links:
        try:
            items = await _host(link.host).resolve(ctx, link.kind, link.id)
        except (NotFound, Changed, Hidden) as error:
            errors.append(error)
            ctx.log("warn", "smp: post {post}: {error}", post=post, error=str(error))
            continue
        for url, name in items:
            found.append(File(url, name=f"{day}_{post}_{name}" if name and day else None))
    if not found and errors:
        raise errors[0]
    return found

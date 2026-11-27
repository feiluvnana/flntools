"""pixiv (pxv): a user's illustrations, manga, ugoira and novels, and single artworks and novels,
read over HTTP from pixiv's web JSON API. See SPEC.md#pxv-pixiv.
"""

from __future__ import annotations

import asyncio
import datetime
import functools
import html
import json
import random
import re
from collections.abc import AsyncIterator, Mapping, Sequence
from pathlib import PurePosixPath
from typing import Any
from urllib.parse import urlsplit

from flnd.plugin import (
    Blocked,
    Browser,
    Changed,
    Ctx,
    Document,
    File,
    Hidden,
    LoginError,
    NotFound,
    Plugin,
    Post,
    Request,
    Response,
    SiteError,
    Target,
    UsageError,
    cookies,
    route,
)

SITE = "https://www.pixiv.net"
HINT = ("give a user, an artwork or a novel: pxv:12345678, pxv:artwork/12345678, "
        "pxv:novel/12345678")
LOGIN_HINT = "export a fresh cookie file, then flnd auth login pxv <file>"
LOGGED_OUT_HINT = "log in: flnd auth login pxv <file>"
SANITY_HINT = "log in, and turn on R-18 (and R-18G) in pixiv's settings: Viewing restrictions"
FRIENDS_HINT = "only the artist's My pixiv friends can see it"
CAPTCHA_HINT = "open www.pixiv.net in your browser, solve it, then run again"
REFERER = {"Referer": f"{SITE}/"}
# The most seconds a person gets to solve Cloudflare's check in the browser window.
CAPTCHA_TIMEOUT = 600
# API requests between two long breaks, and the break's length in seconds (a person's pace).
BREAK_EVERY = 100
BREAK = (30.0, 60.0)
# The most seconds added to the plugin's pace before an API request: the gap is 2 to 4 s.
JITTER = 2.0
# Work ids asked for in one `profile/illusts` request.
BATCH = 100
# What pixiv shows in a work's thumbnail when the login may not see it: `limit_<why>_<size>`.
LIMIT = re.compile(r"//s\.pximg\.net/common/images/limit_([a-z_]+?)(?:_\d+)?\.\w+")
LIMIT_HINTS = {"sanity_level": SANITY_HINT, "mypixiv": FRIENDS_HINT}
# What a user's number must be, as a page's user is only ever its number.
NAME = re.compile(r"@?[\w.-]+")
# The illustType each own category yields, and the kind of list it reads in `profile/all`.
KINDS = {"illusts": (0, "illusts"), "manga": (1, "manga"), "ugoira": (2, "illusts"),
         "novels": (None, "novels")}
CATEGORY_OF = {0: "illusts", 1: "manga", 2: "ugoira"}
UGOIRA = ("both", "mp4")
# What every unsupported page of pixiv says after its own reason.
GIVE = f"; {HINT}"
PAGES = ("ranking.php", "discovery", "request", "bookmark_new_illust.php", "stacc", "settings",
         "dashboard", "tags")


def _refused() -> dict[str, str]:
    """Every page of pixiv that is not a user, an artwork or a novel, as a pattern and a reason."""
    found = {"pixiv.net": f"that page isn't supported{GIVE}",
             "sketch.pixiv.net": f"pixiv Sketch isn't supported{GIVE}",
             "comic.pixiv.net": f"pixiv Comic isn't supported{GIVE}"}
    for lang in ("", "*/"):
        for page in PAGES:
            reason = ("tag pages aren't supported yet" if page == "tags"
                      else "that page isn't supported")
            found[f"pixiv.net/{lang}{page}"] = reason + GIVE
        found[f"pixiv.net/{lang}novel/series"] = f"series aren't supported{GIVE}"
        found[f"pixiv.net/{lang}user/*/series"] = f"series aren't supported{GIVE}"
    return found


class Pixiv(Plugin):
    """pixiv: a user's works, and one artwork or novel."""

    code = "pxv"
    name = "pixiv"
    domains = ("pixiv.net", "pixiv.me")
    # Everything goes over HTTP. A tab opens only to let a person solve Cloudflare's check, and
    # the check needs its images and scripts.
    browser = Browser.ANY
    assets = True
    login = cookies("PHPSESSID")
    # The least gap between two API requests; the plugin adds up to JITTER seconds to it.
    pace = 2.0
    # The image CDN: 4 files at once, never the default.
    limits = {"i.pximg.net": 4}
    folder = "{category}"
    filename = "{date}_{post}_p{num}"
    key = "{post}_{num}"
    fields = ("title", "user")
    unsupported = _refused()

    class Settings(Plugin.Settings):
        """pixiv settings under `[plugins.pxv]`; `pace` is the engine's own key.

        Attributes:
            requests_per_hour: the most API requests one run makes in an hour.
            ugoira: `"both"` saves the MP4, the original zip and a `.json` of the frames;
                `"mp4"` only the MP4. Anything else is read as `"both"`, with a warning.
        """

        requests_per_hour: int = 900
        ugoira: str = "both"

    @classmethod
    def resolve(cls, text: str) -> Target | None:
        """The routes' reading, then the name rule: a user is written by its number.

        Raises:
            UsageError: `pxv:<name>`, or one of the site's pages that isn't supported.
        """
        found = super().resolve(text)
        written = text.strip()
        if found is None and written[:4].lower() == "pxv:":
            value = written[4:]
            if NAME.fullmatch(value) and not value.isdigit():
                raise UsageError("pxv: a pixiv user is written by its number: pxv:12345678",
                                 hint="the number in pixiv.net/users/<number>; "
                                      "a pixiv.me/<name> link works too")
        return found

    def __init__(self) -> None:
        # One instance serves a run: its users' readers and its API request count.
        self._users: dict[str, _User] = {}
        self._calls = 0
        self._solves = 0
        self._failed = False
        self._warned = False
        self._in_page = False
        self._solving = asyncio.Lock()

    # -- routes -----------------------------------------------------------------------------

    @route(*((pattern, "artwork/{id}") for pattern in (
        r"pxv:artwork/{id:\d+}", r"pixiv.net/artworks/{id:\d+}",
        r"pixiv.net/*/artworks/{id:\d+}", r"pixiv.net/i/{id:\d+}",
        r"pixiv.net/member_illust.php?illust_id={id:\d+}",
    )), single=True)
    async def artwork(self, ctx: Ctx, id: str) -> AsyncIterator[Post]:
        """One artwork: `pxv:artwork/12345678`, or `https://www.pixiv.net/artworks/12345678`.

        Raises:
            NotFound: there is no such work.
            Hidden: the login may not see it.
        """
        id = id.removeprefix("artwork/")
        body = await self.ajax(ctx, f"illust/{id}", f"work {id}")
        owner = body.get("userId")
        kind = body.get("illustType")
        if not owner or not isinstance(kind, int):
            raise Changed(f"pxv: work {id} has no owner or type",
                          hint="pixiv's answer may have changed; report it")
        urls = body.get("urls") if isinstance(body.get("urls"), dict) else {}
        hidden = _masked(id, body, *urls.values())
        if hidden is not None:
            raise hidden
        category = CATEGORY_OF.get(kind)
        if category is None:
            ctx.log("warn", "pxv: work {id} is of an unknown type, {kind}", id=id, kind=kind)
            return
        if kind == 2:
            files: Any = functools.partial(self._ugoira, ctx, id, _stamp(body.get("createDate")))
        elif body.get("pageCount") == 1 and isinstance(urls.get("original"), str):
            files = (File(_image(id, urls["original"])),)
        else:
            files = functools.partial(self._pages, ctx, id)
        yield _post(id, body, files, owner=str(owner), category=category)

    @route(*((pattern, "novel/{id}") for pattern in (
        r"pxv:novel/{id:\d+}", r"pixiv.net/novel/show.php?id={id:\d+}",
        r"pixiv.net/*/novel/show.php?id={id:\d+}",
    )), single=True)
    async def novel(self, ctx: Ctx, id: str) -> AsyncIterator[Post]:
        """One novel: `pxv:novel/12345678`, or `https://www.pixiv.net/novel/show.php?id=12345678`.

        Raises:
            NotFound: there is no such novel.
            Hidden: the login may not see it.
        """
        id = id.removeprefix("novel/")
        body = await self.ajax(ctx, f"novel/{id}", f"novel {id}")
        body = _novel_body(id, body)
        owner = body.get("userId")
        if not owner:
            raise Changed(f"pxv: novel {id} has no owner",
                          hint="pixiv's answer may have changed; report it")
        hidden = _masked(id, body, body.get("coverUrl"))
        if hidden is not None:
            raise hidden
        files = functools.partial(self._novel, ctx, id, None, body)
        yield _post(id, body, files, owner=str(owner), category="novels")

    @route(*(pattern for pattern in (
        r"pxv:{id:\d+}", r"pixiv.net/users/{id:\d+}", r"pixiv.net/*/users/{id:\d+}",
        r"pixiv.net/member.php?id={id:\d+}", r"pixiv.net/member_illust.php?id={id:\d+}",
    )))
    async def user(self, ctx: Ctx, id: str) -> str:
        """A user's profile: `pxv:12345678`, or `https://www.pixiv.net/users/12345678`.

        Nothing is asked yet: the first category that needs the works reads them.
        """
        return id

    @user.category
    async def illusts(self, ctx: Ctx, user: str) -> AsyncIterator[Post]:
        """The illustrations (`illustType` 0), newest first."""
        async for post in self._posts(ctx, user, "illusts"):
            yield post

    @user.category
    async def manga(self, ctx: Ctx, user: str) -> AsyncIterator[Post]:
        """The manga (`illustType` 1), newest first."""
        async for post in self._posts(ctx, user, "manga"):
            yield post

    @user.category(folder="ugoira")
    async def ugoira(self, ctx: Ctx, user: str) -> AsyncIterator[Post]:
        """The animations (`illustType` 2), newest first, saved as MP4."""
        async for post in self._posts(ctx, user, "ugoira"):
            yield post

    @user.category(folder="novels", key="novel_{post}_{num}")
    async def novels(self, ctx: Ctx, user: str) -> AsyncIterator[Post]:
        """The novels, newest first: the text as Markdown, the cover and the inline images."""
        async for post in self._posts(ctx, user, "novels"):
            yield post

    def sign(self, request: Request, cookies: Mapping[str, str]) -> Request:
        """Add the web client's headers to an API request; any other is left as it is."""
        parts = urlsplit(request.url)
        if (parts.hostname or "").lower() not in ("www.pixiv.net", "pixiv.net"):
            return request
        if not parts.path.startswith("/ajax/"):
            return request
        sent = {**REFERER, "Accept": "application/json"}
        return request.replace(headers={**sent, **(request.headers or {})})

    def blocked(self, response: Response) -> float | None:
        """Hold on a 429, for its `Retry-After` when it gives one."""
        if response.status != 429:
            return None
        wait = response.headers.get("retry-after", "")
        return float(wait) if wait.isdigit() else 0.0

    # -- reading ----------------------------------------------------------------------------

    async def _posts(self, ctx: Ctx, user: str, category: str) -> AsyncIterator[Post]:
        """A user's works of `category`, newest first, each lazy; batches stop at the head."""
        reader = self._users.setdefault(user, _User(user))
        kind, listed = KINDS[category]
        ids = await reader.ids(self, ctx, listed)
        head = ctx.head
        for start in range(0, len(ids), BATCH):
            chosen = ids[start:start + BATCH]
            works = await reader.batch(self, ctx, listed, chosen)
            stamps = []
            for id in chosen:
                work = works.get(id)
                if not isinstance(work, dict):
                    continue
                made = _stamp(work.get("createDate"))
                if made is not None:
                    stamps.append(made)
                if kind is None:
                    yield _post(id, work, functools.partial(self._novel, ctx, id, work),
                                date=made)
                elif work.get("illustType") == kind:
                    files = (functools.partial(self._ugoira, ctx, id, made, work) if kind == 2
                             else functools.partial(self._pages, ctx, id, work))
                    yield _post(id, work, files, date=made)
            # Ids are in upload order, so once a whole batch is older than the head, so is every
            # later one.
            if stamps and _older(max(stamps), head):
                return

    async def _pages(self, ctx: Ctx, id: str, work: Mapping[str, Any] | None = None) -> list[File]:
        """One work's image files, one request; a masked work is `Hidden`."""
        if work is not None:
            hidden = _masked(id, work, work.get("url"))
            if hidden is not None:
                raise hidden
        body = await self.ajax(ctx, f"illust/{id}/pages", f"work {id}")
        if not isinstance(body, list) or not body:
            raise Changed(f"pxv: work {id} lists no pages",
                          hint="pixiv's answer may have changed; report it")
        found = []
        for page in body:
            urls = page.get("urls") if isinstance(page, dict) else None
            found.append(File(_image(id, urls.get("original") if isinstance(urls, dict) else None)))
        return found

    async def _novel(self, ctx: Ctx, id: str, work: Mapping[str, Any] | None = None,
                     body: Mapping[str, Any] | None = None) -> list[File]:
        """One novel's files: the Markdown text, the cover when it has its own, and the images
        its text shows. `body` is the answer already read; else it is asked for, one request,
        and the inserted illustrations take one more.

        Raises:
            Hidden: a masked novel.
            Changed: an answer with no text.
        """
        if work is not None:
            hidden = _masked(id, work, work.get("url"))
            if hidden is not None:
                raise hidden
        if body is None:
            body = _novel_body(id, await self.ajax(ctx, f"novel/{id}", f"novel {id}"))
        made = _stamp(body.get("createDate"))
        stem = f"{_day(made)}_{id}" if made is not None else id
        text = body["content"]
        embedded = body.get("textEmbeddedImages")
        found = _urls(embedded if isinstance(embedded, dict) else {})
        refs = list(dict.fromkeys(_tag(match[1]) for match in NOVEL_IMAGES.finditer(text)))
        inserted = [ref.removeprefix("pixivimage:") for ref in refs if ref[:5] == "pixiv"]
        if inserted:
            query = "&".join(f"id[]={ref}" for ref in inserted)
            shown = await self.ajax(ctx, f"novel/{id}/insert_illusts?{query}", f"novel {id}")
            shown = shown if isinstance(shown, dict) else {}
            for ref in inserted:
                url = _inserted(shown.get(ref))
                if url is None:
                    ctx.log("warn", "pxv: novel {id}'s image {ref} isn't visible; left as text",
                            id=id, ref=ref)
                else:
                    found["pixivimage:" + ref] = url
        names: dict[str, str] = {}
        images = []
        for ref in refs:
            url = found.get(ref)
            if url is None:
                continue
            names[ref] = f"{stem}_{len(names) + 1}{PurePosixPath(urlsplit(url).path).suffix}"
            images.append(File(url, name=names[ref]))
        files = [File.text(f"{stem}.md", _novel_text(body, names, made))]
        cover = _cover(body.get("coverUrl"))
        if cover:
            files.append(File(mirrors=cover, name=f"{stem}_cover"))
        return files + images

    async def _ugoira(self, ctx: Ctx, id: str, date: int | None,
                      work: Mapping[str, Any] | None = None) -> list[File]:
        """One animation's files, one request: the MP4 made from the frames' zip, and with the
        setting `both` the zip beside it and a `.json` of the frames named like the video."""
        if work is not None:
            hidden = _masked(id, work, work.get("url"))
            if hidden is not None:
                raise hidden
        body = await self.ajax(ctx, f"illust/{id}/ugoira_meta", f"work {id}")
        body = body if isinstance(body, dict) else {}
        frames = body.get("frames")
        delays = []
        for frame in frames if isinstance(frames, list) else ():
            wait = frame.get("delay") if isinstance(frame, dict) else None
            if (not isinstance(frame, dict) or not isinstance(frame.get("file"), str)
                    or not isinstance(wait, int) or isinstance(wait, bool) or wait <= 0):
                delays = []
                break
            delays.append((frame["file"], wait))
        zipped = body.get("originalSrc")
        if not delays or not (zipped or body.get("src")):
            raise Changed(f"pxv: work {id} has no frames or zip",
                          hint="pixiv's answer may have changed; report it")
        if not zipped:
            ctx.log("warn", "pxv: work {id} has no full-size zip; using the small one", id=id)
        both = self._ugoira_mode(ctx) == "both"
        video = File.frames(_image(id, zipped or body["src"]), delays, keep=both)
        if not both:
            return [video]
        stem = f"{_day(date)}_{id}_p0" if date is not None else f"{id}_p0"
        info = json.dumps({"mime_type": body.get("mime_type"), "frames": frames}, indent=2)
        return [video, File.text(f"{stem}.json", info)]

    def _ugoira_mode(self, ctx: Ctx) -> str:
        """The `ugoira` setting, `both` with one warning when it is neither value."""
        mode = getattr(ctx.settings, "ugoira", "both")
        if mode in UGOIRA:
            return mode
        if not self._warned:
            self._warned = True
            ctx.log("warn", "pxv: the ugoira setting is both or mp4, not {mode}; using both",
                    mode=str(mode))
        return "both"

    async def ajax(self, ctx: Ctx, path: str, what: str) -> Any:
        """One API answer's `body`, at a person's pace; the answer is read by status and shape.

        Raises:
            NotFound, Hidden, LoginError, Blocked, SiteError, Changed: as `_answer` says.
        """
        if self._failed:
            raise self._refusal()
        await self._ready(ctx)
        seen = self._solves
        response = await self._send(ctx, path)
        if _challenged(response):
            await self._solve(ctx, seen)
            self._tick(ctx)
            response = await self._send(ctx, path)
            if _challenged(response) and not self._in_page:
                # The clearance may be bound to the browser's own TLS and User-Agent.
                self._in_page = True
                ctx.log("info", "pxv: the check came back; API requests now go from the tab")
                self._tick(ctx)
                response = await self._send(ctx, path)
            if _challenged(response):
                self._failed = True
                raise self._refusal()
        return _answer(response, what, "PHPSESSID" in ctx.cookies)

    async def _send(self, ctx: Ctx, path: str) -> Response:
        return await ctx.fetch(Request(f"{SITE}/ajax/{path}", check=False),
                               in_page=self._in_page)

    def _refusal(self) -> Blocked:
        return Blocked("pxv: pixiv.net wants a captcha", hint=CAPTCHA_HINT, retryable=False)

    async def _solve(self, ctx: Ctx, seen: int) -> None:
        """Have a person solve Cloudflare's check in a tab; one at a time, the others wait."""
        async with self._solving:
            if self._failed:
                raise self._refusal()
            if self._solves != seen:
                return
            try:
                await ctx.open(f"{SITE}/")
            except LoginError:
                pass  # the challenge page's own 403; the tab still shows it

            async def gone() -> bool:
                return not _on_challenge(await ctx.document())

            if not await gone() and not await ctx.handoff(
                    "Solve the pixiv check in the browser window", until=gone,
                    timeout=CAPTCHA_TIMEOUT):
                self._failed = True
                raise self._refusal()
            self._solves += 1
            budget = _budget(ctx)
            budget["cap"] = max(1, budget["cap"] // 2)
            ctx.state["budget"] = budget
            ctx.log("info", "pxv: check solved; this hour's request budget is now {n}",
                    n=budget["cap"])

    async def _ready(self, ctx: Ctx) -> None:
        """Wait as a person would before an API request: a gap, a break, the hourly budget."""
        budget = _budget(ctx)
        if budget["used"] >= budget["cap"]:
            now = ctx.time.wall()
            turn = (budget["hour"] + 1) * 3600
            ctx.log("info", "pxv: pausing until {time} UTC, hourly request budget",
                    time=datetime.datetime.fromtimestamp(turn, datetime.UTC)
                    .strftime("%H:%M"))
            await ctx.pause(turn - now + 1, "hourly request budget")
        if self._calls and self._calls % BREAK_EVERY == 0:
            await ctx.pause(random.uniform(*BREAK), "a break from pixiv")
        elif self._calls:
            await ctx.pause(random.uniform(0, JITTER), "reading the answer")
        self._tick(ctx)

    def _tick(self, ctx: Ctx) -> None:
        """Count one API request, against the run's breaks and the hour's budget."""
        self._calls += 1
        budget = _budget(ctx)
        budget["used"] += 1
        ctx.state["budget"] = budget


def _budget(ctx: Ctx) -> dict[str, int]:
    """This hour's budget from the plugin's state: `hour`, requests `used` and the `cap`."""
    hour = int(ctx.time.wall() // 3600)
    saved = ctx.state.get("budget")
    if isinstance(saved, dict) and saved.get("hour") == hour:
        return {"hour": hour, "used": int(saved.get("used", 0)),
                "cap": int(saved.get("cap", 1))}
    limit = getattr(ctx.settings, "requests_per_hour", 900)
    return {"hour": hour, "used": 0, "cap": max(1, limit)}


def _challenged(response: Response) -> bool:
    """Whether pixiv.net answered with Cloudflare's check: a 403 with `cf-mitigated:
    challenge`, or an HTML page that is the check."""
    host = urlsplit(response.url).hostname or ""
    if host != "pixiv.net" and not host.endswith(".pixiv.net"):
        return False
    if response.status == 403 and response.headers.get("cf-mitigated") == "challenge":
        return True
    body = response.text
    return "<title>Just a moment...</title>" in body or "challenge-platform" in body


def _on_challenge(page: Document) -> bool:
    """Whether the tab shows Cloudflare's check."""
    title = page.first("title")
    return ((title is not None and title.text.strip() == "Just a moment...")
            or page.first('[src*="challenge-platform"]') is not None)


def _answer(response: Response, what: str, logged: bool) -> Any:
    """The `body` of an API answer. Only the status, `error`, and the shape are read, never the
    message text, which is in the account's language.

    Raises:
        LoginError: a 401.
        Blocked: a 429.
        NotFound: a 404, or `error` on an answer that isn't a refusal.
        Hidden: a 400 or 403 with `error`: R-18 setting, My pixiv or private.
        SiteError: another refusal.
        Changed: an answer that isn't pixiv's JSON shape.
    """
    status = response.status
    if status == 401:
        raise LoginError("pxv: pixiv refused the login", hint=LOGIN_HINT)
    if status == 429:
        raise Blocked("pxv: pixiv is limiting requests", hint="wait a while, then run again")
    if status == 404:
        raise NotFound(f"pxv: {what} doesn't exist")
    if status >= 400 and status not in (400, 403):
        raise SiteError(f"pxv: pixiv answered {status} for {what}")
    try:
        payload = response.json
    except Changed:
        if status >= 400:
            raise SiteError(f"pxv: pixiv answered {status} for {what}") from None
        raise Changed(f"pxv: pixiv's answer for {what} isn't JSON",
                      hint="pixiv's answer may have changed; report it") from None
    if not isinstance(payload, dict) or not isinstance(payload.get("error"), bool):
        raise Changed(f"pxv: pixiv's answer for {what} has an unknown shape",
                      hint="pixiv's answer may have changed; report it")
    if payload["error"]:
        if status >= 400:
            raise Hidden(f"pxv: {what} isn't visible to this login",
                         hint=None if logged else LOGGED_OUT_HINT)
        raise NotFound(f"pxv: {what} doesn't exist")
    if status >= 400 or "body" not in payload:
        raise Changed(f"pxv: pixiv's answer for {what} has no body",
                      hint="pixiv's answer may have changed; report it")
    return payload["body"]


def _masked(id: str, work: Mapping[str, Any], *thumbs: Any) -> Hidden | None:
    """`Hidden` when pixiv hides the work from this login (`isMasked`, or a `limit_` image in
    place of its thumbnail), with the hint for the reason it names; None when it shows."""
    why = None
    for thumb in thumbs:
        found = LIMIT.search(thumb) if isinstance(thumb, str) else None
        if found:
            why = found[1]
            break
    if not (work.get("isMasked") or why):
        return None
    return Hidden(f"pxv: work {id} isn't visible to this login", hint=LIMIT_HINTS.get(why or ""))


def _on_cdn(url: Any) -> bool:
    """Whether a URL is on pixiv's image CDN."""
    host = (urlsplit(url).hostname or "") if isinstance(url, str) else ""
    return host == "pximg.net" or host.endswith(".pximg.net")


def _image(id: str, url: Any) -> str:
    """A work's image URL, which must be on pixiv's image CDN."""
    if not _on_cdn(url):
        raise Changed(f"pxv: work {id} has no image URL",
                      hint="pixiv's answer may have changed; report it")
    return url


def _stamp(written: Any) -> int | None:
    """An ISO 8601 `createDate` as Unix seconds; None when it isn't one."""
    try:
        moment = datetime.datetime.fromisoformat(written)
    except (TypeError, ValueError):
        return None
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=datetime.UTC)
    return int(moment.timestamp())


def _day(seconds: int) -> str:
    """The UTC day of Unix seconds, as `{date}` names a file."""
    return datetime.datetime.fromtimestamp(seconds, datetime.UTC).date().isoformat()


def _older(newest: int, head: Mapping[str, Any] | None) -> bool:
    """Whether a time is strictly older than the sync head's, by its Unix seconds or its day."""
    when = head.get("ts") if head else None
    if isinstance(when, (int, float)) and not isinstance(when, bool):
        return newest < when
    if isinstance(when, str):
        try:
            return (datetime.datetime.fromtimestamp(newest, datetime.UTC).date()
                    < datetime.date.fromisoformat(when))
        except ValueError:
            return False
    return False


def _post(id: str, work: Mapping[str, Any], files: Any, *, date: int | None = None,
          **more: Any) -> Post:
    """A work as a post, its images downloaded with pixiv's Referer."""
    if date is None:
        date = _stamp(work.get("createDate"))
    return Post(id, files, date=date, headers=REFERER, title=str(work.get("title") or ""),
                user=str(work.get("userName") or ""), **more)


class _User:
    """A user's work ids and the works of each batch, read once for the run and shared by the
    categories."""

    def __init__(self, id: str) -> None:
        self.id = id
        self.lists: dict[str, list[str]] | None = None
        self.cache: dict[tuple[str, tuple[str, ...]], Mapping[str, Any]] = {}
        self.lock = asyncio.Lock()

    async def ids(self, owner: Pixiv, ctx: Ctx, listed: str) -> list[str]:
        """The ids of one list (`illusts`, `manga` or `novels`) from `profile/all`, newest first."""
        async with self.lock:
            if self.lists is None:
                body = await owner.ajax(ctx, f"user/{self.id}/profile/all", f"user {self.id}")
                self.lists = {name: _ids(self.id, body.get(name) if isinstance(body, dict)
                                         else None)
                              for name in ("illusts", "manga", "novels")}
            return self.lists[listed]

    async def batch(self, owner: Pixiv, ctx: Ctx, listed: str,
                    ids: Sequence[str]) -> Mapping[str, Any]:
        """The works of up to `BATCH` ids, by id."""
        mark = (listed, tuple(ids))
        async with self.lock:
            if mark not in self.cache:
                query = "&".join(f"ids[]={id}" for id in ids)
                path = (f"user/{self.id}/profile/novels?{query}" if listed == "novels"
                        else f"user/{self.id}/profile/illusts?{query}"
                             "&work_category=illustManga&is_first_page=0")
                body = await owner.ajax(ctx, path, f"user {self.id}")
                works = body.get("works") if isinstance(body, dict) else None
                if isinstance(works, list) and not works:
                    works = {}
                if not isinstance(works, dict):
                    raise Changed(f"pxv: user {self.id}'s works have an unknown shape",
                                  hint="pixiv's answer may have changed; report it")
                self.cache[mark] = works
            return self.cache[mark]


def _ids(user: str, found: Any) -> list[str]:
    """The work ids of a `profile/all` list, sorted as numbers, newest first: an object keyed by
    id, or `[]` when it is empty."""
    if found is None or found == []:
        return []
    if not isinstance(found, dict):
        raise Changed(f"pxv: user {user}'s work list has an unknown shape",
                      hint="pixiv's answer may have changed; report it")
    return sorted((key for key in found if str(key).isdigit()), key=int, reverse=True)


# A novel's image tags, as they stand in its text; the group is the tag inside the brackets.
IMAGE = r"\[((?:uploadedimage:\w+|pixivimage:\d+(?:-\d+)?))\]"
NOVEL_IMAGES = re.compile(IMAGE)
NOVEL_MARKUP = re.compile(
    r"\[\[rb:(?P<base>.+?) > (?P<ruby>.+?)\]\]|\[\[jumpuri:(?P<label>.+?) > (?P<url>.+?)\]\]"
    r"|\[chapter:(?P<chapter>.+?)\]|(?P<page>\[newpage\])|\[jump:(?P<jump>\d+)\]"
    r"|(?P<image>" + IMAGE + ")")


def _tag(tag: str) -> str:
    """An image tag's name: `pixivimage:<illust>` is page 1, `pixivimage:<illust>-1`."""
    return tag + "-1" if tag.startswith("pixivimage:") and "-" not in tag else tag


def _novel_body(id: str, body: Any) -> Mapping[str, Any]:
    """A novel's answer, which must hold its text."""
    if not isinstance(body, dict) or not isinstance(body.get("content"), str):
        raise Changed(f"pxv: novel {id} has no text",
                      hint="pixiv's answer may have changed; report it")
    return body


def _urls(embedded: Mapping[str, Any]) -> dict[str, str]:
    """The originals of a novel's uploaded images, by their tag `uploadedimage:<id>`."""
    found = {}
    for key, image in embedded.items():
        urls = image.get("urls") if isinstance(image, dict) else None
        url = urls.get("original") if isinstance(urls, dict) else None
        if _on_cdn(url):
            found[f"uploadedimage:{key}"] = url
    return found


def _inserted(shown: Any) -> str | None:
    """The original of one inserted illustration; None when it isn't visible or has none."""
    if not isinstance(shown, dict):
        return None
    illust = shown.get("illust")
    images = illust.get("images") if isinstance(illust, dict) else None
    url = images.get("original") if isinstance(images, dict) else None
    return url if shown.get("visible") is not False and _on_cdn(url) else None


def _cover(url: Any) -> list[str]:
    """A novel's cover original, as its two likely URLs (`.jpg`, `.png`); none for a default
    cover, which isn't a `novel-cover-master` image."""
    parts = urlsplit(url) if isinstance(url, str) else None
    if (parts is None or parts.scheme != "https" or parts.hostname != "i.pximg.net"
            or "/novel-cover-master/" not in parts.path):
        return []
    path = re.sub(r"^/c/[^/]+/", "/", parts.path).replace("novel-cover-master",
                                                          "novel-cover-original")
    stem = str(PurePosixPath(path).with_suffix("")).removesuffix("_master1200")
    return [f"https://i.pximg.net{stem}{ext}" for ext in (".jpg", ".png")]


def _novel_text(body: Mapping[str, Any], names: Mapping[str, str], made: int | None) -> str:
    """A novel as Markdown: a header, `---`, then its text with pixiv's tags converted. A tag it
    doesn't know stays as written; an image with no file name stays as its tag."""

    def convert(found: re.Match[str]) -> str:
        if found["base"] is not None:
            return f"<ruby>{found['base']}<rt>{found['ruby']}</rt></ruby>"
        if found["label"] is not None:
            url = found["url"].strip()
            return (f"[{found['label']}]({url})" if urlsplit(url).scheme in ("http", "https")
                    and not any(char in url for char in " ()") else found["label"])
        if found["chapter"] is not None:
            return f"## {found['chapter']}"
        if found["page"] is not None:
            return "---"
        if found["jump"] is not None:
            return f"(page {found['jump']})"
        tag = _tag(found["image"][1:-1])
        return f"![]({names[tag]})" if tag in names else found["image"]

    head = [f"# {body.get('title') or ''}", ""]
    if body.get("userId"):
        head.append(f"- Author: {SITE}/users/{body['userId']}")
    if made is not None:
        head.append(f"- Date: {_day(made)}")
    series = body.get("seriesNavData")
    if isinstance(series, dict) and series.get("title"):
        order = series.get("order")
        head.append(f"- Series: {series['title']}" + (f", no. {order}" if order else ""))
    tags = body.get("tags")
    tags = tags.get("tags") if isinstance(tags, dict) else None
    shown = [tag["tag"] for tag in tags if isinstance(tag, dict) and tag.get("tag")] if isinstance(
        tags, list) else []
    if shown:
        head.append(f"- Tags: {', '.join(shown)}")
    caption = body.get("description")
    if isinstance(caption, str) and caption.strip():
        caption = html.unescape(re.sub(r"<[^>]*>", "", re.sub(r"<br\s*/?>", "\n", caption)))
        head += ["", caption.strip()]
    return "\n".join(head) + "\n\n---\n\n" + NOVEL_MARKUP.sub(convert, body["content"]) + "\n"

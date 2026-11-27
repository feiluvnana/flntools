"""pixiv (pxv): a user's illustrations and manga, and single artworks, read over HTTP from
pixiv's web JSON API. See SPEC.md#pxv-pixiv.
"""

from __future__ import annotations

import asyncio
import datetime
import functools
import random
import re
from collections.abc import AsyncIterator, Mapping, Sequence
from typing import Any
from urllib.parse import urlsplit

from flnd.plugin import (
    Blocked,
    Changed,
    Ctx,
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
HINT = "give a user or an artwork: pxv:12345678, pxv:artwork/12345678"
LOGIN_HINT = "export a fresh cookie file, then flnd auth login pxv <file>"
LOGGED_OUT_HINT = "log in: flnd auth login pxv <file>"
SANITY_HINT = "log in, and turn on R-18 (and R-18G) in pixiv's settings: Viewing restrictions"
FRIENDS_HINT = "only the artist's My pixiv friends can see it"
CAPTCHA_HINT = "open www.pixiv.net in your browser, solve it, then run again"
REFERER = {"Referer": f"{SITE}/"}
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
KINDS = {"illusts": (0, "illusts"), "manga": (1, "manga")}
CATEGORY_OF = {0: "illusts", 1: "manga"}
# What every unsupported page of pixiv says after its own reason.
GIVE = f"; {HINT}"
PAGES = ("ranking.php", "discovery", "request", "bookmark_new_illust.php", "stacc", "settings",
         "dashboard", "tags")


def _refused() -> dict[str, str]:
    """Every page of pixiv that is not a user or an artwork, as a pattern and its reason."""
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
    """pixiv: a user's illustrations and manga, and one artwork."""

    code = "pxv"
    name = "pixiv"
    domains = ("pixiv.net", "pixiv.me")
    # Everything goes over HTTP; a browser comes only with the captcha step.
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
        """

        requests_per_hour: int = 900

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
            ctx.log("warn", "pxv: work {id} is an ugoira, which isn't saved yet", id=id)
            return
        if body.get("pageCount") == 1 and isinstance(urls.get("original"), str):
            files: Any = (File(_image(id, urls["original"])),)
        else:
            files = functools.partial(self._pages, ctx, id)
        yield _post(id, body, files, owner=str(owner), category=category)

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
                if work.get("illustType") == kind:
                    files = functools.partial(self._pages, ctx, id, work)
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

    async def ajax(self, ctx: Ctx, path: str, what: str) -> Any:
        """One API answer's `body`, at a person's pace; the answer is read by status and shape.

        Raises:
            NotFound, Hidden, LoginError, Blocked, SiteError, Changed: as `_answer` says.
        """
        await self._ready(ctx)
        response = await ctx.fetch(Request(f"{SITE}/ajax/{path}", check=False))
        return _answer(response, what, "PHPSESSID" in ctx.cookies)

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


def _answer(response: Response, what: str, logged: bool) -> Any:
    """The `body` of an API answer. Only the status, `error`, and the shape are read, never the
    message text, which is in the account's language.

    Raises:
        LoginError: a 401.
        Blocked: a 429, or a Cloudflare challenge.
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
    if status == 403 and "cf-mitigated" in response.headers:
        raise Blocked("pxv: pixiv.net wants a captcha", hint=CAPTCHA_HINT, retryable=False)
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


def _image(id: str, url: Any) -> str:
    """A work's image URL, which must be on pixiv's image CDN."""
    host = (urlsplit(url).hostname or "") if isinstance(url, str) else ""
    if host != "pximg.net" and not host.endswith(".pximg.net"):
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
        """The ids of one list (`illusts` or `manga`) from `profile/all`, newest first."""
        async with self.lock:
            if self.lists is None:
                body = await owner.ajax(ctx, f"user/{self.id}/profile/all", f"user {self.id}")
                self.lists = {name: _ids(self.id, body.get(name) if isinstance(body, dict)
                                         else None)
                              for name in ("illusts", "manga")}
            return self.lists[listed]

    async def batch(self, owner: Pixiv, ctx: Ctx, listed: str,
                    ids: Sequence[str]) -> Mapping[str, Any]:
        """The works of up to `BATCH` ids, by id."""
        mark = (listed, tuple(ids))
        async with self.lock:
            if mark not in self.cache:
                query = "&".join(f"ids[]={id}" for id in ids)
                body = await owner.ajax(
                    ctx, f"user/{self.id}/profile/illusts?{query}"
                         "&work_category=illustManga&is_first_page=0", f"user {self.id}")
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

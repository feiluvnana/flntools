"""What a source declares, and its optional hooks.

A source only says what it wants and reports what it finds, through the session. It never
imports httpx, Playwright, `worker`, `kv`, `log` or `prompt`; a test checks that.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from contextlib import nullcontext
from typing import TYPE_CHECKING, Any, Callable, ClassVar, Mapping, Union

from flnd.target import Target

if TYPE_CHECKING:
    import httpx

    from flnd.session import Session

# Seconds before a retry, or a function of (error, attempt) giving them; None means the usual wait.
Wait = Union[float, Callable[[BaseException, int], Union[float, None]]]


class FetchError(Exception):
    """A request that failed: `status` is the refusing HTTP status, or None (no response, or not JSON).

        try:
            data = session.fetch_json(url)
        except FetchError as error:
            if error.status == 403: ...
    """

    def __init__(self, message: str, *, status: int | None = None):
        super().__init__(message)
        self.status = status


class LoginError(Exception):
    """A login that didn't work, with what to do instead: `LoginError("tlg: run flnd login tlg")`."""


class Source(ABC):
    """One site flnd downloads from: what it declares, `resolve`, and `run`.

        class Instagram(Source):
            code, name, domains = "ins", "Instagram", ("instagram.com",)
            needs = ("sessionid",)

            def resolve(self, text): ...
            def run(self, session): session.save(url, "a.jpg", category="posts")

    Every subclass is listed in `flnd.sources.SOURCES`.
    """

    # The prefix in a target and the cookie file name, such as "ins".
    code: ClassVar[str]
    # Shown in help, such as "Instagram".
    name: ClassVar[str]
    # The site's domains: for bare URLs, and the cookies a login keeps.
    domains: ClassVar[tuple[str, ...]]
    # Short-link hosts (also in `domains`), followed with one HEAD request before `resolve`.
    short_links: ClassVar[tuple[str, ...]] = ()
    # What a target can point to, and one example per accepted form, for help and errors.
    kinds_of_target: ClassVar[tuple[str, ...]] = ("user",)
    examples: ClassVar[tuple[str, ...]] = ()
    # What `--from` can take from this source; without `--from`, a target gets every one.
    categories: ClassVar[tuple[str, ...]] = ()
    # Target kinds that are one post, downloaded whole (ins "post", tik "video"). A post has no
    # categories (tagged or reposts describe a profile's tab, not a post), so `--from` with one is
    # a usage error rather than a filter that silently drops every file.
    single_kinds: ClassVar[tuple[str, ...]] = ()
    # Cookie names, any one of which proves a login.
    needs: ClassVar[tuple[str, ...]] = ()
    requires_login: ClassVar[bool] = False
    login_methods: ClassVar[tuple[str, ...]] = ("browser", "file")
    # For this source's HTTP requests only; the browser always keeps its own.
    user_agent: ClassVar[str | None] = None
    viewport: ClassVar[tuple[int, int]] = (1280, 900)
    browser_args: ClassVar[tuple[str, ...]] = ()
    stealth: ClassVar[bool] = True
    # Files downloading at once, and seconds between two targets (with ±40% jitter).
    concurrency: ClassVar[int] = 4
    pause: ClassVar[float] = 5.0
    wait: ClassVar[Wait] = 1.0

    @abstractmethod
    def resolve(self, text: str) -> Target:
        """Turn one accepted form (a name, an id, a URL) into a `Target`, or raise `TargetError`."""

    @abstractmethod
    def run(self, session: Session) -> Any:
        """Scrape one target, reporting each file with `session.save`. It may be `async def`."""

    def headers(self, cookies: Mapping[str, str]) -> Mapping[str, str]:
        """Extra headers for every request, built from the login's `{name: value}` cookies."""
        return {}

    def sign(self, request: httpx.Request) -> httpx.Request:
        """Sign a request; it runs before every attempt, retries included, so timestamps stay fresh."""
        return request

    def login(self, session: Session, method: str) -> None:
        """Log in by `method`, for a source with its own way (tlg).

        Cookie sources keep this default, and the engine's `browser` and `file` methods log in.
        A login that fails raises `LoginError`, which `flnd login` reports.
        """
        raise NotImplementedError(f"{self.code} has no login method {method!r}")

    def logged_in(self, session: Session) -> bool:
        """Whether a login is stored: by default, one of `needs` is among the saved cookies."""
        return not self.needs or any(name in session.cookies for name in self.needs)

    def connect(self, session: Session) -> Any:
        """A context manager, or an async one, giving `session.client`; entered once per run."""
        return nullcontext()

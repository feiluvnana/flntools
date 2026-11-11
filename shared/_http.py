"""`http.Client` is an `httpx.Client` with sensible defaults, retries, and `json`, `html`, `download`."""

from __future__ import annotations

import email.utils
import time
from pathlib import Path
from types import SimpleNamespace
from typing import Any

try:
    import httpx
except ImportError:
    raise ImportError("shared.http needs httpx: pip install 'flntools[http]'") from None

from shared import _fmt as fmt
from shared import _path as path
from shared._log import log

agents = SimpleNamespace(
    chrome=("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/128.0.0.0 Safari/537.36"),
    safari=("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) "
            "Version/17.6 Safari/605.1.15"),
    iphone=("Mozilla/5.0 (iPhone; CPU iPhone OS 17_6 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) "
            "Version/17.6 Mobile/15E148 Safari/604.1"),
    android=("Mozilla/5.0 (Linux; Android 14; Pixel 8) AppleWebKit/537.36 (KHTML, like Gecko) "
             "Chrome/128.0.0.0 Mobile Safari/537.36"),
)

RETRY_METHODS = {"GET", "HEAD", "PUT", "DELETE", "OPTIONS"}
RETRY_ERRORS = (httpx.NetworkError, httpx.ConnectTimeout, httpx.RemoteProtocolError)
MAX_RETRY_AFTER = 60.0
CHUNK_SIZE = 64 * 1024
# The request extension that carries `request(..., retry=True)` down to the retry transport.
RETRY_EXTENSION = "shared.retry"
# Retry waits read time through this, so tests can patch it.
_sleep = time.sleep


class JSONError(ValueError):
    """A response that should have been JSON was not."""

    def __init__(self, response: httpx.Response):
        self.status = response.status_code
        self.content_type = response.headers.get("content-type", "")
        self.body = response.text[:200]
        super().__init__(f"expected JSON from {response.request.url}, got {self.status} "
                         f"{self.content_type or 'no content type'}: {self.body!r}")


class Client(httpx.Client):
    """An `httpx.Client`. Everything httpx offers works unchanged.

    `retries` extra attempts follow a connection error, a 429, or a 5xx, waiting 1 s, 2 s,
    then 4 s, or the server's Retry-After up to 60 s. Only idempotent methods are retried
    unless a request passes `retry=True`.
    """

    def __init__(self, *, user_agent: str | None = agents.chrome, headers: Any = None, cookies: Any = None,
                 timeout: Any = 30.0, retries: int = 2, follow_redirects: bool = True, **httpx_kwargs: Any):
        merged = httpx.Headers({"User-Agent": user_agent} if user_agent else {})
        merged.update(httpx.Headers(headers or {}))
        super().__init__(headers=merged, cookies=_cookie_jar(cookies), timeout=timeout,
                         follow_redirects=follow_redirects, **httpx_kwargs)
        self.retries = retries
        # httpx builds its transports from verify, proxy, limits, and so on; each one is wrapped.
        self._transport = RetryTransport(self._transport, retries)
        self._mounts = {pattern: transport and RetryTransport(transport, retries)
                        for pattern, transport in self._mounts.items()}

    def request(self, method: str, url: Any, *, retry: bool | None = None, **kwargs: Any) -> httpx.Response:
        """`httpx.Client.request`, plus `retry=True` to retry a POST or `retry=False` to never retry."""
        if retry is not None:
            kwargs["extensions"] = {**(kwargs.get("extensions") or {}), RETRY_EXTENSION: retry}
        return super().request(method, url, **kwargs)

    def json(self, url: Any, *, method: str = "GET", **request_kwargs: Any) -> Any:
        """Send, raise on a 4xx or 5xx, and parse JSON. A body that is not JSON raises `JSONError`."""
        response = self.request(method, url, **request_kwargs)
        response.raise_for_status()
        try:
            return response.json()
        except ValueError:
            raise JSONError(response) from None

    def html(self, url: Any, *, method: str = "GET", **request_kwargs: Any) -> Any:
        """Send, raise on a 4xx or 5xx, and return a `parsel.Selector`. Needs the scrape extra."""
        from shared import _scrape as scrape

        response = self.request(method, url, **request_kwargs)
        response.raise_for_status()
        return scrape.select(response)

    def download(self, url: Any, dest: Any, *, label: str | None = None, skip: bool = True,
                 headers: Any = None) -> bool:
        """Stream `url` into `dest` atomically under a progress bar. Returns False when skipped.

        A status other than 200 or 206 raises `httpx.HTTPStatusError` and leaves no file.
        """
        dest = Path(dest)
        if skip and dest.is_file() and dest.stat().st_size > 0:
            log.info("skip {name}", name=dest.name)
            return False
        with self.stream("GET", url, headers=headers) as response:
            length = response.headers.get("content-length", "")
            total = int(length) if length.isdigit() else None
            with log.progress(label or dest.name, total=total, show=fmt.bytes) as bar:
                if response.status_code not in (200, 206):
                    raise httpx.HTTPStatusError(f"HTTP {response.status_code} for {url}",
                                                request=response.request, response=response)
                written = 0
                with path.atomic(dest) as file:
                    for chunk in response.iter_bytes(CHUNK_SIZE):
                        file.write(chunk)
                        written += len(chunk)
                        # Content-Length counts bytes on the wire, which differ from `written` when compressed.
                        bar.update(response.num_bytes_downloaded or written)
        return True


def download(url: Any, dest: Any, **kwargs: Any) -> bool:
    """`Client.download` with a short-lived default client, for one-off scripts."""
    with Client() as client:
        return client.download(url, dest, **kwargs)


class RetryTransport(httpx.BaseTransport):
    """Wraps another transport and retries connection errors, 429, and 5xx responses."""

    def __init__(self, inner: httpx.BaseTransport, retries: int):
        self.inner = inner
        self.retries = retries

    def handle_request(self, request: httpx.Request) -> httpx.Response:
        opted = request.extensions.get(RETRY_EXTENSION)
        retries = self.retries if (request.method in RETRY_METHODS if opted is None else opted) else 0
        attempt = 0
        while True:
            attempt += 1
            try:
                response = self.inner.handle_request(request)
            except RETRY_ERRORS as error:
                if attempt > retries:
                    raise
                self._wait(request, attempt, f"{type(error).__name__} {error}".strip(), None)
                continue
            if attempt > retries or not (response.status_code == 429 or response.status_code >= 500):
                return response
            response.close()
            self._wait(request, attempt, str(response.status_code), response.headers.get("retry-after"))

    def close(self) -> None:
        self.inner.close()

    def _wait(self, request: httpx.Request, attempt: int, reason: str, retry_after: str | None) -> None:
        log.debug("retry {n} {url}: {reason}", n=attempt, url=request.url, reason=reason)
        server_wait = _retry_after_seconds(retry_after)
        _sleep(min(server_wait, MAX_RETRY_AFTER) if server_wait is not None else 2.0 ** (attempt - 1))


def _retry_after_seconds(value: str | None) -> float | None:
    """Retry-After as seconds: either a number or an HTTP date."""
    if not value:
        return None
    if value.strip().replace(".", "", 1).isdigit():
        return float(value)
    try:
        when = email.utils.parsedate_to_datetime(value)
    except (TypeError, ValueError):
        return None
    return max(0.0, when.timestamp() - time.time())


def _cookie_jar(cookies: Any) -> Any:
    """A `list[dict]` from `flnd.cookies.load_cookies` becomes `httpx.Cookies`, keeping domains and paths."""
    if not isinstance(cookies, list):
        return cookies
    jar = httpx.Cookies()
    for cookie in cookies:
        jar.set(cookie["name"], cookie["value"], domain=cookie.get("domain", ""), path=cookie.get("path", "/"))
    return jar

"""The web (`web`): any link no other plugin claims, as a file, an HLS or DASH stream, or a page's
video and images. See COOKBOOK.md#web-the-web.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from urllib.parse import unquote, urljoin, urlsplit

from flnd.plugin import Browser, Ctx, Document, File, NotFound, Plugin, Post, Request, route

# Extensions that name a file, so a link ending in one is downloaded as it is, without asking
# the server first. Any other link is asked with HEAD.
FILES = frozenset({
    ".jpg", ".jpeg", ".png", ".gif", ".webp", ".avif", ".bmp", ".heic", ".heif", ".svg", ".tif",
    ".tiff", ".mp4", ".m4v", ".mov", ".webm", ".mkv", ".avi", ".flv", ".wmv", ".ts", ".3gp",
    ".mp3", ".m4a", ".aac", ".ogg", ".oga", ".opus", ".wav", ".flac", ".zip", ".7z", ".rar",
    ".tar", ".gz", ".tgz", ".bz2", ".xz", ".pdf", ".epub", ".cbz", ".cbr", ".torrent", ".apk",
    ".dmg", ".exe", ".iso",
})
# A playlist, by its extension, and by its Content-Type.
PLAYLISTS = frozenset({".m3u8", ".mpd"})
PLAYLIST_TYPES = frozenset({
    "application/vnd.apple.mpegurl", "application/x-mpegurl", "audio/mpegurl", "audio/x-mpegurl",
    "application/dash+xml",
})
PAGE_TYPES = frozenset({"text/html", "application/xhtml+xml"})
# The `og:video:type`s that are a player, not a file: an embed page, a Flash player.
PLAYERS = frozenset({"text/html", "application/x-shockwave-flash"})


class Web(Plugin):
    """Any link: a file as it is, a stream by its playlist, or a page's video and images."""

    code = "web"
    name = "the web"
    api = 2
    # It owns no domain: the resolver gives it links no other plugin claims, so every request
    # is a file job, paced by the host's file cap, with no cookies and no walls.
    browser = Browser.NONE
    # Each link is its own target folder, and its route has no category, so the default
    # `{category}` folder is "": the files go straight in it. Named by the link's last path
    # part (`clip`, `watch`), or its host; a second file of the same name takes its number.
    filename = "{name}"
    fields = ("name",)

    @route(r"web:{url:(?i:https?)://\S+}", single=True, newest_first=False)
    async def page(self, ctx: Ctx, url: str) -> AsyncIterator[Post]:
        """Any link: `web:https://example.com/clip.mp4`, `web:https://example.com/watch/42`,
        or the link alone. Nothing is fetched until the link is known to be new.

        Raises:
            NotFound: the link answered 404 or 410, or its page shows no video or image.
        """
        # The fragment is never sent. The post (so the history key) and the Referer are the
        # link without its `user:pw@`, so a credential is never stored or sent on.
        link = url.split("#", 1)[0]
        clean = keyed(link)

        async def files() -> list[File]:
            return await media(ctx, link)

        # Every file is fetched with the link as its Referer, as a browser fetches a page's.
        yield Post(clean, files, headers={"Referer": clean}, name=name_of(link))


async def media(ctx: Ctx, link: str) -> list[File]:
    """The files of a link: by its extension, else by the Content-Type of a HEAD request.

    A server that refuses HEAD (405, 501, a presigned link's 403) gets a GET of one byte.

    Raises:
        NotFound: a 404 or 410, or a page with no video or image.
    """
    extension = suffix(link)
    if extension in PLAYLISTS:
        return [File.stream(link)]
    if extension in FILES:
        return [File(link)]
    head = await ctx.fetch(Request(link, method="HEAD", check=False))
    if head.status in (404, 410):
        raise NotFound(f"web: HTTP {head.status} for {keyed(link)}")
    if not head.ok:
        # A body that isn't HTML is the file. A server that ignores Range sends its whole body:
        # only HTML is read, and a cap on the rest is the engine's fetch's. An HTML 206 is a
        # page cut to one byte, so it is fetched whole.
        part = await ctx.fetch(Request(link, headers={"Range": "bytes=0-0"}))
        if kind(part.headers.get("content-type", "")) not in PAGE_TYPES:
            total = part.headers.get("content-range", "").rpartition("/")[2]
            return [File(link, size=int(total) if total.isdigit() else None)]
        page = await ctx.fetch(link) if part.status == 206 else part
        return found(page.html, page.url or link, link)
    given = kind(head.headers.get("content-type", ""))
    if given in PLAYLIST_TYPES:
        return [File.stream(link)]
    if given in PAGE_TYPES:
        page = await ctx.fetch(link)
        return found(page.html, page.url or link, link)
    size = head.headers.get("content-length", "")
    return [File(link, size=int(size) if size.isdigit() else None)]


def found(document: Document, landed: str, link: str) -> list[File]:
    """A page's media in page order: its `og:video`s (players left out), `<video>` sources and
    `og:image`s, each once without its fragment, a playlist as a stream fetched with the page
    as its Referer. URLs join to an http(s) `<base href>`, else to where the page landed; one
    that is the page or the link is no media.

    Raises:
        NotFound: the page shows none.
    """
    landed = landed.split("#", 1)[0]
    base = landed
    tag = document.first("base[href]")
    if tag is not None:
        given = urljoin(landed, (tag.attr("href") or "").strip())
        base = given if web(given) else landed
    choices = [[item.get("secure_url", ""), item.get("url", "")]
               for item in structured(document, "og:video")
               if kind(item.get("type", "")) not in PLAYERS]
    choices += [[tag.attr("src") or ""] for tag in document.css("video[src], video source[src]")]
    choices += [[item.get("secure_url", ""), item.get("url", "")]
                for item in structured(document, "og:image")]
    # Compared as keys, so the page with or without its `user:pw@` is the page.
    files, seen = [], {keyed(landed), keyed(link)}
    for values in choices:
        # The first value that is a link on the web: a bad `secure_url` leaves its `url`.
        url = next((joined for value in values if value.strip() not in ("", "#")
                    and web(joined := urljoin(base, value.strip()).split("#", 1)[0])), None)
        if url is None or keyed(url) in seen:
            continue
        seen.add(keyed(url))
        extension = suffix(url)
        files.append(File.stream(url, headers={"Referer": keyed(landed)})
                     if extension in PLAYLISTS else File(url))
    if not files:
        raise NotFound(f"web: {keyed(landed)} shows no video or image")
    return files


def structured(document: Document, root: str) -> list[dict[str, str]]:
    """Each Open Graph `root` (`og:video`) of a page, with its structured properties.

    `<root>` (or `<root>:url`) and `<root>:secure_url` start an item when the item before has
    a different one, else fill it in. Any other `<root>:<key>` (`type`) describes the item
    before; with none before, it says nothing.
    """
    items: list[dict[str, str]] = []
    for meta in document.css("meta"):
        written = (meta.attr("property") or meta.attr("name") or "").strip().lower()
        content = (meta.attr("content") or "").strip()
        if not content or (written != root and not written.startswith(root + ":")):
            continue
        key = written[len(root) + 1:] or "url"
        if key in ("url", "secure_url"):
            if not items or items[-1].get(key) not in (None, content):
                items.append({})
        elif not items:
            continue
        items[-1].setdefault(key, content)
    return items


def kind(content_type: str) -> str:
    """A Content-Type's media type, lowercase, without its parameters."""
    return content_type.split(";", 1)[0].strip().lower()


def suffix(url: str) -> str:
    """A URL's extension, lowercase, from the last dot of its last path part (`.mp4` of both
    `clip.MP4` and `.mp4`); "" for none."""
    last = urlsplit(url).path.rsplit("/", 1)[-1]
    return "." + last.rpartition(".")[2].lower() if "." in last else ""


def web(url: str) -> bool:
    """Whether `url` is an http or https link with a host."""
    parts = urlsplit(url)
    return parts.scheme.lower() in ("http", "https") and bool(parts.hostname)


def keyed(link: str) -> str:
    """`link` as a post id and a Referer: no `user:pw@`, no fragment, an empty path as `/`."""
    parts = urlsplit(link)
    host = parts.netloc.rpartition("@")[2]
    return parts._replace(netloc=host, path=parts.path or "/", fragment="").geturl()


def name_of(link: str) -> str:
    """The `{name}` of a link's files: its last path part without the extension, else its host.

    A blank or dot-only stem (`..`, `.mp4`, `%20.mp4`, `%2e%2e.mp4`) is the host's, never a
    file's; an IPv6 host is written `[--1]`, since a file name can't hold its colons.
    """
    parts = urlsplit(link)
    last = unquote(parts.path.rstrip("/").rsplit("/", 1)[-1])
    stem = (last.rpartition(".")[0] if "." in last else last).strip(" .")
    if stem:
        return stem
    host = parts.hostname or "web"
    return f"[{host.replace(':', '-')}]" if ":" in host else host

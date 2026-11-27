"""The built-in plugins, one module per site.

SPEC.md#for-developers explains it."""

from __future__ import annotations

from typing import NamedTuple

__all__ = ["Entry", "TABLE"]


class Entry(NamedTuple):
    """Where a built-in plugin is, and the domains whose links are its."""

    code: str
    module: str
    cls: str
    domains: tuple[str, ...]


# In the order links are tried: `web` reads any link, so it comes last. Each entry is checked
# against its class by tests/test_plugins.py.
TABLE = (
    Entry("tik", "flnd.plugins.tik", "TikTok", ("tiktok.com", "vm.tiktok.com", "vt.tiktok.com")),
    Entry("fbk", "flnd.plugins.fbk", "Facebook", ("facebook.com", "fb.com", "fb.watch")),
    Entry("ins", "flnd.plugins.ins", "Instagram", ("instagram.com",)),
    Entry("thr", "flnd.plugins.thr", "Threads", ("threads.com", "threads.net")),
    Entry("twt", "flnd.plugins.twt", "Twitter", ("x.com", "twitter.com")),
    Entry("loi", "flnd.plugins.loi", "Loibus", ("loix.cc", "moegoat.com")),
    Entry("mht", "flnd.plugins.mht", "Mimi", ("mimihentai.moe",)),
    Entry("smp", "flnd.plugins.smp", "Simp", ("simpcity.cr", "simpcity.su")),
    Entry("pxv", "flnd.plugins.pxv", "Pixiv", ("pixiv.net", "pixiv.me")),
    Entry("web", "flnd.plugins.web", "Web", ()),
)

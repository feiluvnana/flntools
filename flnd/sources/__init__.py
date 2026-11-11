"""The source registry: `SOURCES` is the one list of sources.

Target resolution, help, login and logout all read it, so adding a source means writing its
class and adding it here.
"""

from flnd.sources.base import Source
from flnd.sources.fbk import Facebook
from flnd.sources.ins import Instagram
from flnd.sources.tik import TikTok
from flnd.sources.twt import Twitter

SOURCES: dict[str, type[Source]] = {source.code: source for source in (TikTok, Facebook, Instagram, Twitter)}

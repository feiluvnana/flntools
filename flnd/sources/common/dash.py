"""The best video and audio files of a DASH manifest, for the sources whose sites split them (fbk, ins).

    if streams := dash.best(node.get("video_dash_manifest")):
        session.save(streams.video, name + streams.extension, category="reels", audio=streams.audio)

Only single-file representations are read, which is what Facebook and Instagram serve. The stdlib's
ElementTree never fetches external entities, and expat 2.4.1 or later refuses entity bombs.
"""

from __future__ import annotations

import xml.etree.ElementTree as ElementTree
from typing import Any, NamedTuple

NS = "{urn:mpeg:dash:schema:mpd:2011}"


class Streams(NamedTuple):
    """The URLs of a manifest's best video and best audio, and the extension their merge is saved with."""

    video: str
    audio: str
    extension: str


def best(manifest: Any) -> Streams | None:
    """The highest video (by height, then bandwidth) and audio (by bandwidth), or None to fall back to progressive.

    None when there is no manifest, it can't be read, it uses segment templates or lists, or it
    lacks a video or an audio stream.
    """
    if not isinstance(manifest, str) or not manifest:
        return None
    try:
        root = ElementTree.fromstring(manifest)
    except ElementTree.ParseError:
        return None
    if root.find(f".//{NS}SegmentTemplate") is not None or root.find(f".//{NS}SegmentList") is not None:
        return None
    found: dict[str, list[tuple[int, int, str, str]]] = {"video": [], "audio": []}
    for adaptation in root.iter(f"{NS}AdaptationSet"):
        for representation in adaptation.iter(f"{NS}Representation"):
            mime = representation.get("mimeType") or adaptation.get("mimeType") or ""
            url = (representation.findtext(f"{NS}BaseURL") or "").strip()
            kind = mime.split("/")[0]
            if kind in found and url.startswith("https://"):
                found[kind].append((_number(representation.get("height")), _number(representation.get("bandwidth")),
                                    url, mime))
    if not found["video"] or not found["audio"]:
        return None
    video, audio = max(found["video"])[2:], max(found["audio"], key=lambda entry: entry[1])[2:]
    webm = video[1].endswith("/webm") and audio[1].endswith("/webm")
    return Streams(video[0], audio[0], ".webm" if webm else ".mp4")


def _number(text: str | None) -> int:
    return int(text) if text and text.isdigit() else 0

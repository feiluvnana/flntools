"""dash.best on anonymized manifests shaped like Instagram's and Facebook's. No network."""

import unittest

from flnd.sources.common import dash

HEAD = '<?xml version="1.0"?>\n<MPD xmlns="urn:mpeg:dash:schema:mpd:2011" type="static"><Period>'
TAIL = "</Period></MPD>"


def representation(id, mime, url, *, height=None, bandwidth=1000, codecs="avc1.64001E", segments=True):
    size = f' width="{height * 9 // 16}" height="{height}"' if height else ""
    base = '<SegmentBase indexRange="1-2"><Initialization range="0-1"/></SegmentBase>' if segments else ""
    return (f'<Representation id="{id}" mimeType="{mime}" codecs="{codecs}"{size} bandwidth="{bandwidth}">'
            f"<BaseURL>{url}</BaseURL>{base}</Representation>")


def manifest(*videos, audios=()):
    sets = f'<AdaptationSet id="0">{"".join(videos)}</AdaptationSet>'
    if audios:
        sets += f'<AdaptationSet id="1">{"".join(audios)}</AdaptationSet>'
    return HEAD + sets + TAIL


# Instagram serves H.264 or VP9 in MP4 up to 1080p; Facebook adds AV1 up to 1440p, sometimes listing
# every quality with the same width and height, told apart only by bandwidth.
INSTAGRAM = manifest(
    representation("1v", "video/mp4", "https://cdn.example/360.mp4", height=640, bandwidth=168170),
    representation("2v", "video/mp4", "https://cdn.example/1080.mp4", height=1920, bandwidth=1160975),
    representation("3v", "video/mp4", "https://cdn.example/720.mp4", height=1280, bandwidth=825291),
    audios=[representation("4a", "audio/mp4", "https://cdn.example/a64.mp4", bandwidth=64353, codecs="mp4a.40.5"),
            representation("5a", "audio/mp4", "https://cdn.example/a95.mp4", bandwidth=95480, codecs="mp4a.40.5")])


class BestTest(unittest.TestCase):
    def test_the_highest_video_and_audio_are_chosen(self):
        self.assertEqual(dash.best(INSTAGRAM),
                         dash.Streams("https://cdn.example/1080.mp4", "https://cdn.example/a95.mp4", ".mp4"))

    def test_equal_heights_are_told_apart_by_bandwidth(self):
        text = manifest(representation("1v", "video/mp4", "https://cdn.example/low.mp4", height=1280, bandwidth=688558),
                        representation("2v", "video/mp4", "https://cdn.example/high.mp4", height=1280, bandwidth=2633777),
                        audios=[representation("a", "audio/mp4", "https://cdn.example/a.mp4", codecs="mp4a.40.5")])
        self.assertEqual(dash.best(text).video, "https://cdn.example/high.mp4")

    def test_webm_only_when_both_are_webm(self):
        webm = manifest(representation("v", "video/webm", "https://cdn.example/v.webm", height=720, codecs="vp9"),
                        audios=[representation("a", "audio/webm", "https://cdn.example/a.webm", codecs="opus")])
        self.assertEqual(dash.best(webm).extension, ".webm")
        mixed = manifest(representation("v", "video/webm", "https://cdn.example/v.webm", height=720, codecs="vp9"),
                         audios=[representation("a", "audio/mp4", "https://cdn.example/a.mp4", codecs="mp4a.40.2")])
        self.assertEqual(dash.best(mixed).extension, ".mp4")

    def test_the_mime_type_may_be_on_the_adaptation_set(self):
        text = (HEAD + '<AdaptationSet mimeType="video/mp4"><Representation id="v" height="720" bandwidth="1">'
                "<BaseURL>https://cdn.example/v.mp4</BaseURL></Representation></AdaptationSet>"
                '<AdaptationSet mimeType="audio/mp4"><Representation id="a" bandwidth="1">'
                "<BaseURL>https://cdn.example/a.mp4</BaseURL></Representation></AdaptationSet>" + TAIL)
        self.assertEqual(dash.best(text), dash.Streams("https://cdn.example/v.mp4", "https://cdn.example/a.mp4", ".mp4"))

    def test_segment_templates_and_lists_give_none(self):
        for element in ('<SegmentTemplate media="seg-$Number$.m4s"/>', "<SegmentList/>"):
            text = INSTAGRAM.replace("</AdaptationSet>", element + "</AdaptationSet>", 1)
            self.assertIsNone(dash.best(text), element)

    def test_without_audio_or_video_gives_none(self):
        self.assertIsNone(dash.best(manifest(representation("v", "video/mp4", "https://cdn.example/v.mp4", height=720))))
        self.assertIsNone(dash.best(manifest(audios=[representation("a", "audio/mp4", "https://cdn.example/a.mp4")])))

    def test_nothing_or_junk_gives_none(self):
        for text in (None, "", "not xml", "<MPD/>", 42):
            self.assertIsNone(dash.best(text), text)

    def test_relative_base_urls_are_not_used(self):
        text = INSTAGRAM.replace("https://cdn.example/1080.mp4", "1080.mp4")
        self.assertEqual(dash.best(text).video, "https://cdn.example/720.mp4")

    def test_an_entity_bomb_is_refused(self):
        bomb = ('<?xml version="1.0"?><!DOCTYPE lolz [<!ENTITY lol "lol">'
                + "".join(f'<!ENTITY lol{n} "{("&lol" + (str(n - 1) if n > 1 else "") + ";") * 10}">' for n in range(1, 10))
                + ']><MPD xmlns="urn:mpeg:dash:schema:mpd:2011">&lol9;</MPD>')
        self.assertIsNone(dash.best(bomb))

    def test_an_external_entity_is_never_read(self):
        text = ('<?xml version="1.0"?><!DOCTYPE m [<!ENTITY secret SYSTEM "file:///etc/passwd">]>'
                + INSTAGRAM.split("\n", 1)[1].replace("https://cdn.example/1080.mp4", "&secret;"))
        streams = dash.best(text)
        self.assertNotIn("root", repr(streams))


if __name__ == "__main__":
    unittest.main()

"""HLS and DASH playlists read into segments (`flnkit.stream`), and fetched in order."""

import asyncio
import tracemalloc
import unittest
from unittest import mock

from flnkit import clock, errors, stream
from flnkit.stream import Segment, Variant

BASE = "https://cdn.example.com/v/"

MASTER = """#EXTM3U
#EXT-X-MEDIA:TYPE=AUDIO,GROUP-ID="aud",NAME="en",URI="audio/en.m3u8"
#EXT-X-MEDIA:TYPE=AUDIO,GROUP-ID="aud",NAME="fr",DEFAULT=YES,URI="audio/fr.m3u8"
#EXT-X-STREAM-INF:BANDWIDTH=800000,RESOLUTION=640x360,CODECS="avc1.4d401e,mp4a.40.2",AUDIO="aud"
360p/index.m3u8
#EXT-X-I-FRAME-STREAM-INF:BANDWIDTH=90000,URI="iframes.m3u8"
#EXT-X-STREAM-INF:BANDWIDTH=2400000,RESOLUTION=1280x720
https://other.example.com/720p.m3u8
"""

MEDIA = """#EXTM3U
#EXT-X-TARGETDURATION:6
#EXT-X-KEY:METHOD=NONE
#EXT-X-MAP:URI="init.mp4",BYTERANGE="720@0"
#EXTINF:6.0,
#EXT-X-BYTERANGE:1000@720
all.mp4
#EXTINF:5.5,
#EXT-X-MAP:URI="init.mp4",BYTERANGE="720@0"
#EXT-X-BYTERANGE:500
all.mp4
#EXT-X-DISCONTINUITY
#EXT-X-MAP:URI="/ad/init.mp4"
#EXTINF:4,
../ad/seg0.m4s
#EXT-X-ENDLIST
"""

MPD = """<?xml version="1.0"?>
<MPD xmlns="urn:mpeg:dash:schema:mpd:2011" type="static" mediaPresentationDuration="PT9S">
 <BaseURL>media/</BaseURL>
 <Period>
  <BaseURL>dash/</BaseURL>
  <AdaptationSet mimeType="video/mp4">
   <SegmentTemplate timescale="1000" initialization="$RepresentationID$/init.mp4"
       media="$RepresentationID$/$Time$-$$.m4s">
    <SegmentTimeline><S t="0" d="4000" r="1"/><S d="1000"/></SegmentTimeline>
   </SegmentTemplate>
   <Representation id="v1" bandwidth="500000" width="640" height="360"/>
   <Representation id="v2" bandwidth="900000" width="1280" height="720"/>
  </AdaptationSet>
  <AdaptationSet contentType="audio">
   <Representation id="a1" bandwidth="64000">
    <SegmentTemplate duration="4" startNumber="5" media="a/$Number%03d$.m4s"/>
   </Representation>
   <Representation id="a2" bandwidth="128000">
    <BaseURL>https://audio.example.com/a2.mp4</BaseURL>
   </Representation>
  </AdaptationSet>
  <AdaptationSet mimeType="text/vtt"><Representation id="t" bandwidth="1"/></AdaptationSet>
 </Period>
</MPD>
"""

LISTED = """<MPD type="static"><Period duration="PT1M"><AdaptationSet codecs="avc1.64001f">
 <Representation id="v" bandwidth="1" height="480"><BaseURL>https://example.com/f.mp4</BaseURL>
  <SegmentList timescale="10" duration="20"><Initialization sourceURL="f.mp4" range="0-99"/>
   <SegmentURL media="f.mp4" mediaRange="100-199"/><SegmentURL media="f.mp4" mediaRange="200-299"/>
  </SegmentList></Representation></AdaptationSet></Period></MPD>"""


class HlsTest(unittest.TestCase):
    def test_master_variants_with_their_audio(self):
        variants = stream.hls(MASTER, BASE + "master.m3u8").variants
        self.assertEqual(variants, (
            Variant(BASE + "360p/index.m3u8", 800000, 640, 360, "avc1.4d401e,mp4a.40.2",
                    audio=Variant(BASE + "audio/fr.m3u8")),
            Variant("https://other.example.com/720p.m3u8", 2400000, 1280, 720),
        ))

    def test_media_segments(self):
        found = stream.hls(MEDIA, BASE + "x/index.m3u8")
        self.assertTrue(found.ended)
        self.assertEqual(found.variants, ())
        self.assertEqual(found.segments, (
            Segment(BASE + "x/init.mp4", span=(0, 720), init=True),
            Segment(BASE + "x/all.mp4", 6.0, (720, 1720)),
            Segment(BASE + "x/all.mp4", 5.5, (1720, 2220)),
            Segment("https://cdn.example.com/ad/init.mp4", init=True),
            Segment(BASE + "ad/seg0.m4s", 4.0, discontinuity=True),
        ))

    def test_live_playlist_is_not_ended(self):
        found = stream.hls("#EXTM3U\n#EXTINF:2,\na.ts\n", BASE + "live.m3u8")
        self.assertEqual((found.ended, found.segments), (False, (Segment(BASE + "a.ts", 2.0),)))

    def test_refused(self):
        for name, playlist, said in [
            ("not hls", "<html></html>", "not an HLS playlist"),
            ("aes", '#EXTM3U\n#EXT-X-KEY:METHOD=AES-128,URI="k"\n#EXTINF:2,\na.ts\n',
             "encrypted (AES-128)"),
            ("sample aes", "#EXTM3U\n#EXT-X-KEY:METHOD=SAMPLE-AES,URI=\"k\"\n", "SAMPLE-AES"),
            ("session key", '#EXTM3U\n#EXT-X-SESSION-KEY:METHOD=AES-128,URI="k"\n'
             "#EXT-X-STREAM-INF:BANDWIDTH=1\na.m3u8\n", "encrypted"),
        ]:
            with self.subTest(name), self.assertRaises(errors.SiteError) as caught:
                stream.hls(playlist, BASE + "p.m3u8?token=secret")
            self.assertIn(said, str(caught.exception))
            self.assertNotIn("secret", str(caught.exception))


class DashTest(unittest.TestCase):
    def test_template_with_timeline_and_number(self):
        variants = stream.dash(MPD, BASE + "m.mpd").variants
        self.assertEqual([(v.id, v.height, v.url) for v in variants],
                         [("v1", 360, BASE + "m.mpd"), ("v2", 720, BASE + "m.mpd")])
        self.assertEqual(variants[1].segments, (
            Segment(BASE + "media/dash/v2/init.mp4", init=True),
            Segment(BASE + "media/dash/v2/0-$.m4s", 4.0),
            Segment(BASE + "media/dash/v2/4000-$.m4s", 4.0),
            Segment(BASE + "media/dash/v2/8000-$.m4s", 1.0),
        ))
        audio = variants[0].audio
        self.assertEqual((audio.id, audio.segments),
                         ("a2", (Segment("https://audio.example.com/a2.mp4"),)))

    def test_number_template_counts_from_the_length(self):
        audio = stream.dash(MPD.replace(' mimeType="video/mp4"', ' mimeType="image/png"'),
                            BASE + "m.mpd").variants
        self.assertEqual([v.id for v in audio], ["a1", "a2"])
        self.assertEqual([s.url for s in audio[0].segments],
                         [BASE + f"media/dash/a/{n:03d}.m4s" for n in (5, 6, 7)])

    def test_segment_list_with_ranges(self):
        (variant,) = stream.dash(LISTED, BASE + "l.mpd").variants
        self.assertEqual(variant.segments, (
            Segment("https://example.com/f.mp4", span=(0, 100), init=True),
            Segment("https://example.com/f.mp4", 2.0, (100, 200)),
            Segment("https://example.com/f.mp4", 2.0, (200, 300)),
        ))

    def test_refused(self):
        protected = MPD.replace("<Representation id=\"v1\"",
                                "<ContentProtection schemeIdUri=\"x\"/><Representation id=\"v1\"")
        for name, manifest, said in [
            ("not xml", "{}", "not a DASH manifest"),
            ("not an mpd", "<html/>", "not a DASH manifest"),
            ("live", MPD.replace('type="static"', 'type="dynamic"'), "live"),
            ("two periods", MPD.replace("</Period>", "</Period><Period/>"), "2 periods"),
            ("protected", protected, "protected"),
            ("no length", MPD.replace(' mediaPresentationDuration="PT9S"', "")
             .replace(' mimeType="video/mp4"', ""), "without a length"),
        ]:
            with self.subTest(name), self.assertRaises(errors.SiteError) as caught:
                stream.dash(manifest, BASE + "m.mpd")
            self.assertIn(said, str(caught.exception))

    def test_timeline_starts(self):
        for name, extra, timeline, want in [
            ("rounded back", "", '<S t="0" d="4"/><S t="3" d="4"/>', [0, 3]),
            ("1-tick overlap", "", '<S t="0" d="400"/><S t="399" d="400"/>', [0, 399]),
            ("1% overlap", "", '<S t="0" d="400"/><S t="396" d="400"/>', [0, 396]),
            ("period end", "", '<S t="0" d="4" r="-1"/>', [0, 4, 8]),
            ("next start", "", '<S t="0" d="4" r="-1"/><S t="8" d="1"/>', [0, 4, 8]),
            ("first start", "", '<S t="100" d="4" r="-1"/>', [100, 104, 108]),
            ("offset", 'presentationTimeOffset="90"', '<S t="90" d="4" r="-1"/>', [90, 94, 98]),
        ]:
            with self.subTest(name):
                found = stream.dash(_timed(timeline, extra), BASE + "m.mpd").variants[0]
                self.assertEqual([s.url for s in found.segments],
                                 [BASE + f"{t}.m4s" for t in want])
        numbered = _timed('<S t="0" d="400"/><S t="399" d="400"/>', media="$Number$.m4s")
        (variant,) = stream.dash(numbered, BASE + "m.mpd").variants
        self.assertEqual([s.url for s in variant.segments], [BASE + "1.m4s", BASE + "2.m4s"])


def _timed(timeline, extra="", length="PT9S", media="$Time$.m4s"):
    """An MPD of one video representation `v` whose template has `timeline`."""
    return (f'<MPD type="static" mediaPresentationDuration="{length}"><Period>'
            f'<AdaptationSet mimeType="video/mp4"><SegmentTemplate {extra} media="{media}">'
            f"<SegmentTimeline>{timeline}</SegmentTimeline></SegmentTemplate>"
            '<Representation id="v"/></AdaptationSet></Period></MPD>')


class HostileTest(unittest.TestCase):
    """Malformed, empty or oversized playlists raise `SiteError`, quickly, never anything else."""

    def test_refused(self):
        hls = "#EXTM3U\n{}\na.ts\n".format
        many = "#EXTM3U\n" + "#EXTINF:1,\na.ts\n" * (stream.MOST + 1)
        plain = _timed("", 'duration="1"').replace("<SegmentTimeline></SegmentTimeline>", "")
        huge = f'duration="1" timescale="1{"0" * 400}"'
        for name, read, playlist, said in [
            ("extinf", stream.hls, hls("#EXTINF:abc,"), "can't read"),
            ("byterange", stream.hls, hls("#EXT-X-BYTERANGE:xyz"), "can't read"),
            ("bare byterange", stream.hls, hls("#EXT-X-BYTERANGE"), "can't read"),
            ("map byterange", stream.hls, hls('#EXT-X-MAP:URI="i",BYTERANGE="q"'), "can't read"),
            ("endless extinf", stream.hls, hls("#EXTINF:1e999,"), "no real length"),
            ("negative range", stream.hls, hls("#EXT-X-BYTERANGE:-5@10"), "ends before"),
            ("empty hls", stream.hls, "#EXTM3U\n#EXT-X-ENDLIST\n", "empty"),
            ("many hls", stream.hls, many, "over 100,000"),
            ("no length", stream.dash, _timed('<S d="0" r="-1"/>'), "no length"),
            ("id format", stream.dash, _timed('<S d="1"/>', media="$RepresentationID%03d$"),
             "can't read"),
            ("odd duration", stream.dash, _timed('<S d="1"/>', length="PT1.2.3S"), "can't read"),
            ("overflow", stream.dash, plain.replace('duration="1"', huge), "can't read"),
            ("wide number", stream.dash, plain.replace("$Time$", "$Number%021d$"), "wider"),
            ("long url", stream.dash, plain.replace("$Time$", "$RepresentationID$" * 9)
             .replace('id="v"', f'id="{"v" * 1000}"'), "over 8,192"),
            ("zero timescale", stream.dash, plain.replace('duration="1"',
                                                          'duration="1" timescale="0"'),
             "isn't positive"),
            ("backwards range", stream.dash, LISTED.replace("200-299", "200-100"), "ends before"),
            ("repeat below -1", stream.dash, _timed('<S d="1" r="-5"/>'), "backwards"),
            ("a whole S back", stream.dash, _timed('<S t="0" d="4"/><S t="0" d="4"/>'),
             "backwards"),
            ("same t, in the slack", stream.dash, _timed('<S t="0" d="2"/><S t="0" d="2"/>'),
             "backwards"),
            ("3999 ticks back", stream.dash,
             _timed('<S t="0" d="4000" r="1"/><S t="4001" d="4000"/>'), "backwards"),
            ("over 1% back", stream.dash, _timed('<S t="0" d="400"/><S t="395" d="400"/>'),
             "backwards"),
            ("time backwards", stream.dash, _timed('<S t="10" d="2"/><S t="4" d="2"/>'),
             "backwards"),
            ("big repeat", stream.dash, _timed('<S d="1" r="5000000"/>'), "over 100,000"),
            ("huge repeat", stream.dash, _timed('<S d="1" r="999999999999"/>'), "over 100,000"),
            ("small step", stream.dash,
             plain.replace('duration="1"', 'timescale="1000" duration="1"')
             .replace("PT9S", "PT5000S"), "over 100,000"),
            ("long period", stream.dash, _timed('<S d="1"/>', length="P99999999D"), "period"),
            ("doctype", stream.dash, '<!DOCTYPE MPD [<!ENTITY a "b">]><MPD>&a;</MPD>',
             "DOCTYPE"),
            ("no segment", stream.dash, LISTED.replace("<SegmentURL", "<Other"), "no segment"),
            ("no media", stream.dash, '<MPD type="static"><Period/></MPD>', "no video"),
            ("file uri", stream.hls, "#EXTM3U\n#EXTINF:1,\nfile:///etc/hosts\n", "isn't http"),
            ("file base", stream.dash, LISTED.replace("https://example.com/f.mp4", "file:///f"),
             "isn't http"),
        ]:
            with self.subTest(name), self.assertRaises(errors.SiteError) as caught:
                read(playlist, BASE + "p")
            self.assertIn(said, str(caught.exception))
            self.assertNotIn("abc", str(caught.exception))

    def test_a_template_is_refused_before_it_is_built(self):
        # 2,000 copies of a 10,000-byte id would be 20 MB; the refusal must come first.
        manifest = (_timed("", 'duration="1"', media="$RepresentationID$" * 2000)
                    .replace("<SegmentTimeline></SegmentTimeline>", "")
                    .replace('id="v"', f'id="{"v" * 10_000}"'))
        tracemalloc.start()
        try:
            with self.assertRaises(errors.SiteError):
                stream.dash(manifest, BASE + "m.mpd")
            peak = tracemalloc.get_traced_memory()[1]
        finally:
            tracemalloc.stop()
        self.assertLess(peak, 2 << 20)

    def test_what_is_built_is_capped(self):
        # HLS has no plan: its 1,700-byte text passes, and its 100 segments cost about 29,000.
        hls = "#EXTM3U\n" + "#EXTINF:1,\na.ts\n" * 100
        with mock.patch.object(stream, "OUTPUT_MOST", 20_000), \
                self.assertRaises(errors.SiteError) as caught:
            stream.hls(hls, BASE + "p.m3u8")
        self.assertIn("over 20,000 bytes built", str(caught.exception))
        with mock.patch.object(stream, "OUTPUT_MOST", 27_000), \
                self.assertRaises(errors.SiteError) as caught:
            stream.dash(_timed('<S d="1" r="99"/>', length="PT100S"), BASE + "m.mpd")
        self.assertIn("over 27,000 bytes built", str(caught.exception))
        with mock.patch.object(stream, "OUTPUT_MOST", 100), \
                self.assertRaises(errors.SiteError) as caught:
            stream.hls("#EXTM3U\n" + "#EXTINF:1,\na.ts\n" * 10, BASE + "p.m3u8")
        self.assertIn("a playlist over 100 bytes", str(caught.exception))

    def test_wide_playlists_are_refused_before_anything_is_built(self):
        many = "".join(f'<Representation id="{n}" bandwidth="{n}"/>' for n in range(30))
        template = '<SegmentTemplate timescale="1" duration="1" media="$Number$"/>'
        audio = "".join(f'<AdaptationSet mimeType="audio/mp4">{template}'
                        f'<Representation id="a{n}"/></AdaptationSet>' for n in range(200))
        shapes = {
            "30 representations": f'<AdaptationSet mimeType="video/mp4">{template}{many}'
                                  "</AdaptationSet>",
            "a template at every level": template + '<AdaptationSet mimeType="video/mp4">'
                                         + template + many.replace("/>", f">{template}"
                                                                    "</Representation>")
                                         + "</AdaptationSet>",
            "200 audio sets": audio,
            "long urls": '<AdaptationSet mimeType="video/mp4">'
                         + template.replace("$Number$", "x" * 800 + "$Number$")
                         + '<Representation id="v"/></AdaptationSet>',
        }
        for name, period in shapes.items():
            manifest = ('<MPD type="static" mediaPresentationDuration="PT100000S">'
                        f"<BaseURL>https://example.com/</BaseURL><Period>{period}</Period></MPD>")
            with self.subTest(name), mock.patch.object(stream._Out, "segment") as built:
                tracemalloc.start()
                try:
                    with self.assertRaises(errors.SiteError):
                        stream.dash(manifest, BASE + "m.mpd")
                    peak = tracemalloc.get_traced_memory()[1]
                finally:
                    tracemalloc.stop()
                self.assertEqual(built.call_count, 0)
                self.assertLess(peak, 100 << 20)

    def test_segments_in_all_are_capped_at_the_edge(self):
        def made(count):
            many = "".join(f'<Representation id="{n}"/>' for n in range(count))
            return ('<MPD type="static" mediaPresentationDuration="PT10S"><Period>'
                    '<AdaptationSet mimeType="video/mp4"><SegmentTemplate duration="1" '
                    f'media="$Number$"/>{many}</AdaptationSet></Period></MPD>')

        with mock.patch.object(stream, "MOST", 10), mock.patch.object(stream, "PLAYLIST_MOST", 40):
            self.assertEqual(len(stream.dash(made(4), BASE + "m.mpd").variants), 4)
            with self.assertRaises(errors.SiteError) as caught:
                stream.dash(made(5), BASE + "m.mpd")
        self.assertIn("50 segments in all, over 40", str(caught.exception))

    def test_media_segments_are_capped_at_the_edge_init_not_counted(self):
        repeat = '<S d="1" r="{}"/>'.format
        init = 'initialization="i.mp4"'
        listed = ('<MPD type="static"><Period><AdaptationSet mimeType="video/mp4">'
                  '<Representation id="v"><BaseURL>https://example.com/f.mp4</BaseURL>'
                  '<SegmentList duration="1"><Initialization sourceURL="i.mp4"/>{}'
                  "</SegmentList></Representation></AdaptationSet></Period></MPD>").format
        plain = _timed("", f'duration="1" {init}', "PT{}S").replace(
            "<SegmentTimeline></SegmentTimeline>", "")
        cases = {
            "timeline": lambda n: _timed(repeat(n - 1), init),
            "duration": plain.format,
            "open repeat": lambda n: _timed(repeat(-1), init, f"PT{n}S"),
            "list": lambda n: listed('<SegmentURL media="s"/>' * n),
        }
        for name, made in cases.items():
            with self.subTest(name):
                (variant,) = stream.dash(made(stream.MOST), BASE + "m.mpd").variants
                self.assertEqual(len(variant.segments), stream.MOST + 1)
                with self.assertRaises(errors.SiteError):
                    stream.dash(made(stream.MOST + 1), BASE + "m.mpd")
        mapped = '#EXTM3U\n#EXT-X-MAP:URI="i.mp4"\n' + "#EXTINF:1,\na.ts\n" * stream.MOST
        self.assertEqual(len(stream.hls(mapped, BASE + "p.m3u8").segments), stream.MOST + 1)


class PickTest(unittest.TestCase):
    def test_most_pixels_then_bits_at_or_under_height(self):
        variants = [Variant("a", 900, 640, 360), Variant("b", 500, 1280, 720),
                    Variant("c", 800, 1280, 720), Variant("d", 9000)]
        for height, want in [(None, "c"), (720, "c"), (719, "a"), (360, "a"), (100, "d")]:
            with self.subTest(height=height):
                self.assertEqual(stream.pick(variants, height=height).url, want)
        self.assertIsNone(stream.pick([Variant("a", 1, 1, 360)], height=240))


class FetchAllTest(unittest.TestCase):
    def test_written_in_playlist_order_under_shuffled_completion(self):
        segments = [Segment(f"https://example.com/{n}.ts", span=(n, n + 1)) for n in range(12)]
        # Each fetch yields to the loop a different number of times, so they finish shuffled.
        delays = [5, 0, 3, 9, 1, 7, 2, 8, 0, 4, 6, 1]
        done, written, flying = [], [], [0, 0]

        async def get(url, span):
            flying[0] += 1
            flying[1] = max(flying)
            for _ in range(delays[span[0]]):
                await asyncio.sleep(0)
            flying[0] -= 1
            done.append(span[0])
            return url.encode()

        async def write(data):
            written.append(data)

        with clock.use(clock.Fake()):
            total = asyncio.run(stream.fetch_all(segments, get, write, at_once=4))
        self.assertNotEqual(done, sorted(done))
        self.assertEqual(written, [s.url.encode() for s in segments])
        self.assertEqual(total, sum(len(s.url) for s in segments))
        self.assertEqual(flying[1], 4)

    def test_a_failed_fetch_cancels_the_rest(self):
        started, written = [], []

        async def get(url, span):
            started.append(url)
            if url == "1":
                raise errors.NetworkError("reset")
            await asyncio.sleep(0)
            return b"x"

        async def write(data):
            written.append(data)

        with self.assertRaises(errors.NetworkError):
            asyncio.run(stream.fetch_all([Segment(str(n)) for n in range(10)], get, write,
                                         at_once=3))
        self.assertEqual((started, written), (["0", "1", "2"], [b"x"]))


if __name__ == "__main__":
    unittest.main()

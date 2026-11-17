"""The home's config.toml (`flnd.options.config`), and where each of its settings takes effect.

`ReadTest` reads files written in a temporary home: every key, every bad value (whose message must
name the file, the key and what it takes), and the warning for a key flnd doesn't know.
`SourceRunTest` checks that a source's `[sources.<code>]` settings reach its run (`flnd.site`):
pace, limits, proxy and scroll. `PrecedenceTest` checks the order a setting resolves in: a value
given for one call wins over the file, and the file over the default. No network, no browser.
"""

import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from types import MappingProxyType
from unittest import mock

from example_source import Example
from test_library import LibraryCase
from test_site import SiteCase

import flnd
from flnd import cli, options
from flnd.engine import Engine
from flnd.errors import UsageError
from flnd.options import Config, SourceConfig, config


class Home:
    """A temporary home whose config.toml a test writes."""

    def __init__(self):
        self._folder = TemporaryDirectory()
        self.path = Path(self._folder.name)

    def write(self, text):
        (self.path / options.CONFIG).write_text(text, encoding="utf-8")
        return self.path

    def close(self):
        self._folder.cleanup()


class ReadTest(unittest.TestCase):
    def setUp(self):
        self.home = Home()
        self.addCleanup(self.home.close)
        self.warn = self.enterContext(mock.patch.object(options.ui, "warn"))

    def read(self, text):
        return config(self.home.write(text))

    def test_no_file_is_every_default(self):
        self.assertEqual(config(self.home.path), Config())
        self.assertEqual(Config().source("ins"), SourceConfig())

    def test_every_key_is_read(self):
        found = self.read(
            '[defaults]\nagain = true\ndedupe = "off"\nsidecars = true\n'
            'when = "1y"\n'
            '[sources.ins]\npace = 2\nscroll = "fast"\nlimits = { "cdn.example.com" = 4 }\n'
            'proxy = "socks5://127.0.0.1:1080"\nstop_after = 9\n'
        )
        self.assertEqual(
            (found.again, found.dedupe, found.sidecars, found.when),
            (True, False, True, "1y"),
        )
        self.assertEqual(found.source("ins"), SourceConfig(
            pace=2.0, scroll="fast", limits=MappingProxyType({"cdn.example.com": 4}),
            proxy="socks5://127.0.0.1:1080", stop_after=9))
        self.assertEqual(found.source("twt"), SourceConfig())
        self.warn.assert_not_called()

    def test_a_bad_value_names_the_file_the_key_and_what_it_takes(self):
        cases = {
            '[defaults]\nagain = "yes"\n': "[defaults] again is true or false",
            "[defaults]\nsidecars = 0\n": "[defaults] sidecars is true or false",
            '[defaults]\ndedupe = "skip"\n': '[defaults] dedupe is "link" or "off"',
            '[defaults]\nwhen = "tomorrow"\n': "[defaults] when is text --when takes",
            "[defaults]\nwhen = 2026\n": "[defaults] when is text --when takes",
            "[sources.ins]\npace = -1\n": "[sources.ins] pace is a number of seconds, 0 or more",
            "[sources.ins]\npace = true\n": "[sources.ins] pace is a number of seconds",
            '[sources.ins]\nscroll = "slow"\n': '[sources.ins] scroll is "human" or "fast"',
            '[sources.ins]\nlimits = { "cdn.example.com" = 0 }\n':
                '[sources.ins] limits is a table of "<host>" = <files at once, at least 1>',
            "[sources.ins]\nlimits = 8\n": "[sources.ins] limits is a table",
            '[sources.ins]\nproxy = "ftp://example.com"\n':
                '[sources.ins] proxy is "http://<host>:<port>" or',
            '[sources.ins]\nproxy = "socks5://example.com:port"\n': "[sources.ins] proxy is",
            "[sources.ins]\nstop_after = 0\n":
                "[sources.ins] stop_after is a whole number of at least 1",
            '[defaults]\n"when" = \n': "config.toml",
        }
        for text, message in cases.items():
            with self.subTest(text=text), self.assertRaises(UsageError) as caught:
                self.read(text)
            self.assertIn(str(self.home.path / "config.toml"), str(caught.exception))
            self.assertIn(message, str(caught.exception))
            self.assertTrue(caught.exception.hint)

    def test_a_proxy_password_never_reaches_the_message(self):
        with self.assertRaises(UsageError) as caught:
            self.read('[sources.ins]\nproxy = "ftp://example_user:secret-value@example.com:1"\n')
        self.assertNotIn("secret-value", str(caught.exception))
        self.assertIn("…@example.com:1", str(caught.exception))

    def test_every_proxy_scheme_curl_speaks_is_taken(self):
        for proxy in ("http://127.0.0.1:8080", "https://example.com", "socks5://127.0.0.1:1080",
                      "socks5h://example_user:secret@127.0.0.1:1080"):
            with self.subTest(proxy=proxy):
                self.assertEqual(self.read(f'[sources.ins]\nproxy = "{proxy}"\n')
                                 .source("ins").proxy, proxy)

    def test_an_unknown_key_is_warned_about_once_and_never_ignored_silently(self):
        text = ('[defaults]\ncompres = true\n[old]\naudio = "opus"\n[sources.ins]\nspeed = 1\n'
                "[extras]\nx = 1\n")
        self.read(text)
        self.read(text)
        warned = [call.kwargs["setting"] for call in self.warn.call_args_list]
        self.assertEqual(warned, ["[defaults] compres", "[old]", "[sources.ins] speed",
                                  "[extras]"])
        self.assertEqual(self.warn.call_args.kwargs["path"], str(self.home.path / "config.toml"))

    def test_a_changed_file_is_read_again(self):
        self.assertEqual(self.read('[defaults]\nwhen = "2026"\n').when, "2026")
        self.assertEqual(self.read('[defaults]\nwhen = "1y"\n').when, "1y")


class SourceRunTest(SiteCase):
    """A source's `[sources.<code>]` settings reach its run, over what its class says."""

    def configure(self, text):
        self.home.mkdir(parents=True, exist_ok=True)
        (self.home / "config.toml").write_text(text, encoding="utf-8")

    def test_without_settings_the_source_decides(self):
        run, site = self.open()
        self.assertEqual(run.web.limits.rate, 1.0 / Example.pace if Example.pace else None)
        self.assertEqual(run.web.limits.caps, dict(Example.limits))
        self.assertIsNone(run.session.proxy)
        self.assertEqual(site._browser().scroll, Example.scroll)

    def test_pace_limits_proxy_and_scroll_come_from_the_config(self):
        self.configure('[sources.exm]\npace = 4\nlimits = { "cdn.example.com" = 48 }\n'
                       'proxy = "socks5://127.0.0.1:1080"\nscroll = "fast"\n')
        run, site = self.open()
        self.assertEqual(run.web.limits.rate, 0.25)
        self.assertEqual(run.web.limits.caps, {**Example.limits, "cdn.example.com": 48})
        # One host's limit above the usual most lets a target keep that many files going.
        self.assertEqual(run.jobs, 48)
        self.assertEqual(run.session.proxy, "socks5://127.0.0.1:1080")
        self.assertEqual(site._browser().scroll, "fast")

    def test_a_pace_of_0_is_no_pace(self):
        self.configure("[sources.exm]\npace = 0\n")
        run, _ = self.open()
        self.assertIsNone(run.web.limits.rate)

    def test_the_proxy_reaches_curl(self):
        self.configure('[sources.exm]\nproxy = "http://127.0.0.1:8080"\n')
        run, _ = self.open()
        with mock.patch("curl_cffi.requests.AsyncSession") as made:
            run.session._session()
            # The fake session never sent anything, so there is nothing for the run to close.
            run.session._raw = None
        self.assertEqual(made.call_args.kwargs["proxy"], "http://127.0.0.1:8080")

    def test_the_browser_says_once_that_it_isnt_proxied(self):
        self.configure('[sources.exm]\nproxy = "http://127.0.0.1:8080"\n')
        run, site = self.open()
        site._browser()
        other = type(site)(run, flnd.Target("exm:example_creator", kind="user"))
        self.addCleanup(other.close)
        other._browser()
        told = [line for line in self.lines() if "doesn't use the proxy" in line]
        self.assertEqual(len(told), 1)


class PrecedenceTest(LibraryCase):
    """A value given for one call, then config.toml, then the default."""

    def configure(self, text):
        self.home.mkdir(parents=True, exist_ok=True)
        (self.home / "config.toml").write_text(text, encoding="utf-8")

    def calls(self, method):
        """Record the engine's `method` calls, running each as it would run."""
        real = getattr(Engine, method)
        return self.enterContext(mock.patch.object(Engine, method, autospec=True,
                                                   side_effect=real))

    def test_when_from_the_config_bounds_a_download_and_a_given_one_wins(self):
        # Every post of the example source is from 2026-03-01.
        self.configure('[defaults]\nwhen = "2026-04"\n')
        self.assertEqual(self.fl.download("exs:example_user").wait().saved, 0)
        self.assertEqual(self.fl.download("exs:example_user", when="2026-03").wait().saved, 3)

    def test_an_empty_when_takes_every_day_over_the_config(self):
        self.configure('[defaults]\nwhen = "2026-04"\n')
        self.assertEqual(self.fl.download("exs:example_user", when="").wait().saved, 3)






class FlagTest(unittest.TestCase):
    """The CLI leaves a flag it wasn't given as None, so the library reads the config."""

    def parse(self, *argv):
        return cli.parser().parse_args(list(argv))




if __name__ == "__main__":
    unittest.main()

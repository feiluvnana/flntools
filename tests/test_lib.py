"""Tests for `flnd` top-level library API: `source`, `resolve`, `open`, `download`."""

import tempfile
import unittest
from pathlib import Path
from unittest import mock

import httpx

import flnd
from test_engine import CDN, EngineCase, make_source


class LibraryApiTest(unittest.TestCase):
    def test_source_and_resolve(self):
        src = flnd.source("ins")
        self.assertEqual(src.code, "ins")
        with self.assertRaises(flnd.TargetError):
            flnd.source("unknown_code_xyz")
        target = flnd.resolve("ins:example_user")
        self.assertEqual(target, flnd.Target("ins", "user", "example_user"))

    def test_open_yields_site_with_client_and_state(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp) / ".flnd"
            with flnd.open("ins", home=home) as site:
                self.assertEqual(site.target.source, "ins")
                self.assertIsNotNone(site.state)


class LibraryDownloadTest(EngineCase):
    def test_flnd_download_delegates_to_engine(self):
        self.routes[f"{CDN}/a.jpg"] = httpx.Response(200, content=b"\xff\xd8\xff\xe0\x00\x10JFIF\x00\x01\x01\x00\x00\x01\x00\x01\x00\x00")
        with mock.patch.dict(flnd.SOURCES, {"aaa": make_source("aaa")}):
            report = flnd.download("aaa:a", categories="all", home=self.home)
        self.assertTrue(report.ok)
        self.assertEqual(len(report.targets), 1)
        self.assertEqual(report.targets[0].target, flnd.Target("aaa", "user", "a"))


if __name__ == "__main__":
    unittest.main()

"""src/engine/home.py: samples, and a gone file through the engine (`flnd.db` itself is
test_store.py's).
"""

import os
import tempfile
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from fakes import CDN, EngineCase, make_plugin, noise

from flnd.engine import home as _home
from flnd.engine import store as _store
from flnd.engine.core import Engine
from flnd.errors import Changed
from flnd.plugin import File
from flnkit import net as _net


class DbCase(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.home = Path(folder.name) / ".flnd"


class SampleTest(DbCase):
    def test_samples_saved_at_once_get_their_own_names(self):
        response = SimpleNamespace(url="https://example.com/api", headers={}, content=b"{}")
        barrier = threading.Barrier(2)

        def keep():
            barrier.wait(timeout=5)
            for _ in range(50):
                _home.sample(self.home, 1, Changed("bad shape", response=response))

        threads = [threading.Thread(target=keep) for _ in range(2)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        self.assertEqual(len(list((self.home / _home.LOGS / "1" / "samples").iterdir())), 100)





# ---------------------------------------------------------------------------------------------
# Through the engine
# ---------------------------------------------------------------------------------------------

class GoneTest(EngineCase):
    def setUp(self):
        super().setUp()
        self.routes[f"{CDN}/a.jpg"] = _net.reply(200, content=noise("JPEG"))
        self.routes[f"{CDN}/b.jpg"] = _net.reply(404)
        self.source = make_plugin("aaa", files=[File(f"{CDN}/a.jpg", name="a.jpg"),
                                                File(f"{CDN}/b.jpg", name="b.jpg")])

    def asked(self):
        return sum(str(request.url) == f"{CDN}/b.jpg" for request in self.requests)

    def test_a_404_is_gone_and_asked_again_only_when_told(self):
        engine = Engine(home=self.home, sources={"aaa": self.source})
        engine.download(["aaa:example_user/all"])
        self.assertEqual(self.asked(), 1)
        with _store.open(self.home) as db:
            self.assertEqual(db.known("aaa:b.jpg")[1], "gone")
            self.assertEqual(db._db.all("SELECT key, class FROM failures", ()),
                             [("aaa:b.jpg", "NotFound")])
        engine.download(["aaa:example_user/all"])
        self.assertEqual(self.asked(), 1)
        engine.download(["aaa:example_user/all"], again=True)
        self.assertEqual(self.asked(), 2)

    def test_ls_sizes_come_from_the_database_never_a_walk(self):
        engine = Engine(home=self.home, sources={"aaa": self.source})
        engine.download(["aaa:example_user/all"], sync=True)
        with mock.patch.object(Path, "rglob", side_effect=AssertionError("a walk")), \
                mock.patch.object(os, "walk", side_effect=AssertionError("a walk")):
            [info] = engine.target_ls()
        self.assertEqual((info.files, info.size), (1, len(noise("JPEG"))))


if __name__ == "__main__":
    unittest.main()

"""The Chrome extension's host, relay and client (src/engine/extension.py), against a fake
extension that answers each op as background.js does; SPEC.md#the-chrome-extension."""

import asyncio
import contextlib
import io
import json
import os
import secrets
import stat
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

import no_profiles  # noqa: F401  (refuses real browser profiles and ~/.config/flnd)

import flnd
from flnd.engine import extension, plugins
from flnd.plugins import _manifest

# A fresh secret per run, so no token is written in the tests.
TOKEN = secrets.token_hex(32)


class Fake:
    """Chrome with flnd's extension, on the far side of a host: it answers every op the host
    sends, in parts when the answer is large, and can ask the host the popup's questions."""

    def __init__(self, *, answer=None, popup=None) -> None:
        down, self._down = os.pipe()  # Chrome -> host
        self._up, up = os.pipe()  # host -> Chrome
        self._to_host = open(self._down, "wb", buffering=0)
        self._from_host = open(self._up, "rb", buffering=0)
        self._host_in, self._host_out = open(down, "rb", buffering=0), open(up, "wb")
        self.answer = answer or (lambda op, args: {"ok": True, "value": {"op": op, "args": args}})
        self.seen: list[tuple[str, dict]] = []  # every op the host sent, with its args
        self.sizes: list[int] = []  # the size of every message the host wrote
        self.answers: dict[int, dict] = {}  # the host's answers to the popup, by id
        self._lock, self._changed = threading.Lock(), threading.Condition()
        self.host = extension.Host(self._host_in, self._host_out, token=TOKEN,
                                   path=extension.relay(), popup=popup)
        self._serving = threading.Thread(target=self.host.serve)
        self._reading = threading.Thread(target=self._read)

    def __enter__(self) -> "Fake":
        self._serving.start()
        self._reading.start()
        self.host.ready.wait(10)
        return self

    def __exit__(self, *exc: object) -> None:
        self._to_host.close()  # Chrome closes the port: the host ends
        self._serving.join(10)
        self._host_out.close()
        self._reading.join(10)
        self._host_in.close()
        self._from_host.close()

    def _read(self) -> None:
        joiner = extension.Joiner()
        while (message := extension.read(self._from_host)) is not None:
            self.sizes.append(len(extension.encode(message)))
            whole = joiner.add(message)
            if whole is None:
                continue
            if "op" in whole:
                with self._changed:
                    self.seen.append((whole["op"], whole["args"]))
                    self._changed.notify_all()
                self._write({"id": whole["id"], **self.answer(whole["op"], whole["args"])})
            else:
                with self._changed:
                    self.answers[whole["id"]] = whole
                    self._changed.notify_all()

    def _write(self, message: dict) -> None:
        self.send(*(extension.frame(piece) for piece in extension.parts(message)))

    def send(self, *frames: bytes) -> None:
        """Write frames to the host as they are, well formed or not."""
        with self._lock:
            for data in frames:
                self._to_host.write(data)

    def ask(self, ident: int, op: str, **args) -> dict:
        """Ask the host one of the popup's questions, and return its answer."""
        self._write({"id": ident, "op": op, "args": args})
        return self.until(lambda: self.answers.get(ident))

    def until(self, found):
        with self._changed:
            return self._changed.wait_for(found, timeout=10)


class Case(unittest.IsolatedAsyncioTestCase):
    """Each test in its own HOME and runtime folder, with the token written as install does."""

    def setUp(self) -> None:
        self.temp = Path(tempfile.mkdtemp(prefix="flnd"))
        self.addCleanup(lambda: __import__("shutil").rmtree(self.temp, ignore_errors=True))
        places = {"HOME": str(self.temp), "XDG_RUNTIME_DIR": str(self.temp / "run")}
        patch = mock.patch.dict(os.environ, places)
        patch.start()
        self.addCleanup(patch.stop)
        extension._mkdir(extension.folder())
        extension._put(extension.folder() / "token.json", extension.encode({"token": TOKEN}),
                       0o600)


@unittest.skipIf(sys.platform == "win32", "the relay is a named pipe there")
class HostTest(Case):
    async def test_every_op_reaches_the_extension_and_its_value_comes_back(self):
        with Fake() as fake:
            async with await extension.connect() as client:
                self.assertEqual(client.version, None)
                for op in extension.OPS[1:]:
                    with self.subTest(op=op):
                        value = await client.call(op, tab=7, capture=3)
                        self.assertEqual(value, {"op": op, "args": {"tab": 7, "capture": 3}})
        # The token stays in the host: hello reaches the extension without it.
        self.assertEqual(fake.seen[0], ("hello", {}))
        self.assertEqual([op for op, _ in fake.seen], list(extension.OPS))

    async def test_an_error_answer_raises_the_class_it_names(self):
        refused = {"ok": False, "error": {"class": "NotFound", "message": "no such tab",
                                          "hint": "open it again"}}
        with Fake(answer=lambda op, args: refused if op == "tabs.close" else
                  {"ok": True, "value": None}):
            async with await extension.connect() as client:
                with self.assertRaises(flnd.NotFound) as caught:
                    await client.call("tabs.close", tab=1)
        self.assertEqual((str(caught.exception), caught.exception.hint),
                         ("no such tab", "open it again"))

    async def test_a_large_answer_comes_in_parts_and_is_put_back_together(self):
        html = "<p>" + "x" * (3 * 1024 * 1024) + "</p>"
        with Fake(answer=lambda op, args: {"ok": True, "value": {"html": html}}):
            async with await extension.connect() as client:
                self.assertEqual(await client.call("document", tab=1), {"html": html})

    async def test_two_runs_share_the_host(self):
        with Fake() as fake:
            async with await extension.connect() as one, \
                    await extension.connect() as two:
                found = await asyncio.gather(one.call("document", tab=1),
                                             two.call("document", tab=2))
        self.assertEqual([value["args"] for value in found], [{"tab": 1}, {"tab": 2}])
        self.assertEqual(sorted(args.get("tab", 0) for op, args in fake.seen), [0, 0, 1, 2])

    async def test_a_run_is_refused_before_any_op_without_the_token(self):
        padded = {"token": TOKEN, "pad": "x" * extension.BEFORE_HELLO}
        cases = {"a wrong token": {"id": 1, "op": "hello", "args": {"token": "0" * 64}},
                 "no hello": {"id": 1, "op": "cookies", "args": {"domains": ["example.com"]}},
                 "no token": {"id": 1, "op": "hello", "args": {}},
                 "a hello over 64 KiB": {"id": 1, "op": "hello", "args": padded}}
        with Fake() as fake:
            for name, first in cases.items():
                with self.subTest(name):
                    reader, writer = await asyncio.open_unix_connection(extension.relay())
                    writer.write(extension.frame(first))
                    writer.write_eof()  # a run that half-closes still gets its end at once
                    head = await reader.readexactly(4)
                    size = int.from_bytes(head, sys.byteorder)
                    answer = json.loads(await reader.readexactly(size))
                    self.assertFalse(answer["ok"])
                    self.assertEqual(answer["error"]["class"], "UsageError")
                    try:  # and the host hung up, before reading all of a long frame
                        self.assertEqual(await asyncio.wait_for(reader.read(), 10), b"")
                    except ConnectionError:
                        pass
                    writer.close()
        self.assertEqual(fake.seen, [])

    def test_a_frame_is_read_only_within_its_limit(self):
        message = {"id": 1, "op": "tabs.list", "args": {}}
        data = extension.frame(message)
        size = len(data) - 4
        cases = {"at the limit": (data, size, message), "past it": (data, size - 1, None),
                 "cut short": (data[:-1], size, None), "empty": (b"", size, None)}
        for name, (given, most, found) in cases.items():
            with self.subTest(name):
                self.assertEqual(extension.read(io.BytesIO(given), most), found)

    async def test_a_run_that_says_nothing_is_closed(self):
        with mock.patch.object(extension, "HELLO", 0.05), Fake() as fake:
            reader, writer = await asyncio.open_unix_connection(extension.relay())
            self.assertEqual(await asyncio.wait_for(reader.read(), timeout=2.0), b"")
            writer.close()
        self.assertEqual(fake.seen, [])

    async def test_forward_requires_a_numeric_capture_id(self):
        with Fake() as fake:
            async with await extension.connect() as client:
                with self.assertRaises(flnd.UsageError) as caught:
                    await client.call("capture.read", capture="3")
                self.assertIn("capture.start", str(caught.exception))
        self.assertEqual([op for op, _ in fake.seen if op != "hello"], [])

    async def test_a_malformed_message_from_the_extension_is_dropped(self):
        def part(**fields):
            return extension.frame({"id": 5, "part": 0, "of": 2, "chunk": "", **fields})

        cases = {"a part past its count": part(part=3), "a negative part": part(part=-1),
                 "a chunk that isn't text": part(chunk=5),
                 "an id that isn't a number": part(id=[1]),
                 "bad base64": part(of=1, chunk="@@@"), "too many parts": part(of=10**9),
                 "the count changes": part() + part(of=3),
                 "an answer without a number id": extension.frame({"id": {"a": 1}, "ok": True}),
                 "not JSON": len(b"\xff\xfe").to_bytes(4, sys.byteorder) + b"\xff\xfe",
                 "deep JSON": (lambda body: len(body).to_bytes(4, sys.byteorder) + body)(
                     b"[" * 30000 + b"]" * 30000),
                 "a list": len(b"[]").to_bytes(4, sys.byteorder) + b"[]"}
        crashed = []
        with mock.patch.object(threading, "excepthook", crashed.append), \
                mock.patch("flnd.ui.log.ui.debug") as noted, Fake() as fake:
            async with await extension.connect() as client:
                for name, data in cases.items():
                    with self.subTest(name):
                        fake.send(data)
                        # The host keeps serving: the next op still comes back.
                        self.assertEqual((await client.call("tabs.list"))["op"], "tabs.list")
        self.assertEqual(crashed, [])
        self.assertEqual(noted.call_count, len(cases))
        self.assertIn("dropped", noted.call_args.args[0])

    async def test_a_capture_a_run_leaves_open_is_stopped(self):
        def answer(op, args):
            return {"ok": True, "value": {"capture": 9} if op == "capture.start" else None}

        with Fake(answer=answer) as fake:
            async with await extension.connect() as client:
                await client.call("capture.start", tab=1, match="/api/")
            stopped = fake.until(lambda: ("capture.stop", {"capture": 9}) in fake.seen)
        self.assertTrue(stopped)

    async def test_the_host_ends_with_the_extension_and_leaves_nothing_running(self):
        before = set(threading.enumerate())
        with Fake():
            async with await extension.connect() as client:
                await client.call("tabs.list")
                left = client
        self.assertFalse(await asyncio.to_thread(os.path.exists, extension.relay()))
        self.assertEqual([thread.name for thread in set(threading.enumerate()) - before
                          if thread.name.startswith("flnd")], [])
        with self.assertRaises(flnd.UsageError):
            await left.call("tabs.list")
        with self.assertRaises(flnd.UsageError):
            await extension.connect()

    async def test_the_relay_and_the_token_are_the_users_alone(self):
        with Fake():
            relay = Path(extension.relay())
            modes = {path: stat.S_IMODE(os.stat(path).st_mode)
                     for path in (relay.parent, relay, extension.folder() / "token.json")}
        self.assertEqual(list(modes.values()), [0o700, 0o600, 0o600])
        os.chmod(extension.folder() / "token.json", 0o644)
        with self.assertRaises(flnd.UsageError):
            extension.token()

    def test_a_symlink_is_never_followed(self):
        token, kept = extension.folder() / "token.json", self.temp / "elsewhere"
        kept.mkdir(mode=0o755)
        (kept / "token.json").write_text(json.dumps({"token": TOKEN}))
        os.chmod(kept / "token.json", 0o600)
        token.unlink()
        token.symlink_to(kept / "token.json")
        with self.assertRaises(flnd.UsageError) as caught:
            extension.token()
        self.assertIn(str(token), str(caught.exception))
        with self.assertRaises(flnd.UsageError):
            extension._put(token, b"{}", 0o600)
        # A relay folder that is a symlink is refused before its target's mode changes.
        relay = Path(extension.relay()).parent
        relay.parent.mkdir(parents=True)
        relay.symlink_to(kept)
        with self.assertRaises(flnd.UsageError):
            extension._SocketListener(extension.relay())
        self.assertEqual(stat.S_IMODE(os.stat(kept).st_mode), 0o755)

    def test_the_host_serves_only_flnds_extension(self):
        for given in ("chrome-extension://abcdefghijklmnopabcdefghijklmnop/", None):
            with self.subTest(given), self.assertRaises(flnd.UsageError):
                extension.host(given)


@unittest.skipIf(sys.platform == "win32", "the relay is a named pipe there")
class PopupTest(Case):
    def test_the_popup_resolves_a_link_through_the_host(self):
        home = self.temp / "home"
        with Fake(popup=extension.Popup(home)) as fake:
            found = fake.ask(1, "resolve", url="https://www.instagram.com/example_user/")
            unknown = fake.ask(2, "rename", url="https://example.com/")
            # Text no plugin reads: its error names it once per plugin, past Chrome's 1 MiB.
            large = fake.ask(3, "resolve", url="x" * (2 * 1024 * 1024))
        self.assertEqual(found["value"], {"target": "ins:example_user", "followed": False})
        self.assertEqual(unknown["error"]["class"], "UsageError")
        self.assertFalse(large["ok"])
        # Chrome takes at most 1 MiB from a host: the large answer came in parts.
        self.assertGreater(len(fake.sizes), 4)
        self.assertLessEqual(max(fake.sizes), extension.CAP)

    def test_get_starts_flnd_on_the_resolved_target(self):
        home = self.temp / "home"
        with mock.patch.object(extension.subprocess, "Popen") as started:
            link = {"target": "https://www.instagram.com/example_user/"}
            done = extension.Popup(home)("get", link)
        argv = started.call_args.args[0]
        self.assertEqual(done, {"started": "ins:example_user"})
        self.assertEqual(argv[-5:], ["--home", str(home), "get", "--", "ins:example_user"])


PLUGIN = """
from flnd.plugin import Plugin, Post, route


class Xyz(Plugin):
    code, name, domains, api = "xyz", "Xyz", ("xyz.example.com", "cdn.example.com"), 2

    @route("xyz:{user}", "xyz.example.com/{user}")
    async def user(self, ctx, user):
        yield Post("p1", [])
"""


class PluginDomainsTest(Case):
    """What the host tells the extension to ask for, and to give back, per installed plugin."""

    def setUp(self) -> None:
        super().setUp()
        folder = self.temp / "plugins"
        folder.mkdir()
        source = self.temp / "xyz.py"
        source.write_text(PLUGIN, encoding="utf-8")
        for patch in (mock.patch.dict(os.environ, {plugins.VARIABLE: str(folder)}),
                      mock.patch.multiple(plugins.PLUGINS, _overrides={}, _built_in={},
                                          _removed=set(), _mgr=None, _stamp=None, _warned=0),
                      mock.patch.dict(sys.modules)):
            patch.start()
            self.addCleanup(patch.stop)
        self.source = source

    def install(self) -> None:
        mgr = plugins.PLUGINS.manager()
        mgr.install(self.source)
        plugins.PLUGINS.flush(mgr)

    def test_the_popup_asks_for_an_installed_plugins_domains_alone(self):
        popup = extension.Popup(self.temp / "home")
        self.assertEqual(popup("needs", {"granted": []}), {"ask": [], "drop": []})
        self.install()
        asked = {"code": "xyz", "name": "Xyz", "domains": ["xyz.example.com", "cdn.example.com"]}
        self.assertEqual(popup("needs", {"granted": []}), {"ask": [asked], "drop": []})
        # A granted domain is not asked again; one that is gone is not kept.
        have = ["*://*.xyz.example.com/*"]
        self.assertEqual(popup("needs", {"granted": have})["ask"],
                         [{**asked, "domains": ["cdn.example.com"]}])
        self.assertEqual(popup("needs", {"granted": [*have, "*://*.cdn.example.com/*"]}),
                         {"ask": [], "drop": []})

    def test_uninstalling_a_plugin_makes_the_extension_drop_what_only_it_used(self):
        popup = extension.Popup(self.temp / "home")
        self.install()
        gone = ["*://*.xyz.example.com/*", "*://*.cdn.example.com/*"]
        shared = ["*://*.example.net/*", "*://*/*", "https://other.example.org/*"]
        self.assertEqual(popup("needs", {"granted": [*gone, *shared]})["drop"],
                         ["*://*.example.net/*"])
        mgr = plugins.PLUGINS.manager()
        mgr.uninstall("xyz")
        plugins.PLUGINS.flush(mgr)
        found = popup("needs", {"granted": [*gone, *shared]})
        # Only the extension's own kind of pattern is dropped, never a wider or other grant.
        self.assertEqual(found, {"ask": [], "drop": sorted([*gone, "*://*.example.net/*"])})

    def test_the_extension_answers_an_origin_it_may_not_read_with_a_hint_to_allow_it(self):
        granted = {"example.com"}

        def answer(op, args):
            host = args.get("url", "https://example.com/").split("/")[2]
            if host in granted:
                return {"ok": True, "value": None}
            return {"ok": False, "error": {"class": "UsageError", "hint":
                    f"open the flnd popup to allow {host}",
                    "message": f"flnd's extension may not read {host}"}}

        async def run():
            with Fake(answer=answer):
                async with await extension.connect() as client:
                    await client.call("tabs.open", url="https://example.com/a")
                    with self.assertRaises(flnd.UsageError) as caught:
                        await client.call("tabs.open", url="https://xyz.example.net/a")
            return caught.exception

        error = asyncio.run(run())
        self.assertEqual(error.hint, "open the flnd popup to allow xyz.example.net")

    def test_the_extension_files_ask_for_optional_origins_and_say_the_hint(self):
        manifest = json.loads((extension.SOURCE / "manifest.json").read_text(encoding="utf-8"))
        self.assertEqual(manifest["optional_host_permissions"], ["*://*/*"])
        built_in = dict.fromkeys(domain for entry in _manifest.PLUGINS.values()
                                 for domain in entry["domains"])
        self.assertEqual(manifest["host_permissions"], [f"*://*.{each}/*" for each in built_in])
        background = (extension.SOURCE / "background.js").read_text(encoding="utf-8")
        self.assertIn("open the flnd popup to allow", background)
        # Every op that attaches the debugger goes through `attach`, which checks the grant first.
        attach = background[background.index("async function attach("):]
        self.assertLess(attach.index("await granted("), attach.index("chrome.debugger.attach"))
        self.assertEqual(background.count("chrome.debugger.attach("), 1)
        popup = (extension.SOURCE / "popup.js").read_text(encoding="utf-8")
        self.assertNotIn("<all_urls>", popup)
        self.assertNotIn("*://*/*", popup + background)


class InstallTest(Case):
    def test_install_writes_the_extension_and_its_hosts_and_uninstall_removes_them(self):
        profiles = [self.temp / "Chrome", self.temp / "Brave"]
        with contextlib.redirect_stdout(io.StringIO()) as shown, \
                mock.patch.object(extension, "WINDOWS", False):
            done = extension.install(home=self.temp / "home", profiles=profiles, wait=0)
        where = extension.folder()
        self.assertFalse(done.connected)
        self.assertEqual(done.folder, extension.unpacked())
        self.assertIn(f"choose {extension.unpacked()}", shown.getvalue())
        self.assertEqual(sorted(path.name for path in where.iterdir()),
                         ["flnd-host", "host.json", "token.json", "unpacked"])
        # The extension's own origin serves only these, so it can't fetch the token.
        self.assertEqual(sorted(path.name for path in extension.unpacked().iterdir()),
                         sorted(extension.FILES))
        self.assertEqual(extension.token(), TOKEN)  # a token already there is kept
        self.assertEqual((extension.unpacked() / "manifest.json").read_bytes(),
                         (extension.SOURCE / "manifest.json").read_bytes())
        for path in done.manifests:
            described = json.loads(path.read_text(encoding="utf-8"))
            self.assertEqual(described["allowed_origins"], [extension.origin()])
            self.assertTrue(os.access(described["path"], os.X_OK))
        self.assertEqual(json.loads((where / "host.json").read_text())["home"],
                         str(self.temp / "home"))
        removed = extension.uninstall(profiles=profiles)
        self.assertEqual(sorted(removed), sorted([*done.manifests, where]))
        self.assertFalse(where.exists())

    def test_the_test_suite_refuses_the_real_folders(self):
        real = extension._real_home()
        for path in (real / ".config" / "flnd" / "extension" / "token.json",
                     real / "Library" / "Application Support" / "Google" / "Chrome",
                     real / ".config" / "flnd" / "run" / "host.sock"):
            with self.subTest(path=path), self.assertRaises(RuntimeError):
                extension.guard(path)
        chrome = real / "Library" / "Application Support" / "Google" / "Chrome"
        with self.assertRaises(RuntimeError):
            extension.uninstall(profiles=[chrome])
        with mock.patch.dict(os.environ, {"HOME": str(real)}), self.assertRaises(RuntimeError):
            extension.install(profiles=[], wait=0)


if __name__ == "__main__":
    unittest.main()

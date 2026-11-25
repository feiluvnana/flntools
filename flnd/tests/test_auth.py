"""src/engine/auth.py: the logins folder, the login methods, logout, `auth list`'s rows and the
cookies a run writes back.

No browser, no network: cookie files are written by the tests, in a logins folder that is
a temporary one, never `~/.config/flnd/logins`.
"""

import io
import os
import stat
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from fakes import EngineCase, kept_login, login_cookies, make_plugin, own_login, sign_in

from flnd.engine import auth
from flnd.errors import LoginError, PluginError, UsageError
from flnd.ui.log import ui
from flnkit import clock, net

# One login cookie that ends in 2027, and a cookie of another site.
NETSCAPE = ("# Netscape HTTP Cookie File\n"
            ".aaa.example\tTRUE\t/\tTRUE\t1825861327\tsid\taaa-session\n"
            ".example.com\tTRUE\t/\tTRUE\t1825861327\tsid\telsewhere\n")
ENDS = 1825861327


class AuthCase(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.home = Path(folder.name) / ".flnd"
        # The logins folder, outside the home, as `~/.config/flnd/logins` is; not made yet.
        self.logins = Path(folder.name) / "config" / "logins"
        self.enterContext(mock.patch.object(auth, "LOGINS", self.logins))
        self.dest = io.StringIO()
        self.enterContext(ui.scope(dest=self.dest, width=400, color=False))
        self.plugin = make_plugin("aaa", login=login_cookies("sid"))
        self.export = Path(folder.name) / "cookies.txt"
        self.export.write_text(NETSCAPE)

    def mode(self, path):
        return stat.S_IMODE(path.stat().st_mode)


class FolderTest(AuthCase):
    def test_the_folder_is_beside_the_user_config_and_the_real_one_is_refused_in_tests(self):
        with mock.patch.object(auth, "LOGINS", None):
            with self.assertRaisesRegex(RuntimeError, "refusing the real flnd folder"):
                auth.folder()
            with mock.patch.dict(os.environ, {"FLND_TEST_NO_PROFILES": ""}):
                self.assertEqual(auth.folder().name, "logins")
                self.assertEqual(auth.folder().parent.name, "flnd")


class LoginTest(AuthCase):
    def test_a_cookie_file_is_kept_for_the_plugins_domains_only_with_mode_0600(self):
        found = auth.login(self.home, self.plugin(), "file", path=self.export)
        kept = self.logins / "aaa.txt"
        self.assertEqual([(cookie["domain"], cookie["value"]) for cookie in kept_login("aaa")[0]],
                         [(".aaa.example", "aaa-session")])
        self.assertTrue(self.export.is_file())
        self.assertEqual((found.plugin, found.status, found.file), ("aaa", "ok", str(kept)))
        self.assertEqual(found.expires, "2027-11-10T15:42:07+00:00")
        self.assertNotIn("elsewhere", kept.read_text())
        if os.name != "nt":
            self.assertEqual(self.mode(kept), 0o600)
            self.assertEqual(self.mode(self.logins), 0o700)
        # Never inside a home.
        self.assertEqual([path for path in self.home.rglob("*.txt")], [])

    def test_a_host_cookie_keeps_its_host(self):
        self.export.write_text("# Netscape HTTP Cookie File\n"
                               "aaa.example\tFALSE\t/\tTRUE\t0\t__Host-sid\th\n"
                               "aaa.example\tFALSE\t/\tTRUE\t0\tsid\ts\n")
        auth.login(self.home, self.plugin(), "file", path=self.export)
        self.assertEqual({cookie["name"]: cookie["domain"] for cookie in kept_login("aaa")[0]},
                         {"__Host-sid": "aaa.example", "sid": ".aaa.example"})

    def test_a_json_export_is_kept_as_netscape(self):
        self.export.write_text('[{"name": "sid", "value": "j", "domain": ".aaa.example",'
                               ' "expires": 1825861327}, {"name": "x", "value": "y",'
                               ' "domain": "other.example"}]')
        auth.login(self.home, self.plugin(), "file", path=self.export)
        self.assertEqual((self.logins / "aaa.txt").read_text().splitlines()[0],
                         "# Netscape HTTP Cookie File")
        self.assertEqual([(cookie["name"], cookie["value"]) for cookie in kept_login("aaa")[0]],
                         [("sid", "j")])

    def test_a_login_again_replaces_the_earlier_one(self):
        auth.login(self.home, self.plugin(), "file", path=self.export)
        self.export.write_text(NETSCAPE.replace("aaa-session", "second"))
        auth.login(self.home, self.plugin(), "file", path=self.export)
        self.assertEqual([cookie["value"] for cookie in kept_login("aaa")[0]], ["second"])
        self.assertEqual(sorted(path.name for path in self.logins.iterdir()), ["aaa.txt"])

    def test_a_file_without_a_login_cookie_is_refused_and_keeps_nothing(self):
        wrong = self.export.with_name("wrong.txt")
        wrong.write_text("# Netscape HTTP Cookie File\naaa.example\tFALSE\t/\tTRUE\t0\tother\to\n")
        with self.assertRaises(LoginError) as raised:
            auth.login(self.home, self.plugin(), "file", path=wrong)
        self.assertEqual((str(raised.exception), raised.exception.hint),
                         ("aaa: no login cookie (sid) in wrong.txt",
                          "export the cookies from a browser where you are logged in"))
        self.assertFalse(self.logins.exists())

    def test_the_other_refusals_name_themselves_and_keep_nothing(self):
        table = [("file", {}, UsageError, "needs the cookie file's path"),
                 ("qr", {}, UsageError, "logs in by file"),
                 ("file", {"path": self.export.with_name("none.txt")}, LoginError, "can't read")]
        for method, options, error, said in table:
            with self.subTest(method=method, options=options), \
                    self.assertRaisesRegex(error, said):
                auth.login(self.home, self.plugin(), method, **options)
        with self.assertRaisesRegex(UsageError, "needs no login"):
            auth.login(self.home, make_plugin("bbb")(), "file")
        self.assertFalse(self.logins.exists())

    def test_a_file_whose_login_cookies_have_all_expired_is_refused(self):
        self.export.write_text(NETSCAPE.replace(str(ENDS), str(ENDS - 400_000_000)))
        with mock.patch.object(clock, "wall", lambda: ENDS - 100_000_000):
            with self.assertRaises(LoginError) as raised:
                auth.login(self.home, self.plugin(), "file", path=self.export)
        self.assertEqual((str(raised.exception), raised.exception.hint),
                         ("aaa: the login in cookies.txt has expired",
                          "export a fresh cookie file"))
        self.assertFalse(self.logins.exists())

    def test_one_live_or_session_login_cookie_is_enough(self):
        plugin = make_plugin("aaa", login=login_cookies("sid", "token"))
        past, now = ENDS - 400_000_000, ENDS - 100_000_000
        for name, lines in (
                ("a live one", f".aaa.example\tTRUE\t/\tTRUE\t{past}\tsid\ta\n"
                               f".aaa.example\tTRUE\t/\tTRUE\t{ENDS}\ttoken\tb\n"),
                ("a session one", f".aaa.example\tTRUE\t/\tTRUE\t{past}\tsid\ta\n"
                                  ".aaa.example\tTRUE\t/\tTRUE\t0\ttoken\tb\n")):
            with self.subTest(name):
                self.export.write_text("# Netscape HTTP Cookie File\n" + lines)
                with mock.patch.object(clock, "wall", lambda: now):
                    auth.login(self.home, plugin(), "file", path=self.export)
                self.assertTrue((self.logins / "aaa.txt").is_file())

    def test_a_plugins_own_login_writes_its_secrets_in_start_only(self):
        def start(self, ctx, method):
            ctx.secrets.update(session=f"s-{method}", api_id="1")

        plugin = make_plugin("aaa", login=own_login(start, methods=("qr", "phone")))
        found = auth.login(self.home, plugin(), "qr")
        self.assertEqual((found.plugin, found.status, found.file),
                         ("aaa", "ok", str(self.logins / "aaa.json")))
        self.assertEqual(kept_login("aaa")[1], {"session": "s-qr", "api_id": "1"})
        if os.name != "nt":
            self.assertEqual(self.mode(self.logins / "aaa.json"), 0o600)
            self.assertEqual(self.mode(self.logins), 0o700)

        def check(self, ctx):
            ctx.secrets["late"] = "1"

        written = make_plugin("aaa", login=own_login(check=check))
        with self.assertRaisesRegex(PluginError, "written only during Login.start"):
            auth.login(self.home, written(), "qr")
        self.assertEqual(kept_login("aaa")[1], {"session": "s-qr", "api_id": "1"})

    def test_a_failed_check_keeps_nothing(self):
        plugin = make_plugin("aaa", login=own_login(check=lambda self, ctx: False))
        with self.assertRaisesRegex(LoginError, "the login didn't work"):
            auth.login(self.home, plugin(), "qr")
        self.assertFalse(self.logins.exists())

    def test_a_stored_api_key_is_not_asked_again(self):
        seen = []

        def start(self, ctx, method):
            seen.append(dict(ctx.secrets))
            ctx.secrets["session"] = "new"

        sign_in("aaa", session="old", api_id="1")
        auth.login(self.home, make_plugin("aaa", login=own_login(start))(), "qr")
        self.assertEqual(seen, [{"session": "old", "api_id": "1"}])
        self.assertEqual(kept_login("aaa")[1], {"session": "new", "api_id": "1"})

    def test_secrets_round_trip_through_the_json_file(self):
        sign_in("tlg", session="1BVts-secret", api_id="2", api_hash="abc")
        self.assertEqual(kept_login("tlg")[1], {"session": "1BVts-secret", "api_id": "2",
                                             "api_hash": "abc"})
        self.assertEqual(auth.secrets("nobody"), {})
        (self.logins / "tlg.json").write_text("[1]")
        with self.assertRaisesRegex(LoginError, "isn't a login file"):
            auth.secrets("tlg")
        (self.logins / "tlg.json").write_text("{not json")
        with self.assertRaisesRegex(LoginError, "can't read tlg.json"):
            auth.secrets("tlg")

    def test_ask_answers_only_in_a_terminal_with_its_description(self):
        answers = []

        async def start(self, ctx, method):
            try:
                answers.append(await ctx.ask("Phone number", description="With the country code"))
            except UsageError:
                answers.append(None)
            ctx.secrets["session"] = "s"

        plugin = make_plugin("aaa", login=own_login(start))
        with mock.patch("flnkit.ui.forms.ask", return_value="1") as asked:
            for tty, interactive in ((False, True), (True, False), (True, True)):
                with mock.patch.object(sys.stdin, "isatty", return_value=tty):
                    auth.login(self.home, plugin(), "qr", interactive=interactive)
        self.assertEqual(answers, [None, None, "1"])
        self.assertEqual(asked.mock_calls[0], mock.call("Phone number", secret=False))
        self.assertIn("With the country code", self.dest.getvalue())


class ListAndLogoutTest(AuthCase):
    def test_a_login_lists_its_status_and_its_file(self):
        now = ENDS - 100_000_000
        table = [("ok", now + 90 * 86400), ("expires in 3 days", now + 2 * 86400 + 3600),
                 ("expired", now - 10)]
        for status, ends in table:
            with self.subTest(status):
                sign_in("aaa", [{"name": "sid", "value": "v", "domain": ".aaa.example",
                                 "expires": ends}])
                with mock.patch.object(clock, "wall", lambda: now):
                    found = auth.entry("aaa", self.plugin)
                self.assertEqual((found.status, found.file), (status, str(self.logins / "aaa.txt")))

    def test_a_session_login_never_expires_and_a_missing_one_is_none(self):
        sign_in("aaa", [{"name": "sid", "value": "v", "domain": ".aaa.example"}])
        self.assertEqual(auth.entry("aaa", self.plugin).status, "ok")
        self.assertIsNone(auth.entry("bbb", self.plugin))
        sign_in("tlg", session="s")
        self.assertEqual(auth.entry("tlg", self.plugin).file, str(self.logins / "tlg.json"))
        (self.logins / "aaa.txt").write_bytes(b"\xff\xfe")
        self.assertEqual(auth.entry("aaa", self.plugin).status, "unreadable")

    def test_the_codes_with_a_login_are_listed_in_order(self):
        self.assertEqual(auth.codes(), [])
        sign_in("tlg", session="s")
        sign_in("aaa", [{"name": "sid", "value": "v", "domain": ".aaa.example"}])
        self.assertEqual(auth.codes(), ["aaa", "tlg"])

    def test_logout_deletes_the_files_of_that_plugin_only(self):
        with self.assertRaisesRegex(UsageError, "aaa: not logged in"):
            auth.logout("aaa", self.plugin)
        sign_in("aaa", [{"name": "sid", "value": "v", "domain": ".aaa.example"}], session="s")
        sign_in("bbb", session="b")
        self.assertEqual(auth.logout("aaa", self.plugin).plugin, "aaa")
        self.assertEqual(sorted(path.name for path in self.logins.iterdir()), ["bbb.json"])
        with self.assertRaises(UsageError):
            auth.logout("aaa", self.plugin)


class WriteBackTest(AuthCase):
    def setUp(self):
        super().setUp()
        sign_in("twt", [
            {"name": "ct0", "value": "old-ct0", "domain": ".x.example", "path": "/",
             "expires": 1900000000},
            {"name": "auth_token", "value": "login-token", "domain": ".x.example", "path": "/"}])

    def current(self, domain=".x.example", **values):
        return net.Session(cookies=[{"name": name, "value": value, "domain": domain, "path": "/"}
                                    for name, value in values.items()]).jar

    def saved(self):
        return kept_login("twt")[0]

    def test_a_rotated_cookie_is_saved_in_the_file_and_the_rest_kept(self):
        for domain in (".x.example", "api.x.example"):
            with self.subTest(domain):
                self.assertEqual(auth.write_back("twt", self.current(domain, ct0=f"new{domain}")),
                                 ["ct0"])
                self.assertEqual((self.saved()["ct0"], self.saved()["auth_token"],
                                  len(self.saved())),
                                 (f"new{domain}", "login-token", 2))
        self.assertEqual([cookie["expires"] for cookie in self.saved() if cookie["name"] == "ct0"],
                         [1900000000])
        if os.name != "nt":
            self.assertEqual(self.mode(self.logins / "twt.txt"), 0o600)
        self.assertEqual(sorted(path.name for path in self.logins.iterdir()), ["twt.txt"])

    def test_nothing_new_is_added_and_nothing_written_without_a_change(self):
        table = [self.current(ct0="old-ct0", _ga="track"), self.current(ct0=""), []]
        before = (self.logins / "twt.txt").stat().st_mtime_ns
        for current in table:
            with self.subTest(current=current):
                self.assertEqual(auth.write_back("twt", current), [])
        self.assertEqual([cookie["name"] for cookie in self.saved()], ["ct0", "auth_token"])
        self.assertEqual((self.logins / "twt.txt").stat().st_mtime_ns, before)

    def test_a_plugin_without_a_login_file_writes_nothing(self):
        self.assertEqual(auth.write_back("never", self.current(ct0="x")), [])
        self.assertEqual(sorted(path.name for path in self.logins.iterdir()), ["twt.txt"])

    def test_a_failure_warns_and_no_value_reaches_a_line(self):
        auth.write_back("twt", self.current(ct0="new-ct0"))
        with mock.patch.object(auth, "load", side_effect=OSError("disk full: newer-ct0")):
            self.assertEqual(auth.write_back("twt", self.current(ct0="newer-ct0")), [])
        said = self.dest.getvalue()
        self.assertIn("twt: could not save refreshed cookies (OSError)", said)
        for value in ("old-ct0", "new-ct0", "newer-ct0", "login-token"):
            self.assertNotIn(value, said)
        self.assertEqual(self.saved()["ct0"], "new-ct0")


class RefusedLoginTest(EngineCase):
    """A run of a plugin whose login the site refuses: its targets fail, and the next run tries
    again."""

    def setUp(self):
        super().setUp()
        sign_in("aaa", [{"name": "sid", "value": "one", "domain": "aaa.example"}])
        self.refused = True

        def page(request):
            return net.reply(403 if self.refused else 200, json={"ok": True})

        self.routes["https://aaa.example/p"] = page

        async def route(ctx):
            await ctx.fetch("https://aaa.example/p")

        self.plugin = make_plugin("aaa", route, login=login_cookies("sid"))

    def test_a_refused_login_fails_every_target_with_its_hint_and_the_next_run_tries_again(self):
        report = self.report(self.plugin, targets=["aaa:x", "aaa:y"])
        self.assertEqual([type(each.error) for each in report.targets], [LoginError] * 2)
        self.assertEqual({each.error.hint for each in report.targets},
                         {"export a fresh cookie file, then flnd auth login aaa <file>"})
        self.refused = False
        self.requests.clear()
        self.assertTrue(self.report(self.plugin, targets=["aaa:x"]).ok)
        self.assertEqual([request.headers["cookie"] for request in self.requests], ["sid=one"])

    def test_the_run_sends_the_cookies_of_the_plugins_file(self):
        self.refused = False
        self.assertTrue(self.report(self.plugin, targets=["aaa:x"]).ok)
        self.assertEqual([request.headers["cookie"] for request in self.requests], ["sid=one"])
        self.assertEqual([path.name for path in auth.folder().iterdir()], ["aaa.txt"])


if __name__ == "__main__":
    unittest.main()

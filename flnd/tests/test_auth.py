"""src/engine/auth.py: the sealed vault and its key, accounts, the login methods, logout, the
logins' state and the cookies a run writes back.

No browser, no network: cookie stores are faked, cookie files are written by the tests, and the
vault is a real one in a temporary home, with its key outside it.
"""

import contextlib
import io
import json
import os
import sqlite3
import stat
import sys
import tempfile
import time
import unittest
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from fakes import EngineCase, login_cookies, make_plugin, own_login, sign_in, targets, vaulted
from test_ctx import FakeBrowser, FakeRaw

import flnd
from flnd.engine import auth, jobs, store
from flnd.errors import Blocked, Hidden, LoginError, NotFound, PluginError, UsageError
from flnd.plugin import Account, Target
from flnd.testing import fake_clock
from flnd.ui.log import ui
from flnkit import cookies, db, net

NETSCAPE = ("# Netscape HTTP Cookie File\n"
            ".aaa.example\tTRUE\t/\tTRUE\t1825861327\tsid\taaa-session\n"
            ".example.com\tTRUE\t/\tTRUE\t1825861327\tsid\telsewhere\n")


@contextmanager
def raw(home):
    """`secret.db`, opened as any SQLite tool would."""
    db = sqlite3.connect(home / store.SECRETS, isolation_level=None)
    try:
        yield db
    finally:
        db.close()


class AuthCase(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.home = Path(folder.name) / ".flnd"
        # The key outside the home, as `~/.config/flnd/key` is.
        self.key = Path(folder.name) / "config" / "key"
        self.enterContext(mock.patch.object(auth, "KEYFILE", self.key))
        self.dest = io.StringIO()
        self.enterContext(ui.scope(dest=self.dest, width=400, color=False))
        self.plugin = make_plugin("aaa", login=login_cookies("sid"))

    def accounts(self, code="aaa"):
        with store.open(self.home) as db:
            return [(row.name, row.origin, row.default) for row in db.accounts(code)]

    def stored(self):
        """Every byte of `secret.db` and its WAL."""
        return b"".join(path.read_bytes() for path in self.home.rglob("secret.db*"))


class VaultTest(AuthCase):
    def test_a_value_is_sealed_under_the_key_outside_the_home(self):
        sign_in(self.home, "tlg", session="1BVts-secret", api_hash="abc")
        self.assertEqual(vaulted(self.home, "tlg")[1], {"session": "1BVts-secret", "api_hash": "abc"})
        with raw(self.home) as db:
            [(sealed,)] = db.execute("SELECT sealed FROM secrets WHERE name = 'session'")
        self.assertEqual(sealed[0], 1)
        self.assertFalse(b"1BVts-secret" in self.stored(), "plaintext in secret.db")
        self.assertEqual(len(self.key.read_bytes()), 32)
        self.assertFalse(any(path.name == "key" for path in self.home.rglob("*")))
        if os.name != "nt":
            self.assertEqual(stat.S_IMODE(self.key.stat().st_mode), 0o600)
            self.assertEqual(stat.S_IMODE((self.home / store.SECRETS).stat().st_mode), 0o600)

    def test_s16_secret_db_holds_no_plaintext_after_a_login(self):
        export = self.home.parent / "cookies.txt"
        export.write_text(NETSCAPE.replace("aaa.example", "instagram.com").replace("\tsid\t", "\tsessionid\t"))
        with flnd.open(self.home) as fl:
            fl.login("ins", str(export))
        self.assertTrue(self.stored())
        self.assertFalse(b"aaa-session" in self.stored(), "plaintext in secret.db")

    def test_a_value_moved_to_another_slot_does_not_open(self):
        first, second = sign_in(self.home, "aaa", name="one"), sign_in(self.home, "aaa", name="two")
        with auth.secrets(self.home, first.id) as saved:
            saved["token"] = "t"
        with raw(self.home) as db:
            db.execute("UPDATE secrets SET account = ?", (second.id,))
        with auth.secrets(self.home, second.id) as saved, self.assertRaises(LoginError):
            saved["token"]

    def test_a_missing_or_wrong_key_keeps_the_rows(self):
        sign_in(self.home, "aaa", token="t")
        kept = self.key.read_bytes()
        for name, change in (("missing", self.key.unlink),
                             ("wrong", lambda: self.key.write_bytes(bytes(32)))):
            with self.subTest(name), auth.secrets(self.home, 1, plugin="aaa") as saved:
                change()
                with self.assertRaises(LoginError) as raised:
                    saved["token"]
                self.assertEqual((str(raised.exception), raised.exception.hint),
                                 ("the vault's key is gone", "flnd auth login aaa"))
                self.assertIn("token", saved)
                self.key.unlink(missing_ok=True)
                self.key.write_bytes(kept)
                self.assertEqual(saved["token"], "t")

    def test_no_key_file_or_vault_until_a_secret_is_written(self):
        with auth.secrets(self.home, 1) as saved:
            self.assertEqual((len(saved), "x" in saved), (0, False))
        self.assertFalse(self.key.exists() or self.home.exists())

    def version_1(self, session="plain-session"):
        self.home.joinpath("metadata").mkdir(parents=True)
        with raw(self.home) as db:
            db.execute("CREATE TABLE secrets (slot TEXT NOT NULL, name TEXT NOT NULL,"
                       " value TEXT NOT NULL, PRIMARY KEY (slot, name)) WITHOUT ROWID")
            db.executemany("INSERT INTO secrets VALUES (?, ?, ?)",
                           [("tlg", "session", session), ("ins", "cookies", "[]")])

    def test_a_crash_before_the_vacuum_leaves_no_plaintext(self):
        self.version_1("plain-session-" * 40)
        connect = db.connect

        class Crashing:
            def __init__(self, sql):
                self.sql = sql

            def execute(self, statement, *args):
                if statement == "VACUUM":
                    raise KeyboardInterrupt("crash")
                return self.sql.execute(statement, *args)

            def __getattr__(self, name):
                return getattr(self.sql, name)

        with mock.patch.object(db, "connect", lambda path: Crashing(connect(path))), \
                self.assertRaises(KeyboardInterrupt):
            auth.seal_plaintext(self.home)
        self.assertFalse(b"plain-session-plain" in self.stored(), "plaintext in secret.db")
        self.assertEqual(vaulted(self.home, "tlg")[1], {"session": "plain-session-" * 40})
        self.assertFalse(b"plain-session-plain" in self.stored(), "plaintext in secret.db")
        with raw(self.home) as sql:
            self.assertEqual(sql.execute("PRAGMA freelist_count").fetchone(), (0,))

    def test_plaintext_is_sealed_on_first_open(self):
        self.version_1()
        plugins = {"tlg": make_plugin("tlg", login=own_login())}
        self.assertEqual([row.state for row in auth.logins(self.home, plugins)], ["logged in"])
        self.assertEqual(vaulted(self.home, "tlg")[1], {"session": "plain-session"})
        self.assertEqual([self.accounts(code) for code in ("ins", "tlg")],
                         [[("default", "", True)], [("default", "", True)]])
        self.assertFalse(b"plain-session" in self.stored(), "plaintext in secret.db")


class LoginTest(AuthCase):
    def setUp(self):
        super().setUp()
        self.export = self.home.parent / "cookies.txt"
        self.export.write_text(NETSCAPE)

    def test_a_cookie_file_keeps_the_plugins_domains_only_and_is_never_copied(self):
        found = auth.login(self.home, self.plugin(), "file", path=self.export)
        self.assertEqual((found.name, found.origin, found.is_default), ("account1", "file", True))
        self.assertEqual([(cookie["domain"], cookie["value"]) for cookie in vaulted(self.home, "aaa")[0]],
                         [(".aaa.example", "aaa-session")])
        self.assertTrue(self.export.is_file())
        self.assertEqual(sorted(path.name for path in self.home.rglob("*") if path.is_file()),
                         ["flnd.db", "secret.db"])
        self.assertEqual(found.expires, "2027-11-10T15:42:07+00:00")

    def test_a_browser_store_is_read_by_its_method(self):
        jar = cookies.Jar([{"name": "sid", "value": "s", "domain": "aaa.example", "path": "/"}])
        for method in ("chrome", "firefox", "edge"):
            with self.subTest(method), mock.patch("flnkit.cookies.load", return_value=jar) as read:
                self.assertEqual(auth.login(self.home, self.plugin(), method).origin, method)
                read.assert_called_once_with(method, profile=None)
        self.assertEqual(self.accounts(), [("account1", "edge", True)])

    def test_s14_a_host_cookie_keeps_its_host(self):
        self.export.write_text("# Netscape HTTP Cookie File\n"
                               "aaa.example\tFALSE\t/\tTRUE\t0\t__Host-sid\th\n"
                               "aaa.example\tFALSE\t/\tTRUE\t0\tsid\ts\n")
        auth.login(self.home, self.plugin(), "file", path=self.export)
        self.assertEqual({cookie["name"]: cookie["domain"] for cookie in vaulted(self.home, "aaa")[0]},
                         {"__Host-sid": "aaa.example", "sid": ".aaa.example"})

    def test_a_refused_login_keeps_nothing(self):
        empty = cookies.Jar([{"name": "other", "value": "o", "domain": "aaa.example"}])
        table = [("chrome", {}, LoginError, "no sid for aaa.example in Chrome"),
                 ("file", {}, UsageError, "needs the cookie file's path"),
                 ("qr", {}, UsageError, "logs in by browser, chrome, file"),
                 ("file", {"path": self.home.parent / "none.txt"}, LoginError, "can't read")]
        for method, options, error, said in table:
            store_read = mock.patch("flnkit.cookies.load", return_value=empty)
            with self.subTest(method=method, options=options), \
                    store_read if method == "chrome" else contextlib.nullcontext(), \
                    self.assertRaisesRegex(error, said):
                auth.login(self.home, self.plugin(), method, **options)
        with self.assertRaisesRegex(UsageError, "needs no login"):
            auth.login(self.home, make_plugin("bbb")(), "file")
        self.assertEqual(self.accounts(), [])

    def test_a_plugins_own_login_writes_its_secrets_in_start_only(self):
        def start(self, ctx, method):
            ctx.secrets.update(session=f"s-{method}", api_id="1")

        plugin = make_plugin("aaa", login=own_login(start, methods=("qr", "phone")))
        self.assertEqual(auth.login(self.home, plugin(), "qr").origin, "qr")
        self.assertEqual(vaulted(self.home, "aaa")[1], {"session": "s-qr", "api_id": "1"})

        def check(self, ctx):
            ctx.secrets["late"] = "1"

        written = make_plugin("aaa", login=own_login(check=check))
        with self.assertRaisesRegex(PluginError, "written only during Login.start"):
            auth.login(self.home, written(), "qr")
        self.assertEqual(vaulted(self.home, "aaa")[1], {"session": "s-qr", "api_id": "1"})

    def test_a_failed_check_keeps_nothing(self):
        plugin = make_plugin("aaa", login=own_login(check=lambda self, ctx: False))
        with self.assertRaisesRegex(LoginError, "the login didn't work"):
            auth.login(self.home, plugin(), "qr")
        self.assertEqual(self.accounts(), [])

    def test_a_stored_api_key_is_not_asked_again(self):
        seen = []

        def start(self, ctx, method):
            seen.append(dict(ctx.secrets))
            ctx.secrets["session"] = "new"

        sign_in(self.home, "aaa", session="old", api_id="1")
        auth.login(self.home, make_plugin("aaa", login=own_login(start))(), "qr")
        self.assertEqual(seen, [{"session": "old", "api_id": "1"}])
        self.assertEqual(vaulted(self.home, "aaa")[1], {"session": "new", "api_id": "1"})
        self.assertEqual(self.accounts(), [("default", "qr", True)])

    def test_ask_answers_only_in_a_terminal_with_its_description(self):
        answers = []

        async def start(self, ctx, method):
            try:
                answers.append(await ctx.ask("Phone number", description="With the country code"))
            except UsageError:
                answers.append(None)
            ctx.secrets["session"] = "s"

        plugin = make_plugin("aaa", login=own_login(start))
        with mock.patch("flnd.ui.forms.ask", return_value="1") as asked:
            for tty, interactive in ((False, True), (True, False), (True, True)):
                with mock.patch.object(sys.stdin, "isatty", return_value=tty):
                    auth.login(self.home, plugin(), "qr", interactive=interactive)
        self.assertEqual(answers, [None, None, "1"])
        self.assertEqual(asked.mock_calls[0], mock.call("Phone number", secret=False))
        self.assertIn("With the country code", self.dest.getvalue())


class WindowTest(AuthCase):
    """The `browser` method: flnd's Chromium, visible, at the login page, until the login is set."""

    def window(self, *jars, pages=(1,)):
        """A faked `flnkit.browser.launch`, whose window gives one of `jars` per look."""
        opened, looks = [], []

        class Window:
            raw = SimpleNamespace(pages=list(pages))
            closed, url = False, None

            async def tab(self):
                return SimpleNamespace(goto=self.goto)

            async def goto(self, url):
                self.url = url

            async def cookies(self):
                looks.append(1)
                return cookies.Jar(jars[min(len(looks), len(jars)) - 1])

            async def close(self):
                self.closed = True

        async def launch(**options):
            opened.append((options, Window(), looks))
            return opened[-1][1]

        self.enterContext(mock.patch("flnkit.browser.launch", launch))
        self.enterContext(fake_clock())
        return opened

    def test_the_login_is_kept_once_its_cookie_is_set(self):
        login = [{"name": "sid", "value": "s", "domain": "aaa.example"},
                 {"name": "sid", "value": "elsewhere", "domain": "example.com"}]
        opened = self.window([], [{"name": "other", "value": "o", "domain": "aaa.example"}], login)
        self.assertEqual(auth.login(self.home, self.plugin(), "browser").origin, "browser")
        [(options, window, looks)] = opened
        self.assertEqual((options, window.url, window.closed, len(looks)),
                         ({"headless": False}, "https://example.com/login", True, 3))
        self.assertEqual([cookie["value"] for cookie in vaulted(self.home, "aaa")[0]], ["s"])

    def test_a_closed_window_or_no_login_in_time_keeps_nothing(self):
        for pages, said in (((), "the window was closed before the login"),
                            ((1,), "no login in the window in 10m")):
            with self.subTest(said), self.assertRaisesRegex(LoginError, said):
                opened = self.window([], pages=pages)
                auth.login(self.home, self.plugin(), "browser")
            self.assertTrue(opened[0][1].closed)
        self.assertEqual(self.accounts(), [])


class AccountsTest(AuthCase):
    def login(self, name=None, session="s", **options):
        def start(self, ctx, method):
            ctx.secrets["session"] = session

        plugin = make_plugin("aaa", login=own_login(start, whoami=name))
        return auth.login(self.home, plugin(), "qr", **options)

    def test_each_name_whoami_gives_is_an_account_and_the_first_is_the_default(self):
        self.login("example_user", "one")
        self.login("example_creator", "two")
        self.login("example_user", "three")
        self.assertEqual(self.accounts(), [("example_user", "qr", True),
                                           ("example_creator", "qr", False)])
        self.assertEqual([vaulted(self.home, "aaa", name)[1] for name in ("example_user", "example_creator")],
                         [{"session": "three"}, {"session": "two"}])

    def test_an_unnamed_account_is_asked_for_in_a_terminal_else_the_default_or_account1(self):
        self.login()
        self.login()
        self.assertEqual(self.accounts(), [("account1", "qr", True)])
        with mock.patch.object(sys.stdin, "isatty", return_value=True), \
                mock.patch("flnd.ui.forms.ask", return_value="example_handle") as asked:
            self.login(interactive=True)
        asked.assert_called_once_with("Account name", default="account1")
        self.assertEqual([name for name, _, _ in self.accounts()], ["account1", "example_handle"])

    def test_pick_prefers_the_account_that_worked_then_ok_ones(self):
        for name in ("one", "two", "three"):
            sign_in(self.home, "aaa", name=name)
        with store.open(self.home) as db:
            ids = {row.name: row.id for row in db.accounts("aaa")}
            table = [({}, None, "one"), ({}, "two", "two"), ({"one": "held"}, None, "two"),
                     ({"one": "expired", "two": "expired"}, "two", "three"),
                     ({"one": "held", "two": "expired", "three": "held"}, None, "one"),
                     ({"one": "expired", "two": "expired", "three": "expired"}, None, None)]
            for marks, preferred, expected in table:
                with self.subTest(marks=marks, preferred=preferred):
                    for name, ident in ids.items():
                        db.mark(ident, status=marks.get(name, "ok"))
                    listed = [auth.account(row) for row in db.accounts("aaa")]
                    found = auth.pick(listed, preferred=preferred)
                    self.assertEqual(found and found.name, expected)

    def test_logout_deletes_every_account_and_its_secrets(self):
        self.assertFalse(auth.logout(self.home, "aaa"))
        self.login("example_user")
        self.login("example_creator")
        sign_in(self.home, "bbb", session="b")
        self.assertTrue(auth.logout(self.home, "aaa"))
        self.assertEqual(self.accounts(), [])
        with raw(self.home) as db:
            self.assertEqual(db.execute("SELECT count(*) FROM secrets").fetchone(), (1,))
        self.assertFalse(auth.logout(self.home, "aaa"))

    def test_logins_says_each_plugins_state_and_names_expiring_cookies_only(self):
        now = time.time()
        sign_in(self.home, "aaa", [
            {"name": "sid", "value": "top-secret-1", "domain": "aaa.example", "expires": now + 3600},
            {"name": "other", "value": "top-secret-2", "domain": "aaa.example", "expires": now - 9}],
            origin="chrome")
        sign_in(self.home, "tlg", api_id="1")
        plugins = {"aaa": self.plugin, "tlg": make_plugin("tlg", login=own_login()),
                   "ccc": make_plugin("ccc")}
        found = {row.source: row for row in auth.logins(self.home, plugins)}
        self.assertEqual([(row.state, row.origin) for row in found.values()],
                         [("logged in", "chrome"), ("api key only", ""), ("not logged in", "")])
        [(name, left)] = found["aaa"].expiring
        self.assertEqual(name, "sid")
        self.assertGreater(left, 0)
        self.assertNotIn("top-secret", repr(found))
        # With the key gone, the listing says so, and the rows stay.
        self.key.unlink()
        self.assertEqual([row.state for row in auth.logins(self.home, plugins)],
                         ["key gone", "api key only", "not logged in"])
        self.assertEqual(len(self.accounts()), 1)


class WriteBackTest(AuthCase):
    def setUp(self):
        super().setUp()
        self.account = sign_in(self.home, "twt", [
            {"name": "ct0", "value": "old-ct0", "domain": ".x.example", "path": "/", "expires": 1900000000},
            {"name": "auth_token", "value": "login-token", "domain": ".x.example", "path": "/"}])

    def current(self, domain=".x.example", **values):
        return net.Session(cookies=[{"name": name, "value": value, "domain": domain, "path": "/"}
                                    for name, value in values.items()]).jar

    def saved(self):
        return vaulted(self.home, "twt")[0]

    def test_a_rotated_cookie_is_saved_and_the_rest_kept(self):
        for domain in (".x.example", "api.x.example"):
            with self.subTest(domain):
                self.assertEqual(auth.write_back(self.home, self.account,
                                                 self.current(domain, ct0=f"new{domain}")), ["ct0"])
                self.assertEqual((self.saved()["ct0"], self.saved()["auth_token"], len(self.saved())),
                                 (f"new{domain}", "login-token", 2))
        self.assertEqual([cookie["expires"] for cookie in self.saved() if cookie["name"] == "ct0"],
                         [1900000000])

    def test_nothing_new_is_added_and_nothing_written_without_a_change(self):
        table = [self.current(ct0="old-ct0", _ga="track"), self.current(ct0=""), []]
        for current in table:
            with self.subTest(current=current):
                self.assertEqual(auth.write_back(self.home, self.account, current), [])
        self.assertEqual([cookie["name"] for cookie in self.saved()], ["ct0", "auth_token"])
        self.assertEqual(auth.write_back(self.home / "never", self.account, self.current(ct0="x")), [])
        self.assertFalse((self.home / "never").exists())

    def test_a_failure_warns_and_no_value_reaches_a_line(self):
        auth.write_back(self.home, self.account, self.current(ct0="new-ct0"))
        with mock.patch.object(auth, "secrets", side_effect=OSError("disk full: newer-ct0")):
            self.assertEqual(auth.write_back(self.home, self.account, self.current(ct0="newer-ct0")), [])
        said = self.dest.getvalue()
        self.assertIn("twt: could not save refreshed cookies (OSError)", said)
        for value in ("old-ct0", "new-ct0", "newer-ct0", "login-token"):
            self.assertNotIn(value, said)
        self.assertEqual(self.saved()["ct0"], "new-ct0")



class SwitchTest(AuthCase):
    """`auth.Switcher`, the scheduler's `switch`: which account a refused job moves to."""

    def setUp(self):
        super().setUp()
        self.enterContext(fake_clock())
        self.scheduler = jobs.Scheduler()
        self.listed = [Account(id=n, plugin="aaa", name=name, origin="file", at="")
                       for n, name in enumerate("abc", 1)]
        self.shown = []
        self.switch = auth.Switcher(self.scheduler, lambda code: code == "aaa",
                                    lambda code: self.listed, shown=self.shown.append)

    def job(self, plugin="aaa", account="a"):
        return jobs.Job(kind=jobs.Kind.PAGE, work=None, plugin=plugin, account=account)

    def test_each_trigger_moves_the_job_to_the_next_account_and_journals_it(self):
        journal = Path(self.home.parent / "journal.jsonl")
        table = [(Blocked("blocked", wait=60.0), "blocked"), (LoginError("401"), "expired"),
                 (Hidden("hidden"), "hidden"), (NotFound("gone"), "not_found")]
        with ui.file(journal):
            for error, reason in table:
                with self.subTest(reason):
                    made = self.job()
                    self.assertIs(self.switch(made, error), made)
                    self.assertEqual((made.account, self.shown[-1]), ("b", f"aaa: a → b ({reason})"))
        records = [json.loads(line) for line in journal.read_text().splitlines()]
        self.assertEqual([(record["type"], record["from"], record["to"], record["reason"])
                          for record in records],
                         [("switch", "a", "b", reason) for _, reason in table])
        self.assertEqual((self.switch.expired, list(self.switch.held)), ({("aaa", "a")}, [("aaa", "a")]))
        self.assertEqual([(hold.account, hold.until) for hold in self.scheduler.holds], [("a", 60.0)])

    def test_a_job_never_goes_back_to_an_account_it_left(self):
        made, moves = self.job(), []
        while self.switch(made, LoginError("403")) is not None:
            moves.append(made.account)
        self.assertEqual((moves, made.account), (["b", "c"], "c"))

    def test_a_held_account_is_passed_over_and_with_every_one_held_the_earliest_is_taken(self):
        self.scheduler.hold("aaa", "b", seconds=300.0, reason="blocked")
        made = self.job()
        self.switch(made, Hidden("hidden"))
        self.assertEqual(made.account, "c")
        self.listed[2] = Account(id=3, plugin="aaa", name="c", origin="file", at="",
                                 status="held", held_until="2026-01-01T00:10:00+00:00")
        made = self.job()
        self.switch(made, Hidden("hidden"))
        self.assertEqual(made.account, "b")
        self.listed[1] = Account(id=2, plugin="aaa", name="b", origin="file", at="", status="expired")
        made = self.job()
        self.switch(made, Hidden("hidden"))
        self.assertEqual(made.account, "c")

    def test_nothing_moves_with_switching_off_or_without_an_account(self):
        for made in (self.job(plugin="bbb"), self.job(account=None)):
            with self.subTest(made.plugin, account=made.account):
                self.assertIsNone(self.switch(made, Hidden("hidden")))
        self.assertEqual(self.shown, [])


class SwitchEngineTest(EngineCase):
    """A run of a plugin with `switch = true` and two accounts."""

    def setUp(self):
        super().setUp()
        self.home.mkdir(parents=True)
        (self.home / "config.toml").write_text("[plugins.aaa]\nswitch = true\n")
        for name in ("one", "two"):
            sign_in(self.home, "aaa", [{"name": "sid", "value": name, "domain": "aaa.example"}],
                    name=name)
        self.refused = {"one"}

        def page(request):
            sid = request.headers["cookie"].removeprefix("sid=")
            return net.reply(403 if sid in self.refused else 200, json={"as": sid})

        self.routes["https://aaa.example/p"] = page
        self.seen = []

        async def route(ctx):
            self.seen.append((await ctx.fetch("https://aaa.example/p")).json["as"])

        self.plugin = make_plugin("aaa", route, login=login_cookies("sid"))

    def sent(self):
        return [request.headers["cookie"] for request in self.requests]

    def test_a_refused_account_hands_the_target_to_the_next_which_is_kept(self):
        self.assertTrue(self.report(self.plugin, targets=["aaa:x"]).ok)
        self.assertEqual((self.sent(), self.seen), (["sid=one", "sid=two"], ["two"]))
        self.assertEqual(targets(self.home)["aaa:x"].account, "two")
        with store.open(self.home) as db:
            self.assertEqual([(row.name, row.status) for row in db.accounts("aaa")],
                             [("one", "expired"), ("two", "ok")])
        # The next run starts as the account that worked.
        self.requests.clear()
        self.refused = set()
        self.assertTrue(self.report(self.plugin, targets=["aaa:x"]).ok)
        self.assertEqual(self.sent(), ["sid=two"])

    def test_with_every_account_refused_the_target_fails(self):
        self.refused = {"one", "two"}
        report = self.report(self.plugin, targets=["aaa:x"])
        self.assertIsInstance(report.targets[0].error, LoginError)
        self.assertEqual(self.sent(), ["sid=one", "sid=two"])

    def test_a_page_opened_after_a_move_is_the_new_accounts(self):
        browsers = []

        async def launch(**options):
            # The first account's window is walled; the second's opens.
            status = 403 if not browsers else 200
            browsers.append(FakeBrowser(lambda: FakeRaw([status] * 20), options))
            return browsers[-1]

        async def route(ctx):
            self.seen.append((await ctx.open("https://aaa.example/p")).status)

        self.enterContext(mock.patch("flnkit.browser.launch", launch))
        plugin = make_plugin("aaa", route, login=login_cookies("sid"), browser=True)
        self.assertTrue(self.report(plugin, targets=["aaa:x"]).ok)
        self.assertEqual(self.seen, [200])
        self.assertEqual([[raw.loaded for raw in browser.opened] for browser in browsers],
                         [[["https://aaa.example/p"]], [["https://aaa.example/p"]]])
        self.assertEqual(targets(self.home)["aaa:x"].account, "two")


class BrowserAccountsTest(EngineCase):
    """Two accounts of a browser plugin in one run: each sees its own cookies alone."""

    def setUp(self):
        super().setUp()
        for name in ("one", "two"):
            sign_in(self.home, "aaa", [{"name": "sid", "value": name, "domain": "aaa.example",
                                        "path": "/"}], name=name)
        with store.open(self.home) as db:
            db.progress(Target("aaa:y", kind="user"), cursors={}, complete=[], account="two")
        self.browsers = []

        async def launch(**options):
            self.browsers.append(FakeBrowser(FakeRaw, options))
            return self.browsers[-1]

        self.enterContext(mock.patch("flnkit.browser.launch", launch))
        self.routes["https://aaa.example/api"] = net.reply(200, json={})

    def test_two_accounts_never_see_each_others_cookies(self):
        async def route(ctx):
            await ctx.open(f"https://aaa.example/{ctx.target.value}")
            await ctx.fetch("https://aaa.example/api")

        plugin = make_plugin("aaa", route, login=login_cookies("sid"), browser=True)
        self.assertTrue(self.report(plugin, targets=["aaa:x", "aaa:y"]).ok)
        self.assertEqual(sorted(request.headers["cookie"] for request in self.requests),
                         ["sid=one", "sid=two"])
        self.assertEqual(sorted([cookie["value"] for cookie in browser.added]
                                for browser in self.browsers), [["one"], ["two"]])
        self.assertEqual([vaulted(self.home, "aaa", name)[0]["sid"] for name in ("one", "two")],
                         ["one", "two"])


if __name__ == "__main__":
    unittest.main()

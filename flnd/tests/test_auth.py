"""src/engine/auth.py: the sealed vault and its key, accounts, the login methods, logout, the
logins' state and the cookies a run writes back.

No browser, no network: cookie stores are faked, cookie files are written by the tests, and the
vault is a real one in a temporary home, with its key outside it.
"""

import contextlib
import io
import os
import sqlite3
import stat
import sys
import tempfile
import time
import unittest
from contextlib import contextmanager
from pathlib import Path
from unittest import mock

from fakes import login_cookies, make_plugin, own_login, sign_in, vaulted

import flnd
from flnd.engine import auth, store
from flnd.errors import LoginError, PluginError, UsageError
from flnd.ui.log import ui
from flnkit import cookies, net

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

    def test_plaintext_is_sealed_on_first_open(self):
        self.home.joinpath("metadata").mkdir(parents=True)
        with raw(self.home) as db:
            db.execute("CREATE TABLE secrets (slot TEXT NOT NULL, name TEXT NOT NULL,"
                       " value TEXT NOT NULL, PRIMARY KEY (slot, name)) WITHOUT ROWID")
            db.executemany("INSERT INTO secrets VALUES (?, ?, ?)",
                           [("tlg", "session", "plain-session"), ("ins", "cookies", "[]")])
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


if __name__ == "__main__":
    unittest.main()

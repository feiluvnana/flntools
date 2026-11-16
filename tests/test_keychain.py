"""The vault's key in the OS keychain (`flnd._vault.Keychain`), and `[vault] key` in a home.

The keychain tools are stand-ins: small Python programs named `security` and `secret-tool`,
written in a temporary folder, that keep their items in a JSON file beside them and log every
argument list they get. `flnd._vault.SECURITY` is pointed at the stand-in (macOS), or the folder
put first on PATH (Linux), so the real keychain is never read or changed; under the test suite,
flnd refuses the real tools anyway (`test_the_real_tool_is_refused_under_the_tests`).
"""

import base64
import json
import os
import sys
import tempfile
import textwrap
import unittest
from pathlib import Path
from unittest import mock

import no_profiles  # noqa: F401  (refuses real browser profiles and the real keychain)

from flnd import _home, _vault
from flnd.errors import ToolError, UsageError

# The stand-ins share one body: `security` reads `-i` commands on standard input, and
# `secret-tool` reads the secret to store there, as the real ones do.
TOOL = textwrap.dedent('''\
    import json, sys
    from pathlib import Path

    here = Path(__file__).resolve().parent
    store, log = here / "items.json", here / "argv.log"
    items = json.loads(store.read_text()) if store.exists() else {}
    with log.open("a") as file:
        file.write(json.dumps(sys.argv[1:]) + "\\n")
    if (here / "broken").exists():
        sys.exit("keychain locked")

    def option(words, flag):
        return words[words.index(flag) + 1]

    def attribute(words, name):
        return words[words.index(name) + 1]

    def save():
        store.write_text(json.dumps(items))

    words = sys.argv[1:]
    if Path(sys.argv[0]).name == "security":
        if words == ["-i"]:
            for line in sys.stdin.read().splitlines():
                parts = line.split()
                if parts and parts[0] == "add-generic-password" and not (here / "forgetful").exists():
                    items[option(parts, "-s") + "/" + option(parts, "-a")] = option(parts, "-w")
            save()
            sys.exit(0)
        name = option(words, "-s") + "/" + option(words, "-a")
        if name not in items:
            sys.exit(print("The specified item could not be found in the keychain.",
                           file=sys.stderr) or 44)
        if words[0] == "find-generic-password":
            print(items[name])
        elif words[0] == "delete-generic-password":
            del items[name]
            save()
    else:
        name = attribute(words, "service") + "/" + attribute(words, "account")
        if words[0] == "store":
            items[name] = sys.stdin.read()
            save()
        elif words[0] == "lookup":
            if name not in items:
                sys.exit(1)
            sys.stdout.write(items[name])
        elif words[0] == "clear":
            items.pop(name, None)
            save()
''')


class KeychainCase(unittest.TestCase):
    """A stand-in keychain tool for `platform`, in a temporary folder."""

    platform = "darwin"

    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.tools = Path(folder.name) / "bin"
        self.tools.mkdir()
        for name in ("security", "secret-tool"):
            path = self.tools / name
            path.write_text(f"#!{sys.executable}\n{TOOL}")
            path.chmod(0o755)
        self.home = Path(folder.name) / ".flnd"
        self.enterContext(mock.patch.object(_vault, "_PLATFORM", self.platform))
        self.enterContext(mock.patch.object(_vault, "SECURITY", str(self.tools / "security")))
        self.enterContext(mock.patch.dict(os.environ, {
            "PATH": f"{self.tools}{os.pathsep}{os.environ.get('PATH', '')}"}))
        self.chain = _vault.Keychain("home-example")

    def items(self):
        store = self.tools / "items.json"
        return json.loads(store.read_text()) if store.exists() else {}

    def logged(self):
        log = self.tools / "argv.log"
        return log.read_text() if log.exists() else ""

    def configure(self, where):
        self.home.mkdir(exist_ok=True)
        (self.home / "config.toml").write_text(f'[vault]\nkey = "{where}"\n')


@unittest.skipIf(sys.platform == "win32", "the stand-ins are POSIX scripts")
class MacTest(KeychainCase):
    def test_a_key_is_made_once_and_never_on_a_command_line(self):
        made = self.chain.key()
        self.assertEqual(len(made), _vault.KEY_SIZE)
        self.assertEqual(self.chain.key(), made)
        self.assertEqual(self.items(), {"flnd/home-example": base64.b64encode(made).decode()})
        self.assertNotIn(base64.b64encode(made).decode(), self.logged())

    def test_read_is_none_without_an_item_and_delete_is_quiet(self):
        self.assertIsNone(self.chain.read())
        self.chain.delete()

    def test_write_replaces_and_reads_back(self):
        first, second = bytes(range(32)), bytes(range(1, 33))
        self.chain.write(first)
        self.chain.write(second)
        self.assertEqual(self.chain.read(), second)

    def test_a_keychain_that_forgets_is_caught_by_the_read_back(self):
        (self.tools / "forgetful").touch()
        with self.assertRaises(ToolError) as caught:
            self.chain.write(bytes(range(32)))
        self.assertIn("didn't keep the key", str(caught.exception))

    def test_a_refusal_shows_the_tools_complaint_never_its_output(self):
        self.chain.write(bytes(range(32)))
        (self.tools / "broken").touch()
        with self.assertRaises(ToolError) as caught:
            self.chain.read()
        self.assertIn("keychain locked", str(caught.exception))
        self.assertNotIn(base64.b64encode(bytes(range(32))).decode(), str(caught.exception))

    def test_an_item_that_isnt_a_key_is_refused(self):
        (self.tools / "items.json").write_text(json.dumps({"flnd/home-example": "short"}))
        with self.assertRaisesRegex(Exception, "is not a key"):
            self.chain.read()

    def test_the_vault_seals_under_the_keychains_key(self):
        with _vault.Vault(self.home / "secrets.db", self.chain, "tlg") as saved:
            saved["session"] = "example-session"
        self.assertFalse((self.home / "secret.key").exists())
        with _vault.Vault(self.home / "secrets.db", self.chain, "tlg") as saved:
            self.assertEqual(saved["session"], "example-session")


@unittest.skipIf(sys.platform == "win32", "the stand-ins are POSIX scripts")
class LinuxTest(KeychainCase):
    platform = "linux"

    def test_secret_tool_keeps_the_key(self):
        made = self.chain.key()
        self.assertEqual(self.chain.read(), made)
        self.assertNotIn(base64.b64encode(made).decode(), self.logged())
        self.chain.delete()
        self.assertIsNone(self.chain.read())


class NoKeychainTest(KeychainCase):
    platform = "win32"

    def test_a_system_without_one_is_a_usage_error(self):
        self.assertFalse(_vault.Keychain.available())
        with self.assertRaises(UsageError) as caught:
            self.chain.key()
        self.assertIn("the OS keychain isn't available here", str(caught.exception))

    def test_a_home_that_asks_for_one_says_what_to_set(self):
        self.configure("keychain")
        with self.assertRaises(UsageError) as caught:
            _home.secrets(self.home, "tlg")
        self.assertIn('key = "file" under [vault]', caught.exception.hint)


class GuardTest(unittest.TestCase):
    def test_the_real_tool_is_refused_under_the_tests(self):
        self.assertTrue(os.environ.get(_vault.NO_REAL_KEYCHAIN))
        with mock.patch.object(_vault, "_PLATFORM", "darwin"), \
                mock.patch.object(_vault.subprocess, "run") as run:
            self.assertIsNone(_vault.Keychain.tool())
            with self.assertRaises(UsageError):
                _vault.Keychain("home-example").read()
        run.assert_not_called()


@unittest.skipIf(sys.platform == "win32", "the stand-ins are POSIX scripts")
class HomeTest(KeychainCase):
    """`[vault] key` in a home's config.toml, and the one move between file and keychain."""

    def seal(self):
        with _home.secrets(self.home, "tlg") as saved:
            saved["session"] = "example-session"

    def opened(self):
        with _home.secrets(self.home, "tlg") as saved:
            return saved["session"]

    def account(self):
        return f"flnd/{_home.keychain_account(self.home)}"

    def test_the_default_is_the_key_file_and_the_keychain_is_never_asked(self):
        self.seal()
        self.assertTrue((self.home / "secret.key").is_file())
        self.assertEqual(self.logged(), "")

    def test_moving_to_the_keychain_and_back_keeps_every_secret(self):
        self.seal()
        key = (self.home / "secret.key").read_bytes()
        self.configure("keychain")
        self.assertEqual(self.opened(), "example-session")
        self.assertFalse((self.home / "secret.key").exists())
        self.assertEqual(self.items(), {self.account(): base64.b64encode(key).decode()})
        self.configure("file")
        self.assertEqual(self.opened(), "example-session")
        self.assertEqual((self.home / "secret.key").read_bytes(), key)
        self.assertEqual(self.items(), {})

    def test_a_new_home_on_the_keychain_never_makes_a_key_file(self):
        self.configure("keychain")
        self.seal()
        self.assertEqual(self.opened(), "example-session")
        self.assertFalse((self.home / "secret.key").exists())
        self.assertIn(self.account(), self.items())

    def test_the_file_stays_when_the_keychain_doesnt_keep_the_key(self):
        self.seal()
        self.configure("keychain")
        (self.tools / "forgetful").touch()
        with self.assertRaises(ToolError):
            self.opened()
        self.assertTrue((self.home / "secret.key").is_file())
        (self.tools / "forgetful").unlink()
        self.configure("file")
        self.assertEqual(self.opened(), "example-session")

    def test_a_bad_vault_setting_names_the_values(self):
        self.configure("cloud")
        with self.assertRaises(UsageError) as caught:
            _home.secrets(self.home, "tlg")
        self.assertIn('[vault] key is "file" or "keychain"', str(caught.exception))


if __name__ == "__main__":
    unittest.main()

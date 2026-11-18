"""`flnkit.files`: an archive extracts beside itself, safely, or not at all."""

from __future__ import annotations

import io
import shutil
import tarfile
import tempfile
import tracemalloc
import unittest
import zipfile
from pathlib import Path

from flnd.errors import SiteError
from flnkit import files as _tools
from flnkit import files as _unpack

HAS_7Z = bool(shutil.which("7zz") or shutil.which("7z"))
SEVEN_ZIP = b"7z\xbc\xaf\x27\x1c\x00\x04"
RAR = b"Rar!\x1a\x07\x00"
BIG = 16 * 1024 * 1024  # 16 of `files.CHUNK`
# A zip encrypted with the old ZipCrypto, password "pw", holding `a.txt` = b"secret".
# Python can't write one, so it was made once with `zip -P pw` and kept here.
CRYPTO_ZIP = bytes.fromhex(
    "504b03040a00090000000a7c705de5e8a25c120000000600000005000000612e747874587609"
    "b3ac5f61a71338bd3fada9df1e2db3504b0708e5e8a25c1200000006000000504b01021e030a"
    "00090000000a7c705de5e8a25c1200000006000000050000000000000001000000a481000000"
    "00612e747874504b0506000000000100010033000000450000000000"
)


class FakeTool:
    """A `_tools.fake` handler for 7zz: it answers `stdout`, writes `files`, records the calls."""

    def __init__(self, stdout: str = "", files: dict[str, bytes] | None = None,
                 code: int = 0, err: str = "") -> None:
        self.stdout, self.files, self.code, self.err = stdout, files or {}, code, err
        self.calls: list[tuple[str, ...]] = []

    def __call__(self, argv: tuple[str, ...]) -> tuple[int, str, str]:
        args = argv[1:]
        self.calls.append(args)
        if args[0] == "x" and self.code == 0:
            out = Path(next(arg for arg in args if arg.startswith("-o"))[2:])
            for name, data in self.files.items():
                (out / name).parent.mkdir(parents=True, exist_ok=True)
                (out / name).write_bytes(data)
        return self.code, self.stdout, self.err


def listing(path: Path, entries: dict[str, int]) -> str:
    """What `7zz l -slt` prints for an archive holding `entries` (name to size)."""
    blocks = [f"Path = {path}\nType = 7z\n"]
    blocks += [f"Path = {name}\nSize = {size}\nAttributes = A_ -rw-r--r--\n"
               for name, size in entries.items()]
    return "\n".join(blocks) + "\n"


def peak(work) -> int:
    """The most memory Python held while `work()` ran."""
    tracemalloc.start()
    try:
        work()
        return tracemalloc.get_traced_memory()[1]
    finally:
        tracemalloc.stop()


class ArchiveCase(unittest.TestCase):
    def setUp(self) -> None:
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory()))

    def raw(self, name: str, data: bytes) -> Path:
        path = self.root / name
        path.write_bytes(data)
        return path

    def zip_with(self, entries: dict[str, bytes], name: str = "made.zip") -> Path:
        path = self.root / name
        with zipfile.ZipFile(path, "w", compression=zipfile.ZIP_DEFLATED) as zf:
            for member, data in entries.items():
                zf.writestr(member, data)
        return path

    def tar_with(self, entries: dict[str, bytes], *, name: str = "made.tar",
                 symlink: bool = False) -> Path:
        path = self.root / name
        mode = "w:gz" if name.endswith(".tar.gz") else "w"
        with tarfile.open(path, mode) as tf:
            for member, data in entries.items():
                info = tarfile.TarInfo(member)
                info.size = len(data)
                tf.addfile(info, io.BytesIO(data))
            if symlink:
                link = tarfile.TarInfo("link")
                link.type = tarfile.SYMTYPE
                link.linkname = "a.txt"
                tf.addfile(link)
        return path

    def assert_nothing_extracted(self, archive: Path) -> None:
        """The archive is untouched, and neither its folder nor a stage was left."""
        self.assertTrue(archive.is_file())
        left = sorted(path.name for path in self.root.iterdir() if path != archive)
        self.assertEqual([name for name in left if not name.startswith("made.")
                          or name.endswith(".unpack")], [])
        self.assertFalse(_unpack.folder(archive).exists())


class ExtractTest(ArchiveCase):
    def test_a_zip_or_tar_extracts_beside_itself_into_a_folder_of_its_name(self) -> None:
        archive = self.zip_with({"a.txt": b"alpha\n", "nested/b.bin": b"\x00\x01"},
                                name="post.zip")
        folder = _unpack.extract(archive)
        self.assertEqual(folder, self.root / "post")
        self.assertEqual((folder / "a.txt").read_bytes(), b"alpha\n")
        self.assertEqual((folder / "nested" / "b.bin").read_bytes(), b"\x00\x01")
        self.assertEqual(sorted(p.name for p in self.root.iterdir()), ["post", "post.zip"])
        for name in ("set.tar", "set.tar.gz"):
            with self.subTest(name=name):
                folder = _unpack.extract(self.tar_with({"x/y.txt": b"y"}, name=name))
                self.assertEqual(folder.name, "set")
                self.assertEqual((folder / "x" / "y.txt").read_bytes(), b"y")
                shutil.rmtree(folder)
        for path, expected in (("a/b.tar.gz", "a/b"), ("a/loi-5930.7z", "a/loi-5930"),
                               ("a/.zip", "a/.zip_files")):
            self.assertEqual(_unpack.folder(Path(path)), Path(expected))
        self.assertTrue(_unpack.is_archive(Path("A.ZIP")))
        self.assertFalse(_unpack.is_archive(Path("a.jpg")))

    def test_again_replaces_its_files_keeps_the_rest_and_restarts_a_crashed_stage(self) -> None:
        archive = self.zip_with({"a.txt": b"new"}, name="post.zip")
        (self.root / "post").mkdir()
        (self.root / "post" / "a.txt").write_bytes(b"old")
        (self.root / "post" / "mine.txt").write_bytes(b"mine")
        (self.root / ".post.unpack").mkdir()
        (self.root / ".post.unpack" / "half.txt").write_bytes(b"half")
        folder = _unpack.extract(archive)
        self.assertEqual(sorted(p.name for p in folder.iterdir()), ["a.txt", "mine.txt"])
        self.assertEqual((folder / "a.txt").read_bytes(), b"new")
        self.assertEqual((folder / "mine.txt").read_bytes(), b"mine")
        self.assertFalse((self.root / ".post.unpack").exists())

    def test_nested_archives_are_not_unpacked(self) -> None:
        inner = self.zip_with({"inner.txt": b"inner"}, name="inner.zip").read_bytes()
        folder = _unpack.extract(self.zip_with({"nested.zip": inner}))
        self.assertTrue((folder / "nested.zip").is_file())
        self.assertFalse((folder / "inner.txt").exists())

    def test_a_zipcrypto_zip_takes_its_password_from_python_or_7zip(self) -> None:
        archive = self.raw("made.zip", CRYPTO_ZIP)
        with _tools.fake({}):  # no 7-Zip: Python's zipfile reads ZipCrypto itself
            with self.assertRaisesRegex(SiteError, "wrong or missing password"):
                _unpack.extract(archive, password="nope")
            self.assert_nothing_extracted(archive)
            folder = _unpack.extract(archive, password="pw")
        self.assertEqual((folder / "a.txt").read_bytes(), b"secret")
        shutil.rmtree(folder)
        tool = FakeTool(files={"a.txt": b"secret"})
        with _tools.fake({"7zz": tool}):
            folder = _unpack.extract(archive, password="pw")
        self.assertEqual((folder / "a.txt").read_bytes(), b"secret")
        self.assertEqual(tool.calls[-1][:3], ("x", "-y", "-p"))
        self.assertNotIn("pw", tool.calls[-1])  # given on standard input, never in argv

    def test_a_7z_or_rar_goes_to_7zip_and_is_verified(self) -> None:
        for name, data in (("made.7z", SEVEN_ZIP), ("made.rar", RAR)):
            with self.subTest(name=name):
                archive = self.raw(name, data)
                tool = FakeTool(listing(archive, {"a.txt": 1}), files={"a.txt": b"a"})
                with _tools.fake({"7zz": tool}):
                    self.assertEqual((_unpack.extract(archive) / "a.txt").read_bytes(), b"a")
        archive = self.root / "made.7z"
        short = FakeTool(listing(archive, {"a.txt": 5}), files={"a.txt": b"a"})
        with _tools.fake({"7zz": short}), self.assertRaisesRegex(SiteError, "1 bytes, not 5"):
            _unpack.extract(archive)
        shutil.rmtree(self.root / "made")
        wrong = FakeTool(code=2, err="ERROR: Wrong password : a.txt")
        with _tools.fake({"7zz": wrong}), self.assertRaisesRegex(SiteError, "wrong or missing"):
            _unpack.extract(archive, password="nope")
        self.assert_nothing_extracted(archive)
        with _tools.fake({}), self.assertRaises(_tools.MissingError) as caught:
            _unpack.extract(archive)
        self.assertIn("7zz", caught.exception.hint or "")

    @unittest.skipUnless(HAS_7Z, "needs 7zz or 7z")
    def test_a_real_7z_with_a_password(self) -> None:
        source = self.root / "src"
        source.mkdir()
        (source / "a.txt").write_bytes(b"alpha")
        archive = self.root / "made.7z"
        _tools.sevenzip().run("a", "-y", "-ppw", "-mhe=on", "--", archive, source / "a.txt")
        with self.assertRaises(SiteError):
            _unpack.extract(archive, password="nope")
        self.assertEqual((_unpack.extract(archive, password="pw") / "a.txt").read_bytes(),
                         b"alpha")


class SafetyTest(ArchiveCase):
    def assert_refused(self, archive: Path, **options) -> None:
        with self.assertRaises(SiteError):
            _unpack.extract(archive, **options)
        self.assertFalse((self.root / "evil").exists())
        self.assert_nothing_extracted(archive)

    def test_traversal_reserved_names_and_links_are_refused(self) -> None:
        for name in ("../evil", "a/../../evil", "sub/../evil", "/abs/evil", "C:/evil", "..\\evil",
                     "CON/data.txt"):
            with self.subTest(name=name):
                self.assert_refused(self.zip_with({"ok.txt": b"ok", name: b"x"}))
        with self.assertRaisesRegex(SiteError, "unsafe archive path"):
            _unpack.extract(self.zip_with({"CON/data.txt": b"x"}))
        self.assert_refused(self.tar_with({"a.txt": b"a"}, symlink=True))
        self.assert_refused(self.tar_with({"a.txt": b""}, symlink=True, name="made2.tar"))

    def test_bombs_are_refused_before_anything_is_written(self) -> None:
        archive = self.zip_with({"a.txt": b"aaaa", "b.txt": b"bbbb"})
        self.assert_refused(archive, limits=_unpack.Limits(files=1))
        self.assert_refused(archive, limits=_unpack.Limits(size=3))
        bomb = self.zip_with({"zeros.bin": bytes(BIG // 8)}, name="made-bomb.zip")
        self.assert_refused(bomb)

    def test_links_listed_by_7zip_are_refused(self) -> None:
        for name, raw in (("made.7z", SEVEN_ZIP), ("made.rar", RAR)):
            archive = self.raw(name, raw)
            for attributes in ("A_ lrwxrwxrwx", "AL"):
                with self.subTest(name=name, attributes=attributes):
                    tool = FakeTool(f"Path = {archive}\n\nPath = link\n"
                                    f"Attributes = {attributes}\n")
                    with _tools.fake({"7zz": tool}):
                        self.assert_refused(archive)
                    self.assertEqual([call[0] for call in tool.calls], ["l"])

    def test_a_path_like_a_switch_reaches_7zip_as_a_path(self) -> None:
        archive = self.raw("-sdel.7z", SEVEN_ZIP)
        tool = FakeTool(listing(archive, {}))
        with _tools.fake({"7zz": tool}):
            _unpack.extract(archive)
        for call in tool.calls:
            with self.subTest(call=call):
                self.assertLess(call.index("--"), call.index(str(archive)))

    def test_a_big_entry_is_streamed_not_held_in_memory(self) -> None:
        loose = _unpack.Limits(ratio=1e9)
        zipped = self.zip_with({"big.bin": bytes(BIG)}, name="big.zip")
        self.assertLess(peak(lambda: _unpack.extract(zipped, limits=loose)), BIG // 4)
        self.assertEqual((self.root / "big" / "big.bin").stat().st_size, BIG)
        tarred = self.tar_with({"big.bin": bytes(BIG)}, name="tbig.tar")
        self.assertLess(peak(lambda: _unpack.extract(tarred, limits=loose)), BIG // 4)


class ErrorTest(ArchiveCase):
    def test_a_missing_corrupt_or_damaged_archive_is_the_sites_and_leaves_nothing(self) -> None:
        with self.assertRaises(SiteError):
            _unpack.extract(self.root / "nope.zip")
        bad = self.raw("made.zip", b"not an archive")
        with self.assertRaises(SiteError):
            _unpack.extract(bad)
        bad.write_bytes(b"PK\x03\x04 not really a zip")
        with self.assertRaises(SiteError) as caught:
            _unpack.extract(bad)
        self.assertFalse(caught.exception.retryable)
        self.assert_nothing_extracted(bad)
        archive = self.zip_with({"a.txt": b"alpha" * 1000, "b.txt": b"beta" * 1000})
        raw = bytearray(archive.read_bytes())
        # Zero bytes in the second entry's compressed data: its CRC check fails mid-extract.
        at = raw.find(b"b.txt") + 10
        raw[at:at + 20] = bytes(20)
        archive.write_bytes(bytes(raw))
        with self.assertRaises(SiteError):
            _unpack.extract(archive)
        self.assert_nothing_extracted(archive)


if __name__ == "__main__":
    unittest.main()

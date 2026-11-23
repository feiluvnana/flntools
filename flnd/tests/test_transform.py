"""`flnd transform`: a command run over saved files, each replaced in place on success.

Homes are made by a real run on a faked network (`test_chain.ChainCase`). The "tool" is a tiny
Python script run through `sys.executable`; nothing else runs.
"""

from __future__ import annotations

import io
import json
import shlex
import sqlite3
import sys
from contextlib import redirect_stderr, redirect_stdout
from unittest import mock

from fakes import CDN, Engine, make_plugin
from test_chain import JPEG, ChainCase

import flnd
from flnd import cli
from flnd.engine import run as _run
from flnd.engine import store as _store
from flnd.engine import transform
from flnd.engine.events import Saved, State
from flnd.errors import DiskError, Stopped, UsageError
from flnd.plugin import File
from flnkit import files as _files
from flnkit import net as _net

MP4 = b"\0\0\0\x18ftypisom" + b"v" * 8

# The tool: `tool.py <mode> <in> <out>` writes `T:` and the input's bytes to the output, or
# misbehaves as its mode says.
TOOL = '''
import sys
mode, source, out = sys.argv[1:4]
data = open(source, "rb").read()
if mode == "none":
    sys.exit(0)
if mode == "empty":
    open(out, "wb").close()
else:
    open(out, "wb").write((b"TT:" if mode == "twice" else b"T:") + data)
if mode == "fail":
    print("first line", file=sys.stderr)
    print("it broke", file=sys.stderr)
    sys.exit(3)
'''


class TransformCase(ChainCase):
    def setUp(self):
        super().setUp()
        self.routes[f"{CDN}/p.jpg"] = _net.reply(200, content=JPEG)
        self.routes[f"{CDN}/v.mp4"] = _net.reply(200, content=MP4)

        def both(ctx, context):
            user = ctx.target.value
            return [File(f"{CDN}/p.jpg", name=f"{user}-p.jpg"),
                    File(f"{CDN}/v.mp4", name=f"{user}-v.mp4")]
        self.source = make_plugin("aaa", files=both)
        self.config('[defaults]\ndedupe = "off"\n')
        self.engine().download(["aaa:a|all"])
        self.tool = self.home.parent / "tool.py"
        self.tool.write_text(TOOL, encoding="utf-8")
        self.photo = self.target_dir("aaa", "a") / "a-p.jpg"
        self.video = self.target_dir("aaa", "a") / "a-v.mp4"
        self.events.clear()

    def command(self, mode="ok"):
        return shlex.join([sys.executable, str(self.tool), mode, "{in}", "{out}"])

    def run_transform(self, mode="ok", *, on=("image", "video"), **options):
        self.events.clear()
        self.engine().transform(self.command(mode), on=list(on), parallel=2, **options)
        return [event for event in self.events if not isinstance(event, State)]

    def kinds(self, events):
        return sorted(type(event).__name__ for event in events)

    def transforms(self):
        with _store.open(self.home) as db:
            return db.transforms()

    def leftovers(self):
        return [path.name for path in self.photo.parent.iterdir() if "flnd" in path.name]

    def checked(self):
        with _store.open(self.home) as db:
            found = _store.check(self.home, db)
        return found.missing, found.resized, found.unknown


class ReplaceTest(TransformCase):
    def test_a_success_replaces_the_file_whole_and_check_accepts_it(self):
        events = self.run_transform()
        self.assertEqual(self.kinds(events), ["Saved", "Saved"])
        self.assertEqual(self.photo.read_bytes(), b"T:" + JPEG)
        self.assertEqual(self.video.read_bytes(), b"T:" + MP4)
        self.assertEqual(self.leftovers(), [])
        with _store.open(self.home) as db:
            rows = {row.key: row for row in db.on_disk()}
        row = rows["aaa:a-p.jpg"]
        self.assertEqual((row.size, row.hash, row.mtime), (len(JPEG) + 2,
                         _files.digest(self.photo), self.photo.stat().st_mtime_ns))
        self.assertEqual(self.checked(), ((), (), ()))
        sizes = {event.path.name: event.size for event in events if isinstance(event, Saved)}
        self.assertEqual(sizes, {"a-p.jpg": len(JPEG) + 2, "a-v.mp4": len(MP4) + 2})

    def test_the_modified_time_of_the_original_is_kept(self):
        before = self.photo.stat().st_mtime_ns
        self.run_transform()
        self.assertEqual(self.photo.stat().st_mtime_ns, before)

    def test_a_non_zero_exit_leaves_the_file_and_fails(self):
        events = self.run_transform("fail")
        self.assertEqual(self.kinds(events), ["Failed", "Failed"])
        self.assertIn("exited 3", str(events[0].error))
        self.assertIn("it broke", str(events[0].error))
        self.assertEqual(self.photo.read_bytes(), JPEG)
        self.assertEqual(self.leftovers(), [])
        [row] = self.transforms()
        self.assertEqual((row.done, row.failed), (0, 2))

    def test_an_empty_or_missing_output_leaves_the_file(self):
        for mode in ("empty", "none"):
            with self.subTest(mode):
                events = self.run_transform(mode)
                self.assertEqual(self.kinds(events), ["Failed", "Failed"])
                self.assertEqual(self.photo.read_bytes(), JPEG)
                self.assertEqual(self.leftovers(), [])

    def test_a_crash_before_the_replace_leaves_the_file_and_no_temporary(self):
        with mock.patch("flnd.engine.transform.files.swap", side_effect=OSError("boom")):
            events = self.run_transform()
        self.assertEqual(self.kinds(events), ["Failed", "Failed"])
        self.assertEqual(self.photo.read_bytes(), JPEG)
        self.assertEqual(self.leftovers(), [])
        # Its record says it never finished: a rerun runs the command again.
        events = self.run_transform()
        self.assertEqual(self.kinds(events), ["Saved", "Saved"])

    def test_a_crash_after_the_replace_is_finished_by_the_rerun(self):
        with mock.patch.object(_store.Store, "transform_done", side_effect=OSError("died")), \
                self.assertRaises(OSError):
            self.run_transform()
        self.assertEqual(self.photo.read_bytes(), b"T:" + JPEG)
        events = self.run_transform()
        self.assertEqual(self.kinds(events), ["Skipped", "Skipped"])
        self.assertEqual(self.photo.read_bytes(), b"T:" + JPEG)
        self.assertEqual(self.checked(), ((), (), ()))

    def test_a_file_name_is_never_given_to_a_shell(self):
        odd = self.photo.with_name("a b'; touch PWNED; $(touch PWNED2).jpg")
        self.photo.rename(odd)
        with _store.open(self.home) as db:
            db.update("aaa:a-p.jpg", path=str(odd))
        self.run_transform(on=("image",))
        self.assertEqual(odd.read_bytes(), b"T:" + JPEG)
        self.assertFalse(any(self.home.parent.rglob("PWNED*")))

    def test_an_extracted_file_is_transformed_and_stays_known(self):
        made = self.photo.with_name("made.png")
        made.write_bytes(b"\x89PNG\r\n\x1a\n" + b"x" * 8)
        with _store.open(self.home) as db:
            db.update("aaa:a-p.jpg", unpacked="extracted", extracted=json.dumps([str(made)]))
        self.run_transform(on=("image",))
        self.assertTrue(made.read_bytes().startswith(b"T:"))
        self.assertEqual(self.checked()[2], ())


class SkipTest(TransformCase):
    def test_a_rerun_skips_what_it_made_and_a_new_command_runs_again(self):
        self.run_transform()
        events = self.run_transform()
        self.assertEqual(self.kinds(events), ["Skipped", "Skipped"])
        self.assertEqual(self.photo.read_bytes(), b"T:" + JPEG)
        events = self.run_transform("twice")
        self.assertEqual(self.kinds(events), ["Saved", "Saved"])
        self.assertEqual(self.photo.read_bytes(), b"TT:T:" + JPEG)
        self.assertEqual(len(self.transforms()), 2)

    def test_a_run_cut_short_resumes_with_the_files_left(self):
        real, calls = transform._swap, []

        def once(made, path):
            calls.append(path)
            if len(calls) == 2:
                raise DiskError("power cut")
            real(made, path)
        with mock.patch("flnd.engine.transform._swap", once):
            events = self.run_transform()
        self.assertEqual(self.kinds(events), ["Failed", "Saved"])
        events = self.run_transform()
        self.assertEqual(self.kinds(events), ["Saved", "Skipped"])
        [row] = self.transforms()
        self.assertEqual((row.done, row.skipped, row.failed), (2, 1, 0))

    def test_a_second_ctrl_c_kills_the_program_and_keeps_the_file(self):
        sleeper = self.home.parent / "sleep.py"
        sleeper.write_text("import time\ntime.sleep(60)\n", encoding="utf-8")
        halt = _run.Stop()
        halt.now.set()
        engine = Engine(home=self.home, sources={"aaa": self.source}, emit=self.events.append,
                        stop=halt)
        command = shlex.join([sys.executable, str(sleeper), "{in}", "{out}"])
        with self.assertRaises(Stopped):
            engine.transform(command, on=["image"], parallel=1)
        self.assertEqual(self.photo.read_bytes(), JPEG)
        self.assertEqual(self.leftovers(), [])


class SelectTest(TransformCase):
    def test_on_picks_the_kinds(self):
        events = self.run_transform(on=("video",))
        self.assertEqual([event.path for event in events], [self.video])
        self.assertEqual(self.photo.read_bytes(), JPEG)

    def test_targets_narrow_it(self):
        self.engine().download(["aaa:b|all"])
        other = self.target_dir("aaa", "b") / "b-p.jpg"
        self.run_transform(targets=["aaa:b"], on=("image",))
        self.assertEqual(other.read_bytes(), b"T:" + JPEG)
        self.assertEqual(self.photo.read_bytes(), JPEG)

    def test_dry_plans_and_changes_nothing(self):
        events = self.run_transform(dry=True)
        self.assertEqual(self.kinds(events), ["Planned", "Planned"])
        self.assertEqual({event.state for event in events}, {"new"})
        self.assertEqual(self.photo.read_bytes(), JPEG)
        self.assertEqual(self.transforms(), [])
        self.run_transform()
        events = self.run_transform(dry=True)
        self.assertEqual({event.state for event in events}, {"saved"})


class CommandTest(TransformCase):
    def test_a_command_needs_in_and_out_and_a_program(self):
        for text in ("", "tool {out}", "tool {in}", "echo 'unclosed {in} {out}",
                     "no-such-program-here {in} {out}"):
            with self.subTest(text), self.assertRaises(UsageError):
                transform.split(text)
        self.assertEqual(transform.split(f"{sys.executable} -x a{{in}}b {{out}}")[-2:],
                         ["a{in}b", "{out}"])

    def test_the_library_checks_its_arguments_before_a_run(self):
        with flnd.open(self.home) as fl:
            for options in ({"on": []}, {"on": ["text"]}, {"on": ["image"], "jobs": 0}):
                with self.subTest(options), self.assertRaises(UsageError):
                    fl.transform(self.command(), **options)
            with self.assertRaises(UsageError):
                fl.transform("tool {in}", on=["image"])

    def test_the_library_runs_and_lists_it(self):
        with flnd.open(self.home) as fl:
            report = fl.transform(self.command(), on=["image"]).wait()
            self.assertEqual((report.saved, report.failed), (1, ()))
            [row] = fl.transforms()
        self.assertEqual((row.command, row.done, row.skipped, row.failed),
                         (self.command(), 1, 0, 0))
        self.assertTrue(row.first and row.last)


class CliTest(TransformCase):
    def main(self, *args):
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            code = cli.main(["--home", str(self.home), *args])
        return code, out.getvalue(), err.getvalue()

    def test_transform_runs_lists_and_reports_failures(self):
        code, out, _ = self.main("transform", self.command(), "--on=image", "--jobs", "1")
        self.assertEqual((code, self.photo.read_bytes()), (0, b"T:" + JPEG))
        self.assertIn("1 replaced", out)
        self.assertEqual(self.main("transform", self.command("fail"), "--on", "video")[0], 1)
        _, out, _ = self.main("transform", "list")
        self.assertIn("done", out)
        _, out, _ = self.main("transform", "list", "--json")
        rows = [json.loads(line) for line in out.splitlines()]
        self.assertEqual([(row["done"], row["failed"]) for row in rows], [(1, 0), (0, 1)])

    def test_dry_and_a_missing_on_and_no_placeholder(self):
        code, out, _ = self.main("transform", self.command(), "--on=video", "--dry")
        self.assertEqual(code, 0)
        self.assertIn("planned 1 file", out)
        self.assertEqual(self.video.read_bytes(), MP4)
        self.assertEqual(self.main("transform", self.command())[0], 2)
        self.assertEqual(self.main("transform", "echo {in}", "--on=image")[0], 2)

    def test_help_says_there_is_no_undo(self):
        _, out, _ = self.main("transform", "-h")
        self.assertIn("NO UNDO", " ".join(out.split()))
        self.assertNotIn("--exec", self.main("get", "-h")[1])


class MigrationTest(TransformCase):
    def test_an_old_home_without_the_tables_opens_and_gets_them(self):
        with sqlite3.connect(self.home / _store.DB) as sql:
            sql.execute("DROP TABLE transforms")
            sql.execute("DROP TABLE transform_files")
        sql.close()
        with _store.open(self.home) as db:
            self.assertEqual(db.transforms(), [])
            self.assertEqual(len(db.on_disk()), 2)
        self.run_transform()
        self.assertEqual([row.done for row in self.transforms()], [2])

    def test_items_skip_a_missing_file_and_other_kinds(self):
        with _store.open(self.home) as db:
            rows = db.on_disk()
        self.photo.unlink()
        found = transform.items(rows, {}, ["image", "video"], None)
        self.assertEqual([item.path for item in found], [self.video])
        self.assertEqual(transform.items(rows, {}, ["audio"], None), [])

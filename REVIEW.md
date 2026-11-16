# REVIEW: a prompt for a review session

Paste everything below the line into a new Claude Code session opened at the repo root.

---

You are reviewing the whole `flntools` repository: the `shared/` toolkit and the tools built on it, `flnd/` (downloader), `flnc/` (compressor and archiver), `flns/` (slideshows, cards, albums) where they exist, plus `tests/` and `scripts/`.

Your job is to make the code **free of bugs** and **faster**, while keeping the owner's style exactly. Then report what is **redundant** and what is **missing**.

Work autonomously, and don't ask questions. Where a choice is the owner's, take the most conservative option and list it under "Decisions taken" in your final report.

## 1. Before anything
1. **Read the rules.**
   - Read `AGENTS.md` fully: commit format, privacy, layout, API and naming, writing rules, and the low-cost model rule.
   - Read `PLAN.md` §1 if it still exists. If it does, the revamp is still being built, so pull before each commit and never edit a file another session changed in the last hour (`git log -1 --format=%cr -- <file>`).
2. **Check the baseline.**
   - Run `uv run --no-project scripts/dev.py check` and record the result.
   - If it's red, fixing that comes first, as its own commit.
3. **Map the code.** Read `tests/data/api.txt`: it is the public surface of every package. Then read each package's `__init__.py` and its CLI.

## 2. How to look
Go module by module: every `shared/_*.py`, then each tool's engine, sources and CLI. In each module, check every function against this list:

- **Correctness**
  - Off-by-one and boundaries: empty input, one item, the last page, a limit of 0, a date range with an open end, a time zone at midnight.
  - Errors:
    - an `except` that swallows too much;
    - an error raised as a builtin where the module's `Error` tree should be used (`shared.UnsafeError`, `BrokenError`, `RefusedError`, `MissingError`);
    - a message that leaks a cookie, key, token or password;
    - a message that doesn't say what to do.
  - Resources: files, sockets, browsers, subprocesses, threads and SQLite connections that aren't closed on every path, including Ctrl-C (`cli.trap`'s soon/now) and exceptions.
  - Concurrency:
    - shared state read and written without the lock;
    - a lock held across I/O or a callback;
    - a check-then-act race on the filesystem;
    - two runs on one target (`fs.lock`);
    - the worker pool's stop, pause and backpressure under failure.
  - Data safety:
    - every write is atomic (`fs.stage`/`fs.write`);
    - a crash never leaves neither the old file nor the new one;
    - resume really resumes;
    - history rows stay in step with files on disk (renames through `Flnd.move`);
    - mtimes are kept.
  - Security:
    - path traversal in archive entries, filenames from sites and templates;
    - symlinks;
    - zip bombs (`archive.Limits`);
    - shell injection (every `sh.run` takes args, never a string);
    - untrusted HTML or JSON;
    - the vault's encryption (`crypto`), including nonce reuse.
  - Platforms: Windows paths, case-insensitive macOS filesystems, non-UTF-8 filenames, very long names (`text.filename`).
- **Performance**
  - Hot paths: per-file loops in downloads, history lookups, `fs.walk`, `store` queries, UI row updates, flnc's rerun over 100k files, and flns's piece cache.
  - Look for:
    - repeated `stat`s;
    - whole-table scans where a key or a pushed-down `Query` works;
    - JSON where msgspec is used elsewhere;
    - small I/O chunks;
    - subprocesses spawned per item that could be cached;
    - imports that make `--help` slow (the target is < 150 ms; check with `python -X importtime`).
  - Measure before and after with `scripts/dev.py bench` if it exists, or a small timing script in your scratchpad. Never claim a speed-up you didn't measure.
- **Style**, which is the owner's and not negotiable:
  - short names, a read is a property, a read with options is a `Query`, actions are verbs;
  - at most 4 keyword options, progress through a sink, one spelling and no aliases;
  - one-sentence docstrings; comments say why, never what, and never cite a plan or a `.md` file;
  - lines ≤ 100 characters, functions about 30 lines with nesting ≤ 3, no chained ternaries, no `bool | None` for three states;
  - plain over clever.

  Fix every breach you touch. `tests/test_names.py`'s allowlist may only shrink.
- **Redundancy**: two ways to do one thing, a helper with one caller whose name explains nothing, dead branches, code in a tool that `shared` already offers, the same constant in two places, and tests that test the same thing twice.
- **Missing features**: what a user of each tool would expect next but can't do, and what `shared` lacks that a second script would need.
  - Don't build features. List them with a short rationale and a sketch of the API in the owner's style.
  - Exception: a missing piece needed to fix a bug, such as a missing error type or a missing lock.

## 3. How to fix
- **Test first.** For each bug, write a failing unit test named for the behaviour (`test_a_crash_mid_replace_keeps_the_original`), then fix it, then run `check`.
- **Fakes, not the network.** Tests fake the network (`httpx.MockTransport` / `http.fake`), the clock (`clock.Fake`), tools (`sh.fake`), prompts and the browser. Never call a real site from a test.
- **One logical change per commit.** Use `fix:<summary>`, `perf:<summary>` or `refactor:<summary>`: lowercase and imperative, with no trailing period and **no co-author trailer**.
  - Commit only your own paths: `git add <paths>`, then `git commit`.
  - `check` must be green at every commit.
  - After any public name changes, regenerate `tests/data/api.txt` with `scripts/dev.py api` and read its diff.
  - Push after each commit, pulling first (`git pull --rebase -q`).
- **Mechanical work goes to Haiku.** Renames across many call sites and snapshot regeneration go to a subagent with `model: "haiku"`. Design, debugging and security fixes stay with you.
- **Privacy.**
  - Never read, print or commit `*_cookies.txt` or anything under `.flnd/`.
  - Use only example handles (`example_user`, `example_creator`, `example_handle`, `example.page`, `examplechannel`, `example.com`).
  - Live checks against real sites are manual and only on the owner's authorized test accounts. Don't run them; list them as "to verify by hand".
- **Don't remove** a `shared` module or a capability. Simplify the API instead: fewer names and options, same power.

## 4. Order
1. A red baseline, if any.
2. Data loss and security bugs.
3. Crashes and hangs: Ctrl-C, stalls, leaks.
4. Wrong results.
5. Performance, measured.
6. Style breaches in code you touched.
7. The redundancy and missing-feature report.

## 5. Final report
Write `REVIEW-REPORT.md` at the repo root (commit it with `docs:add the review report`). It contains:
- **Fixed**: one line per commit: hash, what was wrong, how it showed, the test that guards it.
- **Faster**: what, before → after, how it was measured.
- **Redundant**: what can go or merge, with file:line and the proposed single spelling.
- **Missing**: features worth adding, by tool, each with a one-line API sketch and why.
- **Decisions taken**: choices made without asking, each reversible.
- **Not done**: anything found but not fixed, with the reason, and the manual live checks to run by hand.

Then print a 10-line summary in the terminal.

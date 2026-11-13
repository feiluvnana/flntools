# shared: a general toolkit, proven robust

Part of [Phase 8](README.md). What `shared` is for after the user's decisions of 2026-11-13, and
how each module is proven to survive what can go wrong. The API changes are in [api](api.md),
the bugs in [bugs](bugs.md), the speed-ups in [speed](speed.md).

## 1. What `shared` is

The user, 2026-11-13: "shared exists because in future, maybe other module need it, not just
flnd. So keep that in my for any design decision." And: "I dont think remove archive,... is ok,
but maybe just fix bug, improve performance and simplify the api of shared."

So:
- **`shared` is a general toolkit.** Its API is designed for any tool that may use it later, not
  shaped around flnd. A name is not judged by how often flnd calls it.
- **No module and no feature is removed**: `archive`, `bundler`, `media`'s editing verbs,
  `worker`'s options, `crypto`'s hashes and the rest stay, each simplified ([api](api.md)).
- **No flnd idea leaks in**: no site, target, category or post. Generic machinery that moves in
  from flnd is made general first ([flnd › machinery](flnd.md#2-generic-machinery-into-shared)).
- **Tests and docs stand alone**: `shared`'s tests import no flnd, and its examples are written for
  a generic caller (`example.com`, `photos/`, `a.jpg`).
- This replaces every "minimal for flnd" reason in the earlier drafts: the deletion tables, the
  "used means flnd or its tests call it" rule, and moving single-user modules into flnd are gone.

What stays from Phase 7: `shared` is a black box (no `shared._` imports outside it), each module
wraps a library and keeps `.raw`, and its errors share `<module>.Error` under `shared.Error`.

## 2. Every error typed

- **The world's errors are `shared` types.** A file, the network, a program or input data that
  fails raises the module's `Error` (or a subclass under `UnsafeError`, `BrokenError`,
  `RefusedError`, `MissingError`), with the builtin chained as its cause. The audit found about
  15 that escape today ([bugs](bugs.md): H2, H3, H9, K1, M1, M4, M6, W1, A1, and more).
- **A caller's mistake may stay a builtin** (`ValueError`, `TypeError` for an argument that can
  never be right): it is a bug to fix, not a case to catch.
- **A static scan enforces it** (added): a test reads every `raise` in `shared/_*.py` with `ast`.
  Each builtin raised must be listed in `tests/data/raises.txt` with its reason (`caller` for a
  mistake), and the list may only shrink, as `test_names`' allowlist does. Today there are 104
  builtin raises in 21 files (55 `ValueError`, 16 `TypeError`, 10 `ImportError`, …); each is either
  listed as a caller's mistake or becomes a `shared` type in its module's commit.
- **Missing extras** raise the module's `MissingError` when a call needs them, not a bare
  `ImportError` (`media`, `archive`, `bundler` today).

## 3. Faults, injected in tests

Each fault is injected at the module's boundary: `os.write`, `os.replace`, `sqlite3`, the httpx
stream, `subprocess`, `shared.clock`. No real disk is filled and no real network is used.

| Module | Faults |
| --- | --- |
| `atomic` | a child process killed at each point of `writing`; ENOSPC at write and at rename; a read-only folder; the part removed mid-write; a Windows-held file on replace; two processes and `lock` |
| `kv` | two processes writing 1,000 keys each to one store; a child killed mid-transaction; `SQLITE_FULL`; a read-only file; the clock jumping across a `ttl`; a key expiring mid-iteration |
| `http` | a drop at connect, at headers and mid-body; a slow-loris server on the fake clock; 429 storms with `Retry-After` 0, huge and missing; a capped range server; an invalid URL; the clock jumping in a pace wait; `stop` during every wait |
| `log`, `theme` | stderr closed mid-run; a `show=` that raises; the width changing mid-frame; control characters and escapes in every text; a record closing while another thread logs; a question open while rows change |
| `media` | ffmpeg missing, killed, or silent while it prints progress; a feeder that fails; ENOSPC on `save`; a file cut at each header byte; odd EXIF and XMP fields; MPO, cover art, ADTS, 16-bit and CMYK images |
| `worker` | a task raising `BaseException` or `TimeoutError`; Ctrl-C while tasks wait; a task submitting to its own pool; a task asking its owner while the owner is stopping |
| `archive` | zip slip, symlinks, a bomb, a file that is also a folder, case twins, a cut split archive, a wrong password |
| `cookies`, `selector` | a locked or truncated browser database; a BOM; an empty expiry; broken queries |
| `browser` | Playwright absent before any browser opens; a page crashing mid-`goto`; a crashed browser at close |
| `clock` | jumps backward and forward under every waiter |

## 4. Hostile input, fuzzed

Property-based tests with Hypothesis (a dev dependency). Each property says what holds for any
input, usually "a value or the module's `Error`, never anything else, never a write outside its
folder".

| Target | Property |
| --- | --- |
| `cookies.load` (`cookies.txt`, JSON) | a `Jar` or `cookies.Error`; no cookie for another domain |
| Retry-After, HTTP dates, rate headers, `Content-Range` | a finite number of seconds within the clamp, or None; a range inside the file or a refusal |
| the part sidecar | a resumable state or a dropped part |
| `selector.parse` and its queries | a `Selection` or `selector.Error` |
| `fmt.filename` | a name safe on macOS, Linux and Windows: no separators, NUL, control or bidi characters, reserved names, trailing dot or space; at most `max_bytes`; never `.` or `..` |
| `fmt.url` | no user info, query or fragment in the result |
| `log`'s field cleaning | no escape but colour, no control character but `\n`, reaches the screen |
| `media.sniff`, `open(...).check()` on random and mutated files | an extension, None, or `media.Error`; no hang past the fake clock's limit |
| `archive` member names | every path inside `dest`, or `UnsafeError` |
| `crypto.decrypt` on mutated blobs | `crypto.Error`, never a wrong plaintext |
| `cli` parsing of any argv | a result or `cli.UsageError` |

`check` runs each property for 200 examples (`derandomize=True`, so it repeats); `dev.py fuzz`
runs 20,000 and keeps failures in `.hypothesis/` (ignored by git). A failure found by `fuzz`
becomes an `@example`.

## 5. Threads

- Each public class is **safe** from many threads or **one thread at a time**, recorded once in
  `tests/data/threads.txt` (added), one line per class; a test fails on a public class missing
  from it. The writing rules keep docstrings to one sentence, so the list is the record.
- **Stress tests** run each safe class from 16 threads for 1 s with a seeded mix of calls, then
  check its invariants: the live area (no lost or doubled line, no busy loop), `kv` (every write
  readable), `worker.Pool` (every result once; owner calls answered), `http.Client` (pace never
  overtaken, back-off doubling under concurrency: [H5](bugs.md#http-download-pace)).

## 6. Resources

No file handle, thread or child process outlives its `with`: a leak check (`tests/leaks.py`,
added) compares the process's open files, threads and children before and after each test of
`browser` (fake), `media`, `kv`, `worker`, `http.Client` and `download`, `log.record`. It catches
the audit's leaks once fixed ([R2](bugs.md#browser-cookies-selector), [F5](bugs.md#flnd),
[F6](bugs.md#flnd), one stop-watcher thread per download in [speed](speed.md#http-and-download)).

## 7. A size budget per module (added)

`tests/data/budget.json` holds each module's code lines (no docstrings, comments or blank lines),
for `shared` and flnd. `check` prints each against its budget: past it is a warning, past it by
20% a failure, until a commit raises the budget and says why. The budget is set from the code as
Phase 8 leaves it, so simplifying is locked in.

## 8. Targets

| Target | Measure |
| --- | --- |
| typed errors | `raises.txt` lists only caller mistakes; 0 world errors raised as builtins |
| fault tables | every row has a test, passing on macOS and Linux (Windows rows skip elsewhere) |
| fuzzing | 200 examples per property in `check`; 20,000 in `dev.py fuzz` with no failure |
| threads | every public class listed; every safe class stress-tested |
| resources | the leak check passes for every module listed |
| `check`'s time | at most 45 s more than Phase 7's |

## 9. Order

After the bugs and the API changes of each module ([README › order](README.md#order)), so no
time goes into hardening code that is about to change:
1. typed errors: the scan with today's list, then the list shrinking module by module (most of it
   falls out of each module's API commit);
2. faults, one commit per module;
3. fuzzing: Hypothesis in the dev group, then one commit per target;
4. threads, then the leak check;
5. the size budget last.

## Risks

- **Renames break muscle memory** (`archive.pack`, `crypto.digest`, `worker.map`, `log.Row`): each
  is one commit with the snapshot's diff as its review, and no alias is left (AGENTS.md).
- **Tests that lean on private names**: making `theme`'s and `style`'s plumbing private moves
  those tests into `shared`'s own, which may use private names.
- **Fuzzing time**: capped at 200 examples a property in `check`.
- **Process-killing tests on Windows** use `Popen.kill` (`TerminateProcess`), as abrupt as
  `SIGKILL`.

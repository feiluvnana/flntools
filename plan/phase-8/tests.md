# tests: how Phase 8 is tested

Part of [Phase 8](README.md). AGENTS.md's rules hold: **unit tests fake the network
(`httpx.MockTransport`), the browser and the clock**; live checks on real sites are by hand on
the authorized test accounts, never in tests; only example handles. The layout stays as it is:
one `tests/` folder for `shared` and flnd. This file adds what Phase 8 needs: a failing test for
each bug, fault injection, fuzzing, thread stress, the leak check, and counts that stand in for
speed.

Everything marked **added** needs the user's OK.

## 1. A test per bug, first

Each bug in [bugs](bugs.md) names its test. The test is written first and fails on the current
code; the fix makes it pass; both land in one commit. A regression guard lives in the test's name,
as the writing rules say, never in a comment.

## 2. `shared` stands alone

`shared`'s tests (`test_http.py`, `test_log.py`, `test_media.py`, …) import no flnd module and use
generic examples (`example.com`, `photos/a.jpg`), so `shared` is tested as the general toolkit the
user wants it to be ([shared](shared.md#1-what-shared-is)). A test in `test_layers.py` (added)
fails when a `shared` test imports `flnd`.

## 3. Faults

`tests/faults.py` (added) gives three context managers, so a fault table is data:

```python
with faults.oserror("os.replace", errno.ENOSPC, after=0): ...      # the next call raises
with faults.cut(transport, at=Phase.BODY, after_bytes=65536): ...  # a stream ends early
with faults.clock_jump(fake, by=-3600, when=lambda: fake.now() > 10): ...
```

- `oserror` patches one function in the module under test for the length of the block.
- `cut` makes the MockTransport's stream raise `httpx.RemoteProtocolError` at connect, at the
  headers or after N body bytes; `slow` sends a byte per fake second (slow-loris); `capped` answers
  every range with at most N bytes.
- `clock_jump` moves `shared.clock.Fake` when a condition first holds.
- **Process kills** use `tests/kill.py`: the test runs a small script in a child
  (`sys.executable -I`) that signals each named point on a pipe; the parent kills it there
  (`Popen.kill`), then checks the disk.

The fault tables are in [shared › faults](shared.md#3-faults-injected-in-tests). flnd's own
faults (Ctrl-C at each stage, a category failing with encodes queued, a crashed browser, two runs
on one target) are its bugs' tests.

## 4. Hostile input

Hypothesis, in the dev group. Targets and properties are in
[shared › fuzzing](shared.md#4-hostile-input-fuzzed). `check`: 200 examples per property,
`deadline=None`, `derandomize=True`. `dev.py fuzz`: 20,000 with a random seed, failures kept in
`.hypothesis/` (git-ignored), each one becoming an `@example`.

## 5. Threads

`tests/data/threads.txt` lists each public class of `shared` as `safe` or `one thread`; a test
fails on one it doesn't list. Stress tests run each safe class from 16 threads for 1 s with a
seeded mix of calls ([shared › threads](shared.md#5-threads)). They are the only tests with real
time in them, bounded at 1 s each.

## 6. Counts that stand in for speed

Wall times are noisy, so `check` asserts counts that only change when behaviour does, measured
through the MockTransport and the fakes:

| Count | Bound |
| --- | --- |
| connections opened for a 64 MB file in pieces | ≤ the file's connection cap (8) |
| requests for a 32 MB file whose piece connections are slower to start than the first | 16 (no piece given away for a cold start: [S1](speed.md#the-biggest-wins)) |
| requests for 100 photos on one HTTP/2 host | 100, over one connection |
| bytes fetched again after a cut at 90% | ≤ one piece + 64 B |
| bytes served for a resume with another width | = bytes missing ([H6](bugs.md#http-download-pace)) |
| requests after a 429 with `Retry-After: 0` | one more, after the back-off's step ([H1](bugs.md#http-download-pace)) |
| DASH requests after a merge retry | 0 for streams already whole ([F7](bugs.md#flnd)) |
| encodes at once with 3 videos and 10 photos | videos ≤ 1, photos ≤ 8 |
| drawing ticks while a question is open, 1 s | ≤ 10 ([L1](bugs.md#log-theme-prompt-cli-fmt)) |
| `kv` statements for `items()` over 1,000 entries | 1 |
| folder walks for one target's repair | 1 |

Timings belong to [bench](bench.md), which `check` doesn't run.

## 7. Resources

`tests/leaks.py` (added):

```python
class Leaks:
    """The process's open files, threads and children, before and after a block."""
    def __enter__(self) -> Leaks: ...
    def __exit__(self, *error: object) -> None: ...   # fails when any is left after a 1 s grace
```

- Open files from `/dev/fd` (macOS, Linux) or the handle count (Windows); threads from
  `threading.enumerate()`; children from `multiprocessing.active_children()` and a `waitpid` poll.
- A mixin wraps every test of a class in it: `media`, `kv`, `worker`, `http.Client` and
  `download`, `log.record`, the fake `browser`, and flnd's job (no browser, pool or loop thread
  left after a target).

## 8. The time `check` takes

Phase 8 adds the bug tests and fault tests (fast, on fakes), 200 examples per fuzz property (about
20 s), the stress tests (about 15 classes × 1 s) and the count tests. The target is at most
**45 s more** than Phase 7's `check`.

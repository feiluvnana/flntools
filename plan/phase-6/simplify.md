# simplify

Phase 6, across every step. The user asked on 2026-11-12 for flnd to have no download limit, and for everything to be simplified wherever it can be. This file is the result of a review of the whole plan and the code on that day. **Where it disagrees with another Phase 6 file, this one wins**, and the [final numbers](#final-numbers) below are the ones to build to.

## No download limit

flnd has no `--limit` option on the command line. The limit lives only in `Engine(limit=)`, which only tests set, in `Settings.limit`, in `session.full`, and in 11 `session.full` checks across the sources. **All of it goes.** A run ends when the sources have nothing more, or on Ctrl-C. Removed:
- `Engine(limit=)`, `Settings.limit`, `session.full`, and the "Reached limit of N downloads" line;
- every `if session.full` in the sources;
- `--limit` in [architecture](architecture.md#items) and the replay loop's `and not session.full` in [ins](ins.md#three-ways-to-reach-an-api);
- the tests that set a limit (`tests/test_engine.py`).

## The plan, simplified

### 1. One item type: `Post`

[architecture](architecture.md#items) had `Post`, `File` and `Queue`, with a post's metadata carried over to the files after it, `session.saved()` to skip requests, and the job skipping a saved post. That is four mechanisms for "here is a post and its files". Now:

```python
@dataclass(frozen=True)
class Post:
    meta: Mapping[str, Any]                                          # id, date, category, and the site's fields
    files: Sequence[File] | Callable[[], Iterable[File]]             # or a function, called only when needed

@dataclass(frozen=True)
class File:
    source: Download | None = None
    meta: Mapping[str, Any] = {}                                     # num, extension, …
    text: str | None = None
    audio: str | None = None
    headers: Mapping[str, str] | None = None
```

- **`files` may be a function.** The job calls it only when the post passes `--from` and `--time` and its first file's key isn't saved. ins's media info request lives inside that function, so a saved post costs no request at all. **`session.saved()` goes.**
- **`Queue` goes.** No source hands one target to another today. A single post found in a list is just a `Post`.
- No carry-over between items, and `count` is `len(files)`.

### 2. One way to slow down after a throttle, and one learned speed

The plan had five slow-down mechanisms:
- `net.Backoff`, 30 s doubling to 15 min;
- [pace](pace.md)'s learned gap, doubling to 60 s and saved in `state`;
- the learned scroll floor, doubling to 5 s and saved separately;
- `Source.pause`, 5 s between targets;
- `retry_wait`'s own doubling.

Now there are two:
- **`Backoff`** is the wait after a 429, a challenge or a captcha, as today. `session.throttled` calls it.
- **One learned speed per source**, a multiplier from 1× up to 8×, kept in the source's `state`:
  - it doubles on each throttle, and every 50 clean requests or scrolls bring it 20% back toward 1×;
  - the request floor and the scroll floor are each multiplied by it;
  - so there is one number to learn, not two.

**`Source.pause` goes:** the next target's first request is paced like any other. **Retry waits** follow the source's floor, with no doubling of their own.

**A floor is a floor.** The jitter is added on top, 0 to +40%, never taken off. Before, "±40%" went below the floor that pace.md promises, down to 0.18 s for a 0.3 s floor.

### 3. No history migrations

- **`Source.key` keeps each source's key form as it is today:** fbk `{id}`, tik as `tik.py` keys now, tlg its message key. The others are `{id}_{num}`. No old row is rewritten, there's no `version` row, and nothing is downloaded again, profile pictures included. The [scripts](scripts.md#save_post) rewrite and its test go.
- **dates.md's `"dated": source | server | downloaded` field goes**, with its "rename when the real date is found". That can't happen: `flnd sync` skips saved posts before asking the site anything.

### 4. Flows: only what pays

From [flows](flows.md), these go:
- **#9, browser header order and a Chrome cipher list:** Python's `ssl` can't produce Chrome's TLS fingerprint, which is what sites check, and the in-page way covers exactly that case;
- **X's `x-client-transaction-id`:** rejected, at about 80 fragile lines;
- **ins's GraphQL post-query experiment:** gallery-dl never uses one; `/api/v1/media/<pk>/info/` is the one way;
- **tik's four-step chain:** it is now the list API direct, then scrolling. In-page and replay add two more ways for a site that is either answered directly or needs the page anyway.

What stays from flows:
- tik's best-quality file;
- twt's `count: 50` and full feature flags;
- ins's `product_type` rule, its headers, and stories 5 per call;
- fbk's block detection;
- `/accounts/suspended/` as a hard stop.

### 5. Don't fix what a later step deletes

[fixes](fixes.md) #8 (one photo turning off video compression in `sync`) and #12 (sync's triple history scan) patch code that [cli](cli.md#sync) removes or rewrites: sync loses its compress step, and the [job](architecture.md) reads the history once. They're done there, not in step 1. Fix #6 (Twitter's headers) is done once, in `prepare` ([source](source.md)), not first in `headers`.

### 6. One way to fetch on first use

`_fetch_once`, planned for both the Chromium and the model downloads, has one caller: Chromium comes from Playwright's own installer, which has no URL or hash for flnd to check. So the model download is written inside `media.classify`. Both still ask first, as the user decided. The `classify` extra is still offered for install when it's missing.

## The code, simplified

Each change is made in the step that touches the file.

| What | Where | Saves |
| --- | --- | --- |
| **One retry layer.** API calls retry inside `net.Transport`. Downloads bypass that with an `ONE_ATTEMPT` flag and retry themselves with `retrying(patiently(…))`. One `retrying(patiently(…))` now serves `session` and `download` alike, so `Transport`, `ONE_ATTEMPT`, `retry_wait`'s callable branch and the `Wait` type go | `flnd/net.py`, `flnd/sources/base.py` | about 35 lines |
| **One atomic file writer** (write to `.<name>.<random>.part`, then rename) instead of seven. For `shared`, `media.writing` is generalized into a private `_atomic.py` and used by `cookies`, `archive` and `media`. For flnd, one `files.write_atomic`, used by `net`, the job's `text=` files and `vault` | net.py, session.py, vault.py, _cookies.py, _archive.py, _media.py, _media_video.py | about 35 lines, and [fixes](fixes.md#sync) #13 (leftover temporary files) for free: every one is named `.part` |
| **One table of file kinds.** `session.KINDS` / `media_kind` and `net.same_extension` copy `media`'s own tables, so `media.kind_of(extension)` becomes public and flnd's copies go (checked for `.heic` and the audio extensions) | session.py, net.py | about 15 lines |
| **No handover thread.** The job consumes items off the source's loop, so saving never happens on tlg's event loop. `_submit`'s handover, `_hand_over`, `_end_handover` and `SourceRun.on_loop` go | session.py | about 30 lines and a thread |
| **`flnd/login.py` and `flnd/vault.py` become `flnd/auth.py`**: logins, cookie files and secrets in one place | login.py, vault.py | about 15 lines and a module |
| **`flnd/dash.py` moves into `flnd/sources/common.py`**, its only users being fbk and ins; `Streams.extension` goes, because content decides it | dash.py | about 6 lines and a module |
| **Dead engine knobs:** `Engine(headless=)` and `Settings.headless` (nothing sets them, and the browser stays visible); the ffmpeg-missing hint and `Settings.hinted` (ffmpeg comes with a required dependency, and `merge`'s own error already says what's missing) | engine.py, session.py | about 10 lines |
| **sync leftovers:** the extension-rename repair goes, because files are named by content now; the 8-column summary becomes one line per target; one `target_folder()` replaces the three places that build it | sync.py, session.py | about 25 lines |

Kept on purpose:
- **`sync._unrecorded`'s folder scan.** Folders downloaded before Phase 5 recorded settings still exist on the user's machine.
- **The classify extra's install prompt and the learned speed.** Both were asked for.

## Decision

7. **Keep encrypting secrets?** `vault` (about 105 lines) encrypts `secrets.db` with a key kept beside it. That protects only against someone who copies `secrets.db` and not the key file. Storing secrets in a `kv` file with mode 0600, as the cookie files already are, would remove about 70 lines and [fixes](fixes.md#windows) #2. **Recommended: keep it**: it costs little, Telegram's session string is a full login, and a copied database alone stays useless. **Decided 2026-11-12: kept** ([README](README.md#decisions)).

## Final numbers

| | Earlier files say | Build to |
| --- | --- | --- |
| `Source` fields | 8 ([source](source.md)), 11 ([architecture](architecture.md)) | **11**: `code`, `name`, `domains`, `examples`, `categories`, `cookies`, `logins`, `pace`, and the formats **`folder`**, **`filename`** and **`key`**. The file name format is `filename`, because `name` is the source's display name. `pause` goes |
| `Source` hooks | 5 | **5**: `resolve`, `items`, `prepare`, `login`, `connect` |
| `Session` members | 26 ([session](session.md)), 21 ([architecture](architecture.md)) | **20**: `target`, `cookies`, `categories`, `since`, `until`, `wants`, `fetch_json`, `fetch_html`, `goto`, `responses`, `handoff`, `page`, `client`, `state`, `secrets`, `interactive`, `ask`, `info`, `warn`, `throttled` |
| item types | 3 | **2**: `Post` and `File` |
| ways to reach an API | 3 ([ins](ins.md#three-ways-to-reach-an-api)) | **3**: direct, in page and replay; each source uses the fewest that work: tik direct, twt direct, loi direct, ins and fbk replay with in page where needed |
| scroll | 1.5 s ([README](README.md)), 3 s `delay` ([session](session.md)) | **as soon as the page shows progress**, waiting at most 3 s, above a 0.3 s floor times the learned speed |
| `python-jsonpath` | gone ([README](README.md) Order step 4), kept ([api](api.md)) | **kept**: `meta_results` uses `.jsonpath` |
| `--classify` | on `download` and `sync` ([classify](classify.md)) | **`download` only**; `sync` uses the saved setting |
| scripts | about 1,280 ([scripts](scripts.md)), about 1,200 ([architecture](architecture.md)) | **about 1,150**: about 1,200 less the `full` checks, the handover and the migration |

## Tests

- No test sets a limit, and none reads `session.full`.
- The `Post.files` function is called once for a new post, and never for a saved one or one outside `--time`.
- One learned speed per source: a throttle doubles it, 50 clean requests bring it back toward 1×, and the request and scroll floors both scale with it.
- The jitter never goes below the floor.
- `fbk` and `tik` history rows from Phase 5 are skipped by their unchanged keys, with no rewrite.

# architecture

Phase 6, step 3, the frame the [source](source.md), [session](session.md) and [scripts](scripts.md) steps are built in. The user decided on 2026-11-12 that flnd adopts gallery-dl's architecture, which has been tested against these same sites for years. This file says what that architecture is, how it maps onto flnd, and what it replaces. It is based on gallery-dl's `job.py` and `extractor/common.py` (GitHub master 1.32.19, Codeberg 1.33.0-dev).

## gallery-dl in one paragraph

An **extractor** reads a site and **yields messages**, and does nothing else. `Directory(kwdict)` starts a post, `Url(url, kwdict)` is one file, and `Queue(url, kwdict)` is another URL to extract (a post found in a list, an album in a profile). The `kwdict` is the item's **metadata**: `id`, `num`, `date`, `extension`, `category`, `subcategory`, and whatever the site gives. A **job** consumes the messages. For each one it:
1. applies the **filters** (date range, ranges, unique);
2. builds the **path** from format strings (`directory_fmt`, `filename_fmt`) over the metadata;
3. checks the **download archive** by `archive_fmt`;
4. picks a **downloader** by URL scheme (http, ytdl);
5. runs the **postprocessors** at fixed hooks (`prepare`, `file`, `after`, `post`, `finalize`): metadata files, zip, mtime, classify, exec.

The extractor never decides where a file goes, whether it is already there, or what happens to it after.

## flnd on that architecture

| gallery-dl | flnd, Phase 6 | Replaces |
| --- | --- | --- |
| an extractor, `items()` yielding messages | a **`Source`**, whose `items(session)` yields **`Post`**, **`File`** and **`Queue`** ([below](#items)). It may be `async` (tlg) | `run(session)` calling `session.save` |
| `Directory(kwdict)` | **`Post(meta)`**: the metadata every file after it inherits, until the next `Post` | each source threading `date`, `category` and `folder` through every `save` |
| `Url(url, kwdict)` | **`File(source, meta)`**: a URL, mirror URLs, or a `fetch(dest, progress)` function; or `text=` for a text file such as loi's `info.md`; `audio=` and `headers=` as today | `session.save`, `session.write` |
| `Queue(url, kwdict)` | **`Queue(target)`**: another target to extract, run by the engine as a child, at most once per run | sources calling their own helpers for single posts found in lists |
| `kwdict` | **`meta`**, a dict: `category` (the `--from` name), `id`, `date`, `num` (the file's index in its post), `count`, `extension`, plus anything the source adds (`user`, `title`, `album`, `labels` from classify) | — |
| `directory_fmt`, `filename_fmt` | **`Source.folder`** and **`Source.name`**: Python format strings over `meta`. The defaults are `"{category}"` and `"{date:%Y-%m-%d}_{id}{index}.{extension}"`, where `index` is `""` for a one-file post and `"_<num>"` otherwise | file names built by hand in every source; [save_post](scripts.md#save_post) |
| `archive_fmt` and the sqlite archive | **`Source.key`**, default `"{id}_{num}"`, in `.flnd/history.db`: the history, keyed by format | `save(key=)` and the per-source key rules |
| filters (`date-after`, `image-range`, …) | `--from` (on `meta["category"]`) and `--time` (on `meta["date"]`), applied to `Post` and `File` by the engine | `session.wants` in every source |
| downloaders by scheme | `net.download` for `http(s)`, a `fetch` callable for SDK media (tlg), and the DASH merge when `audio` is set | — |
| postprocessors at hooks | **one chain after each file**, in this order: `compress` (`--image`/`--video`/`--audio`), `classify` (`--classify`), `mtime` (the file's date). Each is a function `(path, meta) -> path` in `flnd/post.py` | the compress, rename and timestamp code spread through `session.py` |
| `sleep-request`, `sleep-429` | [pace](pace.md) | — |
| `ratelimit`, `abort`, `exit` | throttles waited out ([ins](ins.md#4-see-and-wait-out-every-throttle)), and hard stops raise `LoginError`; flnd has no download limit | — |

### What flnd keeps that gallery-dl doesn't have

- **The session** for everything a source reads: `fetch_json`, `fetch_html`, the browser, `state`, `secrets`, and the questions during `flnd login`. gallery-dl's extractors make their own `requests` calls. flnd's sources still go through the session, so pacing, cookies, back-off and the browser stay in one place.
- **A browser**, for the [in-page and replay](ins.md#three-ways-to-reach-an-api) ways and the scroll fallback. gallery-dl has none.
- **Saved target settings** and `flnd sync` ([cli](cli.md#saved-settings)).

## Items

```python
@dataclass(frozen=True)
class Post:
    meta: Mapping[str, Any]                      # id, date, category, and the post's own fields

@dataclass(frozen=True)
class File:
    source: Download | None = None               # a URL, mirrors, or fetch(dest, progress)
    meta: Mapping[str, Any] = {}                 # num, extension, and anything the file adds
    text: str | None = None                      # a text file instead of a download
    audio: str | None = None                     # DASH audio, merged into the video
    headers: Mapping[str, str] | None = None

@dataclass(frozen=True)
class Queue:
    target: str                                  # anything `resolve` reads: a URL, "ins:p/ABC123"
```

A source's `items` reads as the site works, and nothing else:

```python
def items(self, session):
    for node in self.timeline(session):                     # API first: direct, in page, or replayed
        post = node["node"]
        yield Post({"id": post["code"], "date": day(post.get("taken_at")), "category": "posts"})
        for num, (url, audio) in enumerate(post_files(post)):
            yield File(url, {"num": num, "extension": "mp4" if audio else "jpg"}, audio=audio,
                       headers=CDN_HEADERS)
```

- **The engine fills what the source leaves out.** `count` from the `File`s of the post, `index` from `num` and `count`, and `date` from the [dates](dates.md) fallbacks (`Last-Modified`, then the download day). The `extension` from the content, as now.
- **Skipping before work.** `session.saved(key)` stays, for a source that can skip a request when the post is already saved (ins's media info). The engine also skips a whole `Post` whose first file's key is saved, and a `--time` range rules out a `Post` before its files are listed. So a generator that's still listing doesn't cost a download.
- **Ctrl-C** stops consuming the generator. Python closes it at the next `yield`. flnd has no download limit ([simplify](simplify.md#no-download-limit)).
- **Responses while scrolling become a loop.** Today a source registers `on_response`, scrolls, and counts what its handler found. Now `session.responses(match, scroll=True)` is an iterator of the JSON responses that match, scrolling the page while the caller asks for more, and ending by the [end rule](session.md#scrolling-on-the-pages-own-time). So the scroll fallback reads like an API loop: `for data in session.responses("graphql", scroll=True): yield from self.parse(data)`.

## What changes in the other steps

- **[source](source.md):**
  - `run(session)` becomes `items(session)`.
  - Three fields with defaults are added: `folder`, `name` and `key`. Only sources whose layout differs set them: loi's post folders and `NN.jpg` previews, and tlg's `<chat>/<kind>`.
  - The fields become 11, and the hooks stay 5.
- **[session](session.md):**
  - `save`, `write`, `folder`, `full`, `on_response` and `scroll` leave the session. Items replace the first four, and `responses(match, scroll=)` replaces the last two.
  - It gains `responses`.
  - From 26 members to 21.
- **[scripts](scripts.md):**
  - `save_post` goes: a source yields a `Post` and its `File`s.
  - `day` and `meta_results` stay.
  - Each script loses its naming and saving code entirely, and its `run` becomes a generator. That's about 80 lines fewer than scripts.md estimated, for about 1,200 in all.
- **[dates](dates.md):** "the engine dates every file" is the default `name` format and the `mtime` postprocessor. The fallbacks are unchanged.
- **[classify](classify.md) and [cli](cli.md):** `--image`, `--video`, `--audio` and `--classify` are the postprocessor chain, configured from the target's saved settings.
- **History:** keys keep their form (`<code>:<id>_<num>`), so the [one-time rewrite](scripts.md#save_post) of old fbk and tik keys still applies. Profile pictures change their names from `avatar_<id>.jpg` to the default `<date>_<id>.jpg` and their keys to `<id>_0`, so they are downloaded once more, about one file per target.

## Files

- `flnd/items.py`, about 40 lines: `Post`, `File`, `Queue`.
- `flnd/job.py`, about 180 lines, the job: consume a source's items, filter, build paths, check the history, queue downloads, run the postprocessors, and run `Queue`s as children. It takes `session.py`'s `save`, its path rules and its unpack-free download handling, so `session.py` keeps only what a source reads.
- `flnd/post.py`, about 60 lines: `compress`, `classify` and `mtime`, each `(path, meta) -> path`.
- `flnd/engine.py` keeps runs, groups and sources. `flnd/session.py` keeps the session.

## Tests

- `tests/test_job.py`, with a fake source whose `items` yields a fixed list:
  - names and folders from the formats;
  - `index` empty for one file and `_<num>` for several;
  - `--from` and `--time` on `Post` and `File`;
  - a saved post skipped whole;
  - Ctrl-C closes the generator, and its `finally` runs;
  - a `Queue` runs once as a child;
  - the postprocessors run in order and are given the renamed path;
  - `text=` writes a file atomically.
- An async `items` (tlg's) is consumed the same way.
- `tests/fakes.py`: `FakeSession` loses `save` and its record of saves. Source tests assert on the list `items` yields, which is simpler than today's saves.

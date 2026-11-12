# scripts

> **Changed by [redesign](redesign.md)** (2026-11-12): the sources are rewritten in step 6 on routes, categories and `Post`/`File`. The engine reads every date, so `day()` goes. `meta_results`, the DASH picker and `watch_responses` live in `sources/common.py`, with the readability rules below.

Phase 6, step 6, after [ins](ins.md). The user asked on 2026-11-12 for every source script to be shorter and easier to read, and for stronger utilities where weak ones are why a script is long. This step rewrites `flnd/sources/*.py` on the Phase 6 session and source, with three new helpers and one deletion; `shared` needs nothing new for it.

> **Changed by [architecture](architecture.md)** (decided 2026-11-12, flnd adopts gallery-dl's architecture): `save_post` goes, because a source yields a `Post` and its `File`s and the engine names and keys them. `day` and `meta_results` stay. The scripts come to about 1,200 lines.


## Where they stand

Lines of code (no docstrings, comments or blank lines), counted 2026-11-12:

| Source | Today | After the session, source and cli steps | After this step |
| --- | --- | --- | --- |
| ins | 204 | about 230 (the media-info API, throttle checks and the grid allowlist add about 30) | about 205 |
| fbk | 256 | about 248 | about 220 |
| twt | 248 | about 239 | about 228 |
| tik | 226 | about 214 | about 188 |
| tlg | 229 | about 224 | about 222 |
| loi | 189 | about 183 | about 178 |
| relay | 26 | — | **gone** |
| `common.py` | — | — | about 40 (new) |
| **total** | **1,378** | about 1,364 | **about 1,280** |

The scripts are mostly site knowledge: JSON paths, endpoints, login flows. That part can't shrink. What can is the same few chores, each written 2 to 4 times in different ways.

## What repeats

| Chore | Where | Lines |
| --- | --- | --- |
| **guessing the file extension** from the URL or the JSON | ins `image_extension` and `IMAGE_EXTENSIONS`, fbk `image_extension`, twt's suffix logic, loi `image_name` | about 20, and all of it does nothing: `session.save` already renames a photo or video to the extension its content calls for |
| **naming and keying a post's files** `<day>_<id>[_<index>].<ext>`, looping with index keys | ins `save_post` and `save_story_page`, fbk `save_media` and `save_stories`, twt, tik `_save_media_item` (23 lines) | about 45 |
| **a timestamp to a day**: digit strings, ms or s, local time, overflow | ins `taken_on`, fbk `posted_on`, tik `posted_on` | about 15, and each one has a bug ([fixes](fixes.md#dates)) |
| **Meta's embedded results**: load a page, find the JSON scripts that hold `__bbox`, walk them for `result` | `relay.py`, ins 3×, fbk 2× | about 30 |
| **counters only for `scroll(count=)`** | tik `counts` and `has_more`, twt `found = [0]`, fbk `Feed.found` | about 15; the [session](session.md#scroll) step removes them |
| **`session.folder` juggling** | ins, fbk | about 8; `save`'s own `folder=` already covers it |

## The helpers

Each is small, and each has at least three callers. They live in **`flnd/sources/common.py`**, not the session, so the session keeps its 26 members. `FakeSession` tests them like any source code.

### `save_post`

```python
@dataclass(frozen=True)
class File:
    url: str | Sequence[str]           # a URL, or mirrors tried in order
    video: bool = False
    audio: str | None = None            # DASH audio, merged into the video

def save_post(session, id: str, files: Sequence[File], *, category: str, date: date | None = None,
              folder: str | None = None, headers: Mapping[str, str] | None = None) -> int:
    """Save one post's files as `<id>[_<index>].jpg|.mp4`, dated by the engine, and return how many were queued."""
```

- **Names:** `<id>[_<index>]`, with `_<index>` only when the post has more than one file, as every source does now. The engine adds the date in front ([dates](dates.md)). `.jpg` or `.mp4` only says photo or video: the content decides the real extension.
- **Keys:** always `<id>_<index>`, a single file included, so `session.saved(f"{id}_0")` means "this post is saved" for every source.
- **The old keys** differ: fbk keys a single file `<id>`, and tik keys a video `<id>` and its images from `_1`. So the history rows of fbk and tik are rewritten to the new keys once, when the engine first opens a Phase 5 `history.db` (about 15 lines, in a `version` row), and nothing is downloaded again.

```python
# ins today
for index, (url, extension, audio) in enumerate(files):
    stem = f"{code}_{index}" if len(files) > 1 else code
    file_name = f"{day}_{stem}.{extension}" if day else f"{stem}.{extension}"
    session.save(url, file_name, category=category, date=day, folder=folder,
                 key=f"{code}_{index}", headers=CDN_HEADERS, audio=audio)
# after
save_post(session, code, post_files(post), category=category, date=day, folder=folder, headers=CDN_HEADERS)
```

### `day`

```python
def day(stamp: Any) -> date | None:
    """The UTC day of a Unix timestamp in seconds or milliseconds, an int, float or digit string; None if it isn't one."""
```

- It reads 10-digit seconds and 13-digit milliseconds, and refuses anything before 2004 or after next year. That catches the 2011 dates a small Instagram `pk` gives.
- It catches `ValueError`, `OverflowError` and `OSError`, which `fromtimestamp` raises for huge values on different platforms.

```python
# tik today
create_time = raw.get("createTime") or raw.get("create_time")
if isinstance(create_time, str) and create_time.isdigit(): create_time = int(create_time)
if isinstance(create_time, (int, float)) and create_time > 100000000:
    return datetime.fromtimestamp(create_time).strftime("%Y-%m-%d")
# after
return day(raw.get("createTime") or raw.get("create_time")) or day(int(item_id) >> 32)
```

### `meta_results`

```python
def meta_results(session, url: str) -> list[Any]:
    """Load a Facebook or Instagram page and return the GraphQL results its HTML embeds; [] when it didn't load."""
```

It replaces `relay.py` and the five places that load a page and then read its embedded results. It reads them with `selector`'s existing `.jsonpath("$..__bbox.result")`, so `relay`'s hand-written recursive walk goes and `selector` gains nothing.

```python
# ins today
if not session.goto(url):
    return
for result in embedded_results(session.page.selector):
    profile.on_result(session, result, None)
# after
for result in meta_results(session, url):
    profile.on_result(session, result, None)
```

## Rejected

- **A `best(items, by=…)` picker:** `max(items, key=…)` is the standard library, one line at each of its four uses.
- **`session.paginate(url, cursor=…)`:** twt is the only caller, and its end rule ("no new tweet") is Twitter's own.
- **A `Post` dataclass, with `run` yielding posts for the engine to save:** ins, fbk, tik and twt's browser fallback save from inside `on_response` callbacks, and tlg's `run` is async. A generator `run` fits none of them. `save_post` gives the same naming without changing how they run.
- **`session.collect(match, parse)`** (`on_response` plus dedupe): `save` already skips what is queued or saved, and what's left of each handler is the site's own parsing.
- **`fmt.day`:** `fmt` turns values into text, and `day` reads one. It stays with the sources until a second module needs it.
- **Error boilerplate:** twt and tik's fetches use the existing `soft=True` and `if data is None: session.warn(…)`. That needs no new API and saves about two lines each.

## Readability, besides length

Each script is rewritten to read top to bottom as the site works, to the [comments](comments.md) standard:
1. **The module docstring:** what the site gives, the folder layout, and the quirks.
2. **The `Source` class**, in its eight fields and its hooks.
3. **`run`**, as a short list of steps that each call one named function: one tab, one post, one story.
4. **The helpers**, in the order `run` calls them.

No function in a source is over about 30 lines, and none takes more than five arguments.

## Tests

- `tests/test_common.py`:
  - `save_post` names and keys a single file and a carousel;
  - `day` reads seconds, milliseconds and digit strings, and gives None for a huge value, a 2011 value and text;
  - `meta_results` returns `[]` for a page that didn't load.
- A Phase 5 `history.db` with fbk and tik rows in the old key forms is rewritten once, and a second open leaves it alone.
- Every source test that passes today passes after the rewrite, with the same file names.

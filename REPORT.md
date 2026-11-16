# REPORT: simplifying flnd's architecture (and flnc/flns before they're built)

Written 2026-11-16 at `a9b8d31`, from reading the code only; §0, §3.5 and §4.5 add the user's direction from the same day. Every count comes from `wc -l`, `grep -c` or an AST line counter (code / docstring / comment / blank). Numbers marked **est.** are estimates. Handles are examples only.

**Headline (flnd)**

| | today | proposed |
| --- | --- | --- |
| flnd lines (engine + sources) | 11,084 | ~8,200 est. (~330 of the cut move into `shared.browser`/`site.pages`) |
| concepts a source author learns | 102 | 90 |
| ways a route can behave | 5 | 3 |
| frames from `download()` to a category | 15 | 6 |
| classes built per download | 16 | 9 |
| public `flnd` names in `api.txt` | 185 | ~165 est. |

The user's decisions (2026-11-16) that the report works from:
- every single-URL route stays (tik video/photo, ins post/reel, tweet, mht chapter, loi post);
- `ins:me` stays;
- each source otherwise keeps its one main target kind;
- mht keeps its manga and query routes, and drops author and genre;
- loi may gain a query route.


## 0. The user's direction (2026-11-16), which this report follows

> "flnd just do things like now each source specify how to login (login and saved credentials flow, the source control all of it, engine just invoke), then a way to listing for each target (so target is for listing). and then those listing will be passed to downloader and also show progress"
>
> "About flnc, just a plain lib to do that, and maybe can be combine to flnd to script so it can compress on the fly. Same with flns."

So flnd has **three roles**, and nothing else:

| role | owns | the engine's part |
| --- | --- | --- |
| **login** (per source) | the whole flow (browser cookies, cookie file, QR, API key, password), where the credentials live, how to refresh them, and how to tell whether the source is logged in | call `source.login(...)` / `source.logout()`; hand the source its own vault |
| **listing** (per target) | a target names one listing; the source turns it into `Post`s (or, for finders, `Target`s) | resolve the target, walk the listing, apply `--from`/`--time`/`--limit` and the sync early-stop |
| **downloader** (the engine) | getting each listed file to disk: dedupe, resume, dates, history, retries, parallelism, progress rows | everything; a source never touches the disk or the history |

flnc and flns become **plain libraries of functions**, with no home object and no engine. They compose with flnd in a script, so files are compressed (or put into a slideshow) as soon as they land. §3.5 and §4.5 give the shapes; they supersede the `Flnc`/`Flns` classes of §4.2–4.3 where the two differ.

---

## 1. Today's architecture, measured

### 1.1 Data flow: `flnd download ins:example_user --from posts`

```
cli.download (cli.py:53)
 └─ Engine.download (engine.py:106)
     ├─ categories_by_source (options.py:63)      ┐ --from is checked twice
     ├─ _validate_targets → targets.validate      ┘ (engine.py:122-123)
     ├─ dry? → _dry_run → _dry_one → _log_dry → job.plan        (path 1)
     ├─ any query? → _download_mixed → _run_query → _discover_and_run → job.discover (path 2)
     └─ _download_normal (engine.py:281)                                              (path 3)
         └─ _run (742): RunShared, job.Board, _Planned, group by source
             └─ shared_browser → _BrowserPool (site.py:600, 690)
                 └─ _run_site (931)
                     ├─ source_run → SourceRun + 2× http.Client via _make_client (site.py:108,132)
                     ├─ TargetSite(account) + logged_in check (engine.py:940)
                     ├─ _written_back, _connected (async bridge) (engine.py:975, 983)
                     └─ job.download (job.py:1030) → fs.lock → _run_download (1042)
                         ├─ TargetSite(target), Ledger, Downloads, _Work
                         ├─ _retry_failed (1099)
                         └─ _execute_listing → _read → _read_categories → _read_one_category
                             ├─ Resume + listing() thread-local (source.py:807, 852)
                             └─ _queue_all → _queue_listed → _Kept.__iter__ → _steps
                                 └─ Instagram.posts(site, profile)            ← the source
                                     └─ site.fetch / site.goto → _PacedPage → _RawProxy
                                         → _BrowserPool thread → Playwright
             files: Downloads.submit → worker.Pool → _task → _download → net.download → disk
```
`sync` adds three more paths (`_sync_normal`, `_sync_queries` → `_sync_query` → `_sync_query_run` → `_sync_query_items`), and `flnd.dry.plan` (dry.py:22) is a sixth, separate one.

### 1.2 Lines per module

| module | total | code | doc | comment | blank | classes | defs |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| `__init__.py` | 93 | 75 | 5 | 0 | 13 | 0 | 4 |
| `cli.py` | 290 | 220 | 28 | 1 | 43 | 0 | 15 |
| `engine.py` | 997 | 859 | 41 | 0 | 97 | 6 | 59 |
| `job.py` | 1,694 | 1,387 | 114 | 1 | 192 | 15 | 104 |
| `site.py` | 831 | 612 | 79 | 20 | 124 | 8 | 81 |
| `source.py` | 1,286 | 672 | 382 | 90 | 214 | 16 | 72 |
| `options.py` | 139 | 98 | 8 | 11 | 22 | 2 | 6 |
| `targets.py` | 374 | 281 | 30 | 0 | 63 | 0 | 29 |
| `dry.py` | 139 | 112 | 6 | 0 | 21 | 0 | 7 |
| `common/auth.py` | 474 | 264 | 119 | 28 | 85 | 1 | 34 |
| `common/net.py` | 341 | 258 | 20 | 8 | 55 | 1 | 26 |
| `common/paths.py` | 153 | 97 | 22 | 12 | 26 | 1 | 6 |
| **engine side** | **6,811** | 4,935 | 854 | 171 | 955 | 50 | 443 |
| `sources/common/*` + registry | 288 | 202 | 34 | 2 | 53 | 2 | 21 |
| fbk / ins / loi / mht (total) | 582 / 963 / 396 / 315 | | | | | | |
| fbk / ins / loi / mht (code) | 394 / 607 / 294 / 248 | | | | | | |
| tik / tlg / twt (total) | 606 / 556 / 567 | | | | | | |
| tik / tlg / twt (code) | 354 / 407 / 400 | | | | | | |
| **sources** | **4,273** | 2,906 | | | | | |
| **total** | **11,084** | 7,841 | 1,407 | 465 | 1,546 | 71 | 701 |

### 1.3 What a source author must learn (102 concepts)

| group | count | items (evidence) |
| --- | ---: | --- |
| names from `flnd.source` | 20 | `__all__` (source.py:33-35) lists 19, plus `caught_up`, which ins.py:26, tik.py:23 and twt.py:28 import though it is outside `__all__` and `api.txt` |
| `@target` options | 4 | `shown`, `discovers`, `from_`, `folder` (source.py:356-358) |
| pattern rules | 5 | `{name}`, `{name:regex}`, `*`, `?{query}`, host pattern vs code pattern (source.py:155-191) |
| `@route.category` options | 3 | `newest_first`, `folder`, `key` (source.py:318) |
| Source ClassVars to set | 16 | `code name domains unsupported needs requires_login login_methods pace folder filename key fields user_agent browser stealth concurrency` (source.py:908-947) |
| derived ClassVars | 4 | `routes forms categories single_kinds` (source.py:918-927) |
| overridable methods | 6 | `resolve prepare login connect date logged_in` (source.py:1011-1100) |
| `Site` members | 14 | source.py:630-730 |
| `Login` members | 4 | source.py:740-767 |
| `Post` | 9 | `id files(list or lazy) date headers **fields replace filed lazy category` (source.py:453-496) |
| `File` | 5 | URL/mirrors/fetch, `audio`, `name`, `size`, `File.text` (source.py:392-427) |
| reserved fields | 6 | `ENGINE_FIELDS` (source.py:49) |
| `sources.common` | 6 | `watch_responses`, `Responses`, `warm_tabs` (feed.py:15), `meta_results`, `embedded_results` (relay.py:12), `dash.best` |

**Ways a route can behave: 5.**
1. A single post (yields `Post`).
2. A main target with categories.
3. `discovers=True` (ins:me).
4. A `?{query}` pattern (mht).
5. A route without categories that yields `Target`s, run by `job._run_discovered` (job.py:1612). mht `author`/`genre` (mht.py:103-127) have no `discovers` flag and go down this second path.

### 1.4 Layers and classes per download

- **Frames** from `Engine.download` to the category method: `download → _download_normal → _run → _run_site → job.download → _run_download → _execute_listing → _read → _read_categories → _read_one_category → _queue_all → _queue_listed → _Kept.__iter__ → _steps → category`, which is **15**.
- **Classes built per download: 16.** `Engine`, `Options` (twice, via `replace`), `RunShared`, `_Planned`, `job.Board`, `_BrowserPool`, `SourceRun`, `http.Client` ×2, `TargetSite` ×2 (account and target), `Ledger`, `Downloads`, `_Work`, `Resume`, `_Kept`, `TargetReport`, `Report`. A browser source adds `_SharedBrowser`, `_SharedCookies`, `_PacedPage` and `_RawProxy` (site.py:563-690).

---

## 2. Problems, each proved

| # | problem | evidence | cost |
| --- | --- | --- | --- |
| P1 | **Every `from_=` is redundant**: 7 uses, and each equals the default | loi.py:59 `("photo","archive")` is its categories; mht.py:66 `("chapters","cover")` is too; tlg.py:73 `("photo","video","file")` is too; mht.py:51,106,119,132 `from_=()` sit on routes without categories, whose default is already empty (source.py:307-311) | 1 option, 40 lines in targets.py (`_validate_from`, `_route_from`, `_target_routes`, `_check_from_entry`) |
| P2 | **Every route `folder=` repeats `Source.folder`**: 7 of 7 | loi.py:60 = loi.py:49; mht.py:52,67,107,120,133 = mht.py:41; tlg.py:74 = tlg.py:61 | 1 option; a third level in `_folder_format` (job.py:1681) |
| P3 | **`discovers=True` is needed once** | mht.py:131 sits on a `?{query}` route, which already sets it (source.py:278-279). Only ins.py:146 needs it | 1 option for 1 route |
| P4 | **`shown=` works around the one-placeholder rule** | ins.py:146 `@target("{me:me}", shown="ins:me")`: the pattern needs a fake placeholder because of source.py:289-294; tlg.py:72 has no pattern at all because it overrides `resolve` | 1 option, 2 users |
| P5 | **`--from` is validated twice, and categories are filtered twice** | `categories_by_source` (options.py:63-91) and `targets._validate_from` (targets.py:311-339) run back to back (engine.py:122-123). `job.wanted` (job.py:261) and `options.selected` (options.py:54) are the same filter | ~110 lines in two modules; two error texts for one mistake |
| P6 | **Folder checks run on every download and know every source** | `_validate_folders` runs per call (targets.py:255); `FOLDER_FIELDS` (targets.py:28-30) hard-codes the union of every source's `fields` (title, order, author, genres, chapters, album, part, chat, n), which makes `Source.fields` redundant | work on every run; a new source edits `targets.py` |
| P7 | **Dead code reached through `getattr`** | `getattr(route, "params", {})` (targets.py:283): `params` left with `flnd.query` in e4fc627. `getattr(route, "folder", None)` (targets.py:262, job.py:1685) although `Route.folder` always exists. `getattr(flnd.targets, "validate", None)` (engine.py:805) for a function in the same package. `Source.browser = True` is set by fbk.py:156, ins.py:108, tik.py:317 and twt.py:153, and **nothing reads it** (0 readers in flnd/) | a reader can't tell what is live |
| P8 | **One login check, four copies** | engine.py:437-439, 810-816, 941-952, dry.py:42-43 | 4 places to change one message |
| P9 | **Six download paths** | `_download_normal`, `_download_mixed`, `_dry_run/_dry_one`, `_sync_normal`, `_sync_queries/_sync_query*`, `dry.plan`; each opens `RunShared`, `Board` and `shared_browser` itself (engine.py:156, 418, 463, 495, 581, 755) | engine.py is 997 lines, and 235 of them (356-375, 452-665) serve query targets |
| P10 | **Eight finder functions in two modules** | engine `_run_query`, `_discover_and_run`, `_sync_query`, `_sync_query_run`, `_sync_query_items`; job `discover`, `_yield_arg_targets`, `_run_discovered` | 8 functions for ins:me and one mht query |
| P11 | **Copy-paste docstrings** | `_sync_query` says "Run a dry-run plan for a single target." (engine.py:575); `_fail_unreached` repeats `_run_site`'s docstring (engine.py:965) | misleads readers |
| P12 | **The early-stop rules exist 4 times** | older-than-since: `OLDER_IN_A_ROW` (job.py:48, `_Kept`) and `OLD_STREAK`+`older_streak` in ins.py:70/870, tik.py:51/73, twt.py:84/445. Saved streak: `SAVED_IN_A_ROW` (job.py:49, `_queue_listed`), `SAVED_STREAK`+`Resume.caught_up` (source.py:64, 835), and `_hit_saved` in ins.py:858, tik.py:63, twt.py:457. Each `_hit_saved` repeats its category's `newest_first` list (ins.py:860) | 28 lines refer to these in the 3 sources; on sync, **each newest-first post is looked up in history twice**: `Ledger.take`, then `caught_up → Ledger.is_saved` (job.py:1573) |
| P13 | **A thread-local channel between `pages()` and the job** | `_listing = threading.local()` (source.py:881), `listing()` (852), `Resume.taken` (829) so that only the first `pages` call resumes | the cursor's owner is invisible from the source; a second `pages` call silently doesn't resume |
| P14 | **The shared-browser stack** | `_BrowserPool`, `_SharedBrowser`, `_SharedCookies`, `_RawProxy`, `_wrap_raw`, `_unwrap_raw` (site.py:599-782, **184 lines**). `_RawProxy.__getattr__` sends every Playwright attribute read through `executor.submit().result()` (site.py:633-643) | a thread hop per DOM call; `_SharedCookies.clear` does nothing (666-667) |
| P15 | **Private coupling through `getattr`** | feed.py:40 `getattr(site, "_push_tab")`, :84 `"_take_warm_responses"`, :113 `"_pop_tab"`, :155 `"_warm_tabs"`; site.py:412 `_take_warm_responses` only calls `_stop_warm_watch`; `_make_client` writes `http.Client._raw/_interval/_stop` (site.py:115-119); `web._call` and `web._stop` (site.py:251, 470, 521) | `shared.http`/`browser` can't change without breaking flnd |
| P16 | **Constant passed as a function** | `_PacedPage(..., speed=lambda: 1.0)` (site.py:294), then `SCROLL_FLOOR * self._speed()` (583) | indirection with no variation |
| P17 | **Async bridging is spread out** | 29 lines in engine/job/site (`run_async`, `iscoroutine`, `__anext__`, `__aenter__`), for **one** async source: tlg has all 15 `async def`s; the other 6 have 0 | 6 places know that a source may be async |
| P18 | **Two ways to say "query"** | `Target.query` and `kind == "query"` (source.py:136, 142) | |
| P19 | **targets.py is 20 functions over untyped dict rows** | `isinstance(x, dict)` guards in every helper (targets.py:85-372) | step 7's typed `store.Row` makes them attribute reads |

**`discovers`/query machinery after the route cut.** Finders that remain: `ins:me` (ins.py:146), `mht?query` (mht.py:129), and the proposed `loi?query`. All three yield `Target`s of their own source. So `_run_discovered`, the routeless-yields-targets path, and the cross-source `_run_child` branch (engine.py:560-565) have no user left. One finder path is enough.

---

## 3. The proposed architecture

### 3.1 Target model: one decorator, three behaviours

| behaviour | declared by | after the cut |
| --- | --- | --- |
| single post | `@target(...)` returning `Iterator[Post]` | tik video/photo, ins post/reel, tweet, mht chapter |
| main target | `@target(...)` plus `@<route>.category` methods | tik/ins/twt/fbk profile, tlg chat, loi post, mht manga |
| finder | a `?{query}` pattern, or `finds=True` | ins:me, mht?query, loi?query |

```python
@target(*patterns, finds: bool = False)          # was: shown, discovers, from_, folder
@<route>.category(newest_first=False, folder=None, key=None)   # unchanged
```
- A literal pattern with no placeholder is allowed for finders: `@target("me", finds=True)`. The value is the literal itself, so `shown` goes.
- tlg sets `forms = ("tlg:<@username, chat title or id>",)` next to its own `resolve`, which replaces its `shown`.
- `from_` is always the route's categories; `folder` is always the category's, then the Source's.
- `browser` goes. Folder templates are checked once, in `Source.__init_subclass__` (a `TypeError` at import, like pattern errors), against `ENGINE_FIELDS | Source.fields`. `FOLDER_FIELDS` goes.

### 3.2 Engine layers collapsed

| today | proposed |
| --- | --- |
| `Engine`, module `download`/`open`/`resolve`, `dry.plan` | **`Flnd(home)`** (PLAN §3.1): `download preview track untrack forget sync targets files move login logout logins connect` |
| `RunShared`, `shared_browser`, `_BrowserPool`, `_SharedBrowser`, `_SharedCookies`, `_RawProxy`, `_Planned`, grouping in `_run` | **`Run`**: one `with` block's resources (stop trap, history and targets tables, one lazily opened `shared.browser`, a `Session` per source) |
| `SourceRun`, `source_run`, `_make_client`, account `TargetSite`, `_connected`, `_written_back`, 4 login checks | **`Session`**: one source in a run; it checks the login once, enters `connect`, owns the client and the loop, and writes cookies back |
| `job.download`, `_run_download`, `_execute_listing`, `_read*`, `_catch_up_newest`, `_queue_*`, `_Work`, `Resume`, `listing()`, thread-local | **`Job`**: one target. It holds its cursors, so `site.pages` reads them from its `Site`, with no thread-local |
| 8 finder functions (P10) | `Run.find(target)`: one generator used by `download`, `preview` and `sync` (with `seen` and `MAX_SYNC_NEW`) |
| `Options`, `categories_by_source`, `_validate_from`, `wanted`, `selected` | `Options` (frozen record, 5 fields); `--from` is checked once, at resolve time |
| `TargetSite` + `_PacedPage` + tab stack + warm tabs | `Site` (one per Job); tabs, watch and warm move into `shared.browser` (PLAN §2.11) |
| `Ledger`, `Downloads`, `Board` | unchanged (they hold the resume, dedupe and dating logic, and are well tested) |

New flow:
```
Flnd.download ─┬─ resolve + check --from (once)
               └─ with Run(home) as run:                      # trap, stores, lazy browser
                     for target: run.session(source)            # login check, connect, cookies back
                        ├─ finder?  run.find(target) → Job per child
                        └─ Job(session, target, options).download()
                              ├─ Site (fetch, goto, page, pages, warm)
                              ├─ list categories (cursor in Job) → _kept(--time, streaks)
                              ├─ Ledger (history, dedupe, links, dates)
                              └─ Downloads (worker.Pool) → http.download → disk
```
Frames from `download` to a category: `Flnd.download → Run.download → Job.download → Job._list → _kept → category`, which is **6**. Classes: `Flnd, Run, Session, Job, Site, Ledger, Downloads, Board, Result`, which is **9**.

### 3.3 Before/after: three sources

**mht** (HTTP, with its query route). Before (mht.py:49-139, trimmed):
```python
@target(r"mimihentai.moe/manga/*/chapter/{id:\d+}", from_=(), folder="chapters/{order} {title}")
def chapter(self, site, id): ...
@target(r"{id:\d+}", r"mimihentai.moe/manga/{id:\d+}",
        from_=("chapters", "cover"), folder="chapters/{order} {title}")
def manga(self, site, id): ...
@target(r"mimihentai.moe/author/{author:[^/]+}", r"mimihentai.moe/authors/{author:[^/]+}",
        from_=(), folder="chapters/{order} {title}")
def author(self, site, author): ...          # yields Targets: second discovery path
@target(r"mimihentai.moe/genre/{genre:[^/]+}", ..., from_=(), folder="chapters/{order} {title}")
def genre(self, site, genre): ...
@target("?{query}", discovers=True, from_=(), folder="chapters/{order} {title}")
def query(self, site, query): ...
def _search_pages(site, url, params):        # 20 lines around pages(fetch, next_cursor)
```
After:
```python
@target(r"mimihentai.moe/manga/*/chapter/{id:\d+}")
def chapter(self, site, id): ...
@target(r"{id:\d+}", r"mimihentai.moe/manga/{id:\d+}")
def manga(self, site, id): ...
@target("?{query}")
def query(self, site: Site, query: str) -> Iterator[Target]:
    """Manga matching `query`: words, or `author:` `genre:` `parody:` `character:` `chapters:` terms."""
    text, params, chapters = _parse_query(site, query)
    url = f"{API}manga/search" if text is not None else f"{API}manga/advanced-search"
    for page in site.pages(url, params={"q": text} if text else params, cursor="next_cursor"):
        for item in page.get("items") or []:
            if all(item.get("chapter_count") in wanted for wanted in chapters):
                self._remember(item)
                yield Target(self.code, "manga", str(item["id"]))
```
`lookup_genres`/`lookup_authors`/`_resolve_id` stay, because query terms use them. This saves about 50 lines (315 → ~265 est.).

**loi** (HTTP only) gains a query route. History: 51a504e added a loi search route, and e4fc627 removed it (−122 lines in loi.py, −64 in test_loi.py) along with `flnd.query`'s `Param`/`Choice`. The removed code shows the site's API:

| endpoint (from e4fc627) | use |
| --- | --- |
| `GET {API}user/loi/search/{quote(words)}/{sort}` | text search; sort ∈ `latest`, `popular`, `choicest` |
| `GET {API}user/library/loier/id?category_id=&sort=` | by author |
| `GET {API}user/library/tag/id?tag_id=&sort=` | by tag |
| `GET {API}user/library/loiers`, `user/library/tags?group_id=1..3` | name → id lookups |
| page shape `{data: [...], pagination: {total}}`, `?page=1..` | stop on an empty page, on no new ids, or at `total` |

Before (loi.py:55-61):
```python
@target(r"{id:\d+}", r"loix.cc/lois/{id:\d+}", r"moegoat.com/lois/{id:\d+}",
        from_=("photo", "archive"), folder="{author}/{id} {title}/{part}")
def post(self, site, id): ...
```
After:
```python
@target(r"{id:\d+}", r"loix.cc/lois/{id:\d+}", r"moegoat.com/lois/{id:\d+}")
def post(self, site, id): ...

@target("?{words}")
def search(self, site: Site, words: str) -> Iterator[Target]:
    """Posts matching search words, newest first: `loi?example words`."""
    url = f"{API}user/loi/search/{quote(words, safe='')}/latest"
    seen: set[str] = set()
    for number in itertools.count(1):
        body = site.fetch(url, params={"page": number}).json
        ids = [str(item["id"]) for item in body.get("data") or [] if "id" in item]
        if not set(ids) - seen:              # an empty page, or the site repeating its last one
            return
        seen.update(ids)
        yield from (Target(self.code, "post", id) for id in ids)
```
Words only for now (see Q3). Each found post downloads like `loi:<id>`, so the VIP quota logic is unchanged.

**tik** (browser). Its decorators don't change. The duplicated stop rules go (P12). Before (tik.py:276-290):
```python
for data in self._posts:
    items = items_of(data)
    ...
    while queue:
        yield queue.popleft()
    old = older_streak(items, since, old)
    has_more = isinstance(data, dict) and data.get("hasMore")
    no_more = isinstance(data, dict) and "hasMore" in data and not has_more
    if no_more or old >= OLD_STREAK or _hit_saved(items, wanted):
        return True, old
```
After:
```python
for data in self._posts:                     # site.page.watch(..., until=page.scrolled) (PLAN §2.11)
    ...
    while queue:
        yield queue.popleft()                # the job closes this generator at 5 older or 5 saved
    if isinstance(data, dict) and data.get("hasMore") is False:
        return
```
`_hit_saved`, `older_streak` and `OLD_STREAK` go from tik, ins and twt, which saves ~70 lines. `caught_up` leaves the API. The engine closes the generator on the 5th saved or older post (`_queue_listed`, `_Kept`). So no extra scroll starts, but a new test must prove it (§6).

### 3.4 Before/after: the engine's download path

Before (engine.py:106-144, 931-956, abridged):
```python
def download(self, targets, *, categories=None, time=Range(), limit=None, dry=False, sync=False, again=False):
    ...
    cats_map = categories_by_source(categories, resolved, self.sources)
    _validate_targets(self.sources, categories, resolved)
    base = Options(since=..., until=..., limit=limit, again=again, stop_at_saved=sync and not again)
    by_source = {code: replace(base, categories=cats_map.get(code)) for code in ...}
    with cli.stopping() as stop:
        if dry: return self._finish_report(self._dry_run(resolved, by_source, span, stop), stop)
        if any(self._is_query(t) for t in resolved):
            reports = self._download_mixed(resolved, by_source, span=span, sync=sync, stop=stop)
            return self._complete_download(resolved, reports, sync=sync, stop=stop)
        return self._download_normal(resolved, by_source, sync=sync, stop=stop)

def _run_site(run_settings, board, reports, work):
    source, group = work
    with source_run(run_settings, source) as run:
        account = TargetSite(run, Target(source.code, ACCOUNT, ""))
        if not source.logged_in(account) and source.requires_login: ...   # copy 3 of 4
        with _written_back(run), _connected(run, account):
            for planned in group:
                reports[planned.number] = job.download(run, planned.target, board, planned.options)
```
After:
```python
def download(self, targets, *, only=None, time="", limit=None, sync=False, again=False) -> Result:
    """Download each target; `only` names categories, as `--from` does."""
    asked = self._ask(targets, only=only, time=time, limit=limit, again=again)   # resolve + check once
    with Run(self.home, self.sources) as run:
        result = Result(tuple(item for target, options in asked
                              for item in run.download(target, options)))
    self._record(asked, sync=sync)
    return result

class Run:
    def download(self, target, options) -> Iterator[Report]:
        session = self.session(target.source)          # login, connect, cookies back: once
        if session.route(target).finds:
            for child in self.find(session, target, options):
                yield Job(session, child, options.child).download()
        else:
            yield Job(session, target, options).download()
```
`preview` is the same walk with `Job.plan()` in place of `Job.download()`, which replaces `_dry_run`, `_dry_one`, `_log_dry` and `dry.plan`. `sync` passes stored options plus `stop_at_saved=True`.


### 3.5 The three roles in code

**Login: the source owns it.** Today the flow is split. `auth.py` (474 lines) does browser and file imports, origin tracking, expiry, write-back, logout and the vault, while `Source.login`/`login_methods`/`logged_in` (source.py:931, 1064, 1094) do part of it, and the engine checks the login in 4 places (P8). Proposed:
```python
class Instagram(Source):
    def login(self, auth: Auth, how: str, /) -> None:
        """Log in by `how`: "chrome:Default", a cookies.txt path, or "qr"."""
        auth.cookies = auth.browser_cookies(how) if ":" in how else auth.cookie_file(how)

    def logged_in(self, auth: Auth, /) -> bool:
        return "sessionid" in auth.cookies
```
- `Auth` is the one object a source gets for credentials:
  - its own encrypted vault (`auth.secrets`);
  - `auth.cookies` (a `Jar`, saved on set);
  - helpers that read cookies from a browser or a file, and `auth.ask(...)` for prompts;
  - `auth.expiring`.
- Sources with other credentials (tlg's session, an API key) store them in `auth.secrets` themselves.
- The engine only calls `source.login(auth, how)`, `source.logout(auth)` and `source.logged_in(auth)`: once per run, in `Session`.
- `auth.py` shrinks to the vault and the `Auth` helpers, about 250 lines (est.). The default `Source.login` covers the browser and file case, so most sources write nothing.

**Listing: a target names a listing.** It's unchanged in spirit. Each `@target` route returns the listing (`Iterator[Post]`, or categories that do), and finders return `Iterator[Target]`. A source never sees history, files or progress: the cursor lives in `Job` (§3.2), and the early stops live in the job only (P12).

**Downloader: the engine, a pipeline the caller can pull from.** `download` yields each file as it lands, so a script can act on it at once:
```python
import flnd, flnc

fl = flnd.Flnd(".flnd")
for saved in fl.download("ins:example_user", only=["posts"]):  # Saved(path, target, post, kind)
    if saved.kind == "image":
        flnc.compress(saved.path, to="auto")
```
- The CLI is that loop too: it feeds `ui.live` rows from the same events, so the progress display is a consumer, not a callback.
- `report = flnd.Report.of(fl.download(...))` (or `list(...)`) gives the totals for callers who only want them.
- Renaming a file (flnc changing `.jpg` to `.avif`) goes back through `fl.move(old, new)`, so history stays true.

| | today | proposed |
| --- | --- | --- |
| where login logic lives | `auth.py` + `Source.login` + 4 engine checks | `Source.login/logout/logged_in` + an `Auth` helper; 1 engine call site |
| what a listing may touch | `site.state`, history through `caught_up`, a thread-local cursor | the site only |
| how a script sees progress | it can't: `download` returns a `Report` at the end | it iterates `Saved` events as they happen |

---

## 4. flnc and flns: one common shape, smaller APIs

Neither exists yet. Everything here comes from PLAN §4 and §5.

### 4.1 What the three tools share

| concern | flnd (PLAN §3) | flnc (§4) | flns (§5) | proposal |
| --- | --- | --- | --- | --- |
| home + store | `Flnd(".flnd")` | `Flnc(".flnd")`, `metadata/flnc.db` | `Flns(".flnd")`, `flns.db` + `cache/flns/` | one layout record exported by flnd (`flnd.Home`: `media`, `metadata`, `cache`); flnc and flns may import flnd's public API. One db per tool under `metadata/`, a cache under `cache/<tool>/` |
| selection | `fl.files.filter(target=).sort("date")` | `paths…`, `--under` | `ns.photos.filter(target=, kind=)`; `--from-target --time --kind --limit --order`; positional file/folder/target | **one selection**: `fl.files` (a `Query`) plus a `flnd.Selection` record (5 fields, so a record by the AGENTS rule). `ns.photos` goes; flnc takes paths or `fl.files` |
| skip if inputs unchanged | history rows (step 7) | `File` row: stat + `level` + `tools`; extract table | `Output` row: `inputs` digest + `options` + `tools`; frame/piece digests | **`shared.store.Memo`**: `memo.fresh(key, inputs, **meta) -> bool`, `memo.record(...)`, keyed by input stat/digest, an options digest and a tools string. flnc files, flnc extracts and flns outputs all use it (flnd's history keeps its own key) |
| preview / `--dry` | `preview`, `--dry` | `preview`, `--dry` | `preview`, `--dry` | the library has `preview` everywhere (it returns `list[Item]`); the CLI has `--dry`, which calls it. No `dry=` switch, per rule 5 |
| forget | `forget` | `forget` | `forget` | same signature: paths or targets |
| tools | — | `tools`, `install` | `tools` | **move to shared**: `media.tools` (`Query[Tool]`) and `media.install` (§2.14 already finds tools through `sh.find` with pinned specs); each CLI mounts one shared `tools [--install]` command |
| result | `report.ok/files/failed` | `report.saved/files/failed` | `Result(path, skipped, seconds)` | **`shared.Report`** (msgspec Struct): `files`, `failed`, `skipped`, `seconds`, `ok`; flnc adds `saved`, flns adds `path` and `duration` |
| progress | the engine's `Board` writes `ui` rows | implicit | `Flns(progress=…)` | one rule: verbs write `ui.group` rows, and only the CLI wraps `ui.live`. `Flns(progress=)` goes |
| CLI globals | `--ui -v --home` | same | same | already `shared.cli` (§2.16); `--dry` and `--again` become shared annotated flags too |

**Naming clashes this finds (one word, one meaning):**
- `--from` means categories in flnd, while flns's `--from-target` means a target. Use `--target` in flns.
- `level` means `high|max` quality in flnc compress, but a 7z level in `archive` (§2.13). Drop the archive one (Q11).
- flnd's library `only=` is `--from` on the CLI (Q9).

### 4.2 flnc: cut to the minimum

| item | planned | proposed | why |
| --- | --- | --- | --- |
| `compress` | §4.1 | keep | the tool's point |
| `preview` | §4.1 | keep | shared shape (§4.1 above) |
| `archive`, `extract` | §4.1–4.2 | **CLI only**, over `shared.archive.create` / `open().extract` (§2.13) | the library layer would only forward; the extract record moves to `Memo` |
| `files` | §4.1 | keep (`Query[File]`) | |
| `saved` | §4.1 | **cut**: `Report.saved` per run; `flnc files` prints the total | a one-line sum over `files` |
| `forget` | §4.1 | keep | |
| `tools`, `install` | §4.1 | **move to `shared.media`** | flns needs the same |
| records | `Report`, `Plan`, `Tool`, `File`+`State`, extract table (**6**) | `File`+`State`, `Item` (shared), `Report` (shared) (**3**) | |
| `compress` options | `to level jobs` + `--dry --again` (**5**) | `to level again` (**3**) | `jobs="cpu"` is the right default (§4.5); `dry` is `preview` |
| other options | `dest password split`, `passwords remove`, `--level`, `--into`, `--state --under` (**9**) | CLI only, 8 (`--level` goes, see Q11) | |
| library members | **9** | **4** (`compress preview files forget`) | |

### 4.3 flns: cut to the minimum

| item | planned | proposed | why |
| --- | --- | --- | --- |
| `slideshow` | §5.3 | keep | |
| `card`, `collage` | §5.3–5.4 | **one `card`**; `layout` covers `strip-3/4 grid-4 polaroid postcard grid-3x3 mosaic` | both make one image from N photos with a `Theme` and the same pure `layout()→boxes, draw()` (§5.6); `stills` already holds both (§5.9) |
| `album` | §5.3–5.4 | keep | multi-page PDF, a title page, pagination |
| `photos` | §5.3 | **cut**: `fl.files.filter(kind="image")` | duplicates flnd's selection |
| `preview`, `outputs`, `forget` | §5.3 | keep | shared shape |
| `tools` | §5.3–5.4 | shared | |
| `transitions` command | §5.4 | **cut**: `--transition`'s help lists the names read from ffmpeg (§5.2) | a command that only prints choices |
| `Show` fields | 12 (§5.3) | **10**: drop `motion` and `crop` until "later" lands (only CENTER in the MVP) | YAGNI; `seed` stays but defaults from the inputs |
| `Page` record | §5.3 (`size`, `row_height`) | **cut**: `album(photos, dest, page="a4", title=)`; the row height is derived | a 2-field record for 1 verb |
| `Photo` record | §5.3 (7 fields) | **cut**: flnd's file record + `media.probe` (rotation, §5.7) | duplicates `fl.files` rows |
| `Output` row | §5.8 | `Memo` | §4.1 above |
| `Flns(progress=)` | §5.3 | cut | §4.1 above |
| `--hw` | §5.4 | fold into `--codec` with choices `h264`, `hevc`, `av1`, `hw` | one choice, not two flags |
| enums | `Motion Fit Crop Codec` (**4**) | `Fit Codec` (**2**) | |
| library members | **9** | **6** (`slideshow card album preview outputs forget`) | |
| CLI commands | **8** | **6** (`slideshow card album outputs forget tools`) | |
| slideshow CLI options | 13 | 10 (no `--motion`, no `--hw`; `--seed` stays) | |

### 4.4 What moves to shared and what stays

- **Into shared** (step 9, or step 3 for `Memo`): `store.Memo` (~80 lines est.), `shared.Report` (~30), `media.tools`/`install` (already planned in §2.14), shared `--dry`/`--again` flags in `cli` (~20).
- **Into flnd's public API** (step 6): `Flnd.files`, `Flnd.move` (flnc needs it), `flnd.Home`, `flnd.Selection`.
- **Stays per tool**: each pipeline (flnc encode/verify/place; flns normalize/pieces/concat), its record (`File`, `Show`, `Theme`), its CLI verbs.


### 4.5 flnc and flns as plain libraries (the user's direction; supersedes 4.2–4.3 where they differ)

No `Flnc`/`Flns` class and no home. Each is a module of verbs over paths, and the caller decides where files come from (flnd, a folder, anything). The "skip if done" memory is an optional `store.Memo` the caller passes; the CLI passes one under `--home`.

```python
import flnc
r = flnc.compress("a.jpg", to="auto", level="high")      # Result(path, before, after, kind, kept)
flnc.compress(path, memo=memo)                            # skipped if the memo says it's done
flnc.plan("a.jpg")                                        # what compress would do (the CLI's --dry)
flnc.archive(["photos/"], "photos.7z", password="pw")     # thin over shared.archive
flnc.extract("a.zip", passwords=["pw"])

import flns
flns.slideshow(paths, "show.mp4", style=flns.Show(transition="random"), music="song.mp3")
flns.card(paths[:4], "strip.jpg", layout="strip-4", theme=flns.themes.film)
flns.album(paths, "2026.pdf", page="a4", title="2026")
flns.plan(paths, style=show)
```

**Composed with flnd in a script:**
```python
fl = flnd.Flnd(".flnd")
memo = store.open(".flnd/metadata/flnc.db")[flnc.Done]
for saved in fl.download("ins:example_user"):
    result = flnc.compress(saved.path, memo=memo)
    if result.path != saved.path:
        fl.move(saved.path, result.path)                  # keep flnd's history true
flns.slideshow([f.path for f in fl.files.filter(target="ins:example_user", kind="image").get()],
               "example_user.mp4")
```

| | §4.2–4.3 plan | plain libraries |
| --- | --- | --- |
| flnc public members | `Flnc` + 9 → 4 | 4 functions (`compress plan archive extract`) + `Done` row + `Result` |
| flns public members | `Flns` + 9 → 6 | 4 functions (`slideshow card album plan`) + `Show`, `Theme`, `themes` |
| home, store, `files`/`outputs`/`forget` | in the class | the CLI only (a `--home` memo, `flnc files`, `flns outputs`, `forget`); the library doesn't need them |
| flnd coupling | flnc calls `Flnd.move` itself | none: the script (or the CLI's `--flnd` mode) calls `fl.move` |
| lines (est.) | ~600 / ~1,250 | ~500 / ~1,150 |

The CLIs stay thin: `flnc compress <paths…>` builds the memo from `--home`. When the paths are inside a flnd home, it calls `fl.move` for renamed files, so flnd's history stays true, as PLAN §4.4 requires.

---

## 5. Comparison table

| measure | flnd today | flnd proposed | flnc planned → proposed | flns planned → proposed |
| --- | ---: | ---: | ---: | ---: |
| lines, engine side | 6,811 | ~4,200 est. | ~850 → ~600 est. | ~1,650 → ~1,250 est. |
| lines, sources | 4,273 | ~4,000 est. (+loi query ~15) | — | — |
| lines, total | 11,084 | ~8,200 est. | | |
| author concepts | 102 | 90 | — | — |
| route behaviours | 5 | 3 | — | — |
| `@target` options | 4 | 1 | — | — |
| frames per download | 15 | 6 | — | — |
| classes per download | 16 | 9 | — | — |
| library members | `Engine` 15 + 3 functions | `Flnd` ~14 | 9 → 4 | 9 → 6 |
| records/types | 5 (`Report TargetReport TargetInfo AuthInfo Options`) | 4 (`Result Target Login Options`) | 6 → 3 | 9 + 4 enums → 4 + 2 enums |
| options counted | `download` 7 | 6 | 14 → 11 | 32 CLI → 24 |
| public `flnd` names (`api.txt`) | 185 | ~165 est. | — | — |
| tests touched (of 531 flnd tests) | — | ~6 deleted, ~95 rewritten, ~430 unchanged, ~12 new | new | new |

Where the 102 → 90 comes from:
- names 20 → 16: authors don't name `Route` or `Category`; `pages` becomes `site.pages`; `caught_up` goes;
- `@target` options 4 → 1;
- ClassVars 16 → 15 (`browser`), derived 4 → 3 (`single_kinds`);
- `Site` 14 → 15: `try_fetch` becomes `fetch(check=False)`, and `pages` and `warm` are added;
- `Post` 9 → 8: `filed` becomes engine-private;
- common helpers 6 → 3: watch becomes `page.watch`.

The bigger gain is hidden coupling: no thread-local, no `getattr` into private `Site` members, and one finder path.

---

## 6. Proof the simplification keeps behaviour

### 6.1 Behaviour that stays, and its guards

| behaviour | existing tests (unchanged unless noted) |
| --- | --- |
| resume from a saved cursor | test_job.py:1007, 1023, 1038, 1049, 554 (rewritten to read the cursor from `Job`, assertions kept) |
| sync early stop (`caught_up`) | test_job.py:453, 482, 516, 528, 565, 576; test_sync.py:187 |
| older-than-since stop | test_job.py:386, 397 |
| same file across categories | test_job.py:1188, 1197, 1201, 1213, 1256 |
| dates and redating | test_job.py:1094-1157 (7 tests) |
| login, public access, cookie write-back | test_engine.py:205, 228, 234, 246, 269, 447, 461, 649-684 |
| Ctrl-C (soon/now) | test_engine.py:343, 367, 920; test_job.py:757, 774, 842, 896; test_site.py:176 |
| async source on its loop | test_engine.py:401, 424; test_job.py:600 |
| browser: one window, tabs, warm | test_site.py:266-301, 440, 463, 481 (move with the code to `tests/test_browser.py` once `shared.browser` owns tabs) |
| `--from` refused on a single post | test_job.py:123, 134; test_engine.py:594 |
| target resolution | test_target.py (20), test_source.py (50; the `shown` tests 244/248 become literal-pattern and `forms` tests) |
| finders (ins:me, mht query) | test_ins.py:802, 811, 839; test_mht.py:145, 159, 211, 244; test_engine.py:733, 748, 759, 775 |
| every source's output names | test_{fbk,ins,loi,mht,tik,tlg,twt}.py (221 tests through `FakeSite`/`collect`) |

### 6.2 Tests that go with removed code
- test_mht.py:194 `test_author_and_genre_page_routes`.
- test_tik.py:478, test_twt.py:348 and test_ins.py:609 patch `caught_up`. They are replaced by one engine test.
- About 3 cases in test_engine.py and test_source.py that pin the removed `discovers`, `from_` and route `folder` options.

### 6.3 New tests
1. On a newest-first listing with saved posts 1-5, the job closes the generator before the next batch: no extra scroll or page fetch (a `FakeWatch` counter, as test_ins.py:609 does). This guards P12.
2. One finder path: download, preview and sync of `mht?…`, `ins:me` and `loi?…` give the same children; on sync, already-seen children are skipped and at most 25 new ones run.
3. loi search: pagination stops on an empty page, on a repeated page, and on Ctrl-C; each child is `loi:<id>` (with `httpx.MockTransport`/`FakeSite`).
4. A folder template error raises `TypeError` at class creation.
5. One login check covers download, preview, sync and finder runs.
6. The browser runs on the job's thread under `ui.live` (PLAN §2.17). A lazy-file refresh from a download thread goes through `Pool.call` (§2.7).
7. flnc/flns: `Memo.fresh` is false when an input's mtime, the options or the tools string changes; flnc's `Flnd.move` makes flnd's `_held` find the renamed file (PLAN §7).

### 6.4 Risks

| risk | where it could break | mitigation |
| --- | --- | --- |
| resume | cursors move from a thread-local into `Job` | keep test_job.py:1007-1049 unchanged in substance; one cursor per category |
| sync early stop | deleting source-side `_hit_saved` could add one scroll per stop | new test 1; if a live run shows an extra batch, restore it as one public `site.enough` rather than 3 copies |
| dedupe across categories | `Ledger` is untouched | test_job.py:1188-1256 |
| dates | `_Kept`/`Ledger.redate` are untouched | test_job.py:1094-1157 |
| login | 4 checks become 1 | test_engine.py login set, plus new test 5 |
| Ctrl-C | fewer frames between trap and job | test_engine.py:343/367/920 and the test_job.py Ctrl-C set |
| browser thread | dropping `_RawProxy` assumes Playwright runs only on the job's thread | `ui.live` keeps work on the calling thread; refreshes use `Pool.call`; new test 6; land this only after `shared.browser`/`worker` (step 4) |
| discovered children not saving cursors (`Options.query`, job.py:1062) | the unified finder must keep this | test 2 asserts no cursor rows for children |

---

## 7. Migration steps

Each step is one commit, with `check` green and `api.txt` regenerated. Mechanical steps go to a Haiku subagent.

**Now, before or alongside the `shared` refactors.** These touch flnd only, so they don't conflict with the work on `shared`; the only overlap is `api.txt`, a mechanical merge.

| # | commit | removes | tests |
| --- | --- | --- | --- |
| A | `refactor:drop redundant route options and the dead browser flag` | 7 `from_`, 7 route `folder`, mht `discovers`, `Source.browser`, `Route.params` getattr | none change |
| B | `refactor:remove mht author and genre routes` | 2 routes, `job._run_discovered`, cross-source `_run_child` | −1 (test_mht.py:194) |
| C | `refactor:check --from once and folder templates at class creation` | `targets._validate_*`, `FOLDER_FIELDS`, `options.selected` | ~3 adjusted |
| D | `refactor:one login check and one finder path in the engine` | P8, P10, P11 | test_engine.py finder tests rewritten |
| E | `feat:add loi search route` | — | +4 (new test 3) |
| F | `refactor:keep the early-stop rules in the job only` | P12, `caught_up` | −3, +1 (new test 1) |
| G | `refactor:allow literal finder patterns and drop shown` | P4 | test_source.py:244/248 |

**After PLAN §6 step 4** (`worker`, `http`, `browser`):
- H `refactor:site on shared http and browser`: `_make_client` → `http.Client(rate=, retry=)`; `_BrowserPool` and its proxies → one `shared.browser` per run; `feed.Responses` → `page.watch`; `warm_tabs` → `site.warm`.

**PLAN §6 steps 6-8:**
- 6 `refactor:` `Flnd`, `Run`, `Session`, `Job`, records, `move`, `site.pages` (cursor in `Job`), `flnd.Home`, `flnd.Selection`, `Flnd.files`. This needs step 3 (`store`).
- 7 `perf:` history and `targets` on typed `store.Row`s (P19), the `_held` fix, segmented downloads, the import-time test.
- 8 `feat:` CLI on `Flnd` and `ui.live`; README.

**PLAN §6 steps 9-19, adjusted:**
- 9 `feat:` `shared.media` **plus** `media.tools/install`, `store.Memo`, `shared.Report`, shared `--dry`/`--again` flags.
- 10 `feat:` flnc library (`compress preview files forget`), its `File` row on `Memo`, the image pipeline, and the CLI.
- 11 `feat:` flnc video and audio.
- 12 `feat:` `flnc archive`/`extract` as CLI commands over `shared.archive` (small; it can fold into 10).
- 13 `docs:` README and AGENTS.md.
- 14 `feat:` `media.render/concat/fit/font`, probe rotation.
- 15 `feat:` flns library: selection through `fl.files`, outputs on `Memo` (smaller than planned).
- 16 `feat:` flns slideshow (without motion or crop).
- 17 `feat:` flns `card`, covering collages: layouts and themes.
- 18 `feat:` flns album PDF.
- 19 `docs:` flns README. Later: motion, crop, `loop`, album HTML, `reel`, `recap`.

---

## 8. Open questions (each with a recommended default)

| # | question | default |
| --- | --- | --- |
| Q1 | finder flag: `finds=True` on `@target`, or a separate `@finder` decorator? | `finds=True`: one decorator; `?{…}` implies it |
| Q2 | PLAN §3.2 writes `key, folder = "exm", "{user}"`, where `key` means the source code; today `key` is the history-key format (source.py:939) | keep `code` for the source and `key` for history; fix the PLAN example |
| Q3 | loi query scope: words only, or also `author:`/`tag:`/`sort:` (endpoints in §3.3)? | words only, newest first; add terms when asked |
| Q4 | delete source-side streak checks (P12)? | yes, with new test 1 and a live check on the test accounts |
| Q5 | one HTTP client per source (token bucket per host) or the two today (paced site, unpaced CDN)? | one, per PLAN §2.8 |
| Q6 | the browser on the job's thread or on its own thread? | the job's thread; `Pool.call` for refreshes |
| Q7 | finder sync cap `MAX_SYNC_NEW = 25` (engine.py:28) | keep |
| Q8 | `--from` is **required** for profiles (options.py:72), but source.py:921 says "without `--from`, a target gets every one", and PLAN §3.3 shows `[--from]` | keep it required (the code and test_commands.py:127); fix the comment |
| Q9 | library `only=` vs CLI `--from` | `only=` in the library and `--from` on the CLI, as PLAN §3.1/§3.3 say; document it once |
| Q10 | merge flns `album` into `card` too? | no: pagination and PDF differ |
| Q11 | flnc `archive --level` clashes with compress `--level` | drop it; `shared.archive` uses its own best default |
| Q12 | `Memo` name (flnd's `job.Ledger` already means the history) | `store.Memo` |
| Q13 | `fl.download` yields `Saved` events (a script compresses on the fly) instead of returning a `Report` | yes, a generator; `flnd.Report.of(events)` for totals; the CLI consumes the same events for `ui.live` |
| Q14 | `Source.login` owns credentials entirely, with an `Auth` helper object | yes; the default implementation covers browser and file cookies, so most sources write no login code |

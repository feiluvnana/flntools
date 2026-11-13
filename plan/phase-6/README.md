# Phase 6: lean and simple

Phase 6 makes flntools quicker to set up, the same on every OS, smaller to read, and calmer to watch. It is based on an audit of the whole repository on 2026-11-12, and on the problems the user named.

**The [redesign](redesign.md) is part of Phase 6.** It was the Phase 7 proposal, and the user merged it into Phase 6 on 2026-11-12. It gives sources, the engine and `shared` one fluent, consistently named API. **It wins over every other file here, [simplify](simplify.md) included.** Where it says nothing, the other files hold.

| Problem | Where it's solved |
| --- | --- |
| the source and engine APIs should be simple, and `shared` fluent, concise and consistent, with verbs for actions and nouns for reads | [redesign](redesign.md): routes and categories as methods, typed `Post`/`File`, a 13-member `site`, `Engine` with one method per command, API conventions with a test, and `shared` redesigned module by module |
| ideas from gallery-dl and download managers | [redesign](redesign.md#b5-what-flnd-takes-from-gallery-dl): early stops, `sync` stopping at saved posts, cookie write-back; downloads that resume, split, keep 6 connections per host and refresh expired links |
| every credential encrypted, cookies included | [redesign](redesign.md#b7-every-credential-encrypted) |
| a post link with `--from`, and categories without it | [redesign](redesign.md#a4-categories-are-methods): `--from` with a post link is an error; without `--from`, every category |

| `make setup` takes a long time | [setup](setup.md): `uv sync` with a lockfile; the 305 MB browser download only when there is no Chrome, and never the unused headless shell |
| make isn't well supported on Windows | [setup](setup.md): every task in a stdlib `scripts/dev.py`; the Makefile is a one-rule shortcut with no shell syntax |
| the session API is bloated and its defaults aren't sensible | [session](session.md): 35 members become 26; scrolling ends on time, not a count, with no 200-scroll cap; page loads are paced and checked |
| Instagram's tagged tab is blocked when each post's page is opened | [ins](ins.md): posts read through the media info API, skipped before any request when already saved or out of range, paced, and every throttle waited out; then **API first for every source**: ins, fbk and tik replay the site's own pagination request from the page instead of scrolling, and twt refreshes stale query IDs |
| the download rows blink, a prompt shows a white block, and log lines color only their glyph | [ui](ui.md): frames overwrite in place inside synchronized output with the cursor hidden; short-lived rows are never drawn; prompts draw their own cursor; a line takes its level's color unless its text has its own style |
| simplify everything | [api](api.md): every public API, with its change or none; flnd's own APIs lose what flnd doesn't use, and `shared`'s are judged by usefulness |
| the `Source` API has too many fields | [source](source.md): 18 fields and 7 hooks become 8 and 5; `categories` holds its defaults, `prepare` replaces `headers` and `sign`, and fields no source sets become engine constants |
| too many options and commands | [cli](cli.md): `download` keeps `--from`, merges `--since` and `--until` into `--time <since>,<until>`, and gains `--image`, `--video`, `--audio` `<format>,<level>`; `--unpack`, `--zip`, `--locale` and `--timezone` go; `sync` becomes `sync`, which only repairs and fetches new posts, with no options; `login` asks for everything; `logout <sources…>` or `all`; `install-browser` goes, and flnd uses Chrome, offering Chromium only when Chrome is missing |
| the source scripts are long and hard to read | [scripts](scripts.md): extension guessing deleted (the content already decides it); `save_post`, `day` and `meta_results` in `flnd/sources/common.py`; 1,378 lines become about 1,280, each script reading top to bottom |
| some files are saved without their date | [dates](dates.md): the engine puts the date in the name from `save(date=)`, sets every file's modified time to it, and falls back to the CDN's `Last-Modified`, then the download day |
| the pace should be as small as the sites allow | [pace](pace.md): each site's own rate headers, Instagram's known budgets (275 GraphQL calls per 10 min, 75 `/api/v1/` per 11 min), and a gap learned per source above a floor (twt 0.5 s, tik 1 s, fbk 1.5 s) |
| adopt gallery-dl's architecture | [architecture](architecture.md): a source yields `Post`, `File` and `Queue` items with metadata; one job filters, names files from format strings, checks the history, downloads, and runs the postprocessors (compress, classify, mtime) |
| optimize the flows from what gallery-dl has learned | [flows](flows.md): TikTok's best non-watermarked file, TikTok's list APIs direct first, X's 50-per-page timelines with its full feature flags, Instagram's `product_type` grid rule and headers, browser-like header order for every direct call, and `/accounts/suspended/` as a hard stop |
| simplify everywhere, and no download limit | [simplify](simplify.md): no limit; one item type; one back-off and one learned speed; no history migrations; one retry layer and one atomic writer; fewer modules. **It wins where another file disagrees**, and holds the final numbers |
| ~~a classifier for photos and videos~~ (removed 2026-11-13) | [classify](classify.md): `media.classify(path)`, a small CLIP-style model on ONNX Runtime, downloaded on first use; `--classify` groups files in `labels/<label>/` hard-link folders, never renaming them |
| the code needs more comments | [comments](comments.md): a docstring standard for every module, class and function, and more *why* comments at every rule a reader has to work out |

The audit also found 15 bugs, and the gallery-dl comparison a 16th, among them corrupted downloads on Windows, failed targets shown as ok with exit status 0, and Twitter's login sent to its CDNs. They are in [fixes](fixes.md).

## Order

Each step ends with `check` green and is committed on its own. The [redesign](redesign.md) replaced the old steps 3 (its architecture part), 4 and 6 on 2026-11-12, and added steps; [redesign › Where each part is built](redesign.md#where-each-part-is-built) has the detail.
1. [fixes](fixes.md): the bugs, each with a test that fails first. **Built** (`dc471b0`).
2. [setup](setup.md): `uv.lock`, dependency groups, `scripts/dev.py`, the thin Makefile, and no browser download. **Built** (`f13c182`).
3. [cli](cli.md) and [pace](pace.md): four commands with `--time` and `--image`/`--video`/`--audio`, saved settings, `flnd/auth.py`, Chrome only, `pace.py`, `compress.py` and `files.py`. **Built** (`e8aa96c`). Every category without `--from`, and `--from` refused for a single post, are in the working tree, not yet committed.
4. [redesign › the `shared` API](redesign.md#part-c-the-shared-api): the conventions and their test, the API snapshot, the error tree, then each module redesigned; `http.fetch` and `http.Response`. It replaces [api](api.md)'s tables; api's `__all__` rule stays.
5. [redesign › routes](redesign.md#a3-targets-are-routes): `flnd.source`, `@target` patterns, `unsupported`, generated forms.
6. [redesign › categories, posts, `site` and the job](redesign.md#a4-categories-are-methods), and the six sources on them. It replaces [architecture](architecture.md), [source](source.md), and [scripts](scripts.md)' naming and saving. It takes in [session](session.md)'s rules (`goto` checked and paced, scrolling on the page's time, requests paced) and [dates](dates.md) (the engine dates every file).
7. [redesign › the engine API](redesign.md#part-b-the-engine-api) and [every credential encrypted](redesign.md#b7-every-credential-encrypted).
8. [redesign › downloads](redesign.md#b6-what-flnd-takes-from-download-managers-idm): `http.download` with resume and split downloads, 6 connections per host, expired links refreshed, graceful Ctrl-C.
9. [ins](ins.md) and [flows](flows.md): tagged and reposts through the API, then API first for ins, fbk, tik and twt, with what gallery-dl does, and live runs of the test accounts. Built on the redesign's `site.fetch(…, in_page=True)` and `pages()`.
10. [redesign › the rest of gallery-dl's lessons](redesign.md#b5-what-flnd-takes-from-gallery-dl): `sync` stopping at saved posts, cookie write-back, `scripts/dev.py try`.
11. ~~[classify](classify.md): `--classify`, with the classifier as `Media.labels`.~~ Built (`9a3f720`), then **removed 2026-11-13** by the user: no `--classify`, no `Media.labels`, no `classify` extra.
12. [ui](ui.md): the live area (no blinking, and cheap: only changed rows drawn, by one thread, only when something changed), the prompt cursor, and whole-line log colors.
13. [comments](comments.md) and the redesign's docs: every file brought to the docstring and comment standard; `flnd/source.py`'s "writing a source"; plan/README.md's Naming section replaced by the [conventions](redesign.md#2-api-conventions).

[simplify](simplify.md) applies to every step, each change in the step that touches its file, except where the redesign says otherwise.

## The WIP commit

The work-in-progress commit on `main` ("sussy") tried items of this phase: a Windows block in the Makefile, the `typecheck` task renamed `type`, `session.downloaded`, ins image posts saved from the grid with their date from the `pk`, and scroll defaults of 0.2 s and 1,000 scrolls. Its code is taken back out in the commit that adds this plan, so `main` is the last green Phase 5 code plus the plan. Each piece comes back in its step, changed where the audit found a problem:

| WIP piece | Phase 6 |
| --- | --- |
| the `ifeq ($(OS),Windows_NT)` Makefile | replaced by `scripts/dev.py` ([setup](setup.md)) |
| `make type` | kept as the `type` task |
| `session.downloaded(key)` | renamed `saved`, then replaced by `Post.files` as a function the job calls only for new posts ([simplify](simplify.md#1-one-item-type-post)) |
| saving grid images, dated by `pk` | kept, with an allowlist and a full-size check ([ins](ins.md#3-save-from-the-grid-only-what-the-grid-really-has)) |
| scroll `delay=0.2`, `max=1000` | not kept: scrolling waits for the page's progress above a learned floor, and ends after 10 s with no progress ([session](session.md#scrolling-on-the-pages-own-time)) |
| the Protocol's `delay=2.0, max=200` against the code's `0.2, 1000` | gone with the new signature |

## Decisions

The redesign's own decisions, some still open, are in [redesign › Decisions](redesign.md#decisions). Ask the user before building the step that needs an open one. These were all decided by the user:
1. ~~Remove `media`'s unused API?~~ **Decided 2026-11-12: keep it.** `shared` is judged by usefulness, not by flnd's calls ([api](api.md)).
2. ~~Run the browser headless by default?~~ **Decided 2026-11-12: no.** The browser stays visible by default, as today.
3. ~~Color `info` lines too?~~ **Decided 2026-11-12: yes.** Every level colors its whole line, `info` in cyan ([ui](ui.md#whole-log-lines-in-their-levels-color)).
4. ~~Which classifier model?~~ **Decided 2026-11-12: MobileCLIP-S0**, or OpenCLIP ViT-B/32 if its licence doesn't allow flnd's use; checked before step 11 ([classify](classify.md#the-model)). **Checked 2026-11-12: OpenCLIP ViT-B/32 (LAION-2B, MIT)**, as MobileCLIP's weights are for research only ([classify › Licence](classify.md#licence-and-model-file-checked-2026-11-12)).
5. ~~A `nsfw` label?~~ **Decided 2026-11-12: yes**, with a higher threshold ([classify](classify.md#labels)).
6. ~~Is `classify` required or an extra?~~ **Decided 2026-11-12: an extra**, which `media.classify` offers to install the first time it's used, as Chrome's Chromium and the model are ([classify](classify.md#flnd)).

Still to check while building, not decisions: the live checks in [ins](ins.md#live-checks) and [source](source.md#tests).
8. ~~Keep `--classify` and `Media.labels`?~~ **Decided 2026-11-13: no.** Both are removed, with `shared/_classify.py`, `flnd/common/labels.py`, the `classify` extra and its licences; `cli.opt(const=)` stays in `shared`.
7. ~~Keep encrypting secrets?~~ **Decided 2026-11-12: keep it.** `vault` stays, merged with `login` into `flnd/auth.py` ([simplify](simplify.md#decision)).

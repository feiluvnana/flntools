# Phase 6: lean and simple

Phase 6 makes flntools quicker to set up, the same on every OS, smaller to read, and calmer to watch. It is based on an audit of the whole repository on 2026-11-12, and on the problems the user named:

| Problem | Where it's solved |
| --- | --- |
| `make setup` takes a long time | [setup](setup.md): `uv sync` with a lockfile; the 305 MB browser download only when there is no Chrome, and never the unused headless shell |
| make isn't well supported on Windows | [setup](setup.md): every task in a stdlib `scripts/dev.py`; the Makefile is a one-rule shortcut with no shell syntax |
| the session API is bloated and its defaults aren't sensible | [session](session.md): 35 members become 26; scrolling ends on time, not a count, with no 200-scroll cap; page loads are paced and checked |
| Instagram's tagged tab is blocked when each post's page is opened | [ins](ins.md): posts read through the media info API, skipped before any request when already saved or out of range, paced, and every throttle waited out |
| the download rows blink, a prompt shows a white block, and log lines color only their glyph | [ui](ui.md): frames overwrite in place inside synchronized output with the cursor hidden; short-lived rows are never drawn; prompts draw their own cursor; a line takes its level's color unless its text has its own style |
| simplify everything | [api](api.md): every public API, with its change or none; flnd's own APIs lose what flnd doesn't use, and `shared`'s are judged by usefulness |
| the `Source` API has too many fields | [source](source.md): 18 fields and 7 hooks become 8 and 5; `categories` holds its defaults, `prepare` replaces `headers` and `sign`, and fields no source sets become engine constants |
| too many options and commands | [cli](cli.md): `download` keeps `--from`, merges `--since` and `--until` into `--time <since>,<until>`, and gains `--image`, `--video`, `--audio` `<format>,<level>`; `--unpack`, `--zip`, `--locale` and `--timezone` go; `sync` becomes `sync`, which only repairs and fetches new posts, with no options; `login` asks for everything; `logout <sources…>` or `all`; `install-browser` goes, and flnd uses Chrome, offering Chromium only when Chrome is missing |
| the source scripts are long and hard to read | [scripts](scripts.md): extension guessing deleted (the content already decides it); `save_post`, `day` and `meta_results` in `flnd/sources/common.py`; 1,378 lines become about 1,280, each script reading top to bottom |
| some files are saved without their date | [dates](dates.md): the engine puts the date in the name from `save(date=)`, sets every file's modified time to it, and falls back to the CDN's `Last-Modified`, then the download day |
| a classifier for photos and videos | [classify](classify.md): `media.classify(path)`, a small CLIP-style model on ONNX Runtime, downloaded on first use; `--classify` names files `<labels>_<date>_<name>` |
| the code needs more comments | [comments](comments.md): a docstring standard for every module, class and function, and more *why* comments at every rule a reader has to work out |

The audit also found 15 bugs, among them corrupted downloads on Windows, failed targets shown as ok with exit status 0, and Twitter's login sent to its CDNs. They are in [fixes](fixes.md).

## Order

Each step ends with `check` green and is pushed on its own.
1. [fixes](fixes.md): the bugs, each with a test that fails first.
2. [setup](setup.md): `uv.lock`, dependency groups, `scripts/dev.py`, the thin Makefile, and no browser download.
3. [cli](cli.md), [source](source.md) and [session](session.md): the lean `Source`; four commands with `--image`/`--video`/`--audio`; Chrome only; the smaller session protocol and its defaults, with `goto` checked and paced; `compress.py` and `files.py` split out of `session.py`.
4. [api](api.md): the removals and private names in `shared`, and `python-jsonpath` gone.
5. [ins](ins.md): tagged and reposts through the API, with a full live run of the test account.
6. [scripts](scripts.md) and [dates](dates.md): the sources rewritten on the new session and helpers, with every file dated by the engine.
7. [classify](classify.md): `media.classify` and `--classify`.
8. [ui](ui.md): the live area, the prompt cursor, and whole-line log colors.
9. [comments](comments.md): every file brought to the docstring and comment standard. Each earlier step already writes its own new code to it.

## The WIP commit

The work-in-progress commit on `main` ("sussy") tried items of this phase: a Windows block in the Makefile, the `typecheck` task renamed `type`, `session.downloaded`, ins image posts saved from the grid with their date from the `pk`, and scroll defaults of 0.2 s and 1,000 scrolls. Its code is taken back out in the commit that adds this plan, so `main` is the last green Phase 5 code plus the plan. Each piece comes back in its step, changed where the audit found a problem:

| WIP piece | Phase 6 |
| --- | --- |
| the `ifeq ($(OS),Windows_NT)` Makefile | replaced by `scripts/dev.py` ([setup](setup.md)) |
| `make type` | kept as the `type` task |
| `session.downloaded(key)` | kept ([session](session.md#members)) |
| saving grid images, dated by `pk` | kept, with an allowlist and a full-size check ([ins](ins.md#3-save-from-the-grid-only-what-the-grid-really-has)) |
| scroll `delay=0.2`, `max=1000` | not kept: 1.5 s, and an end rule of 10 s with no progress ([session](session.md#scroll)) |
| the Protocol's `delay=2.0, max=200` against the code's `0.2, 1000` | gone with the new signature |

## Decisions

All decided by the user:
1. ~~Remove `media`'s unused API?~~ **Decided 2026-11-12: keep it.** `shared` is judged by usefulness, not by flnd's calls ([api](api.md)).
2. ~~Run the browser headless by default?~~ **Decided 2026-11-12: no.** The browser stays visible by default, as today.
3. ~~Color `info` lines too?~~ **Decided 2026-11-12: yes.** Every level colors its whole line, `info` in cyan ([ui](ui.md#whole-log-lines-in-their-levels-color)).
4. ~~Which classifier model?~~ **Decided 2026-11-12: MobileCLIP-S0**, or OpenCLIP ViT-B/32 if its licence doesn't allow flnd's use; checked before step 7 ([classify](classify.md#the-model)).
5. ~~A `nsfw` label?~~ **Decided 2026-11-12: yes**, with a higher threshold ([classify](classify.md#labels)).
6. ~~Is `classify` required or an extra?~~ **Decided 2026-11-12: an extra**, which `media.classify` offers to install the first time it's used, as Chrome's Chromium and the model are ([classify](classify.md#flnd)).

Still to check while building, not decisions: MobileCLIP-S0's licence, and the live checks in [ins](ins.md#live-checks) and [source](source.md#tests).
